#!/usr/bin/env python3
"""
╔═══════════════════════════════════════════════════════════════════════╗
║  MODx Hosting Panel · Nebula Edition · v1.0 FINAL                     ║
║  Single-file, production-grade hosting platform                       ║
║                                                                       ║
║  Features:                                                            ║
║   • Python · Node.js · Static site hosting                            ║
║   • Auto-detect + fully editable start / build commands               ║
║   • ZIP / TAR upload with auto-extract (zip-slip safe)                ║
║   • Subfolder file manager, in-browser editor, bulk upload            ║
║   • Environment variables per instance                                ║
║   • 24×7 watchdog · auto-restart · boot-restore                       ║
║   • Streaming build console · live logs · activity feed               ║
║   • Single-user auth · dark green "Nebula" theme                      ║
╚═══════════════════════════════════════════════════════════════════════╝
"""
import os, re, sys, json, sqlite3, subprocess, secrets, time
import socket, shutil, signal, threading, zipfile, io, tarfile
import urllib.request, urllib.error, urllib.parse, traceback
from functools import wraps
from flask import (Flask, request, session, redirect, url_for,
                   render_template_string, send_from_directory,
                   abort, flash, Response)

# ─────────────────────────────── Config ───────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024   # 1 GB uploads
app.config["PROPAGATE_EXCEPTIONS"] = False

AUTH_USER = os.environ.get("PANEL_USER", "Zesty")
AUTH_PASS = os.environ.get("PANEL_PASS", "123456")

BASE_DIR = os.environ.get("HOSTING_DIR", "/app/hosting")
DATA_DIR = os.path.join(BASE_DIR, "data")
INST_DIR = os.path.join(BASE_DIR, "instances")
LOG_DIR  = os.path.join(BASE_DIR, "logs")
DB_PATH  = os.path.join(DATA_DIR, "hosting.db")
for _d in (DATA_DIR, INST_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)

PROCS = {}          # iid -> Popen
LAST_START = {}     # iid -> ts (watchdog cooldown)
BUILD_LOCK = threading.Lock()

# ───────────────────────────── Error handlers ─────────────────────────
@app.errorhandler(500)
def _internal_error(e):
    tb = traceback.format_exc()
    print("\n[500]", e, "\n", tb, flush=True)
    return Response(
        "<html><body style='background:#040b07;color:#ff5577;"
        "font-family:ui-monospace,monospace;padding:30px'>"
        "<h2>◈ Internal Server Error</h2>"
        "<p style='color:#7ea88f'>Check server logs for the full traceback.</p>"
        "<pre style='background:#010704;color:#8affb5;padding:16px;"
        "border-radius:10px;overflow:auto'>" + tb.replace("<", "&lt;") + "</pre>"
        "</body></html>", 500)

@app.errorhandler(404)
def _not_found(e):
    return Response(
        "<html><body style='background:#040b07;color:#7ea88f;"
        "font-family:ui-monospace,monospace;padding:30px'>"
        "<h2 style='color:#e6fff0'>◈ 404 — Not Found</h2>"
        "<p><a href='/' style='color:#5bffce'>← Back to panel</a></p>"
        "</body></html>", 404)

# ─────────────────────────────── Database ─────────────────────────────
def db():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c

def _table_columns(d, table):
    try:
        return {r["name"] for r in d.execute("PRAGMA table_info(%s)" % table)}
    except Exception:
        return set()

def init_db():
    """Create or migrate the schema. Safe to call on every boot."""
    d = db()

    # --- Detect legacy / incompatible schema -----------------------------
    cols = _table_columns(d, "instances")
    needs_rebuild = bool(cols) and ("slug" not in cols or "user_id" in cols)
    if needs_rebuild:
        print("[db] legacy 'instances' schema detected — rebuilding table", flush=True)
        try:
            d.execute("DROP TABLE IF EXISTS instances_legacy")
            d.execute("ALTER TABLE instances RENAME TO instances_legacy")
        except Exception as e:
            print("[db] rename failed (%s) — dropping table" % e, flush=True)
            try: d.execute("DROP TABLE instances")
            except Exception: pass

    # --- Create fresh schema ---------------------------------------------
    d.executescript("""
    CREATE TABLE IF NOT EXISTS instances (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT    NOT NULL,
        slug          TEXT    UNIQUE NOT NULL,
        type          TEXT    NOT NULL,
        work_dir      TEXT    DEFAULT '',
        start_cmd     TEXT    DEFAULT '',
        build_cmd     TEXT    DEFAULT '',
        env           TEXT    DEFAULT '',
        port          INTEGER,
        dir           TEXT    NOT NULL,
        status        TEXT    DEFAULT 'stopped',
        autostart     INTEGER DEFAULT 1,
        auto_build    INTEGER DEFAULT 1,
        deps_ready    INTEGER DEFAULT 0,
        created_at    INTEGER,
        last_boot     INTEGER
    );
    CREATE TABLE IF NOT EXISTS activity (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        iid      INTEGER,
        kind     TEXT,
        message  TEXT,
        ts       INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_activity_iid ON activity(iid);
    CREATE INDEX IF NOT EXISTS idx_activity_ts  ON activity(ts);
    """)

    # --- Best-effort migration from legacy table -------------------------
    legacy_cols = _table_columns(d, "instances_legacy")
    if legacy_cols:
        print("[db] migrating rows from legacy table…", flush=True)
        try:
            rows = d.execute("SELECT * FROM instances_legacy").fetchall()
            for r in rows:
                keys = set(r.keys())
                name = (r["name"] if "name" in keys else "app") or "app"
                base = re.sub(r'[^a-z0-9-]+', '-', str(name).lower()).strip('-') or "app"
                slug = base + "-" + secrets.token_hex(2)
                typ  = r["type"]       if "type"       in keys else "static"
                port = r["port"]       if "port"       in keys else None
                dr   = r["dir"]        if "dir"        in keys else ""
                st   = r["status"]     if "status"     in keys else "stopped"
                ct   = r["created_at"] if "created_at" in keys else now()
                d.execute(
                    "INSERT OR IGNORE INTO instances "
                    "(name,slug,type,work_dir,start_cmd,build_cmd,env,"
                    " port,dir,status,autostart,auto_build,deps_ready,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (name, slug, typ, "", "", "", "", port, dr, st, 1, 1, 0, ct))
            d.execute("DROP TABLE instances_legacy")
            print("[db] migration complete", flush=True)
        except Exception as e:
            print("[db] row migration failed: %s" % e, flush=True)

    # --- Safety net: ensure every expected column exists -----------------
    expected = {
        "slug": "TEXT", "work_dir": "TEXT DEFAULT ''",
        "start_cmd": "TEXT DEFAULT ''", "build_cmd": "TEXT DEFAULT ''",
        "env": "TEXT DEFAULT ''", "autostart": "INTEGER DEFAULT 1",
        "auto_build": "INTEGER DEFAULT 1", "deps_ready": "INTEGER DEFAULT 0",
        "last_boot": "INTEGER",
    }
    have = _table_columns(d, "instances")
    for col, decl in expected.items():
        if col not in have:
            try:
                d.execute("ALTER TABLE instances ADD COLUMN %s %s" % (col, decl))
                print("[db] added missing column:", col, flush=True)
            except Exception as e:
                print("[db] could not add column", col, "->", e, flush=True)

    d.commit(); d.close()

# ─────────────────────────────── Helpers ──────────────────────────────
def now(): return int(time.time())

def log_activity(iid, kind, message):
    try:
        d = db()
        d.execute("INSERT INTO activity(iid,kind,message,ts) VALUES(?,?,?,?)",
                  (iid, kind, (message or "")[:500], now()))
        d.execute("DELETE FROM activity WHERE id NOT IN "
                  "(SELECT id FROM activity ORDER BY id DESC LIMIT 500)")
        d.commit(); d.close()
    except Exception:
        pass

def port_free(p):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", p)); s.close(); return True
    except OSError:
        return False

def alloc_port():
    d = db()
    used = {r["port"] for r in d.execute("SELECT port FROM instances WHERE port IS NOT NULL")}
    d.close()
    p = 4100
    while p < 6000:
        if p not in used and port_free(p): return p
        p += 1
    raise RuntimeError("No free ports available")

def human_size(n):
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return ("%d %s" % (f, unit)) if unit == "B" else ("%.1f %s" % (f, unit))
        f /= 1024.0
    return "%.1f PB" % f

def safe_join(base, *paths):
    base = os.path.abspath(base)
    target = os.path.abspath(os.path.join(base, *paths))
    if target != base and not target.startswith(base + os.sep):
        return None
    return target

def norm_rel(p):
    if not p: return ""
    p = str(p).replace("\\", "/").strip("/")
    out = []
    for part in p.split("/"):
        if part in ("", ".", ".."): continue
        clean = re.sub(r'[^\w.\- ]', '_', part).strip()
        if clean: out.append(clean)
    return "/".join(out)

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))

# ─────────────────────────────── Auth ─────────────────────────────────
def logged_in(): return session.get("logged_in") is True

def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not logged_in(): return redirect(url_for("login"))
        return f(*a, **kw)
    return w

# ──────────────────────────── Instance model ──────────────────────────
def get_inst(iid):
    d = db()
    r = d.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    d.close(); return r

def inst_dir(inst): return os.path.join(INST_DIR, inst["dir"])

def inst_work_dir(inst):
    if inst["work_dir"]:
        return safe_join(inst_dir(inst), inst["work_dir"]) or inst_dir(inst)
    return inst_dir(inst)

def inst_log(iid): return os.path.join(LOG_DIR, "inst_%d.log" % iid)

def is_running(iid):
    p = PROCS.get(iid)
    return bool(p and p.poll() is None)

def read_log(iid, max_bytes=20000):
    lp = inst_log(iid)
    if not os.path.exists(lp): return ""
    try:
        with open(lp, "rb") as f:
            f.seek(0, 2); size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""

def write_log(iid, text):
    try:
        with open(inst_log(iid), "a") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except Exception:
        pass

def clear_log(iid):
    try: open(inst_log(iid), "w").close()
    except Exception: pass

