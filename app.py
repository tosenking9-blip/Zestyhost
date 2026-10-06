#!/usr/bin/env python3
"""MODx Hosting Panel — single-file hosting platform"""
import os, re, sqlite3, subprocess, secrets, hashlib, time, socket, shutil, signal
import urllib.request, urllib.error
from functools import wraps
from flask import (Flask, request, session, redirect, url_for,
                   render_template_string, jsonify, send_from_directory,
                   abort, flash, Response)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

ADMIN_USER = "MODxADMIN"
ADMIN_PASS = "MODxFF11"

BASE_DIR = "/app/hosting"
DATA_DIR = os.path.join(BASE_DIR, "data")
INST_DIR = os.path.join(BASE_DIR, "instances")
LOG_DIR  = os.path.join(BASE_DIR, "logs")
DB_PATH  = os.path.join(DATA_DIR, "hosting.db")
for d in (DATA_DIR, INST_DIR, LOG_DIR):
    os.makedirs(d, exist_ok=True)

PROCS = {}

def db():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c

def init_db():
    d = db()
    d.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS instances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        port INTEGER,
        dir TEXT NOT NULL,
        status TEXT DEFAULT 'stopped',
        created_at INTEGER
    );
    """)
    d.commit(); d.close()

def hash_pw(p): return hashlib.sha256(p.encode()).hexdigest()

def port_free(p):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try: s.bind(("127.0.0.1", p)); s.close(); return True
    except: return False

def alloc_port():
    d = db()
    used = {r["port"] for r in d.execute("SELECT port FROM instances WHERE port IS NOT NULL")}
    d.close()
    p = 4100
    while p in used or not port_free(p):
        p += 1
        if p > 5000: raise Exception("No free ports")
    return p

def current_user():
    uid = session.get("uid")
    if not uid: return None
    d = db()
    u = d.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    d.close()
    return u

def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not current_user(): return redirect(url_for("login"))
        return f(*a, **kw)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not session.get("is_admin"): return redirect(url_for("admin_login"))
        return f(*a, **kw)
    return w

def get_inst(iid, user_id=None):
    d = db()
    if user_id:
        row = d.execute("SELECT * FROM instances WHERE id=? AND user_id=?", (iid, user_id)).fetchone()
    else:
        row = d.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    d.close(); return row

def inst_dir(inst):
    return os.path.join(INST_DIR, inst["dir"])

def is_running(iid):
    p = PROCS.get(iid)
    if p and p.poll() is None: return True
    return False

def start_inst(inst):
    if is_running(inst["id"]): return
    idir = inst_dir(inst)
    os.makedirs(idir, exist_ok=True)
    logf = open(os.path.join(LOG_DIR, "inst_" + str(inst["id"]) + ".log"), "ab", buffering=0)
    env = dict(os.environ, PORT=str(inst["port"]))
    if inst["type"] == "python":
        cmd = ["python3", "main.py"]
    elif inst["type"] == "node":
        cmd = ["node", "index.js"]
    else:
        return
    try:
        p = subprocess.Popen(cmd, cwd=idir, stdout=logf, stderr=logf, env=env,
                             preexec_fn=os.setsid)
        PROCS[inst["id"]] = p
        d = db(); d.execute("UPDATE instances SET status='running' WHERE id=?", (inst["id"],)); d.commit(); d.close()
    except Exception as e:
        with open(os.path.join(LOG_DIR, "inst_" + str(inst["id"]) + ".log"), "a") as f:
            f.write("\n[panel] start failed: " + str(e) + "\n")

def stop_inst(iid):
    p = PROCS.pop(iid, None)
    if p and p.poll() is None:
        try: os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except: pass
        try: p.wait(timeout=5)
        except:
            try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except: pass
    d = db(); d.execute("UPDATE instances SET status='stopped' WHERE id=?", (iid,)); d.commit(); d.close()

init_db()

CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#08080f;--surf:#111119;--surf2:#171722;--brd:rgba(255,255,255,0.07);
--pri:#7c5cff;--pri2:#00d9ff;--ok:#00e676;--warn:#ffab00;--err:#ff5577;
--txt:#e8e8f5;--mut:#7a7a90}
body{font-family:'Space Grotesk',system-ui,sans-serif;background:var(--bg);color:var(--txt);min-height:100vh;line-height:1.5}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:-1;
background:radial-gradient(circle at 20% 20%,rgba(124,92,255,0.10),transparent 40%),
radial-gradient(circle at 80% 80%,rgba(0,217,255,0.08),transparent 40%)}
a{color:var(--pri2);text-decoration:none}
a:hover{text-decoration:underline}
.container{max-width:1200px;margin:0 auto;padding:0 20px}
.topbar{border-bottom:1px solid var(--brd);background:rgba(8,8,15,0.85);backdrop-filter:blur(14px);position:sticky;top:0;z-index:50}
.topbar-in{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;max-width:1200px;margin:0 auto}
.logo{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:16px;background:linear-gradient(135deg,var(--pri),var(--pri2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.nav{display:flex;gap:8px;align-items:center}
.nav a,.nav button{color:var(--mut);font-size:13px;padding:7px 14px;border-radius:8px;border:1px solid transparent;background:none;cursor:pointer;font-family:inherit;text-decoration:none}
.nav a:hover{color:var(--txt);background:var(--surf2);text-decoration:none}
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 18px;border-radius:10px;font-size:13px;font-weight:500;border:none;cursor:pointer;text-decoration:none;font-family:inherit;transition:all .2s}
.btn-p{background:linear-gradient(135deg,var(--pri),var(--pri2));color:#fff}
.btn-p:hover{transform:translateY(-1px);box-shadow:0 8px 24px rgba(124,92,255,.35);text-decoration:none}
.btn-g{background:transparent;color:var(--txt);border:1px solid var(--brd)}
.btn-g:hover{border-color:var(--pri);background:rgba(124,92,255,.06);text-decoration:none}
.btn-d{background:rgba(255,85,119,.12);color:var(--err);border:1px solid rgba(255,85,119,.3)}
.btn-d:hover{background:rgba(255,85,119,.2);text-decoration:none}
.btn-s{padding:5px 11px;font-size:12px;border-radius:7px}
.card{background:var(--surf);border:1px solid var(--brd);border-radius:16px;padding:22px;transition:all .25s}
.card:hover{border-color:rgba(124,92,255,.25)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
h1{font-size:clamp(26px,4vw,42px);font-weight:700;letter-spacing:-1px;margin-bottom:12px}
h2{font-size:22px;font-weight:600;margin-bottom:16px}
h3{font-size:16px;font-weight:600;margin-bottom:6px}
.mut{color:var(--mut);font-size:14px}
.tag{display:inline-block;padding:2px 9px;border-radius:100px;font-size:11px;font-family:'JetBrains Mono',monospace;font-weight:500}
.tag-ok{background:rgba(0,230,118,.12);color:var(--ok)}
.tag-off{background:rgba(122,122,144,.15);color:var(--mut)}
.tag-info{background:rgba(124,92,255,.15);color:var(--pri)}
input,select,textarea{width:100%;padding:11px 14px;background:var(--surf2);border:1px solid var(--brd);border-radius:10px;color:var(--txt);font-family:inherit;font-size:14px;outline:none;transition:border .2s}
input:focus,select:focus,textarea:focus{border-color:var(--pri)}
label{display:block;font-size:12px;color:var(--mut);margin-bottom:6px;font-weight:500}
.field{margin-bottom:16px}
.flash{padding:11px 16px;border-radius:10px;font-size:13px;margin-bottom:16px}
.flash-ok{background:rgba(0,230,118,.1);color:var(--ok);border:1px solid rgba(0,230,118,.2)}
.flash-err{background:rgba(255,85,119,.1);color:var(--err);border:1px solid rgba(255,85,119,.2)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:10px;color:var(--mut);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--brd)}
td{padding:12px 10px;border-bottom:1px solid var(--brd)}
tr:last-child td{border-bottom:none}
code,pre{font-family:'JetBrains Mono',monospace;font-size:12px}
pre{background:#000;padding:14px;border-radius:10px;overflow:auto;color:#9f9;max-height:400px;border:1px solid var(--brd)}
.center{min-height:calc(100vh - 60px);display:flex;align-items:center;justify-content:center;padding:20px}
.auth-card{width:100%;max-width:400px;background:var(--surf);border:1px solid var(--brd);border-radius:20px;padding:36px}
.logo-big{font-family:'JetBrains Mono',monospace;font-size:24px;font-weight:700;text-align:center;margin-bottom:8px;background:linear-gradient(135deg,var(--pri),var(--pri2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
@media(max-width:600px){.container{padding:0 14px}.nav a,.nav button{padding:6px 10px;font-size:12px}}
"""

