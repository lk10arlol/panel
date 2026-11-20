"""
Scraper LK10 - panneau de contrôle (Flask + MongoDB).

- Lance tes scripts Python (Selenium) en sous-processus, un à la fois par défaut.
- Lit leurs logs en direct, en tire : progression, élément en cours, ajouts, erreurs.
- Sauvegarde progression + historique dans MongoDB (le disque Render est effacé à chaque redéploiement).
- Si le service redémarre, il reprend tout seul les scripts qui tournaient (ils reprennent à la bonne page).
"""
import os
import re
import sys
import json
import time
import hmac
import uuid
import signal
import hashlib
import threading
import subprocess
from collections import deque
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify, send_from_directory
from pymongo import MongoClient, DESCENDING

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
BASE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(BASE, "scripts")
WRAPPER = os.path.join(BASE, "runner_wrapper.py")
DATA_ROOT = os.environ.get("DATA_ROOT", "/tmp/scraper-data")

MONGODB_URI = os.environ["MONGODB_URI"]
DB_NAME = os.environ.get("MONGODB_DB", "scraper_control")
DASH_PASSWORD = os.environ["DASH_PASSWORD"]
SECRET = os.environ.get("SESSION_SECRET") or hashlib.sha256(("lk10:" + DASH_PASSWORD).encode()).hexdigest()
TOKEN = hmac.new(SECRET.encode(), b"dashboard-v1", hashlib.sha256).hexdigest()

MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "1"))      # 1 Chrome à la fois = RAM sous contrôle
AUTO_RETRY_MAX = int(os.environ.get("AUTO_RETRY_MAX", "5"))  # relances auto si blocage / plantage
AUTO_RETRY_DELAY = int(os.environ.get("AUTO_RETRY_DELAY", "180"))
RING_SIZE = 600

# Ajoute ici d'autres scripts (ex. le script films) : il apparaît tout seul dans l'interface
# dès que le fichier existe dans /scripts.
SCRIPTS = {
    "series_pages": {
        "label": "Séries · toutes les pages",
        "kind": "serie",
        "file": "scrap_serie_toutes_pages.py",
        "desc": "Parcourt la liste des séries page par page jusqu'à la dernière, puis s'arrête.",
    },
    "series_genre": {
        "label": "Séries · par mot-clé",
        "kind": "serie",
        "file": "scrap_serie_genre.py",
        "desc": "Recherche par mot-clé (ex. noel, christmas) et synchronise chaque saison.",
        "keywords": "noel, christmas",
    },
    "films_pages": {
        "label": "Films · toutes les pages",
        "kind": "film",
        "file": "scrap_toutes_pages.py",
        "desc": "Parcourt la liste des films page par page jusqu'à la dernière, puis s'arrête.",
    },
}

INSTANCE = uuid.uuid4().hex[:8]

mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=15000)
db = mongo[DB_NAME]
try:
    db.logs.create_index("ts", expireAfterSeconds=14 * 24 * 3600)
    db.events.create_index([("kind", 1), ("ts", -1)])
    db.runs.create_index([("script", 1), ("started_at", -1)])
    db.files.create_index([("script", 1), ("name", 1)], unique=True)
except Exception as e:  # ne bloque pas le démarrage
    print("index mongo:", e, flush=True)

app = Flask(__name__, static_folder=None)


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    if not isinstance(dt, datetime):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def ser(doc):
    out = {}
    for k, v in doc.items():
        if k == "_id":
            out["id"] = str(v)
        elif isinstance(v, datetime):
            out[k] = iso(v)
        else:
            out[k] = v
    return out


def script_available(sid):
    return os.path.exists(os.path.join(SCRIPTS_DIR, SCRIPTS[sid]["file"]))