# ─────────────────────────────── Auto-detect ──────────────────────────
def detect_commands(idir, typ):
    """Return (start_cmd, build_cmd) inferred from a project layout."""
    start = ""
    build = ""

    def has(f):  return os.path.isfile(os.path.join(idir, f))
    def exists(f): return os.path.exists(os.path.join(idir, f))

    if typ == "python":
        if has("manage.py"):
            start = "python3 manage.py runserver 0.0.0.0:$PORT"
            if has("requirements.txt"):
                build = "pip install -r requirements.txt"
        elif has("main.py") or has("app.py") or has("server.py"):
            entry = next(f for f in ("main.py", "app.py", "server.py") if has(f))
            if exists("requirements.txt"):
                try:
                    reqs = open(os.path.join(idir, "requirements.txt")).read().lower()
                except Exception:
                    reqs = ""
                if "fastapi" in reqs or "uvicorn" in reqs:
                    mod = "main:app" if entry == "main.py" else "app:app"
                    start = "uvicorn %s --host 0.0.0.0 --port $PORT" % mod
                else:
                    start = "python3 %s" % entry
                build = "pip install -r requirements.txt"
            else:
                start = "python3 %s" % entry
        elif has("bot.py"):
            start = "python3 bot.py"
            if has("requirements.txt"):
                build = "pip install -r requirements.txt"
        elif exists("requirements.txt"):
            build = "pip install -r requirements.txt"

    elif typ == "node":
        pkg = os.path.join(idir, "package.json")
        scripts = {}
        if os.path.isfile(pkg):
            try:
                data = json.load(open(pkg))
                scripts = data.get("scripts") or {}
            except Exception:
                scripts = {}
            if "start" in scripts:
                start = "npm start"
            elif scripts.get("dev"):
                start = "npm run dev"
            elif has("index.js"):
                start = "node index.js"
            elif has("server.js"):
                start = "node server.js"
            elif has("app.js"):
                start = "node app.js"

            if has("package-lock.json"):
                build = "npm ci --omit=dev"
            else:
                build = "npm install --omit=dev"
            if "build" in scripts:
                build = (build + " && npm run build") if build else "npm run build"
        elif has("index.js"):
            start = "node index.js"
        elif has("server.js"):
            start = "node server.js"

    # typ == "static" → no commands needed
    return start, build

# ─────────────────────────────── Process ctrl ─────────────────────────
def _env_for(inst):
    env = dict(os.environ,
               PORT=str(inst["port"]),
               HOST="0.0.0.0",
               PYTHONUNBUFFERED="1",
               NODE_ENV="production",
               MODX_INSTANCE=inst["slug"])
    for line in (inst["env"] or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env

def run_streaming(iid, cmd, label, cwd, timeout=2400):
    """Run a shell command and stream stdout/stderr into the instance log."""
    write_log(iid, "\n[%s] ▸ %s" % (label, cmd))
    write_log(iid, "[%s] cwd: %s" % (label, cwd))
    log_activity(iid, label, "running: %s" % cmd[:200])
    try:
        p = subprocess.Popen(
            cmd, cwd=cwd, shell=True, executable="/bin/bash",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONUNBUFFERED="1"),
            preexec_fn=os.setsid)
        start = time.time()
        for raw in iter(p.stdout.readline, b""):
            write_log(iid, "[%s] %s" % (label, raw.decode("utf-8", "replace").rstrip()))
            if time.time() - start > timeout:
                try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except Exception: pass
                write_log(iid, "[%s] ⏱ timeout — killed" % label)
                break
        p.wait(timeout=30)
        code = p.returncode
        write_log(iid, "[%s] %s (exit %d)" % (label,
                    "✓ finished" if code == 0 else "✗ failed", code))
        log_activity(iid, label, "%s (exit %d)" % (
                    "finished" if code == 0 else "failed", code))
        return code
    except Exception as e:
        write_log(iid, "[%s] ✗ error: %s" % (label, e))
        log_activity(iid, "error", "%s: %s" % (label, e))
        return -1

def start_inst(inst):
    if is_running(inst["id"]): return False

    # Static sites have no process
    if inst["type"] == "static":
        d = db()
        d.execute("UPDATE instances SET status='running', last_boot=? WHERE id=?",
                  (now(), inst["id"]))
        d.commit(); d.close()
        log_activity(inst["id"], "start", "Static live")
        return True

    cmd = (inst["start_cmd"] or "").strip()
    if not cmd:
        start_cmd, _ = detect_commands(inst_work_dir(inst), inst["type"])
        cmd = start_cmd
        if cmd:
            d = db()
            d.execute("UPDATE instances SET start_cmd=? WHERE id=?", (cmd, inst["id"]))
            d.commit(); d.close()
            write_log(inst["id"], "[panel] auto-detected start command: %s" % cmd)

    if not cmd:
        write_log(inst["id"], "[panel] no start command configured — open Settings")
        log_activity(inst["id"], "error", "No start command")
        return False

    wd = inst_work_dir(inst)
    os.makedirs(wd, exist_ok=True)

    logf = open(inst_log(inst["id"]), "ab", buffering=0)
    logf.write(("\n[panel] ▶ launching at %s\n[panel] cmd: %s\n[panel] cwd: %s\n"
                % (time.strftime("%Y-%m-%d %H:%M:%S"), cmd, wd)).encode())
    try:
        p = subprocess.Popen(
            cmd, cwd=wd, shell=True, executable="/bin/bash",
            stdout=logf, stderr=logf, env=_env_for(inst),
            preexec_fn=os.setsid, close_fds=True)
        PROCS[inst["id"]] = p
        d = db()
        d.execute("UPDATE instances SET status='running', last_boot=? WHERE id=?",
                  (now(), inst["id"]))
        d.commit(); d.close()
        log_activity(inst["id"], "start", "Started (pid %d): %s" % (p.pid, cmd[:120]))
        return True
    except Exception as e:
        write_log(inst["id"], "[panel] start failed: %s" % e)
        log_activity(inst["id"], "error", "Start failed: %s" % e)
        return False

def stop_inst(iid, silent=False):
    p = PROCS.pop(iid, None)
    if p and p.poll() is None:
        try: os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception: pass
        try: p.wait(timeout=6)
        except Exception:
            try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception: pass
    d = db()
    d.execute("UPDATE instances SET status='stopped' WHERE id=?", (iid,))
    d.commit(); d.close()
    if not silent:
        log_activity(iid, "stop", "Stopped")

def restart_inst(inst):
    stop_inst(inst["id"], silent=True)
    time.sleep(0.6)
    return start_inst(inst)

# ─────────────────────────────── Build ────────────────────────────────
def build_inst(iid, async_=True):
    def work():
        with BUILD_LOCK:
            inst = get_inst(iid)
            if not inst: return
            cmd = (inst["build_cmd"] or "").strip()
            if not cmd:
                _, detected = detect_commands(inst_work_dir(inst), inst["type"])
                cmd = detected
                if cmd:
                    d = db()
                    d.execute("UPDATE instances SET build_cmd=? WHERE id=?", (cmd, iid))
                    d.commit(); d.close()
                    write_log(iid, "[panel] auto-detected build command: %s" % cmd)
            if not cmd:
                write_log(iid, "[build] no build command — skipping")
                return
            code = run_streaming(iid, cmd, "build", inst_work_dir(inst))
            if code == 0:
                d = db()
                d.execute("UPDATE instances SET deps_ready=1 WHERE id=?", (iid,))
                d.commit(); d.close()
    if async_:
        threading.Thread(target=work, daemon=True).start()
    else:
        work()

# ─────────────────────────────── Watchdog ─────────────────────────────
def watchdog():
    while True:
        try:
            d = db()
            rows = d.execute(
                "SELECT * FROM instances WHERE status='running' "
                "AND autostart=1 AND type!='static'"
            ).fetchall()
            d.close()
            for r in rows:
                if is_running(r["id"]): continue
                if time.time() - LAST_START.get(r["id"], 0) < 6: continue
                LAST_START[r["id"]] = time.time()
                inst = get_inst(r["id"])
                if inst:
                    write_log(inst["id"], "[watchdog] process died — auto-restarting")
                    log_activity(inst["id"], "watchdog", "Auto-restart triggered")
                    start_inst(inst)
        except Exception as e:
            print("[watchdog]", e, flush=True)
        time.sleep(6)

# ─────────────────────────────── Archives ─────────────────────────────
def extract_zip_bytes(data, target, strip_root=False):
    count = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        members = [m for m in z.infolist() if not m.is_dir()]
        single_root = None
        if strip_root and members:
            roots = set(m.filename.split("/", 1)[0]
                        for m in members if "/" in m.filename)
            if len(roots) == 1 and all("/" in m.filename for m in members):
                single_root = next(iter(roots))
        for m in members:
            rel_raw = m.filename.replace("\\", "/")
            if single_root and rel_raw.startswith(single_root + "/"):
                rel_raw = rel_raw[len(single_root) + 1:]
            rel = norm_rel(rel_raw)
            if not rel: continue
            dest = safe_join(target, rel)
            if not dest: continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(m) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count

def extract_tar_bytes(data, target, strip_root=False):
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
        members = [m for m in t.getmembers() if m.isfile()]
        single_root = None
        if strip_root and members:
            roots = set(m.name.split("/", 1)[0]
                        for m in members if "/" in m.name)
            if len(roots) == 1 and all("/" in m.name for m in members):
                single_root = next(iter(roots))
        for m in members:
            rel_raw = m.name.replace("\\", "/")
            if single_root and rel_raw.startswith(single_root + "/"):
                rel_raw = rel_raw[len(single_root) + 1:]
            rel = norm_rel(rel_raw)
            if not rel: continue
            dest = safe_join(target, rel)
            if not dest: continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            f = t.extractfile(m)
            if not f: continue
            with f, open(dest, "wb") as out:
                shutil.copyfileobj(f, out)
            count += 1
    return count