BASE = """<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} · MODx Hosting</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>""" + CSS + """</style></head><body>
<div class="topbar"><div class="topbar-in">
  <a href="/" class="logo">&lt; MODx /&gt;</a>
  <div class="nav">
    {% if user %}
      <a href="{{ url_for('dashboard') }}">Dashboard</a>
      <a href="{{ url_for('logout') }}">Logout ({{ user.username }})</a>
    {% elif is_admin %}
      <a href="{{ url_for('admin_dash') }}">Admin</a>
      <a href="{{ url_for('admin_logout') }}">Logout</a>
    {% else %}
      <a href="{{ url_for('login') }}">Login</a>
      <a href="{{ url_for('admin_login') }}">Admin</a>
    {% endif %}
  </div>
</div></div>
<div class="container" style="padding-top:30px;padding-bottom:60px">
{% with msgs = get_flashed_messages(with_categories=true) %}
  {% for cat, m in msgs %}<div class="flash flash-{{ 'ok' if cat=='ok' else 'err' }}">{{ m }}</div>{% endfor %}
{% endwith %}
{{ body|safe }}
</div></body></html>"""

def render(title, body):
    u = current_user()
    return render_template_string(BASE, title=title, body=body,
                                  user=u, is_admin=session.get("is_admin"))

@app.route("/")
def index():
    body = """
<div style="text-align:center;padding:60px 0 40px">
  <div class="tag tag-info" style="margin-bottom:18px">● Production Ready</div>
  <h1>Host websites &amp; bots<br><span style="background:linear-gradient(135deg,#7c5cff,#00d9ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent">at the speed of thought.</span></h1>
  <p class="mut" style="max-width:600px;margin:16px auto 32px;font-size:16px">
    Deploy Python apps, Node services, and static sites in seconds.
    Real processes, real ports, real infra — no Docker required.
  </p>
  <div style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap">
    <a href="/login" class="btn btn-p">Get Started →</a>
    <a href="/modx/admin" class="btn btn-g">Admin Panel</a>
  </div>
</div>
<div class="grid" style="margin-top:70px">
  <div class="card"><div style="font-size:26px;margin-bottom:12px">⚡</div>
    <h3>Instant Deploy</h3><p class="mut">Push code, click start. Runs in milliseconds on isolated ports.</p></div>
  <div class="card"><div style="font-size:26px;margin-bottom:12px">🐍</div>
    <h3>Python &amp; Node</h3><p class="mut">First-class support for Flask, FastAPI, Express — anything.</p></div>
  <div class="card"><div style="font-size:26px;margin-bottom:12px">🌐</div>
    <h3>Static Sites</h3><p class="mut">Drop HTML/CSS/JS and get a live URL instantly.</p></div>
  <div class="card"><div style="font-size:26px;margin-bottom:12px">📊</div>
    <h3>Live Logs</h3><p class="mut">Watch stdout/stderr stream from your app in real time.</p></div>
  <div class="card"><div style="font-size:26px;margin-bottom:12px">🔒</div>
    <h3>Isolated</h3><p class="mut">Each instance runs in its own process group and directory.</p></div>
  <div class="card"><div style="font-size:26px;margin-bottom:12px">🛠️</div>
    <h3>File Manager</h3><p class="mut">Edit files directly from the browser. No SSH needed.</p></div>
</div>
"""
    return render("Home", body)

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        u = request.form["username"]; p = request.form["password"]
        d = db(); row = d.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone(); d.close()
        if row and row["password_hash"] == hash_pw(p):
            session["uid"] = row["id"]; session["is_admin"] = False
            return redirect(url_for("dashboard"))
        flash("Invalid credentials", "err")
    body = """
<div class="center" style="min-height:auto;padding-top:40px"><div class="auth-card">
  <div class="logo-big">&lt; MODx /&gt;</div>
  <p class="mut" style="text-align:center;margin-bottom:26px">Sign in to your panel</p>
  <form method="post">
    <div class="field"><label>Username</label><input name="username" required autofocus></div>
    <div class="field"><label>Password</label><input name="password" type="password" required></div>
    <button class="btn btn-p" style="width:100%;justify-content:center">Sign In</button>
  </form>
  <p class="mut" style="text-align:center;margin-top:18px;font-size:12px">
    Accounts are created by the admin only.
  </p>
</div></div>
"""
    return render("Login", body)

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    u = current_user()
    d = db(); rows = d.execute("SELECT * FROM instances WHERE user_id=? ORDER BY id DESC", (u["id"],)).fetchall(); d.close()
    cards = ""
    for r in rows:
        running = is_running(r["id"]) or r["status"] == "running"
        tag = '<span class="tag tag-ok">● Running</span>' if running else '<span class="tag tag-off">○ Stopped</span>'
        link_html = '<a href="/app/' + str(r["id"]) + '/" target="_blank" class="btn btn-g btn-s">Open ↗</a>' if running else ''
        cards += '<div class="card">'
        cards += '<div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:12px">'
        cards += '<div><h3>' + r["name"] + '</h3><p class="mut" style="font-size:12px">' + r["type"] + ' · port ' + str(r["port"]) + '</p></div>'
        cards += tag + '</div>'
        cards += '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:14px">'
        cards += '<a href="/dashboard/i/' + str(r["id"]) + '" class="btn btn-p btn-s">Manage</a>'
        cards += link_html + '</div></div>'
    if not cards:
        cards = '<div class="card" style="text-align:center;padding:50px"><p class="mut">No instances yet. Create your first one →</p></div>'
    body = (
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:26px;flex-wrap:wrap;gap:12px">'
        '<div><h1 style="margin-bottom:4px">Dashboard</h1>'
        '<p class="mut">Welcome back, ' + u["username"] + '</p></div>'
        '<a href="/dashboard/new" class="btn btn-p">+ New Instance</a></div>'
        '<div class="grid">' + cards + '</div>'
    )
    return render("Dashboard", body)