# ----------------------------------------------------------------------------
# PARSING DES LOGS DES SCRIPTS
# ----------------------------------------------------------------------------
LEVEL_RE = re.compile(r"^\d{4}-\d\d-\d\d [\d:,]+ - (DEBUG|INFO|WARNING|ERROR|CRITICAL) - (.*)$")
P_PAGE = re.compile(r"PAGE\s+(\d+)\s*/\s*(\d+|\?)")
P_LAST = re.compile(r"Dernière page du site\s*:\s*(\d+)")
P_ITEM_P = re.compile(r"^\s*\[p(\d+)\s*·\s*(\d+)/(\d+)\]\s*➡️\s*(.*)$")
P_ITEM = re.compile(r"^\s*\[(\d+)/(\d+)\]\s*➡️\s*(.*)$")
P_TRAIT = re.compile(r"TRAITEMENT\s*:\s*(.+)$")
P_CREATED = re.compile(r"\[MONGODB\].*?(?:Création réussie|Film enregistré)\s*:\s*(.+)$")
P_UPDATED = re.compile(r"\[MONGODB\].*?Mise à jour.*?réussie\s*:\s*(.+)$")
P_EP = re.compile(r"\[(VF|VOSTFR)\]\s*(.+?)\s+en cours de scrape")
P_SKIP = re.compile(r"NEXT : déjà en base|est déjà à jour")
P_SKIPPED_PAGE = re.compile(r"\((\d+) déjà en base / traitées\)")
P_FAIL = re.compile(r"Aucun lien valide|Titre vide|Page non reconnue|Erreur sauvegarde|TMDB introuvable")
P_FINISH = re.compile(r"🏁|Toutes les pages ont été parcourues")
P_BLOCKED = re.compile(r"⛔")
P_FATAL = re.compile(r"Une erreur est survenue|Impossible de lancer Chrome|Base de données illisible|Login refusé|Variables d'environnement manquantes|Erreur réseau : ")
P_FATAL_CONFIG = re.compile(r"Login refusé|Variables d'environnement manquantes")
P_RESUME = re.compile(r"Reprise à la page (\d+)")


class Job:
    def __init__(self, script_id, run_id, params):
        self.script_id = script_id
        self.run_id = run_id
        self.params = params
        self.proc = None
        self.thread = None
        self.alive = True
        self.stop_requested = False
        self.stop_evt = threading.Event()
        self.ring = deque(maxlen=RING_SIZE)
        self.seq = 0
        self.status = "running"
        self.retries = 0
        self.progress = {"page": None, "total": None, "item": None, "items": None}
        self.current = ""
        self.detail = ""
        self.stats = {"created": 0, "updated": 0, "skipped": 0, "failed": 0, "warnings": 0}
        self.finished = False
        self.blocked = False
        self.fatal = False
        self.fatal_config = False
        self.err_logged = 0
        self.file_sig = {}
        self.last_level = "INFO"
        self.started_at = now()


jobs = {}            # script_id -> Job (le dernier, vivant ou terminé)
jobs_lock = threading.RLock()


def push_line(job, level, msg):
    job.seq += 1
    job.ring.append({"n": job.seq, "t": now().strftime("%H:%M:%S"), "l": level, "m": msg})