def extract_archive(data, filename, target, strip_root=False):
    low = filename.lower()
    if low.endswith(".zip"):
        return extract_zip_bytes(data, target, strip_root), "zip"
    if low.endswith((".tar", ".tar.gz", ".tgz",
                     ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        return extract_tar_bytes(data, target, strip_root), "tar"
    raise ValueError("Unsupported archive: " + filename)

# ─────────────────────────────── Theme ────────────────────────────────
CSS = r"""
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#040b07;--bg2:#061410;--surf:#0a1a12;--surf2:#0f2a1b;--surf3:#133324;
  --brd:rgba(0,255,140,0.10);--brd2:rgba(0,255,140,0.22);
  --pri:#00ff9d;--pri2:#5bffce;--pri3:#b9ffd3;
  --ok:#00ff9d;--warn:#ffcc44;--err:#ff5577;--info:#5bffce;
  --txt:#e6fff0;--mut:#7ea88f;--mut2:#527a63;
}
html,body{background:var(--bg);color:var(--txt);
  font-family:'Inter',system-ui,-apple-system,sans-serif;
  min-height:100vh;line-height:1.55;-webkit-font-smoothing:antialiased;font-size:14px}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:-2;
  background:
    radial-gradient(900px circle at 12% -8%,rgba(0,255,157,0.10),transparent 60%),
    radial-gradient(900px circle at 108% 8%,rgba(91,255,206,0.07),transparent 55%),
    radial-gradient(700px circle at 50% 118%,rgba(0,255,157,0.05),transparent 60%)}
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:-1;
  background-image:linear-gradient(rgba(0,255,140,0.022) 1px,transparent 1px),
                   linear-gradient(90deg,rgba(0,255,140,0.022) 1px,transparent 1px);
  background-size:46px 46px;
  mask-image:radial-gradient(ellipse at center,black 25%,transparent 82%);
  -webkit-mask-image:radial-gradient(ellipse at center,black 25%,transparent 82%)}
a{color:var(--pri2);text-decoration:none;transition:color .15s}
a:hover{color:var(--pri3)}
::selection{background:rgba(0,255,157,0.28);color:#fff}

.container{max-width:1280px;margin:0 auto;padding:0 22px}
.topbar{border-bottom:1px solid var(--brd);background:rgba(4,11,7,0.82);
  backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);
  position:sticky;top:0;z-index:100}
.topbar-in{display:flex;align-items:center;justify-content:space-between;
  padding:13px 22px;max-width:1280px;margin:0 auto;gap:14px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:11px;font-weight:600;font-size:15px}
.brand .mark{width:30px;height:30px;border-radius:9px;
  background:linear-gradient(135deg,var(--pri),var(--pri2));
  display:grid;place-items:center;color:#04180d;
  font-family:'JetBrains Mono',monospace;font-weight:700;font-size:14px;
  box-shadow:0 4px 20px rgba(0,255,157,.35)}
.brand .name{font-family:'JetBrains Mono',monospace;font-weight:700;
  letter-spacing:-0.2px;
  background:linear-gradient(135deg,var(--pri),var(--pri2));
  -webkit-background-clip:text;background-clip:text;
  -webkit-text-fill-color:transparent}
.nav{display:flex;gap:3px;align-items:center;flex-wrap:wrap}
.nav a{color:var(--mut);font-size:13px;padding:7px 14px;border-radius:9px;
  transition:all .15s;font-weight:500}
.nav a:hover,.nav a.active{color:var(--txt);background:var(--surf2);text-decoration:none}
.nav a.cta{background:linear-gradient(135deg,var(--pri),var(--pri2));
  color:#04180d;font-weight:600}
.nav a.cta:hover{box-shadow:0 6px 22px rgba(0,255,157,.38);color:#04180d}

.btn{display:inline-flex;align-items:center;gap:7px;padding:9px 17px;
  border-radius:11px;font-size:13px;font-weight:500;border:none;cursor:pointer;
  font-family:inherit;transition:all .18s;text-decoration:none;line-height:1;white-space:nowrap}
.btn-p{background:linear-gradient(135deg,var(--pri),var(--pri2));color:#04180d;font-weight:600}
.btn-p:hover{transform:translateY(-1px);box-shadow:0 10px 28px rgba(0,255,157,.35);
  text-decoration:none;color:#04180d}
.btn-g{background:var(--surf2);color:var(--txt);border:1px solid var(--brd)}
.btn-g:hover{border-color:var(--pri);background:rgba(0,255,157,.08);
  text-decoration:none;color:var(--txt)}
.btn-d{background:rgba(255,85,119,.10);color:var(--err);border:1px solid rgba(255,85,119,.28)}
.btn-d:hover{background:rgba(255,85,119,.18);text-decoration:none;color:var(--err)}
.btn-w{background:rgba(255,204,68,.10);color:var(--warn);border:1px solid rgba(255,204,68,.28)}
.btn-w:hover{background:rgba(255,204,68,.18);text-decoration:none;color:var(--warn)}
.btn-s{padding:6px 12px;font-size:12px;border-radius:8px}
.btn-xs{padding:4px 9px;font-size:11px;border-radius:6px}
.btn[disabled]{opacity:.45;cursor:not-allowed;transform:none!important;box-shadow:none!important}

.card{background:linear-gradient(180deg,var(--surf),rgba(10,26,18,0.7));
  border:1px solid var(--brd);border-radius:16px;padding:22px;
  transition:border-color .22s,transform .22s}
.card:hover{border-color:var(--brd2)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}

h1{font-size:clamp(24px,3.4vw,36px);font-weight:700;letter-spacing:-0.9px;
  margin-bottom:10px;line-height:1.15}
h2{font-size:20px;font-weight:600;margin-bottom:14px;letter-spacing:-0.3px}
h3{font-size:15px;font-weight:600;margin-bottom:6px}
.mut{color:var(--mut);font-size:14px}
.mut2{color:var(--mut2);font-size:12px}
.small{font-size:12px}
.mono{font-family:'JetBrains Mono',monospace}

.tag{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;
  border-radius:100px;font-size:11px;font-family:'JetBrains Mono',monospace;
  font-weight:500;line-height:1.5}
.tag .dot{width:6px;height:6px;border-radius:50%;background:currentColor;
  box-shadow:0 0 8px currentColor}
.tag-ok{background:rgba(0,255,157,.10);color:var(--ok);border:1px solid rgba(0,255,157,.22)}
.tag-off{background:rgba(126,168,143,.10);color:var(--mut);border:1px solid rgba(126,168,143,.18)}
.tag-info{background:rgba(91,255,206,.10);color:var(--info);border:1px solid rgba(91,255,206,.22)}
.tag-warn{background:rgba(255,204,68,.10);color:var(--warn);border:1px solid rgba(255,204,68,.25)}

input,select,textarea{width:100%;padding:11px 14px;background:var(--surf2);
  border:1px solid var(--brd);border-radius:11px;color:var(--txt);
  font-family:inherit;font-size:14px;outline:none;transition:all .18s}
input:focus,select:focus,textarea:focus{border-color:var(--pri);
  background:var(--surf3);box-shadow:0 0 0 3px rgba(0,255,157,.10)}
input[type=checkbox]{width:auto;padding:0;margin:0}
textarea{font-family:'JetBrains Mono',monospace;font-size:13px;line-height:1.6;resize:vertical}
select{cursor:pointer}
label{display:block;font-size:12px;color:var(--mut);margin-bottom:7px;
  font-weight:500;letter-spacing:.2px}
.field{margin-bottom:16px}
.field .hint{font-size:11px;color:var(--mut2);margin-top:6px;
  font-family:'JetBrains Mono',monospace;line-height:1.55}
.field .hint code{background:var(--surf2);padding:1px 6px;border-radius:5px;color:var(--pri2)}

.flash{padding:12px 16px;border-radius:11px;font-size:13px;margin-bottom:16px;
  display:flex;align-items:center;gap:9px}
.flash-ok{background:rgba(0,255,157,.08);color:var(--pri2);border:1px solid rgba(0,255,157,.22)}
.flash-err{background:rgba(255,85,119,.08);color:var(--err);border:1px solid rgba(255,85,119,.22)}

table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:10px 8px;color:var(--mut);font-weight:500;font-size:11px;
  text-transform:uppercase;letter-spacing:.6px;border-bottom:1px solid var(--brd)}
td{padding:10px 8px;border-bottom:1px solid var(--brd);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(0,255,157,.02)}

pre{background:#010704;padding:16px;border-radius:12px;overflow:auto;
  color:#8affb5;max-height:440px;border:1px solid var(--brd);
  font-family:'JetBrains Mono',monospace;font-size:12px;line-height:1.6;
  white-space:pre-wrap;word-break:break-word}
code{font-family:'JetBrains Mono',monospace;font-size:12px;
  background:var(--surf2);padding:1px 6px;border-radius:5px;color:var(--pri2)}
pre code{background:none;padding:0;color:inherit}

.cmd-chip{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;
  border-radius:8px;background:var(--surf2);border:1px solid var(--brd);
  font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--pri2);
  max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.center{min-height:calc(100vh - 70px);display:flex;align-items:center;
  justify-content:center;padding:24px}
.auth-card{width:100%;max-width:420px;
  background:linear-gradient(180deg,var(--surf),rgba(10,26,18,0.85));
  border:1px solid var(--brd);border-radius:22px;padding:40px;
  box-shadow:0 30px 90px rgba(0,0,0,.55)}
.auth-mark{width:58px;height:58px;border-radius:16px;margin:0 auto 20px;
  background:linear-gradient(135deg,var(--pri),var(--pri2));
  display:grid;place-items:center;color:#04180d;
  font-family:'JetBrains Mono',monospace;font-weight:700;font-size:24px;
  box-shadow:0 14px 44px rgba(0,255,157,.42)}

.hero{padding:72px 0 32px;text-align:center}
.hero .badge{display:inline-flex;align-items:center;gap:8px;padding:6px 15px;
  border-radius:100px;background:rgba(0,255,157,.08);
  border:1px solid rgba(0,255,157,.22);color:var(--pri2);font-size:11px;
  font-weight:500;margin-bottom:24px;font-family:'JetBrains Mono',monospace;
  letter-spacing:.5px;text-transform:uppercase}
.hero h1{font-size:clamp(34px,5.2vw,60px);line-height:1.05;
  letter-spacing:-2px;margin-bottom:18px}
.hero .grad{background:linear-gradient(135deg,var(--pri) 0%,var(--pri2) 50%,var(--pri3) 100%);
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.hero p{max-width:640px;margin:0 auto 34px;color:var(--mut);
  font-size:16px;line-height:1.65}

.feature{padding:24px;border-radius:16px;
  background:linear-gradient(180deg,var(--surf),rgba(10,26,18,0.5));
  border:1px solid var(--brd);transition:all .25s}
.feature:hover{border-color:var(--brd2);transform:translateY(-2px)}
.feature .ico{width:44px;height:44px;border-radius:12px;display:grid;
  place-items:center;background:rgba(0,255,157,.09);
  border:1px solid rgba(0,255,157,.22);font-size:20px;margin-bottom:14px}
.feature h3{font-size:15px;margin-bottom:6px}
.feature p{font-size:13px;color:var(--mut);line-height:1.6}

.stat{padding:18px 22px;border-radius:14px;background:var(--surf);
  border:1px solid var(--brd);position:relative;overflow:hidden}
.stat::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;
  background:linear-gradient(180deg,var(--pri),var(--pri2))}
.stat .lbl{font-size:11px;color:var(--mut);text-transform:uppercase;
  letter-spacing:.8px;font-weight:500}
.stat .val{font-size:28px;font-weight:700;margin-top:6px;letter-spacing:-1px}

.crumb{font-family:'JetBrains Mono',monospace;font-size:13px;padding:11px 15px;
  background:var(--surf2);border-radius:11px;margin-bottom:13px;
  word-break:break-all;border:1px solid var(--brd)}
.crumb a{color:var(--pri2)}
.crumb .sep{color:var(--mut2);margin:0 6px}

.drop{border:2px dashed rgba(0,255,157,.22);border-radius:14px;padding:20px;
  text-align:center;transition:all .2s;background:rgba(0,255,157,.02)}
.drop:hover{border-color:var(--pri);background:rgba(0,255,157,.06)}

.kv{display:grid;grid-template-columns:150px 1fr;gap:10px 18px;font-size:13px}
.kv .k{color:var(--mut);font-size:12px}
.kv .v{font-family:'JetBrains Mono',monospace;color:var(--pri3);word-break:break-all}

.acts{display:flex;gap:8px;flex-wrap:wrap}
.tabs{display:flex;gap:4px;padding:5px;background:var(--surf2);border-radius:12px;
  border:1px solid var(--brd);margin-bottom:20px;flex-wrap:wrap}
.tabs button{padding:9px 18px;border-radius:9px;border:none;background:none;
  color:var(--mut);font-family:inherit;font-size:13px;font-weight:500;
  cursor:pointer;transition:all .15s}
.tabs button:hover{color:var(--txt)}
.tabs button.active{background:linear-gradient(135deg,var(--pri),var(--pri2));
  color:#04180d;font-weight:600;box-shadow:0 4px 14px rgba(0,255,157,.28)}
.tab-panel{display:none}
.tab-panel.active{display:block}

.dot-live{display:inline-block;width:8px;height:8px;border-radius:50%;
  background:var(--ok);box-shadow:0 0 0 0 rgba(0,255,157,.6);
  animation:pulse 2s infinite}
@keyframes pulse{
  0%{box-shadow:0 0 0 0 rgba(0,255,157,.6)}
  70%{box-shadow:0 0 0 8px rgba(0,255,157,0)}
  100%{box-shadow:0 0 0 0 rgba(0,255,157,0)}
}

@media(max-width:760px){
  .container{padding:0 14px}
  .topbar-in{padding:11px 14px}
  .nav a{padding:6px 10px;font-size:12px}
  .hero{padding:42px 0 18px}
  .hero h1{letter-spacing:-1.2px}
  .kv{grid-template-columns:1fr;gap:4px}
  .kv .k{margin-top:8px}
  .tabs button{padding:8px 13px;font-size:12px}
}
"""

BASE = """<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} · MODx Nebula</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>◈</text></svg>">
<style>""" + CSS + """</style></head><body>
<div class="topbar"><div class="topbar-in">
  <a href="/" class="brand">
    <div class="mark">◈</div>
    <span class="name">MODx / Nebula</span>
  </a>
  <div class="nav">
    {% if logged %}
      <a href="{{ url_for('dashboard') }}" {% if active=='dash' %}class="active"{% endif %}>Dashboard</a>
      <a href="{{ url_for('activity_page') }}" {% if active=='act' %}class="active"{% endif %}>Activity</a>
      <a href="{{ url_for('docs_page') }}" {% if active=='docs' %}class="active"{% endif %}>Docs</a>
      <a href="{{ url_for('new_inst') }}" class="cta">＋ New App</a>
      <a href="{{ url_for('logout') }}">Sign Out</a>
    {% else %}
      <a href="{{ url_for('docs_page') }}">Docs</a>
      <a href="{{ url_for('login') }}" class="cta">Sign In</a>
    {% endif %}
  </div>
</div></div>
<div class="container" style="padding-top:28px;padding-bottom:80px">
{% with msgs = get_flashed_messages(with_categories=true) %}
  {% for cat, m in msgs %}
    <div class="flash flash-{{ 'ok' if cat=='ok' else 'err' }}">
      <span>{{ '✓' if cat=='ok' else '⚠' }}</span><span>{{ m }}</span>
    </div>
  {% endfor %}
{% endwith %}
{{ body|safe }}
</div>
<script>
function switchTab(name){
  document.querySelectorAll('.tabs button').forEach(function(b){
    b.classList.toggle('active', b.dataset.tab === name);
  });
  document.querySelectorAll('.tab-panel').forEach(function(p){
    p.classList.toggle('active', p.dataset.tab === name);
  });
  try{ localStorage.setItem('modx-tab', name); }catch(e){}
}
function copyCmd(txt, el){
  navigator.clipboard.writeText(txt).then(function(){
    if(el){ var o = el.textContent; el.textContent = '✓ Copied';
      setTimeout(function(){ el.textContent = o; }, 1100); }
  });
}
window.addEventListener('DOMContentLoaded', function(){
  var saved = null;
  try{ saved = localStorage.getItem('modx-tab'); }catch(e){}
  if(saved){
    var btn = document.querySelector('.tabs button[data-tab="'+saved+'"]');
    if(btn) switchTab(saved);
  }
});
</script>
</body></html>"""

def render(title, body, active=""):
    return render_template_string(BASE, title=title, body=body,
                                  logged=logged_in(), active=active)

# ─────────────────────────────── Public routes ────────────────────────
@app.route("/")
def index():
    if logged_in(): return redirect(url_for("dashboard"))
    body = """
<div class="hero">
  <div class="badge">◈ 24×7 watchdog · auto-build · zip deploy</div>
  <h1>Ship anything.<br><span class="grad">Stay online. Always.</span></h1>
  <p>One panel to host Python apps, Node services, and static sites.
     Upload a zip, we auto-detect the runtime and commands, install dependencies,
     and keep your app running around the clock.</p>
  <div class="acts" style="justify-content:center">
    <a href="/login" class="btn btn-p" style="padding:11px 22px">Open Panel →</a>
    <a href="/docs" class="btn btn-g" style="padding:11px 22px">Read Docs</a>
  </div>
</div>

<div class="grid" style="margin-top:64px">
  <div class="feature"><div class="ico">◈</div><h3>Auto-detect Runtime</h3>
    <p>Drop a project — we scan for <code>package.json</code>,
       <code>requirements.txt</code>, <code>main.py</code>, <code>index.js</code>,
       <code>manage.py</code> and pick the right commands.</p></div>
  <div class="feature"><div class="ico">⚙</div><h3>Custom Start &amp; Build</h3>
    <p>Override anything. Set your own <b>start command</b> and <b>build command</b>
       per instance. They support shell syntax and <code>$PORT</code>.</p></div>
  <div class="feature"><div class="ico">📦</div><h3>Zip · Tar · Bulk Upload</h3>
    <p>Upload <code>.zip</code>, <code>.tar.gz</code>, or many files at once.
       Auto-extract with zip-slip protection and optional root-folder stripping.</p></div>
  <div class="feature"><div class="ico">🛡</div><h3>Always-On Watchdog</h3>
    <p>Process died? We restart it in under 6 seconds. Panel restarted?
       Everything boots back automatically — 24×7.</p></div>
  <div class="feature"><div class="ico">📜</div><h3>Streaming Console</h3>
    <p>Live stdout/stderr in the browser. Dependency installs and build steps
       stream into the same console, so you always see what's happening.</p></div>
  <div class="feature"><div class="ico">🗂</div><h3>Full File Manager</h3>
    <p>Browse, edit, create folders, delete recursively — from the browser.
       Sub-folder uploads, nested paths, everything supported.</p></div>
</div>
"""
    return render("Home", body)

@app.route("/login", methods=["GET","POST"])
def login():
    if logged_in(): return redirect(url_for("dashboard"))
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == AUTH_USER and p == AUTH_PASS:
            session["logged_in"] = True
            flash("Welcome back, " + AUTH_USER, "ok")
            return redirect(url_for("dashboard"))
        flash("Invalid credentials", "err")
    body = """
<div class="center" style="min-height:auto;padding-top:52px">
<div class="auth-card">
  <div class="auth-mark">◈</div>
  <h2 style="text-align:center;margin-bottom:6px">Welcome back</h2>
  <p class="mut" style="text-align:center;margin-bottom:28px;font-size:13px">
    Sign in to the MODx Nebula panel</p>
  <form method="post">
    <div class="field"><label>Username</label>
      <input name="username" required autofocus autocomplete="username"></div>
    <div class="field"><label>Password</label>
      <input name="password" type="password" required autocomplete="current-password"></div>
    <button class="btn btn-p" style="width:100%;justify-content:center;padding:12px">Sign In</button>
  </form>
  <p class="mut2" style="text-align:center;margin-top:20px;font-family:'JetBrains Mono',monospace">
    single-user · 24×7 · always-on
  </p>
</div>
</div>
"""
    return render("Login", body)

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("index"))

# ─────────────────────────────── Dashboard ────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    d = db()
    rows = d.execute("SELECT * FROM instances ORDER BY id DESC").fetchall()
    d.close()

    total = len(rows)
    running = sum(1 for r in rows
                  if is_running(r["id"]) or (r["type"]=="static" and r["status"]=="running"))

    cards = ""
    for r in rows:
        is_live = is_running(r["id"]) or (r["type"] == "static" and r["status"] == "running")
        tag = ('<span class="tag tag-ok"><span class="dot"></span>Live</span>'
               if is_live else
               '<span class="tag tag-off"><span class="dot"></span>Idle</span>')
        open_btn = ('<a href="/app/%d/" target="_blank" class="btn btn-g btn-s">Open ↗</a>'
                    % r["id"]) if is_live else ''
        cmd = (r["start_cmd"] or "").strip()
        cmd_html = ('<div class="cmd-chip" title="%s">$ %s</div>' % (esc(cmd), esc(cmd))
                    if cmd else
                    '<div class="cmd-chip" style="color:var(--mut)">auto-detect</div>')
        cards += (
            '<div class="card">'
            '<div style="display:flex;justify-content:space-between;align-items:flex-start;'
            'margin-bottom:12px;gap:10px">'
            '<div style="min-width:0">'
            '<h3 style="margin-bottom:4px">' + esc(r["name"]) + '</h3>'
            '<p class="mut2 mono">' + esc(r["type"]) + ' · :' + str(r["port"]) + '</p>'
            '</div>' + tag + '</div>'
            '<div style="margin-bottom:14px">' + cmd_html + '</div>'
            '<div class="acts">'
            '<a href="/dashboard/i/' + str(r["id"]) + '" class="btn btn-p btn-s">Manage</a>'
            + open_btn +
            '</div></div>'
        )
    if not cards:
        cards = ('<div class="card" style="grid-column:1/-1;text-align:center;padding:56px">'
                 '<div style="font-size:36px;margin-bottom:12px">◈</div>'
                 '<h3 style="margin-bottom:6px">No apps yet</h3>'
                 '<p class="mut" style="margin-bottom:20px">Create your first instance to get started.</p>'
                 '<a href="/dashboard/new" class="btn btn-p">+ New Instance</a>'
                 '</div>')

    body = (
        '<div style="display:flex;justify-content:space-between;align-items:center;'
        'margin-bottom:24px;flex-wrap:wrap;gap:14px">'
        '<div><h1 style="margin-bottom:4px">Dashboard</h1>'
        '<p class="mut">Signed in as <b>' + AUTH_USER + '</b> · watchdog active</p></div>'
        '<a href="/dashboard/new" class="btn btn-p">＋ New Instance</a></div>'

        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));'
        'gap:14px;margin-bottom:26px">'
        '<div class="stat"><div class="lbl">Total Instances</div><div class="val">'
        + str(total) + '</div></div>'
        '<div class="stat"><div class="lbl">Live Now</div>'
        '<div class="val" style="color:var(--ok)">' + str(running) + '</div></div>'
        '<div class="stat"><div class="lbl">Stopped</div>'
        '<div class="val" style="color:var(--mut)">' + str(total - running) + '</div></div>'
        '<div class="stat"><div class="lbl">Watchdog</div>'
        '<div class="val" style="font-size:15px;padding-top:8px">'
        '<span class="dot-live"></span> every 6 s</div></div>'
        '</div>'

        '<div class="grid">' + cards + '</div>'
    )
    return render("Dashboard", body, active="dash")