@app.route("/dashboard/new", methods=["GET","POST"])
@login_required
def new_inst():
    u = current_user()
    if request.method == "POST":
        name = re.sub(r'[^a-zA-Z0-9_-]', '', request.form["name"])[:30]
        typ = request.form["type"]
        if not name:
            flash("Invalid name", "err"); return redirect(url_for("new_inst"))
        try:
            port = alloc_port()
            slug = "u" + str(u["id"]) + "_" + name
            idir = os.path.join(INST_DIR, slug)
            os.makedirs(idir, exist_ok=True)
            if typ == "python":
                with open(os.path.join(idir, "main.py"), "w") as f:
                    f.write('import os\nfrom http.server import HTTPServer, BaseHTTPRequestHandler\n\nclass H(BaseHTTPRequestHandler):\n    def do_GET(self):\n        self.send_response(200)\n        self.send_header("Content-Type","text/html")\n        self.end_headers()\n        self.wfile.write(b"<h1>Hello from MODx Python app!</h1>")\n\nif __name__ == "__main__":\n    p = int(os.environ.get("PORT", 8000))\n    print("Listening on " + str(p))\n    HTTPServer(("0.0.0.0", p), H).serve_forever()\n')
            elif typ == "node":
                with open(os.path.join(idir, "index.js"), "w") as f:
                    f.write('const http=require("http");\nconst port=process.env.PORT||8000;\nhttp.createServer((req,res)=>{res.writeHead(200,{"Content-Type":"text/html"});res.end("<h1>Hello from MODx Node app!</h1>");}).listen(port,()=>console.log("Listening on "+port));\n')
            elif typ == "static":
                with open(os.path.join(idir, "index.html"), "w") as f:
                    f.write('<!DOCTYPE html><html><head><title>My Site</title><style>body{font-family:sans-serif;background:#0a0a0f;color:#fff;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}h1{background:linear-gradient(135deg,#7c5cff,#00d9ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-size:48px}</style></head><body><h1>It works!</h1></body></html>')
            d = db()
            cur = d.execute("INSERT INTO instances(user_id,name,type,port,dir,status,created_at) VALUES(?,?,?,?,?,?,?)",
                            (u["id"], name, typ, port, slug, "stopped", int(time.time())))
            d.commit(); iid = cur.lastrowid; d.close()
            flash("Instance '" + name + "' created!", "ok")
            return redirect(url_for("inst_detail", iid=iid))
        except Exception as e:
            flash("Error: " + str(e), "err")
    body = """
<h1>New Instance</h1>
<p class="mut" style="margin-bottom:22px">Pick a runtime — a starter file will be created for you.</p>
<div style="max-width:560px"><div class="card">
<form method="post">
  <div class="field"><label>Instance Name</label>
    <input name="name" required pattern="[a-zA-Z0-9_-]+" placeholder="my-app" autofocus>
    <p class="mut" style="font-size:11px;margin-top:6px">Letters, numbers, dash, underscore only</p>
  </div>
  <div class="field"><label>Runtime</label>
    <select name="type">
      <option value="static">🌐 Static Website (HTML/CSS/JS)</option>
      <option value="python">🐍 Python App</option>
      <option value="node">⬢ Node.js App</option>
    </select>
  </div>
  <button class="btn btn-p" style="width:100%;justify-content:center">Create Instance</button>
</form>
</div></div>
"""
    return render("New Instance", body)