def handle_line(job, raw):
    line = raw.rstrip("\r\n")
    if not line.strip():
        return
    m = LEVEL_RE.match(line)
    if m:
        level, msg = m.group(1), m.group(2)
        job.last_level = level
        if not msg.strip():       # le message commence par "\n" : le contenu arrive sur la ligne suivante
            return
    else:
        msg = line
        level = "INFO"
        if line.startswith("Traceback") or re.match(r"^\w*(Error|Exception)\b", line):
            level = "ERROR"
    if "❌" in msg or "⛔" in msg:
        level = "ERROR" if level == "INFO" else level
    elif "⚠️" in msg and level == "INFO":
        level = "WARNING"

    push_line(job, level, msg.strip())

    # --- progression
    mm = P_PAGE.search(msg)
    if mm:
        job.progress["page"] = int(mm.group(1))
        if mm.group(2).isdigit():
            job.progress["total"] = int(mm.group(2))
        job.progress["item"] = None
        job.progress["items"] = None
        job.detail = ""
    mm = P_LAST.search(msg)
    if mm:
        job.progress["total"] = int(mm.group(1))
    mm = P_RESUME.search(msg)
    if mm:
        job.progress["page"] = int(mm.group(1))
    mm = P_ITEM_P.match(msg)
    if mm:
        job.progress["page"] = int(mm.group(1))
        job.progress["item"], job.progress["items"] = int(mm.group(2)), int(mm.group(3))
        job.current = mm.group(4).strip()
        job.detail = ""
    else:
        mm = P_ITEM.match(msg)
        if mm:  # script par mot-clé : [i/N] ➡️ url
            job.progress["page"], job.progress["total"] = int(mm.group(1)), int(mm.group(2))
            job.current = mm.group(3).strip()
            job.detail = ""
    mm = P_TRAIT.search(msg)
    if mm:
        job.current = mm.group(1).strip()
    mm = P_EP.search(msg)
    if mm:
        job.detail = f"{mm.group(1)} · {mm.group(2)}"

    # --- résultats
    kind = SCRIPTS[job.script_id]["kind"]
    mm = P_CREATED.search(msg)
    if mm:
        job.stats["created"] += 1
        save_event(job, kind, "created", mm.group(1).strip())
    else:
        mm = P_UPDATED.search(msg)
        if mm:
            job.stats["updated"] += 1
            save_event(job, kind, "updated", mm.group(1).strip())
    if P_SKIP.search(msg):
        job.stats["skipped"] += 1
    mm = P_SKIPPED_PAGE.search(msg)
    if mm:
        job.stats["skipped"] += int(mm.group(1))
    if P_FAIL.search(msg):
        job.stats["failed"] += 1

    if P_FINISH.search(msg):
        job.finished = True
    if P_BLOCKED.search(msg):
        job.blocked = True
    if P_FATAL.search(msg):
        job.fatal = True
    if P_FATAL_CONFIG.search(msg):
        job.fatal_config = True

    if level in ("WARNING", "ERROR", "CRITICAL"):
        job.stats["warnings"] += 1
        if job.err_logged < 3000:
            job.err_logged += 1
            try:
                db.logs.insert_one({"run_id": job.run_id, "script": job.script_id, "ts": now(),
                                    "level": level, "msg": msg.strip()[:600]})
            except Exception:
                pass


def save_event(job, kind, action, title):
    try:
        db.events.insert_one({"run_id": job.run_id, "script": job.script_id, "kind": kind,
                              "action": action, "title": title[:300], "ts": now()})
    except Exception:
        pass


# ----------------------------------------------------------------------------
# FICHIERS DE PROGRESSION (sauvés dans MongoDB, car le disque Render est éphémère)
# ----------------------------------------------------------------------------
FILE_RE = re.compile(r"^(progress|done|failed)[\w\-]*\.(json|txt)$")


def restore_files(script_id, workdir):
    for d in db.files.find({"script": script_id}):
        path = os.path.join(workdir, d["name"])
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(d.get("content", ""))


def sync_files(job, workdir):
    try:
        names = os.listdir(workdir)
    except Exception:
        return
    for name in names:
        if not FILE_RE.match(name):
            continue
        path = os.path.join(workdir, name)
        try:
            st = os.stat(path)
            sig = (st.st_mtime, st.st_size)
            if job.file_sig.get(name) == sig or st.st_size > 12_000_000:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            db.files.update_one({"script": job.script_id, "name": name},
                                {"$set": {"content": content, "updated": now()}}, upsert=True)
            job.file_sig[name] = sig
        except Exception as e:
            print("sync_files:", e, flush=True)


# ----------------------------------------------------------------------------
# EXÉCUTION
# ----------------------------------------------------------------------------
def snapshot(job):
    return {
        "status": job.status,
        "progress": job.progress,
        "current": job.current,
        "detail": job.detail,
        "stats": job.stats,
        "retries": job.retries,
    }


def flush_run(job, **extra):
    try:
        db.runs.update_one({"_id": job.run_id},
                           {"$set": {**snapshot(job), "heartbeat": now(), "instance": INSTANCE, **extra}})
    except Exception:
        pass


def ticker(job, workdir):
    n = 0
    while job.alive:
        time.sleep(5)
        n += 1
        flush_run(job)
        if n % 6 == 0:
            sync_files(job, workdir)


