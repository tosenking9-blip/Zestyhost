#!/usr/bin/env python3
"""MODx Hosting Panel — single-file hosting platform (Green Edition)"""
import os, re, sqlite3, subprocess, secrets, hashlib, time, socket, shutil, signal
import urllib.request, urllib.error, urllib.parse, zipfile, io, threading
from functools import wraps
from flask import (Flask, request, session, redirect, url_for,
                   render_template_string, jsonify, send_from_directory,
                   abort, flash, Response)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

USERNAME = "Zesty"
PASSWORD = "123456"

BASE_DIR = "/app/hosting"
DATA_DIR = os.path.join(BASE_DIR, "data")
INST_DIR = os.path.join(BASE_DIR, "instances")
LOG_DIR  = os.path.join(BASE_DIR, "logs")
DB_PATH  = os.path.join(DATA_DIR, "hosting.db")
for d in (DATA_DIR, INST_DIR, LOG_DIR):
    os.makedirs(d, exist_ok=True)

PROCS = {}
LAST_START = {}

# ---------- DB ----------
def db():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c

def init_db():
    d = db()
    d.executescript("""
    CREATE TABLE IF NOT EXISTS instances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        port INTEGER,
        dir TEXT NOT NULL,
        status TEXT DEFAULT 'stopped',
        created_at INTEGER
    );
    """)
    d.commit(); d.close()

# ---------- helpers ----------
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

def logged_in():
    return session.get("logged_in") is True

def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not logged_in(): return redirect(url_for("login"))
        return f(*a, **kw)
    return w

def get_inst(iid):
    d = db(); row = d.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone(); d.close()
    return row

def inst_dir(inst):
    return os.path.join(INST_DIR, inst["dir"])

def safe_join(base, *paths):
    base = os.path.abspath(base)
    target = os.path.abspath(os.path.join(base, *paths))
    if target != base and not target.startswith(base + os.sep):
        return None
    return target

def norm_rel(p):
    """Normalize a relative path; strip traversal, sanitize each segment."""
    if not p: return ""
    p = str(p).replace("\\", "/").strip("/")
    parts = []
    for part in p.split("/"):
        if part in ("", ".", ".."): continue
        part = re.sub(r'[^\w.\- ]', '_', part).strip()
        if part: parts.append(part)
    return "/".join(parts)

def human_size(n):
    for unit in ("B","KB","MB","GB"):
        if n < 1024: return ("%.0f %s" % (n, unit)) if unit=="B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f TB" % n

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

# ---------- 24/7 watchdog ----------
def watchdog():
    while True:
        try:
            d = db()
            rows = d.execute("SELECT * FROM instances WHERE status='running'").fetchall()
            d.close()
            for r in rows:
                if not is_running(r["id"]):
                    now = time.time()
                    if now - LAST_START.get(r["id"], 0) < 5:
                        continue
                    LAST_START[r["id"]] = now
                    inst = get_inst(r["id"])
                    if inst: start_inst(inst)
        except Exception as e:
            print("[watchdog]", e)
        time.sleep(8)