@app.route("/dashboard/i/<int:iid>")
@login_required
def inst_detail(iid):
    u = current_user()
    inst = get_inst(iid, u["id"])
    if not inst: abort(404)
    running = is_running(iid) or inst["status"] == "running"
    files = []
    idir = inst_dir(inst)
    if os.path.isdir(idir):
        for f in sorted(os.listdir(idir)):
            fp = os.path.join(idir, f)
            if os.path.isfile(fp):
                files.append({"name": f, "size": os.path.getsize(fp)})
    file_rows = ""
    for f in files:
        file_rows += '<tr><td><a href="/dashboard/i/' + str(iid) + '/edit?f=' + f["name"] + '">' + f["name"] + '</a></td>'
        file_rows += '<td class="mut">' + str(f["size"]) + ' B</td>'
        file_rows += '<td style="text-align:right"><form method="post" action="/dashboard/i/' + str(iid) + '/delete-file" style="display:inline" onsubmit="return confirm(\'Delete?\')">'
        file_rows += '<input type="hidden" name="f" value="' + f["name"] + '">'
        file_rows += '<button class="btn btn-d btn-s">Del</button></form></td></tr>'
    if not file_rows:
        file_rows = '<tr><td colspan="3" class="mut">No files yet</td></tr>'
    log_path = os.path.join(LOG_DIR, "inst_" + str(iid) + ".log")
    log = ""
    if os.path.exists(log_path):
        with open(log_path, "r", errors="replace") as lf:
            log = lf.read()[-6000:]
    running_tag = '<span class="tag tag-ok">● Running</span>' if running else '<span class="tag tag-off">○ Stopped</span>'
    btn_start = '<form method="post" action="/dashboard/i/' + str(iid) + '/start" style="display:inline"><button class="btn btn-p btn-s">▶ Start</button></form>' if not running else ""
    btn_stop  = '<form method="post" action="/dashboard/i/' + str(iid) + '/stop" style="display:inline"><button class="btn btn-g btn-s">⏹ Stop</button></form>' if running else ""
    open_btn  = '<a href="/app/' + str(iid) + '/" target="_blank" class="btn btn-g btn-s">↗ Open</a>'
    log_script = (
        "<script>setInterval(function(){fetch('/dashboard/i/" + str(iid) + "/log')"
        ".then(function(r){return r.text()})"
        ".then(function(t){document.getElementById('log').textContent=t||'(no output)';});"
        "},3000);</script>"
    )
    body = (
        '<div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:20px;flex-wrap:wrap;gap:12px">'
        '<div><a href="/dashboard" class="mut" style="font-size:13px">← Dashboard</a>'
        '<h1 style="margin-top:6px">' + inst["name"] + ' ' + running_tag + '</h1>'
        '<p class="mut">' + inst["type"] + ' · port ' + str(inst["port"]) + '</p></div>'
        '<div style="display:flex;gap:8px;flex-wrap:wrap">' + btn_start + btn_stop + open_btn +
        '<form method="post" action="/dashboard/i/' + str(iid) + '/delete" style="display:inline" onsubmit="return confirm(\'Delete this instance permanently?\')">'
        '<button class="btn btn-d btn-s">🗑 Delete</button></form></div></div>'
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px" id="grid">'
        '<div class="card"><h3 style="margin-bottom:14px">📁 Files</h3>'
        '<table>' + file_rows + '</table>'
        '<form method="post" action="/dashboard/i/' + str(iid) + '/upload" enctype="multipart/form-data" style="margin-top:14px;display:flex;gap:8px">'
        '<input type="file" name="file" required style="padding:8px">'
        '<button class="btn btn-p btn-s">↑ Upload</button></form>'
        '<form method="post" action="/dashboard/i/' + str(iid) + '/new-file" style="margin-top:10px;display:flex;gap:8px">'
        '<input name="fname" placeholder="newfile.txt" required>'
        '<button class="btn btn-g btn-s">+ New</button></form></div>'
        '<div class="card"><h3 style="margin-bottom:14px">📜 Live Log</h3>'
        '<pre id="log" style="height:340px">' + (log or 'No output yet — start the app to see logs.') + '</pre>'
        '<button class="btn btn-g btn-s" onclick="location.reload()" style="margin-top:10px">↻ Refresh</button>'
        '</div></div>'
        '<style>@media(max-width:800px){#grid{grid-template-columns:1fr!important}}</style>'
        + log_script
    )
    return render(inst["name"], body)