def prepare_script(job, workdir):
    sc = SCRIPTS[job.script_id]
    src = os.path.join(SCRIPTS_DIR, sc["file"])
    dst = os.path.join(workdir, sc["file"])
    with open(src, "r", encoding="utf-8") as f:
        code = f.read()
    kw = (job.params.get("keywords") or "").strip()
    if kw and sc.get("keywords"):
        words = [w.strip() for w in re.split(r"[,;\n]", kw) if w.strip()]
        if words:
            code = re.sub(r"^SEARCH_KEYWORDS\s*=.*$", "SEARCH_KEYWORDS = " + repr(words), code, count=1, flags=re.M)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(code)
    return dst


def kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass


def run_once(job, workdir):
    """Lance le script une fois. Renvoie le code retour."""
    path = prepare_script(job, workdir)
    env = os.environ.copy()
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    job.finished = job.blocked = job.fatal = job.fatal_config = False
    job.status = "running"
    flush_run(job)
    proc = subprocess.Popen(
        [sys.executable, "-u", WRAPPER, path], cwd=workdir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=True,
    )
    job.proc = proc
    try:
        for line in proc.stdout:
            handle_line(job, line)
    finally:
        rc = proc.wait()
        kill_group(proc)  # ne laisse aucun Chrome orphelin
    return rc


def execute(job):
    workdir = os.path.join(DATA_ROOT, job.script_id)
    os.makedirs(workdir, exist_ok=True)
    try:
        restore_files(job.script_id, workdir)
    except Exception as e:
        push_line(job, "WARNING", f"Restauration de la progression impossible : {e}")
    threading.Thread(target=ticker, args=(job, workdir), daemon=True).start()

    final = "error"
    try:
        while True:
            rc = run_once(job, workdir)
            sync_files(job, workdir)
            if job.stop_requested:
                final = "stopped"
                break
            if rc != 0 or job.fatal:
                final = "error"
            elif job.blocked:
                final = "blocked"
            else:
                final = "finished"
                break
            retry_ok = (job.params.get("auto_retry", True) and not job.fatal_config
                        and job.retries < AUTO_RETRY_MAX and not job.finished)
            if not retry_ok:
                break
            job.retries += 1
            job.status = "waiting"
            push_line(job, "WARNING", f"Relance automatique {job.retries}/{AUTO_RETRY_MAX} dans {AUTO_RETRY_DELAY}s "
                                      f"(la progression est conservée).")
            flush_run(job)
            if job.stop_evt.wait(AUTO_RETRY_DELAY):
                final = "stopped"
                break
    except Exception as e:
        push_line(job, "ERROR", f"Erreur du panneau de contrôle : {e}")
        final = "error"
    finally:
        job.status = final
        job.alive = False
        sync_files(job, workdir)
        flush_run(job, ended_at=now())

    # enchaînement éventuel (« tout lancer à la suite »)
    chain = job.params.get("chain") or []
    if chain and final in ("finished", "blocked", "error") and not job.stop_requested:
        nxt, rest = chain[0], chain[1:]
        try:
            start_script(nxt, {**job.params, "chain": rest})
        except Exception as e:
            print("chain:", e, flush=True)


def running_count():
    with jobs_lock:
        return sum(1 for j in jobs.values() if j.alive)


def start_script(script_id, params, run_id=None, retries=0):
    if script_id not in SCRIPTS or not script_available(script_id):
        raise ValueError("Script indisponible")
    with jobs_lock:
        cur = jobs.get(script_id)
        if cur and cur.alive:
            raise RuntimeError("Ce script tourne déjà.")
        if running_count() >= MAX_PARALLEL:
            raise RuntimeError("Un autre script tourne déjà (un seul à la fois pour ménager la RAM).")
        if run_id is None:
            # un autre service (déploiement qui se chevauche) fait déjà tourner ce script ?
            busy = db.runs.find_one({"script": script_id, "status": {"$in": ["running", "waiting"]},
                                     "heartbeat": {"$gt": now() - timedelta(seconds=45)},
                                     "instance": {"$ne": INSTANCE}})
            if busy:
                raise RuntimeError("Ce script tourne déjà sur une autre instance.")
            run_id = uuid.uuid4().hex
            db.runs.insert_one({"_id": run_id, "script": script_id, "kind": SCRIPTS[script_id]["kind"],
                                "status": "running", "started_at": now(), "heartbeat": now(),
                                "instance": INSTANCE, "params": params, "progress": {}, "stats": {}})
        job = Job(script_id, run_id, params)
        job.retries = retries
        jobs[script_id] = job
        job.thread = threading.Thread(target=execute, args=(job,), daemon=True)
        job.thread.start()
    return run_id