# ─────────────────────────────── New instance ─────────────────────────
@app.route("/dashboard/new", methods=["GET","POST"])
@login_required
def new_inst():
    if request.method == "POST":
        name = re.sub(r'[^a-zA-Z0-9_\- ]', '', request.form.get("name","")).strip()[:40]
        typ = request.form.get("type", "static")
        if not name:
            flash("Please enter a name", "err"); return redirect(url_for("new_inst"))
        if typ not in ("python", "node", "static"):
            typ = "static"
        try:
            base_slug = re.sub(r'[^a-zA-Z0-9_-]+', '-', name).strip('-').lower()[:36] or "app"
            slug = base_slug; n = 1
            d = db()
            while d.execute("SELECT 1 FROM instances WHERE slug=?", (slug,)).fetchone():
                n += 1; slug = base_slug + "-" + str(n)
            dir_name = slug + "_" + secrets.token_hex(3)
            idir = os.path.join(INST_DIR, dir_name)
            os.makedirs(idir, exist_ok=True)

            # Scaffold starter files
            if typ == "python":
                with open(os.path.join(idir, "main.py"), "w") as f: f.write(PY_MAIN)
                with open(os.path.join(idir, "requirements.txt"), "w") as f: f.write(PY_REQ)
            elif typ == "node":
                with open(os.path.join(idir, "index.js"), "w") as f: f.write(NODE_MAIN)
                with open(os.path.join(idir, "package.json"), "w") as f: f.write(NODE_PKG)
            elif typ == "static":
                with open(os.path.join(idir, "index.html"), "w") as f: f.write(STATIC_HTML)

            start_cmd, build_cmd = detect_commands(idir, typ)
            port = alloc_port()
            cur = d.execute(
                "INSERT INTO instances(name,slug,type,work_dir,start_cmd,build_cmd,"
                "env,port,dir,status,autostart,auto_build,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, slug, typ, "", start_cmd, build_cmd, "",
                 port, dir_name, "stopped", 1, 1, now()))
            d.commit(); iid = cur.lastrowid; d.close()
            log_activity(iid, "create", "Instance '%s' created (%s)" % (name, typ))
            flash("Instance '" + name + "' created", "ok")
            return redirect(url_for("inst_detail", iid=iid))
        except Exception as e:
            print("[new_inst]", e, "\n", traceback.format_exc(), flush=True)
            flash("Error: " + str(e), "err")
    body = """
<h1>New Instance</h1>
<p class="mut" style="margin-bottom:22px">Pick a runtime — we'll scaffold a starter
   project and auto-detect the start &amp; build commands.</p>
<div style="max-width:600px"><div class="card">
<form method="post">
  <div class="field">
    <label>Instance Name</label>
    <input name="name" required pattern="[a-zA-Z0-9_\\- ]+" placeholder="my-app" autofocus>
    <div class="hint">Letters, numbers, dash, underscore and spaces only</div>
  </div>
  <div class="field">
    <label>Runtime</label>
    <select name="type">
      <option value="static">🌐 Static Website (HTML / CSS / JS)</option>
      <option value="python">🐍 Python App (Flask · FastAPI · Django · Bot)</option>
      <option value="node">⬢ Node.js App (Express · Next · Custom)</option>
    </select>
  </div>
  <button class="btn btn-p" style="width:100%;justify-content:center;padding:12px">
    Create Instance →
  </button>
</form>
</div></div>
"""
    return render("New Instance", body, active="dash")