@app.route("/dashboard/i/<int:iid>/log")
@login_required
def inst_log(iid):
    u = current_user()
    inst = get_inst(iid, u["id"])
    if not inst: abort(404)
    lp = os.path.join(LOG_DIR, "inst_" + str(iid) + ".log")
    if not os.path.exists(lp): return ""
    with open(lp, "r", errors="replace") as f:
        return f.read()[-6000:]

@app.route("/dashboard/i/<int:iid>/start", methods=["POST"])
@login_required
def inst_start(iid):
    u = current_user(); inst = get_inst(iid, u["id"])
    if inst: start_inst(inst)
    return redirect(url_for("inst_detail", iid=iid))

@app.route("/dashboard/i/<int:iid>/stop", methods=["POST"])
@login_required
def inst_stop(iid):
    u = current_user(); inst = get_inst(iid, u["id"])
    if inst: stop_inst(iid)
    return redirect(url_for("inst_detail", iid=iid))

@app.route("/dashboard/i/<int:iid>/delete", methods=["POST"])
@login_required
def inst_delete(iid):
    u = current_user(); inst = get_inst(iid, u["id"])
    if inst:
        stop_inst(iid)
        try: shutil.rmtree(inst_dir(inst))
        except: pass
        d = db(); d.execute("DELETE FROM instances WHERE id=?", (iid,)); d.commit(); d.close()
        flash("Instance deleted", "ok")
    return redirect(url_for("dashboard"))