# ---------- CSS / BASE ----------
CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#05100a;--surf:#0c1a12;--surf2:#12271b;--brd:rgba(0,255,140,0.10);
--pri:#00e676;--pri2:#69f0ae;--ok:#00e676;--warn:#ffab00;--err:#ff5577;
--txt:#e6fff0;--mut:#7ea88f}
body{font-family:'Space Grotesk',system-ui,sans-serif;background:var(--bg);color:var(--txt);min-height:100vh;line-height:1.5}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:-1;
background:radial-gradient(circle at 20% 20%,rgba(0,230,118,0.10),transparent 40%),
radial-gradient(circle at 80% 80%,rgba(105,240,174,0.08),transparent 40%)}
a{color:var(--pri2);text-decoration:none}
a:hover{text-decoration:underline}
.container{max-width:1200px;margin:0 auto;padding:0 20px}
.topbar{border-bottom:1px solid var(--brd);background:rgba(5,16,10,0.85);backdrop-filter:blur(14px);position:sticky;top:0;z-index:50}
.topbar-in{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;max-width:1200px;margin:0 auto}
.logo{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:16px;background:linear-gradient(135deg,var(--pri),var(--pri2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.nav{display:flex;gap:8px;align-items:center}
.nav a,.nav button{color:var(--mut);font-size:13px;padding:7px 14px;border-radius:8px;border:1px solid transparent;background:none;cursor:pointer;font-family:inherit;text-decoration:none}
.nav a:hover{color:var(--txt);background:var(--surf2);text-decoration:none}
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 18px;border-radius:10px;font-size:13px;font-weight:500;border:none;cursor:pointer;text-decoration:none;font-family:inherit;transition:all .2s}
.btn-p{background:linear-gradient(135deg,var(--pri),var(--pri2));color:#04140b;font-weight:600}
.btn-p:hover{transform:translateY(-1px);box-shadow:0 8px 24px rgba(0,230,118,.35);text-decoration:none}
.btn-g{background:transparent;color:var(--txt);border:1px solid var(--brd)}
.btn-g:hover{border-color:var(--pri);background:rgba(0,230,118,.06);text-decoration:none}
.btn-d{background:rgba(255,85,119,.12);color:var(--err);border:1px solid rgba(255,85,119,.3)}
.btn-d:hover{background:rgba(255,85,119,.2);text-decoration:none}
.btn-s{padding:5px 11px;font-size:12px;border-radius:7px}
.card{background:var(--surf);border:1px solid var(--brd);border-radius:16px;padding:22px;transition:all .25s}
.card:hover{border-color:rgba(0,230,118,.25)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
h1{font-size:clamp(26px,4vw,42px);font-weight:700;letter-spacing:-1px;margin-bottom:12px}
h2{font-size:22px;font-weight:600;margin-bottom:16px}
h3{font-size:16px;font-weight:600;margin-bottom:6px}
.mut{color:var(--mut);font-size:14px}
.tag{display:inline-block;padding:2px 9px;border-radius:100px;font-size:11px;font-family:'JetBrains Mono',monospace;font-weight:500}
.tag-ok{background:rgba(0,230,118,.12);color:var(--ok)}
.tag-off{background:rgba(126,168,143,.15);color:var(--mut)}
.tag-info{background:rgba(0,230,118,.15);color:var(--pri2)}
input,select,textarea{width:100%;padding:11px 14px;background:var(--surf2);border:1px solid var(--brd);border-radius:10px;color:var(--txt);font-family:inherit;font-size:14px;outline:none;transition:border .2s}
input:focus,select:focus,textarea:focus{border-color:var(--pri)}
label{display:block;font-size:12px;color:var(--mut);margin-bottom:6px;font-weight:500}
.field{margin-bottom:16px}
.flash{padding:11px 16px;border-radius:10px;font-size:13px;margin-bottom:16px}
.flash-ok{background:rgba(0,230,118,.1);color:var(--ok);border:1px solid rgba(0,230,118,.2)}
.flash-err{background:rgba(255,85,119,.1);color:var(--err);border:1px solid rgba(255,85,119,.2)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:10px;color:var(--mut);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--brd)}
td{padding:10px;border-bottom:1px solid var(--brd)}
tr:last-child td{border-bottom:none}
code,pre{font-family:'JetBrains Mono',monospace;font-size:12px}
pre{background:#000;padding:14px;border-radius:10px;overflow:auto;color:#8effb0;max-height:400px;border:1px solid var(--brd)}
.center{min-height:calc(100vh - 60px);display:flex;align-items:center;justify-content:center;padding:20px}
.auth-card{width:100%;max-width:400px;background:var(--surf);border:1px solid var(--brd);border-radius:20px;padding:36px}
.logo-big{font-family:'JetBrains Mono',monospace;font-size:24px;font-weight:700;text-align:center;margin-bottom:8px;background:linear-gradient(135deg,var(--pri),var(--pri2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.crumb{font-family:'JetBrains Mono',monospace;font-size:13px;padding:10px 14px;background:var(--surf2);border-radius:10px;margin-bottom:12px;word-break:break-all}
.crumb a{color:var(--pri2)}
.drop{border:2px dashed rgba(0,230,118,.25);border-radius:12px;padding:18px;text-align:center;transition:all .2s}
.drop:hover{border-color:var(--pri);background:rgba(0,230,118,.04)}
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
    {% if logged %}
      <a href="{{ url_for('dashboard') }}">Dashboard</a>
      <a href="{{ url_for('logout') }}">Logout</a>
    {% else %}
      <a href="{{ url_for('login') }}">Login</a>
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
    return render_template_string(BASE, title=title, body=body, logged=logged_in())

# ---------- routes ----------
@app.route("/")
def index():
    if logged_in(): return redirect(url_for("dashboard"))
    body = """
<div style="text-align:center;padding:60px 0 40px">
  <div class="tag tag-info" style="margin-bottom:18px">● 24×7 Always-On Hosting</div>
  <h1>Host websites &amp; bots<br><span style="background:linear-gradient(135deg,#00e676,#69f0ae);-webkit-background-clip:text;-webkit-text-fill-color:transparent">at the speed of thought.</span></h1>
  <p class="mut" style="max-width:600px;margin:16px auto 32px;font-size:16px">
    Deploy Python apps, Node services, and static sites in seconds.
    Upload ZIPs, manage subfolders, watch live logs — all in one panel.
  </p>
  <div style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap">
    <a href="/login" class="btn btn-p">Get Started →</a>
  </div>
</div>
<div class="grid" style="margin-top:70px">
  <div class="card"><div style="font-size:26px;margin-bottom:12px">⚡</div>
    <h3>Instant Deploy</h3><p class="mut">Push code, click start. Runs in milliseconds on isolated ports.</p></div>
  <div class="card"><div style="font-size:26px;margin-bottom:12px">📦</div>
    <h3>ZIP Upload</h3><p class="mut">Upload a .zip of your project — we extract it automatically, safe from zip-slip.</p></div>
  <div class="card"><div style="font-size:26px;margin-bottom:12px">📁</div>
    <h3>Subfolders</h3><p class="mut">Full folder navigation, create folders, upload into any nested path.</p></div>
  <div class="card"><div style="font-size:26px;margin-bottom:12px">🛡️</div>
    <h3>24×7 Watchdog</h3><p class="mut">Crashed processes auto-restart. Your apps stay online around the clock.</p></div>
  <div class="card"><div style="font-size:26px;margin-bottom:12px">📊</div>
    <h3>Live Logs</h3><p class="mut">Watch stdout/stderr stream from your app in real time.</p></div>
  <div class="card"><div style="font-size:26px;margin-bottom:12px">🌐</div>
    <h3>Static Sites</h3><p class="mut">Drop HTML/CSS/JS (or a zip) and get a live URL instantly.</p></div>
</div>
"""
    return render("Home", body)

@app.route("/login", methods=["GET","POST"])
def login():
    if logged_in(): return redirect(url_for("dashboard"))
    if request.method == "POST":
        u = request.form.get("username","")
        p = request.form.get("password","")
        if u == USERNAME and p == PASSWORD:
            session["logged_in"] = True
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
</div></div>
"""
    return render("Login", body)

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    d = db(); rows = d.execute("SELECT * FROM instances ORDER BY id DESC").fetchall(); d.close()
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
        '<p class="mut">Signed in as ' + USERNAME + ' · 24×7 watchdog active</p></div>'
        '<a href="/dashboard/new" class="btn btn-p">+ New Instance</a></div>'
        '<div class="grid">' + cards + '</div>'
    )
    return render("Dashboard", body)

@app.route("/dashboard/new", methods=["GET","POST"])
@login_required
def new_inst():
    if request.method == "POST":
        name = re.sub(r'[^a-zA-Z0-9_-]', '', request.form["name"])[:30]
        typ = request.form["type"]
        if not name:
            flash("Invalid name", "err"); return redirect(url_for("new_inst"))
        try:
            port = alloc_port()
            slug = name + "_" + secrets.token_hex(3)
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
                    f.write('<!DOCTYPE html><html><head><title>My Site</title><style>body{font-family:sans-serif;background:#05100a;color:#e6fff0;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}h1{background:linear-gradient(135deg,#00e676,#69f0ae);-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-size:48px}</style></head><body><h1>It works!</h1></body></html>')
            d = db()
            cur = d.execute("INSERT INTO instances(name,type,port,dir,status,created_at) VALUES(?,?,?,?,?,?)",
                            (name, typ, port, slug, "stopped", int(time.time())))
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

# ---------- file manager helpers ----------
def render_file_rows(iid, inst, path):
    base = inst_dir(inst)
    target = safe_join(base, path) if path else base
    if target is None or not os.path.isdir(target):
        return '<tr><td colspan="3" class="mut" style="padding:16px;text-align:center">Invalid path</td></tr>'
    items = []
    try:
        for name in os.listdir(target):
            fp = os.path.join(target, name)
            is_dir = os.path.isdir(fp)
            rel = (path + "/" + name) if path else name
            try: sz = 0 if is_dir else os.path.getsize(fp)
            except: sz = 0
            items.append((is_dir, name, rel, sz))
    except Exception:
        pass
    items.sort(key=lambda x: (not x[0], x[1].lower()))
    if not items:
        return '<tr><td colspan="3" class="mut" style="padding:22px;text-align:center">📭 Empty folder — upload files or extract a ZIP</td></tr>'
    rows = ""
    for is_dir, name, rel, size in items:
        q = urllib.parse.quote(rel)
        if is_dir:
            rows += '<tr><td>📁 <a href="/dashboard/i/' + str(iid) + '?p=' + q + '"><b>' + name + '</b>/</a></td>'
            rows += '<td class="mut">folder</td>'
        else:
            rows += '<tr><td>📄 <a href="/dashboard/i/' + str(iid) + '/edit?f=' + q + '">' + name + '</a></td>'
            rows += '<td class="mut">' + human_size(size) + '</td>'
        rows += '<td style="text-align:right">'
        rows += '<form method="post" action="/dashboard/i/' + str(iid) + '/delete-file" style="display:inline" onsubmit="return confirm(\'Delete this?\')">'
        rows += '<input type="hidden" name="f" value="' + rel.replace('"','&quot;') + '">'
        rows += '<input type="hidden" name="p" value="' + path.replace('"','&quot;') + '">'
        rows += '<button class="btn btn-d btn-s">Del</button></form>'
        rows += '</td></tr>'
    return rows

def render_breadcrumb(iid, path):
    html = '<a href="/dashboard/i/' + str(iid) + '">🏠 root</a>'
    if not path: return html
    acc = ""
    for part in path.split("/"):
        acc = (acc + "/" + part) if acc else part
        html += ' <span style="color:var(--mut)">/</span> <a href="/dashboard/i/' + str(iid) + '?p=' + urllib.parse.quote(acc) + '">' + part + '</a>'
    return html

# ---------- instance detail (with file manager) ----------
@app.route("/dashboard/i/<int:iid>")
@login_required
def inst_detail(iid):
    inst = get_inst(iid)
    if not inst: abort(404)
    path = norm_rel(request.args.get("p", ""))
    running = is_running(iid) or inst["status"] == "running"

    file_rows = render_file_rows(iid, inst, path)
    crumb = render_breadcrumb(iid, path)
    safe_path_attr = path.replace('"', '&quot;')

    log_path = os.path.join(LOG_DIR, "inst_" + str(iid) + ".log")
    log = ""
    if os.path.exists(log_path):
        with open(log_path, "r", errors="replace") as lf:
            log = lf.read()[-6000:]

    running_tag = '<span class="tag tag-ok">● Running</span>' if running else '<span class="tag tag-off">○ Stopped</span>'
    btn_start = '<form method="post" action="/dashboard/i/' + str(iid) + '/start" style="display:inline"><button class="btn btn-p btn-s">▶ Start</button></form>' if not running else ""
    btn_stop  = '<form method="post" action="/dashboard/i/' + str(iid) + '/stop" style="display:inline"><button class="btn btn-g btn-s">⏹ Stop</button></form>' if running else ""
    btn_restart = '<form method="post" action="/dashboard/i/' + str(iid) + '/restart" style="display:inline"><button class="btn btn-g btn-s">↻ Restart</button></form>' if running else ""
    open_btn  = '<a href="/app/' + str(iid) + '/" target="_blank" class="btn btn-g btn-s">↗ Open</a>'

    up_btn = ""
    if path:
        parent = "/".join(path.split("/")[:-1])
        up_btn = '<a href="/dashboard/i/' + str(iid) + ('?p=' + urllib.parse.quote(parent) if parent else '') + '" class="btn btn-g btn-s">↑ Up</a>'

    log_script = (
        "<script>setInterval(function(){fetch('/dashboard/i/" + str(iid) + "/log')"
        ".then(function(r){return r.text()})"
        ".then(function(t){var e=document.getElementById('log');if(e)e.textContent=t||'(no output)';});"
        "},3000);</script>"
    )

    body = (
        '<div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:20px;flex-wrap:wrap;gap:12px">'
        '<div><a href="/dashboard" class="mut" style="font-size:13px">← Dashboard</a>'
        '<h1 style="margin-top:6px">' + inst["name"] + ' ' + running_tag + '</h1>'
        '<p class="mut">' + inst["type"] + ' · port ' + str(inst["port"]) + ' · 24×7 watchdog ' + ('ON' if running else 'standby') + '</p></div>'
        '<div style="display:flex;gap:8px;flex-wrap:wrap">' + btn_start + btn_stop + btn_restart + open_btn +
        '<form method="post" action="/dashboard/i/' + str(iid) + '/delete" style="display:inline" onsubmit="return confirm(\'Delete this instance permanently?\')">'
        '<button class="btn btn-d btn-s">🗑 Delete</button></form></div></div>'
        '<div class="card" style="margin-bottom:16px"><h3 style="margin-bottom:12px">📁 File Manager</h3>'
        '<div class="crumb">' + crumb + '</div>'
        '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px">'
        + up_btn +
        '<button class="btn btn-g btn-s" onclick="document.getElementById(\'newfolder\').style.display=document.getElementById(\'newfolder\').style.display===\'block\'?\'none\':\'block\'">+ Folder</button>'
        '<button class="btn btn-g btn-s" onclick="document.getElementById(\'newfile\').style.display=document.getElementById(\'newfile\').style.display===\'block\'?\'none\':\'block\'">+ File</button>'
        '</div>'
        '<div id="newfolder" style="display:none;margin-bottom:12px">'
        '<form method="post" action="/dashboard/i/' + str(iid) + '/new-folder" style="display:flex;gap:8px">'
        '<input type="hidden" name="p" value="' + safe_path_attr + '">'
        '<input name="fname" placeholder="folder-name" required>'
        '<button class="btn btn-p btn-s">Create Folder</button></form></div>'
        '<div id="newfile" style="display:none;margin-bottom:12px">'
        '<form method="post" action="/dashboard/i/' + str(iid) + '/new-file" style="display:flex;gap:8px">'
        '<input type="hidden" name="p" value="' + safe_path_attr + '">'
        '<input name="fname" placeholder="filename.txt" required>'
        '<button class="btn btn-p btn-s">Create File</button></form></div>'
        '<table>' + file_rows + '</table>'
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:16px" id="ups">'
        '<div class="drop">'
        '<form method="post" action="/dashboard/i/' + str(iid) + '/upload" enctype="multipart/form-data">'
        '<input type="hidden" name="p" value="' + safe_path_attr + '">'
        '<p class="mut" style="margin-bottom:10px;font-size:13px">⬆ Upload file(s) here</p>'
        '<input type="file" name="files" multiple required style="padding:8px;margin-bottom:10px">'
        '<button class="btn btn-p btn-s">Upload</button></form></div>'
        '<div class="drop">'
        '<form method="post" action="/dashboard/i/' + str(iid) + '/upload-zip" enctype="multipart/form-data">'
        '<input type="hidden" name="p" value="' + safe_path_attr + '">'
        '<p class="mut" style="margin-bottom:10px;font-size:13px">📦 Upload ZIP → auto-extract</p>'
        '<input type="file" name="zip" accept=".zip" required style="padding:8px;margin-bottom:10px">'
        '<button class="btn btn-p btn-s">Extract</button></form></div>'
        '</div>'
        '</div>'
        '<div class="card"><h3 style="margin-bottom:14px">📜 Live Log</h3>'
        '<pre id="log" style="height:320px">' + (log.replace("&","&amp;").replace("<","&lt;") or 'No output yet — start the app to see logs.') + '</pre>'
        '<button class="btn btn-g btn-s" onclick="location.reload()" style="margin-top:10px">↻ Refresh</button>'
        '</div>'
        '<style>@media(max-width:800px){#ups{grid-template-columns:1fr!important}}</style>'
        + log_script
    )
    return render(inst["name"], body)

@app.route("/dashboard/i/<int:iid>/log")
@login_required
def inst_log(iid):
    if not get_inst(iid): abort(404)
    lp = os.path.join(LOG_DIR, "inst_" + str(iid) + ".log")
    if not os.path.exists(lp): return ""
    with open(lp, "r", errors="replace") as f:
        return f.read()[-6000:]

@app.route("/dashboard/i/<int:iid>/start", methods=["POST"])
@login_required
def inst_start(iid):
    inst = get_inst(iid)
    if inst: start_inst(inst)
    return redirect(url_for("inst_detail", iid=iid))

@app.route("/dashboard/i/<int:iid>/stop", methods=["POST"])
@login_required
def inst_stop(iid):
    if get_inst(iid): stop_inst(iid)
    return redirect(url_for("inst_detail", iid=iid))

@app.route("/dashboard/i/<int:iid>/restart", methods=["POST"])
@login_required
def inst_restart(iid):
    inst = get_inst(iid)
    if inst:
        stop_inst(iid)
        time.sleep(0.5)
        start_inst(inst)
    return redirect(url_for("inst_detail", iid=iid))

@app.route("/dashboard/i/<int:iid>/delete", methods=["POST"])
@login_required
def inst_delete(iid):
    inst = get_inst(iid)
    if inst:
        stop_inst(iid)
        try: shutil.rmtree(inst_dir(inst))
        except: pass
        d = db(); d.execute("DELETE FROM instances WHERE id=?", (iid,)); d.commit(); d.close()
        flash("Instance deleted", "ok")
    return redirect(url_for("dashboard"))

# ---------- file editor ----------
@app.route("/dashboard/i/<int:iid>/edit")
@login_required
def inst_edit(iid):
    inst = get_inst(iid)
    if not inst: abort(404)
    fname = norm_rel(request.args.get("f", ""))
    if not fname: abort(400)
    fp = safe_join(inst_dir(inst), fname)
    if not fp or not os.path.isfile(fp): abort(404)
    try:
        with open(fp, "r", errors="replace") as f: content = f.read()
    except: content = ""
    # Limit editor to 2 MB
    if len(content) > 2_000_000:
        flash("File too large to edit (>2MB)", "err")
        return redirect(url_for("inst_detail", iid=iid))
    safe_content = content.replace('&','&amp;').replace('<','&lt;')
    q = urllib.parse.quote(fname)
    body = (
        '<a href="/dashboard/i/' + str(iid) + '?p=' + urllib.parse.quote("/".join(fname.split("/")[:-1])) + '" class="mut" style="font-size:13px">← Back</a>'
        '<h1 style="margin-top:6px">Edit: ' + fname + '</h1>'
        '<form method="post" action="/dashboard/i/' + str(iid) + '/save">'
        '<input type="hidden" name="f" value="' + fname.replace('"','&quot;') + '">'
        '<textarea name="content" style="min-height:460px;font-family:\'JetBrains Mono\',monospace;font-size:13px;line-height:1.5" spellcheck="false">' + safe_content + '</textarea>'
        '<button class="btn btn-p" style="margin-top:12px">💾 Save</button></form>'
    )
    return render("Edit " + fname, body)

@app.route("/dashboard/i/<int:iid>/save", methods=["POST"])
@login_required
def inst_save(iid):
    inst = get_inst(iid)
    if not inst: abort(404)
    fname = norm_rel(request.form["f"])
    fp = safe_join(inst_dir(inst), fname)
    if not fp: abort(400)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w") as f: f.write(request.form.get("content", ""))
    flash("Saved " + fname, "ok")
    return redirect(url_for("inst_edit", iid=iid, f=fname))

# ---------- uploads ----------
@app.route("/dashboard/i/<int:iid>/upload", methods=["POST"])
@login_required
def inst_upload(iid):
    inst = get_inst(iid)
    if not inst: abort(404)
    p = norm_rel(request.form.get("p", ""))
    target = safe_join(inst_dir(inst), p) if p else inst_dir(inst)
    if not target: abort(400)
    os.makedirs(target, exist_ok=True)
    files = request.files.getlist("files") or request.files.getlist("file")
    count = 0
    for f in files:
        if not f or not f.filename: continue
        safe_name = re.sub(r'[^\w.\- ]', '_', os.path.basename(f.filename))
        if not safe_name: continue
        f.save(os.path.join(target, safe_name))
        count += 1
    flash("Uploaded " + str(count) + " file(s)", "ok")
    return redirect(url_for("inst_detail", iid=iid, p=p))

@app.route("/dashboard/i/<int:iid>/upload-zip", methods=["POST"])
@login_required
def inst_upload_zip(iid):
    inst = get_inst(iid)
    if not inst: abort(404)
    p = norm_rel(request.form.get("p", ""))
    target = safe_join(inst_dir(inst), p) if p else inst_dir(inst)
    if not target: abort(400)
    os.makedirs(target, exist_ok=True)
    f = request.files.get("zip")
    if not f or not f.filename:
        flash("No ZIP selected", "err")
        return redirect(url_for("inst_detail", iid=iid, p=p))
    try:
        data = f.read()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            extracted = 0
            for member in z.infolist():
                name = member.filename
                if not name or name.endswith("/"): continue
                # sanitize
                name = name.replace("\\", "/")
                parts = [re.sub(r'[^\w.\- ]', '_', seg) for seg in name.split("/") if seg and seg not in (".","..")]
                if not parts: continue
                rel = "/".join(parts)
                dest = safe_join(target, rel)
                if not dest: continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(member) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                extracted += 1
        flash("Extracted " + str(extracted) + " file(s) from ZIP", "ok")
    except zipfile.BadZipFile:
        flash("Invalid ZIP file", "err")
    except Exception as e:
        flash("Extract error: " + str(e), "err")
    return redirect(url_for("inst_detail", iid=iid, p=p))

@app.route("/dashboard/i/<int:iid>/new-file", methods=["POST"])
@login_required
def inst_newfile(iid):
    inst = get_inst(iid)
    if not inst: abort(404)
    p = norm_rel(request.form.get("p", ""))
    fname = norm_rel(request.form.get("fname", ""))
    if fname:
        fp = safe_join(inst_dir(inst), p, fname)
        if fp:
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            if not os.path.exists(fp): open(fp, "w").close()
            flash("Created " + fname, "ok")
    return redirect(url_for("inst_detail", iid=iid, p=p))

@app.route("/dashboard/i/<int:iid>/new-folder", methods=["POST"])
@login_required
def inst_newfolder(iid):
    inst = get_inst(iid)
    if not inst: abort(404)
    p = norm_rel(request.form.get("p", ""))
    fname = norm_rel(request.form.get("fname", ""))
    if fname:
        fp = safe_join(inst_dir(inst), p, fname)
        if fp:
            os.makedirs(fp, exist_ok=True)
            flash("Created folder " + fname, "ok")
    return redirect(url_for("inst_detail", iid=iid, p=p))

@app.route("/dashboard/i/<int:iid>/delete-file", methods=["POST"])
@login_required
def inst_delfile(iid):
    inst = get_inst(iid)
    if not inst: abort(404)
    p = norm_rel(request.form.get("p", ""))
    fname = norm_rel(request.form.get("f", ""))
    if fname:
        fp = safe_join(inst_dir(inst), fname)
        if fp:
            try:
                if os.path.isdir(fp): shutil.rmtree(fp)
                else: os.remove(fp)
                flash("Deleted " + fname, "ok")
            except Exception as e:
                flash("Delete failed: " + str(e), "err")
    return redirect(url_for("inst_detail", iid=iid, p=p))

# ---------- proxy ----------
@app.route("/app/<int:iid>/", defaults={"path": ""})
@app.route("/app/<int:iid>/<path:path>")
def proxy(iid, path):
    inst = get_inst(iid)
    if not inst: abort(404)
    idir = inst_dir(inst)
    if inst["type"] == "static":
        if path == "" or path.endswith("/"): path = (path or "") + "index.html"
        fp = safe_join(idir, path)
        if not fp or not os.path.isfile(fp): abort(404)
        return send_from_directory(idir, path.replace("\\","/"))
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

# ---------- boot ----------
init_db()

# auto-start previously-running instances on panel boot
def boot_autostart():
    time.sleep(1)
    try:
        d = db()
        rows = d.execute("SELECT * FROM instances WHERE status='running'").fetchall()
        d.close()
        for r in rows:
            inst = get_inst(r["id"])
            if inst: start_inst(inst)
    except Exception as e:
        print("[boot]", e)

threading.Thread(target=boot_autostart, daemon=True).start()
threading.Thread(target=watchdog, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print("🚀 MODx Hosting Panel (Green) running on port " + str(port))
    print("   Login → username: " + USERNAME + "  password: " + PASSWORD)
    app.run(host="0.0.0.0", port=port, threaded=True)