# ─────────────────────────── Instance detail ──────────────────────────
@app.route("/dashboard/i/<int:iid>")
@login_required
def inst_detail(iid):
    inst = get_inst(iid)
    if not inst: abort(404)

    path = norm_rel(request.args.get("p", ""))
    is_live = is_running(iid) or (inst["type"] == "static" and inst["status"] == "running")

    # ── File listing
    base = inst_dir(inst)
    target = safe_join(base, path) if path else base
    rows_html = ""
    if target and os.path.isdir(target):
        items = []
        try:
            for n in os.listdir(target):
                fp = os.path.join(target, n)
                is_dir = os.path.isdir(fp)
                try: size = 0 if is_dir else os.path.getsize(fp)
                except Exception: size = 0
                items.append((is_dir, n, size))
        except Exception:
            pass
        items.sort(key=lambda x: (not x[0], x[1].lower()))
        for is_dir, n, size in items:
            rel = (path + "/" + n) if path else n
            q = urllib.parse.quote(rel)
            if is_dir:
                rows_html += (
                    '<tr>'
                    '<td>📁 <a href="/dashboard/i/' + str(iid) + '?p=' + q + '"><b>'
                    + esc(n) + '</b>/</a></td>'
                    '<td class="mut mono" style="font-size:11px">folder</td>'
                    '<td style="text-align:right">'
                    '<form method="post" action="/dashboard/i/' + str(iid) +
                    '/delete-file" style="display:inline" '
                    'onsubmit="return confirm(\'Delete folder &amp; contents?\')">'
                    '<input type="hidden" name="f" value="' + esc(rel) + '">'
                    '<input type="hidden" name="p" value="' + esc(path) + '">'
                    '<button class="btn btn-d btn-xs">Del</button></form></td></tr>')
            else:
                rows_html += (
                    '<tr>'
                    '<td>📄 <a href="/dashboard/i/' + str(iid) + '/edit?f=' + q + '">'
                    + esc(n) + '</a></td>'
                    '<td class="mut mono" style="font-size:11px">' + human_size(size) + '</td>'
                    '<td style="text-align:right">'
                    '<a href="/dashboard/i/' + str(iid) + '/download?f=' + q +
                    '" class="btn btn-g btn-xs">Get</a> '
                    '<form method="post" action="/dashboard/i/' + str(iid) +
                    '/delete-file" style="display:inline" onsubmit="return confirm(\'Delete?\')">'
                    '<input type="hidden" name="f" value="' + esc(rel) + '">'
                    '<input type="hidden" name="p" value="' + esc(path) + '">'
                    '<button class="btn btn-d btn-xs">Del</button></form></td></tr>')
        if not rows_html:
            rows_html = ('<tr><td colspan="3" class="mut" style="padding:26px;text-align:center">'
                         '📭 Empty folder — upload files, a zip, or a tar.gz</td></tr>')
    else:
        rows_html = ('<tr><td colspan="3" class="mut" style="padding:20px;text-align:center">'
                     'Invalid path</td></tr>')

    # ── Breadcrumb
    crumb = '<a href="/dashboard/i/' + str(iid) + '">🏠 root</a>'
    if path:
        acc = ""
        for part in path.split("/"):
            acc = (acc + "/" + part) if acc else part
            crumb += (' <span class="sep">/</span> '
                      '<a href="/dashboard/i/' + str(iid) + '?p=' +
                      urllib.parse.quote(acc) + '">' + esc(part) + '</a>')

    up_btn = ""
    if path:
        parent = "/".join(path.split("/")[:-1])
        href = "/dashboard/i/" + str(iid) + ("?p=" + urllib.parse.quote(parent) if parent else "")
        up_btn = '<a href="' + href + '" class="btn btn-g btn-s">↑ Up</a>'

    safe_path_attr = esc(path)

    # ── Tabs
    tabs = (
        '<div class="tabs">'
        '<button data-tab="files" class="active" onclick="switchTab(\'files\')">📁 Files</button>'
        '<button data-tab="console" onclick="switchTab(\'console\')">📜 Console</button>'
        '<button data-tab="settings" onclick="switchTab(\'settings\')">⚙ Settings</button>'
        '<button data-tab="activity" onclick="switchTab(\'activity\')">⏱ Activity</button>'
        '</div>'
    )

    # ── Files tab
    files_tab = (
        '<div class="tab-panel active" data-tab="files"><div class="card">'
        '<div class="crumb">' + crumb + '</div>'
        '<div class="acts" style="margin-bottom:14px">' + up_btn +
        '<button class="btn btn-g btn-s" onclick="var e=document.getElementById(\'mkfolder\');'
        'e.style.display=e.style.display===\'block\'?\'none\':\'block\'">＋ Folder</button>'
        '<button class="btn btn-g btn-s" onclick="var e=document.getElementById(\'mkfile\');'
        'e.style.display=e.style.display===\'block\'?\'none\':\'block\'">＋ File</button>'
        '</div>'
        '<div id="mkfolder" style="display:none;margin-bottom:12px">'
        '<form method="post" action="/dashboard/i/' + str(iid) +
        '/new-folder" style="display:flex;gap:8px">'
        '<input type="hidden" name="p" value="' + safe_path_attr + '">'
        '<input name="fname" placeholder="folder-name" required>'
        '<button class="btn btn-p btn-s">Create Folder</button></form></div>'
        '<div id="mkfile" style="display:none;margin-bottom:12px">'
        '<form method="post" action="/dashboard/i/' + str(iid) +
        '/new-file" style="display:flex;gap:8px">'
        '<input type="hidden" name="p" value="' + safe_path_attr + '">'
        '<input name="fname" placeholder="filename.txt" required>'
        '<button class="btn btn-p btn-s">Create File</button></form></div>'
        '<table><thead><tr><th>Name</th><th>Size</th><th></th></tr></thead>'
        '<tbody>' + rows_html + '</tbody></table>'

        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:20px" id="ups">'
        '<div class="drop">'
        '<form method="post" action="/dashboard/i/' + str(iid) +
        '/upload" enctype="multipart/form-data">'
        '<input type="hidden" name="p" value="' + safe_path_attr + '">'
        '<p class="mut" style="margin-bottom:10px;font-size:13px">⬆ Upload file(s)</p>'
        '<input type="file" name="files" multiple required style="padding:8px;margin-bottom:10px">'
        '<button class="btn btn-p btn-s">Upload</button></form></div>'
        '<div class="drop">'
        '<form method="post" action="/dashboard/i/' + str(iid) +
        '/upload-archive" enctype="multipart/form-data">'
        '<input type="hidden" name="p" value="' + safe_path_attr + '">'
        '<p class="mut" style="margin-bottom:10px;font-size:13px">📦 Upload ZIP / TAR → auto-extract</p>'
        '<input type="file" name="archive" '
        'accept=".zip,.tar,.gz,.tgz,.tar.gz,.tar.bz2,.tar.xz" required '
        'style="padding:8px;margin-bottom:10px">'
        '<label style="display:flex;align-items:center;gap:7px;font-size:12px;'
        'color:var(--mut);margin-bottom:10px;justify-content:center">'
        '<input type="checkbox" name="strip_root" value="1"> '
        'Strip single top-level folder</label>'
        '<button class="btn btn-p btn-s">Extract</button></form></div>'
        '</div>'
        '</div>'
        '<style>@media(max-width:800px){#ups{grid-template-columns:1fr!important}}</style>'
        '</div>'
    )

    # ── Console tab
    log_content = read_log(iid, 20000)
    console_tab = (
        '<div class="tab-panel" data-tab="console"><div class="card">'
        '<div style="display:flex;justify-content:space-between;align-items:center;'
        'margin-bottom:12px;flex-wrap:wrap;gap:8px">'
        '<h3>Live Console</h3>'
        '<div class="acts">'
        '<span class="mut2 mono">auto-refresh 3 s</span>'
        '<form method="post" action="/dashboard/i/' + str(iid) +
        '/clear-log" style="display:inline">'
        '<button class="btn btn-g btn-s">Clear log</button></form>'
        '</div></div>'
        '<pre id="logbox">' + (esc(log_content) if log_content else
            '<span style="color:#527a63">— no output yet — start the app to see logs —</span>')
        + '</pre>'
        '</div></div>'
    )

    # ── Settings tab
    auto_build_checked = 'checked' if inst["auto_build"] else ''
    autostart_checked  = 'checked' if inst["autostart"] else ''
    settings_tab = (
        '<div class="tab-panel" data-tab="settings"><div class="card">'
        '<form method="post" action="/dashboard/i/' + str(iid) + '/save-settings">'
        '<h3 style="margin-bottom:14px">Runtime &amp; Commands</h3>'

        '<div class="field"><label>Work Directory (relative)</label>'
        '<input name="work_dir" value="' + esc(inst["work_dir"] or "") + '" '
        'placeholder="e.g. myapp-main (or blank for root)">'
        '<div class="hint">If your zip extracted into a single sub-folder, set this to '
        'that folder name.</div></div>'

        '<div class="field"><label>Start Command</label>'
        '<input name="start_cmd" value="' + esc(inst["start_cmd"] or "") + '" '
        'placeholder="e.g. python3 main.py  ·  npm start  ·  uvicorn main:app --port $PORT">'
        '<div class="hint">Shell command. <code>$PORT</code> and <code>$HOST</code> '
        'are set automatically. Leave blank for static sites.</div></div>'

        '<div class="field"><label>Build Command</label>'
        '<input name="build_cmd" value="' + esc(inst["build_cmd"] or "") + '" '
        'placeholder="e.g. pip install -r requirements.txt  ·  '
        'npm install &amp;&amp; npm run build">'
        '<div class="hint">Runs before start when Auto-build is on. Optional.</div></div>'

        '<div class="field"><label>Environment Variables</label>'
        '<textarea name="env" rows="5" '
        'placeholder="KEY=value&#10;ANOTHER_KEY=another">'
        + esc(inst["env"] or "") + '</textarea>'
        '<div class="hint">One <code>KEY=VALUE</code> per line. Lines starting with '
        '<code>#</code> are ignored.</div></div>'

        '<div style="display:flex;gap:22px;margin-bottom:18px;flex-wrap:wrap">'
        '<label style="display:flex;align-items:center;gap:8px;font-size:13px;'
        'color:var(--txt);margin:0">'
        '<input type="checkbox" name="auto_build" value="1" ' + auto_build_checked +
        '> Auto-build on start</label>'
        '<label style="display:flex;align-items:center;gap:8px;font-size:13px;'
        'color:var(--txt);margin:0">'
        '<input type="checkbox" name="autostart" value="1" ' + autostart_checked +
        '> Auto-restart (24×7)</label>'
        '</div>'

        '<div class="acts">'
        '<button class="btn btn-p">💾 Save Settings</button>'
        '</form>'
        '<form method="post" action="/dashboard/i/' + str(iid) +
        '/auto-detect" style="display:inline">'
        '<button class="btn btn-w">🪄 Auto-detect Commands</button></form>'
        '<form method="post" action="/dashboard/i/' + str(iid) +
        '/build" style="display:inline">'
        '<button class="btn btn-g">⚙ Run Build Now</button></form>'
        '</div>'
        '</div></div>'
    )

    # ── Activity tab
    d = db()
    acts = d.execute("SELECT * FROM activity WHERE iid=? ORDER BY id DESC LIMIT 60",
                     (iid,)).fetchall()
    d.close()
    acts_html = ""
    for a in acts:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a["ts"]))
        acts_html += ('<tr><td class="mut mono" style="font-size:11px;white-space:nowrap">'
                      + ts + '</td>'
                      '<td><span class="tag tag-info">' + esc(a["kind"]) + '</span></td>'
                      '<td class="mono" style="font-size:12px">' + esc(a["message"]) + '</td></tr>')
    if not acts_html:
        acts_html = ('<tr><td colspan="3" class="mut" style="padding:20px;text-align:center">'
                     'No activity yet</td></tr>')
    activity_tab = (
        '<div class="tab-panel" data-tab="activity"><div class="card">'
        '<h3 style="margin-bottom:14px">Recent Activity</h3>'
        '<table><thead><tr><th>Time</th><th>Type</th><th>Message</th></tr></thead>'
        '<tbody>' + acts_html + '</tbody></table>'
        '</div></div>'
    )

    # ── Header actions
    if is_live:
        start_btn = ''
        stop_btn = ('<form method="post" action="/dashboard/i/' + str(iid) +
                    '/stop" style="display:inline">'
                    '<button class="btn btn-g btn-s">⏹ Stop</button></form>')
        restart_btn = ('<form method="post" action="/dashboard/i/' + str(iid) +
                       '/restart" style="display:inline">'
                       '<button class="btn btn-g btn-s">↻ Restart</button></form>')
        open_btn = ('<a href="/app/' + str(iid) +
                    '/" target="_blank" class="btn btn-p btn-s">↗ Open</a>')
    else:
        start_btn = ('<form method="post" action="/dashboard/i/' + str(iid) +
                     '/start" style="display:inline">'
                     '<button class="btn btn-p btn-s">▶ Start</button></form>')
        stop_btn = ''
        restart_btn = ''
        open_btn = ''

    live_tag = ('<span class="tag tag-ok"><span class="dot"></span>Live</span>'
                if is_live else
                '<span class="tag tag-off"><span class="dot"></span>Idle</span>')

    body = (
        '<a href="/dashboard" class="mut" style="font-size:13px">← Dashboard</a>'
        '<div style="display:flex;justify-content:space-between;align-items:flex-start;'
        'margin-top:6px;margin-bottom:20px;flex-wrap:wrap;gap:12px">'
        '<div style="min-width:0">'
        '<h1 style="margin-bottom:6px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">'
        + esc(inst["name"]) + ' ' + live_tag + '</h1>'
        '<p class="mut2 mono">' + esc(inst["type"]) + ' · port ' + str(inst["port"]) +
        ' · slug ' + esc(inst["slug"]) + '</p>'
        '</div>'
        '<div class="acts">' + start_btn + stop_btn + restart_btn + open_btn +
        '<form method="post" action="/dashboard/i/' + str(iid) +
        '/delete" style="display:inline" '
        'onsubmit="return confirm(\'Delete this instance and all its files?\')">'
        '<button class="btn btn-d btn-s">🗑 Delete</button></form></div></div>'

        + tabs + files_tab + console_tab + settings_tab + activity_tab +

        '<script>'
        'setInterval(function(){'
        'fetch("/dashboard/i/' + str(iid) + '/log")'
        '.then(function(r){return r.text()})'
        '.then(function(t){'
        'var e=document.getElementById("logbox");'
        'if(e){var atBottom=(e.scrollHeight-e.scrollTop-e.clientHeight)<40;'
        'e.textContent=t||"— no output yet —";'
        'if(atBottom)e.scrollTop=e.scrollHeight;}'
        '}).catch(function(){});'
        '},3000);'
        '</script>'
    )
    return render(inst["name"], body, active="dash")