@app.route("/dashboard/i/<int:iid>/edit")
@login_required
def inst_edit(iid):
    u = current_user(); inst = get_inst(iid, u["id"])
    if not inst: abort(404)
    fname = request.args.get("f", "")
    if not re.match(r'^[\w.\-]+$', fname): abort(400)
    fp = os.path.join(inst_dir(inst), fname)
    if not os.path.isfile(fp): abort(404)
    try:
        with open(fp, "r", errors="replace") as f: content = f.read()
    except: content = ""
    safe_content = content.replace('&','&amp;').replace('<','&lt;')
    body = (
        '<a href="/dashboard/i/' + str(iid) + '" class="mut" style="font-size:13px">← Back</a>'
        '<h1 style="margin-top:6px">Edit: ' + fname + '</h1>'
        '<form method="post" action="/dashboard/i/' + str(iid) + '/save">'
        '<input type="hidden" name="f" value="' + fname + '">'
        '<textarea name="content" style="min-height:420px;font-family:monospace;font-size:13px;line-height:1.5" spellcheck="false">' + safe_content + '</textarea>'
        '<button class="btn btn-p" style="margin-top:12px">💾 Save</button></form>'
    )
    return render("Edit " + fname, body)

@app.route("/dashboard/i/<int:iid>/save", methods=["POST"])
@login_required
def inst_save(iid):
    u = current_user(); inst = get_inst(iid, u["id"])
    if not inst: abort(404)
    fname = request.form["f"]
    if not re.match(r'^[\w.\-]+$', fname): abort(400)
    with open(os.path.join(inst_dir(inst), fname), "w") as f: f.write(request.form["content"])
    flash("Saved", "ok")
    return redirect(url_for("inst_edit", iid=iid, f=fname))

@app.route("/dashboard/i/<int:iid>/upload", methods=["POST"])
@login_required
def inst_upload(iid):
    u = current_user(); inst = get_inst(iid, u["id"])
    if not inst: abort(404)
    f = request.files.get("file")
    if f and f.filename:
        safe = re.sub(r'[^a-zA-Z0-9._-]', '', f.filename)
        if safe: f.save(os.path.join(inst_dir(inst), safe))
    return redirect(url_for("inst_detail", iid=iid))