def stop_script(script_id):
    with jobs_lock:
        job = jobs.get(script_id)
    if not job or not job.alive:
        return False
    job.stop_requested = True
    job.params["chain"] = []
    job.stop_evt.set()
    p = job.proc
    if p and p.poll() is None:
        try:
            os.kill(p.pid, signal.SIGINT)   # arrêt propre : le script sauvegarde sa progression
        except Exception:
            pass

        def hard():
            time.sleep(25)
            if p.poll() is None:
                kill_group(p)
        threading.Thread(target=hard, daemon=True).start()
    return True


def watchdog():
    """Reprend les scripts dont l'instance précédente est morte (redémarrage / redéploiement Render)."""
    time.sleep(8)
    while True:
        try:
            stale = now() - timedelta(seconds=60)
            for r in db.runs.find({"status": {"$in": ["running", "waiting"]},
                                   "instance": {"$ne": INSTANCE}, "heartbeat": {"$lt": stale}}):
                got = db.runs.find_one_and_update(
                    {"_id": r["_id"], "heartbeat": {"$lt": stale}, "instance": r.get("instance")},
                    {"$set": {"instance": INSTANCE, "heartbeat": now(), "status": "running"}})
                if not got:
                    continue
                sid = r["script"]
                if sid in SCRIPTS and script_available(sid) and running_count() < MAX_PARALLEL:
                    try:
                        start_script(sid, r.get("params") or {}, run_id=r["_id"],
                                     retries=(r.get("retries") or 0))
                        print(f"reprise auto de {sid}", flush=True)
                    except Exception as e:
                        print("reprise:", e, flush=True)
                        db.runs.update_one({"_id": r["_id"]}, {"$set": {"status": "interrupted"}})
                else:
                    db.runs.update_one({"_id": r["_id"]}, {"$set": {"status": "interrupted"}})
        except Exception as e:
            print("watchdog:", e, flush=True)
        time.sleep(30)


threading.Thread(target=watchdog, daemon=True).start()


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
@app.before_request
def guard():
    p = request.path
    if not p.startswith("/api/") or p == "/api/login":
        return None
    auth = request.headers.get("Authorization", "")
    if not hmac.compare_digest(auth, "Bearer " + TOKEN):
        return jsonify({"error": "unauthorized"}), 401
    return None


@app.post("/api/login")
def login():
    pw = (request.get_json(silent=True) or {}).get("password", "")
    if hmac.compare_digest(str(pw).encode(), DASH_PASSWORD.encode()):
        return jsonify({"token": TOKEN})
    time.sleep(1)
    return jsonify({"error": "Mot de passe incorrect"}), 401


@app.get("/health")
def health():
    return "ok"


@app.get("/api/status")
def status():
    out = []
    for sid, sc in SCRIPTS.items():
        avail = script_available(sid)
        item = {"id": sid, "label": sc["label"], "kind": sc["kind"], "desc": sc["desc"],
                "available": avail, "file": sc["file"], "keywords": sc.get("keywords"),
                "running": False, "run": None}
        with jobs_lock:
            job = jobs.get(sid)
        if job:
            item["running"] = job.alive
            item["run"] = {**snapshot(job), "started_at": iso(job.started_at)}
        else:
            last = db.runs.find_one({"script": sid}, sort=[("started_at", DESCENDING)])
            if last:
                item["run"] = {k: last.get(k) for k in ("status", "progress", "current", "detail", "stats", "retries")}
                item["run"]["started_at"] = iso(last.get("started_at"))
                item["run"]["ended_at"] = iso(last.get("ended_at"))
                if last.get("status") in ("running", "waiting"):
                    item["running"] = True   # tourne sur une autre instance / en cours de reprise
        out.append(item)
    return jsonify({"scripts": out, "running": running_count(), "max_parallel": MAX_PARALLEL})