# ─────────────────────────────── File routes ──────────────────────────
@app.route("/dashboard/i/<int:iid>/log")
@login_required
def inst_log(iid):
    if not get_inst(iid): abort(404)
    return read_log(iid, 20000)

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
    except Exception:
        content = ""
    if len(content) > 2_000_000:
        flash("File too large to edit (>2 MB)", "err")
        return redirect(url_for("inst_detail", iid=iid))
    parent = "/".join(fname.split("/")[:-1])
    back = "/dashboard/i/" + str(iid) + ("?p=" + urllib.parse.quote(parent) if parent else "")
    body = (
        '<a href="' + back + '" class="mut" style="font-size:13px">← Back</a>'
        '<h1 style="margin-top:6px">Edit: <span class="mono" '
        'style="font-size:.7em;color:var(--pri2)">' + esc(fname) + '</span></h1>'
        '<form method="post" action="/dashboard/i/' + str(iid) + '/save">'
        '<input type="hidden" name="f" value="' + esc(fname) + '">'
        '<textarea name="content" style="min-height:520px;margin-top:14px" '
        'spellcheck="false">' + esc(content) + '</textarea>'
        '<div class="acts" style="margin-top:14px">'
        '<button class="btn btn-p">💾 Save</button>'
        '<a href="' + back + '" class="btn btn-g">Cancel</a>'
        '</div></form>'
    )
    return render("Edit " + fname, body, active="dash")