@app.route("/dashboard/i/<int:iid>/new-file", methods=["POST"])
@login_required
def inst_newfile(iid):
    u = current_user(); inst = get_inst(iid, u["id"])
    if not inst: abort(404)
    fname = re.sub(r'[^a-zA-Z0-9._-]', '', request.form["fname"])
    if fname:
        fp = os.path.join(inst_dir(inst), fname)
        if not os.path.exists(fp): open(fp, "w").close()
    return redirect(url_for("inst_detail", iid=iid))

@app.route("/dashboard/i/<int:iid>/delete-file", methods=["POST"])
@login_required
def inst_delfile(iid):
    u = current_user(); inst = get_inst(iid, u["id"])
    if not inst: abort(404)
    fname = request.form["f"]
    if re.match(r'^[\w.\-]+$', fname):
        try: os.remove(os.path.join(inst_dir(inst), fname))
        except: pass
    return redirect(url_for("inst_detail", iid=iid))

@app.route("/app/<int:iid>/", defaults={"path": ""})
@app.route("/app/<int:iid>/<path:path>")
def proxy(iid, path):
    inst = get_inst(iid)
    if not inst: abort(404)
    idir = inst_dir(inst)
    if inst["type"] == "static":
        if path == "" or path.endswith("/"): path = (path or "") + "index.html"
        fp = os.path.join(idir, path)
        if not os.path.abspath(fp).startswith(os.path.abspath(idir)): abort(403)
        if not os.path.isfile(fp): abort(404)
        return send_from_directory(idir, path)
    if not is_running(iid):
        return Response("<pre style='color:#f55;padding:20px;font-family:monospace'>Instance not running</pre>", status=502, mimetype="text/html")
    target = "http://127.0.0.1:" + str(inst["port"]) + "/" + path
    if request.query_string: target += "?" + request.query_string.decode()
    try:
        req = urllib.request.Request(target, method=request.method)
        for k, v in request.headers.items():
            if k.lower() not in ("host", "content-length", "connection"): req.add_header(k, v)
        body_data = request.get_data() if request.method in ("POST", "PUT", "PATCH") else None
        with urllib.request.urlopen(req, data=body_data, timeout=30) as r:
            return Response(r.read(), status=r.status, headers={k: v for k, v in r.headers.items() if k.lower() not in ("transfer-encoding", "connection", "content-encoding")})
    except urllib.error.HTTPError as e:
        return Response(e.read(), status=e.code)
    except Exception as e:
        return Response("<pre style='color:#f55;padding:20px;font-family:monospace'>Proxy error: " + str(e) + "</pre>", status=502, mimetype="text/html")

@app.route("/modx/admin", methods=["GET","POST"])
def admin_login():
    if request.method == "POST":
        u = request.form["username"]; p = request.form["password"]
        if u == ADMIN_USER and p == ADMIN_PASS:
            session["is_admin"] = True; session.pop("uid", None)
            return redirect(url_for("admin_dash"))
        flash("Access denied", "err")
    body = """
<div class="center" style="min-height:auto;padding-top:40px"><div class="auth-card">
  <div class="logo-big" style="background:linear-gradient(135deg,#ff5577,#ffab00);-webkit-background-clip:text;-webkit-text-fill-color:transparent">ADMIN</div>
  <p class="mut" style="text-align:center;margin-bottom:26px">Restricted access</p>
  <form method="post">
    <div class="field"><label>Admin Username</label><input name="username" required autofocus></div>
    <div class="field"><label>Password</label><input name="password" type="password" required></div>
    <button class="btn btn-p" style="width:100%;justify-content:center">Authenticate</button>
  </form>
</div></div>
"""
    return render("Admin Login", body)

@app.route("/modx/admin/logout")
def admin_logout():
    session.clear(); return redirect(url_for("index"))