@app.post("/api/start")
def api_start():
    body = request.get_json(silent=True) or {}
    ids = [s for s in (body.get("scripts") or []) if s in SCRIPTS and script_available(s)]
    if not ids:
        return jsonify({"error": "Aucun script disponible à lancer."}), 400
    params = {"keywords": body.get("keywords") or "", "auto_retry": bool(body.get("auto_retry", True)),
              "chain": ids[1:]}
    try:
        run_id = start_script(ids[0], params)
    except (RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"ok": True, "run_id": run_id})


@app.post("/api/stop")
def api_stop():
    sid = (request.get_json(silent=True) or {}).get("script")
    return jsonify({"ok": stop_script(sid)})


@app.post("/api/reset")
def api_reset():
    sid = (request.get_json(silent=True) or {}).get("script")
    if sid not in SCRIPTS:
        return jsonify({"error": "script inconnu"}), 400
    with jobs_lock:
        job = jobs.get(sid)
    if job and job.alive:
        return jsonify({"error": "Arrête d'abord le script."}), 409
    db.files.delete_many({"script": sid})
    wd = os.path.join(DATA_ROOT, sid)
    if os.path.isdir(wd):
        for n in os.listdir(wd):
            if FILE_RE.match(n):
                try:
                    os.remove(os.path.join(wd, n))
                except Exception:
                    pass
    return jsonify({"ok": True})


@app.get("/api/items")
def api_items():
    kind = request.args.get("kind", "serie")
    action = request.args.get("action")
    limit = min(int(request.args.get("limit", 60)), 200)
    skip = int(request.args.get("skip", 0))
    q = {"kind": kind}
    if action in ("created", "updated"):
        q["action"] = action
    items = [ser(d) for d in db.events.find(q).sort("ts", DESCENDING).skip(skip).limit(limit)]
    since = now() - timedelta(hours=24)
    counts = {
        "created_24h": db.events.count_documents({"kind": kind, "action": "created", "ts": {"$gt": since}}),
        "updated_24h": db.events.count_documents({"kind": kind, "action": "updated", "ts": {"$gt": since}}),
        "created_total": db.events.count_documents({"kind": kind, "action": "created"}),
        "updated_total": db.events.count_documents({"kind": kind, "action": "updated"}),
    }
    return jsonify({"items": items, "counts": counts})


@app.get("/api/errors")
def api_errors():
    limit = min(int(request.args.get("limit", 40)), 200)
    q = {}
    if request.args.get("script"):
        q["script"] = request.args["script"]
    return jsonify({"errors": [ser(d) for d in db.logs.find(q).sort("ts", DESCENDING).limit(limit)]})


@app.get("/api/log")
def api_log():
    sid = request.args.get("script")
    after = int(request.args.get("after", 0))
    with jobs_lock:
        job = jobs.get(sid)
    if not job:
        return jsonify({"lines": [], "last": 0})
    lines = [l for l in list(job.ring) if l["n"] > after]
    return jsonify({"lines": lines[-300:], "last": job.seq, "alive": job.alive})


@app.get("/api/runs")
def api_runs():
    return jsonify({"runs": [ser(d) for d in db.runs.find().sort("started_at", DESCENDING).limit(30)]})


@app.get("/manifest.json")
def manifest():
    return jsonify({
        "name": "Scraper LK10", "short_name": "Scraper", "start_url": "/", "display": "standalone",
        "background_color": "#0b0b10", "theme_color": "#0b0b10",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
    })


@app.get("/icon.svg")
def icon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="110" '
           'fill="#e50914"/><text x="256" y="330" font-family="Arial" font-weight="900" font-size="230" '
           'text-anchor="middle" fill="#fff">LK</text></svg>')
    return app.response_class(svg, mimetype="image/svg+xml")


@app.get("/")
def index():
    return send_from_directory(os.path.join(BASE, "static"), "index.html")