@app.route("/dashboard/i/<int:iid>/save", methods=["POST"])
@login_required
def inst_save(iid):
    inst = get_inst(iid)
    if not inst: abort(404)
    fname = norm_rel(request.form.get("f", ""))
    fp = safe_join(inst_dir(inst), fname)
    if not fp: abort(400)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w") as f: f.write(request.form.get("content", ""))
    flash("Saved " + fname, "ok")
    return redirect(url_for("inst_edit", iid=iid, f=fname))

@app.route("/dashboard/i/<int:iid>/download")
@login_required
def inst_download(iid):
    inst = get_inst(iid)
    if not inst: abort(404)
    fname = norm_rel(request.args.get("f", ""))
    fp = safe_join(inst_dir(inst), fname)
    if not fp or not os.path.isfile(fp): abort(404)
    return send_from_directory(os.path.dirname(fp), os.path.basename(fp),
                               as_attachment=True)

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

@app.route("/dashboard/i/<int:iid>/upload-archive", methods=["POST"])
@login_required
def inst_upload_archive(iid):
    inst = get_inst(iid)
    if not inst: abort(404)
    p = norm_rel(request.form.get("p", ""))
    target = safe_join(inst_dir(inst), p) if p else inst_dir(inst)
    if not target: abort(400)
    os.makedirs(target, exist_ok=True)
    f = request.files.get("archive")
    if not f or not f.filename:
        flash("No archive selected", "err")
        return redirect(url_for("inst_detail", iid=iid, p=p))
    strip = request.form.get("strip_root") == "1"
    try:
        data = f.read()
        count, kind = extract_archive(data, f.filename, target, strip_root=strip)
        flash("Extracted " + str(count) + " file(s) from " + kind, "ok")
        if not (inst["start_cmd"] or "").strip() and not (inst["build_cmd"] or "").strip():
            s, b = detect_commands(inst_work_dir(inst), inst["type"])
            if s or b:
                d = db()
                d.execute("UPDATE instances SET start_cmd=?, build_cmd=? WHERE id=?",
                          (s, b, iid))
                d.commit(); d.close()
                flash("Auto-detected commands", "ok")
    except Exception as e:
        print("[extract]", e, "\n", traceback.format_exc(), flush=True)
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

# ─────────────────────────── Instance control ─────────────────────────
@app.route("/dashboard/i/<int:iid>/start", methods=["POST"])
@login_required
def inst_start(iid):
    inst = get_inst(iid)
    if inst:
        if inst["auto_build"] and not inst["deps_ready"] and inst["build_cmd"]:
            build_inst(iid, async_=False)
        start_inst(inst)
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
        stop_inst(iid, silent=True)
        time.sleep(0.5)
        start_inst(inst)
    return redirect(url_for("inst_detail", iid=iid))

@app.route("/dashboard/i/<int:iid>/build", methods=["POST"])
@login_required
def inst_build(iid):
    if get_inst(iid):
        build_inst(iid, async_=True)
        flash("Build started — watch the console", "ok")
    return redirect(url_for("inst_detail", iid=iid))

@app.route("/dashboard/i/<int:iid>/delete", methods=["POST"])
@login_required
def inst_delete(iid):
    inst = get_inst(iid)
    if inst:
        stop_inst(iid, silent=True)
        try: shutil.rmtree(inst_dir(inst))
        except Exception: pass
        d = db()
        d.execute("DELETE FROM instances WHERE id=?", (iid,))
        d.execute("DELETE FROM activity WHERE iid=?", (iid,))
        d.commit(); d.close()
        flash("Instance deleted", "ok")
    return redirect(url_for("dashboard"))

@app.route("/dashboard/i/<int:iid>/clear-log", methods=["POST"])
@login_required
def inst_clear_log(iid):
    if get_inst(iid): clear_log(iid)
    return redirect(url_for("inst_detail", iid=iid))

@app.route("/dashboard/i/<int:iid>/auto-detect", methods=["POST"])
@login_required
def inst_auto_detect(iid):
    inst = get_inst(iid)
    if not inst: abort(404)
    s, b = detect_commands(inst_work_dir(inst), inst["type"])
    d = db()
    d.execute("UPDATE instances SET start_cmd=?, build_cmd=? WHERE id=?", (s, b, iid))
    d.commit(); d.close()
    if s or b:
        flash("Auto-detected → start: %s  ·  build: %s" % (s or "—", b or "—"), "ok")
    else:
        flash("Could not detect — set commands manually", "err")
    return redirect(url_for("inst_detail", iid=iid))

@app.route("/dashboard/i/<int:iid>/save-settings", methods=["POST"])
@login_required
def inst_save_settings(iid):
    inst = get_inst(iid)
    if not inst: abort(404)
    work_dir = norm_rel(request.form.get("work_dir", ""))
    start_cmd = request.form.get("start_cmd", "").strip()
    build_cmd = request.form.get("build_cmd", "").strip()
    env = request.form.get("env", "")
    auto_build = 1 if request.form.get("auto_build") else 0
    autostart = 1 if request.form.get("autostart") else 0
    d = db()
    d.execute("UPDATE instances SET work_dir=?, start_cmd=?, build_cmd=?, env=?, "
              "auto_build=?, autostart=?, deps_ready=0 WHERE id=?",
              (work_dir, start_cmd, build_cmd, env, auto_build, autostart, iid))
    d.commit(); d.close()
    flash("Settings saved", "ok")
    return redirect(url_for("inst_detail", iid=iid))

# ─────────────────────────── Activity & docs ──────────────────────────
@app.route("/activity")
@login_required
def activity_page():
    d = db()
    rows = d.execute(
        "SELECT a.*, i.name AS iname FROM activity a "
        "LEFT JOIN instances i ON i.id=a.iid ORDER BY a.id DESC LIMIT 200"
    ).fetchall()
    d.close()
    html = ""
    for a in rows:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a["ts"]))
        html += ('<tr><td class="mut mono" style="font-size:11px;white-space:nowrap">'
                 + ts + '</td>'
                 '<td>' + esc(a["iname"] or "—") + '</td>'
                 '<td><span class="tag tag-info">' + esc(a["kind"]) + '</span></td>'
                 '<td class="mono" style="font-size:12px">' + esc(a["message"]) + '</td></tr>')
    if not html:
        html = ('<tr><td colspan="4" class="mut" style="padding:26px;text-align:center">'
                'No activity yet</td></tr>')
    body = (
        '<h1 style="margin-bottom:6px">Activity</h1>'
        '<p class="mut" style="margin-bottom:20px">Recent events across all instances.</p>'
        '<div class="card"><table><thead><tr><th>Time</th><th>Instance</th>'
        '<th>Type</th><th>Message</th></tr></thead><tbody>' + html + '</tbody></table></div>'
    )
    return render("Activity", body, active="act")