@app.route("/modx/admin/dashboard")
@admin_required
def admin_dash():
    d = db()
    users = d.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    insts = d.execute("SELECT i.*, u.username FROM instances i JOIN users u ON i.user_id=u.id ORDER BY i.id DESC").fetchall()
    d.close()
    urows = ""
    for u in users:
        urows += '<tr><td>#' + str(u["id"]) + '</td><td><b>' + u["username"] + '</b></td>'
        urows += '<td class="mut">' + time.strftime("%Y-%m-%d", time.localtime(u["created_at"] or 0)) + '</td>'
        urows += '<td style="text-align:right"><form method="post" action="/modx/admin/users/' + str(u["id"]) + '/delete" style="display:inline" onsubmit="return confirm(\'Delete user + all instances?\')">'
        urows += '<button class="btn btn-d btn-s">Delete</button></form></td></tr>'
    if not urows: urows = '<tr><td colspan="4" class="mut">No users yet</td></tr>'
    irows = ""
    for i in insts:
        irows += '<tr><td>#' + str(i["id"]) + '</td><td><b>' + i["name"] + '</b></td><td>' + i["username"] + '</td>'
        irows += '<td><span class="tag tag-info">' + i["type"] + '</span></td>'
        irows += '<td class="mut">:' + str(i["port"]) + '</td></tr>'
    if not irows: irows = '<tr><td colspan="5" class="mut">No instances</td></tr>'
    running_count = sum(1 for i in insts if is_running(i["id"]))
    body = (
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:26px;flex-wrap:wrap;gap:12px">'
        '<div><h1 style="margin-bottom:4px">Admin Panel</h1><p class="mut">Manage users and instances</p></div>'
        '<a href="/modx/admin/users/new" class="btn btn-p">+ Create User</a></div>'
        '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:26px">'
        '<div class="card"><div class="mut" style="font-size:12px;text-transform:uppercase">Users</div><div style="font-size:32px;font-weight:700;margin-top:6px">' + str(len(users)) + '</div></div>'
        '<div class="card"><div class="mut" style="font-size:12px;text-transform:uppercase">Instances</div><div style="font-size:32px;font-weight:700;margin-top:6px">' + str(len(insts)) + '</div></div>'
        '<div class="card"><div class="mut" style="font-size:12px;text-transform:uppercase">Running</div><div style="font-size:32px;font-weight:700;margin-top:6px;color:var(--ok)">' + str(running_count) + '</div></div>'
        '</div>'
        '<div class="card" style="margin-bottom:20px"><h3 style="margin-bottom:14px">👥 Users</h3>'
        '<table><tr><th>ID</th><th>Username</th><th>Created</th><th></th></tr>' + urows + '</table></div>'
        '<div class="card"><h3 style="margin-bottom:14px">📦 All Instances</h3>'
        '<table><tr><th>ID</th><th>Name</th><th>Owner</th><th>Type</th><th>Port</th></tr>' + irows + '</table></div>'
    )
    return render("Admin", body)

@app.route("/modx/admin/users/new", methods=["GET","POST"])
@admin_required
def admin_new_user():
    if request.method == "POST":
        u = re.sub(r'[^a-zA-Z0-9_]', '', request.form["username"])[:30]
        p = request.form["password"]
        if not u or len(p) < 4:
            flash("Username or password too short", "err")
        else:
            try:
                d = db()
                d.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                          (u, hash_pw(p), int(time.time())))
                d.commit(); d.close()
                flash("User '" + u + "' created", "ok")
                return redirect(url_for("admin_dash"))
            except sqlite3.IntegrityError:
                flash("Username already exists", "err")
    body = """
<a href="/modx/admin/dashboard" class="mut" style="font-size:13px">← Admin</a>
<h1 style="margin-top:6px">Create User</h1>
<div style="max-width:520px;margin-top:20px"><div class="card">
<form method="post">
  <div class="field"><label>Username</label><input name="username" required pattern="[a-zA-Z0-9_]+" autofocus></div>
  <div class="field"><label>Password</label><input name="password" type="text" required minlength="4"></div>
  <button class="btn btn-p" style="width:100%;justify-content:center">Create User</button>
</form>
</div></div>
"""
    return render("Create User", body)

@app.route("/modx/admin/users/<int:uid>/delete", methods=["POST"])
@admin_required
def admin_del_user(uid):
    d = db()
    insts = d.execute("SELECT * FROM instances WHERE user_id=?", (uid,)).fetchall()
    for i in insts:
        stop_inst(i["id"])
        try: shutil.rmtree(inst_dir(i))
        except: pass
    d.execute("DELETE FROM instances WHERE user_id=?", (uid,))
    d.execute("DELETE FROM users WHERE id=?", (uid,))
    d.commit(); d.close()
    flash("User deleted", "ok")
    return redirect(url_for("admin_dash"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print("🚀 MODx Hosting Panel running on port " + str(port))
    app.run(host="0.0.0.0", port=port, threaded=True)