@app.route("/docs")
def docs_page():
    body = """
<h1 style="margin-bottom:6px">Documentation</h1>
<p class="mut" style="margin-bottom:26px">Everything you need to host apps on this panel.</p>

<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px">

<div class="card">
<h3>◈ Quick start</h3>
<ol class="mut" style="padding-left:18px;line-height:2">
<li>Click <b>New App</b> and pick a runtime.</li>
<li>Open the instance → <b>Files</b> tab.</li>
<li>Upload a <code>.zip</code> of your project (or edit starter files directly).</li>
<li>Open <b>Settings</b> → click <b>🪄 Auto-detect Commands</b>.</li>
<li>Click <b>▶ Start</b> — you're live.</li>
</ol>
</div>

<div class="card">
<h3>⚙ Start &amp; Build Commands</h3>
<p class="mut">Both commands run through <code>bash</code> in your work directory.
<code>$PORT</code> and <code>$HOST</code> are injected automatically.</p>
<div class="hint mono" style="margin-top:10px">
Python Flask → <code>python3 app.py</code><br>
FastAPI → <code>uvicorn main:app --host 0.0.0.0 --port $PORT</code><br>
Django → <code>python3 manage.py runserver 0.0.0.0:$PORT</code><br>
Node → <code>npm start</code> or <code>node index.js</code><br>
Build → <code>pip install -r requirements.txt</code> or
<code>npm install &amp;&amp; npm run build</code>
</div>
</div>

<div class="card">
<h3>📦 Zip / Tar uploads</h3>
<p class="mut">Upload any archive in the Files tab. Paths are sanitized against
zip-slip. If your archive has a single top-level folder (like GitHub zips),
tick <b>Strip single top-level folder</b> and we'll promote its contents to the root.</p>
</div>

<div class="card">
<h3>🛡 24×7 Auto-restart</h3>
<p class="mut">Every instance with <b>Auto-restart</b> enabled is checked every 6 seconds.
If the process dies, it's restarted. If the panel itself restarts, all
previously-running apps boot back automatically.</p>
</div>

<div class="card">
<h3>🗂 Subfolders &amp; File Manager</h3>
<p class="mut">Create folders, upload nested files, edit any text file in-browser,
delete recursively. Work directory (in Settings) tells the panel where your
app really lives inside the instance folder.</p>
</div>

<div class="card">
<h3>🔑 Environment Variables</h3>
<p class="mut">Add <code>KEY=value</code> lines in Settings. They're exported into
both build and start commands.</p>
</div>

</div>

<div class="card" style="margin-top:20px">
<h3>Live URL format</h3>
<p class="mut">Every running instance is reachable at
<code>/app/&lt;instance-id&gt;/</code> through this panel's reverse proxy.
Static sites are served directly; Python and Node apps are proxied to their port.</p>
</div>
"""
    return render("Docs", body, active="docs")

# ─────────────────────────────── Proxy ────────────────────────────────
@app.route("/app/<int:iid>/", defaults={"path": ""})
@app.route("/app/<int:iid>/<path:path>")
def proxy(iid, path):
    inst = get_inst(iid)
    if not inst: abort(404)
    idir = inst_dir(inst)

    if inst["type"] == "static":
        if path == "" or path.endswith("/"):
            path = (path or "") + "index.html"
        fp = safe_join(idir, path)
        if not fp or not os.path.isfile(fp): abort(404)
        return send_from_directory(idir, path.replace("\\", "/"))

    if not is_running(iid):
        return Response(
            "<html><body style='background:#040b07;color:#ff5577;"
            "font-family:ui-monospace,monospace;padding:30px'>"
            "<h3>◈ Instance not running</h3>"
            "<p style='color:#7ea88f'>Start it from the dashboard.</p>"
            "</body></html>", status=502, mimetype="text/html")

    target = "http://127.0.0.1:" + str(inst["port"]) + "/" + path
    if request.query_string:
        target += "?" + request.query_string.decode()
    try:
        req = urllib.request.Request(target, method=request.method)
        for k, v in request.headers.items():
            if k.lower() not in ("host", "content-length", "connection",
                                 "accept-encoding"):
                req.add_header(k, v)
        body = (request.get_data()
                if request.method in ("POST", "PUT", "PATCH", "DELETE") else None)
        with urllib.request.urlopen(req, data=body, timeout=60) as r:
            headers = {k: v for k, v in r.headers.items()
                       if k.lower() not in ("transfer-encoding", "connection",
                                            "content-encoding", "content-length")}
            return Response(r.read(), status=r.status, headers=headers)
    except urllib.error.HTTPError as e:
        return Response(e.read(), status=e.code)
    except Exception as e:
        return Response(
            "<html><body style='background:#040b07;color:#ff5577;"
            "font-family:ui-monospace,monospace;padding:30px'>"
            "<h3>◈ Proxy error</h3><pre>" + esc(str(e)) + "</pre>"
            "</body></html>", status=502, mimetype="text/html")

# ─────────────────────────────── Scaffolds ────────────────────────────
PY_MAIN = '''#!/usr/bin/env python3
"""main.py — application entrypoint."""
import os
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8000))
HOST = os.environ.get("HOST", "0.0.0.0")

PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>MODx Python</title><style>
body{margin:0;height:100vh;display:grid;place-items:center;
  background:radial-gradient(circle at 30% 20%,#0a2a1a,#030b06);
  font-family:system-ui;color:#e6fff0}
.box{padding:46px 66px;text-align:center;
  background:rgba(0,255,157,.06);border:1px solid rgba(0,255,157,.25);
  border-radius:22px;backdrop-filter:blur(10px)}
h1{margin:0 0 12px;font-size:46px;letter-spacing:-1px;
  background:linear-gradient(135deg,#00ff9d,#5bffce);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent}
p{margin:0;color:#7ea88f;font-size:14px;font-family:ui-monospace,monospace}
</style></head><body><div class="box">
<h1>◈ MODx Python</h1><p>listening on %d</p>
</div></body></html>""" % PORT

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        data = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, fmt, *a):
        print("[%s] %s" % (self.address_string(), fmt % a))

if __name__ == "__main__":
    print("◈ Listening on %s:%d" % (HOST, PORT))
    HTTPServer((HOST, PORT), H).serve_forever()
'''

PY_REQ = """# Python dependencies — add packages then click "Run Build Now" in Settings.
# Example:
# flask>=3.0
# requests
# fastapi
# uvicorn
"""

NODE_MAIN = '''#!/usr/bin/env node
/** index.js — application entrypoint */
const http = require("http");
const PORT = process.env.PORT || 8000;
const HOST = process.env.HOST || "0.0.0.0";

const PAGE = `<!DOCTYPE html><html><head><meta charset="utf-8">
<title>MODx Node</title><style>
body{margin:0;height:100vh;display:grid;place-items:center;
  background:radial-gradient(circle at 70% 30%,#0a2a1a,#030b06);
  font-family:system-ui;color:#e6fff0}
.box{padding:46px 66px;text-align:center;
  background:rgba(0,255,157,.06);border:1px solid rgba(0,255,157,.25);
  border-radius:22px;backdrop-filter:blur(10px)}
h1{margin:0 0 12px;font-size:46px;letter-spacing:-1px;
  background:linear-gradient(135deg,#00ff9d,#5bffce);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent}
p{margin:0;color:#7ea88f;font-size:14px;font-family:ui-monospace,monospace}
</style></head><body><div class="box">
<h1>◈ MODx Node</h1><p>listening on port ${PORT}</p>
</div></body></html>`;

http.createServer((req, res) => {
  res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  res.end(PAGE);
}).listen(PORT, HOST, () => console.log("◈ Listening on " + HOST + ":" + PORT));
'''

NODE_PKG = '''{
  "name": "modx-app",
  "version": "1.0.0",
  "private": true,
  "main": "index.js",
  "scripts": {
    "start": "node index.js"
  },
  "dependencies": {}
}
'''

STATIC_HTML = '''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MODx Static Site</title>
<style>
  *{margin:0;box-sizing:border-box}
  body{min-height:100vh;display:grid;place-items:center;
       background:radial-gradient(circle at 50% 40%,#0a2a1a,#030b06);
       font-family:system-ui;color:#e6fff0}
  .box{padding:56px 76px;text-align:center;
       background:rgba(0,255,157,.06);border:1px solid rgba(0,255,157,.25);
       border-radius:26px;backdrop-filter:blur(12px)}
  h1{font-size:54px;margin:0 0 14px;letter-spacing:-1.5px;
     background:linear-gradient(135deg,#00ff9d,#5bffce);
     -webkit-background-clip:text;-webkit-text-fill-color:transparent}
  p{color:#7ea88f;font-family:ui-monospace,monospace;font-size:13px}
</style></head><body><div class="box">
<h1>◈ It works!</h1><p>Replace index.html to begin.</p>
</div></body></html>
'''

# ─────────────────────────────── Boot ─────────────────────────────────
init_db()

def boot_autostart():
    time.sleep(1.5)
    try:
        d = db()
        rows = d.execute("SELECT * FROM instances WHERE status='running'").fetchall()
        d.close()
        for r in rows:
            inst = get_inst(r["id"])
            if inst and inst["type"] != "static":
                print("[boot] restoring", inst["name"], flush=True)
                start_inst(inst)
            elif inst and inst["type"] == "static":
                d = db()
                d.execute("UPDATE instances SET status='running' WHERE id=?", (inst["id"],))
                d.commit(); d.close()
    except Exception as e:
        print("[boot]", e, flush=True)

threading.Thread(target=boot_autostart, daemon=True).start()
threading.Thread(target=watchdog, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print("\n╔══════════════════════════════════════════════════╗")
    print("║  MODx Hosting Panel · Nebula Edition · v1.0       ║")
    print("╠══════════════════════════════════════════════════╣")
    print("║  URL       → http://0.0.0.0:%-5d                 ║" % port)
    print("║  Username  → %-35s║" % AUTH_USER)
    print("║  Password  → %-35s║" % AUTH_PASS)
    print("║  Storage   → %-35s║" % BASE_DIR)
    print("╚══════════════════════════════════════════════════╝\n")
    app.run(host="0.0.0.0", port=port, threaded=True)
