#!/usr/bin/env python3
"""anime-dl: a small web UI + API around ani-cli that downloads into a Jellyfin library.

Deliberately narrow: it can search, queue downloads and report progress.
It never deletes or overwrites anything in the library.
"""

import glob
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANIME_DIR = os.environ.get("ANIME_DIR", "/anime")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
API_TOKEN = os.environ.get("API_TOKEN", "")
PORT = int(os.environ.get("PORT", "8686"))
QUALITY = os.environ.get("QUALITY", "best")
MAX_QUEUED = int(os.environ.get("MAX_QUEUED_EPISODES", "500"))
SUB_LANG = os.environ.get("SUBTITLE_LANG", "en")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLED_ANI_CLI = os.environ.get("BUNDLED_ANI_CLI", "/opt/ani-cli/ani-cli")
ANI_CLI = os.path.join(CONFIG_DIR, "bin", "ani-cli")
JOBS_FILE = os.path.join(CONFIG_DIR, "jobs.json")
INCOMING = os.path.join(ANIME_DIR, ".incoming")  # dot-folder: Jellyfin ignores it

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")
EPISODE_RE = re.compile(r"^\d+(\.\d+)?$")
QUERY_RE = re.compile(r"^[\w][\w \-'.!:,]{0,99}$")

lock = threading.Lock()
wakeup = threading.Event()
jobs = []  # newest last
current = {"proc": None, "job": None}


# ---------- helpers ----------

def now():
    return int(time.time())


def clean(text):
    return ANSI_RE.sub("", text).replace("\x1b[2K", "").strip()


def safe_name(name):
    """Turn a title into a single safe path component."""
    name = unicodedata.normalize("NFC", str(name))
    name = "".join(c for c in name if unicodedata.category(c)[0] != "C")
    name = name.replace(":", "")
    name = re.sub(r'[<>"/\\|?*]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name or name in (".", ".."):
        raise ValueError("invalid name")
    return name[:150]


def check_query(query):
    query = re.sub(r"\s+", " ", str(query or "")).strip()
    if not QUERY_RE.match(query):
        raise ValueError("query must start with a letter/number and use only letters, numbers, spaces and - ' . ! : ,")
    return query


def ani_cli_version(path):
    try:
        with open(path) as f:
            m = re.search(r'^version_number="([^"]+)"', f.read(), re.M)
        return tuple(int(x) for x in m.group(1).split(".")) if m else ()
    except (OSError, ValueError):
        return ()


def install_ani_cli():
    """Keep a writable copy in /config so the in-app update survives restarts.
    A newer bundled version (from rebuilding the image) replaces it."""
    os.makedirs(os.path.dirname(ANI_CLI), exist_ok=True)
    if ani_cli_version(BUNDLED_ANI_CLI) > ani_cli_version(ANI_CLI):
        shutil.copyfile(BUNDLED_ANI_CLI, ANI_CLI)
    os.chmod(ANI_CLI, 0o755)


def kill_tree(proc):
    """ani-cli spawns yt-dlp/ffmpeg; kill its whole process group."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_ani(args, player, workdir, pick_first=False, timeout=120, on_line=None):
    """Run ani-cli headless. Returns (exit code, output, captured menu, history entry)."""
    env = {
        "PATH": APP_DIR + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": workdir,
        "TERM": "dumb",
        "ANI_CLI_MENU": "capmenu",
        "ANI_CLI_PLAYER": player,
        "ANI_CLI_HIST_DIR": workdir,
        "ANI_CLI_DOWNLOAD_DIR": workdir,
        "ANI_CLI_LOG": "0",
        "ANI_CLI_QUALITY": QUALITY,
        "CAP_FILE": os.path.join(workdir, "menu.txt"),
        "CAP_PICK_FIRST": "1" if pick_first else "0",
    }
    proc = subprocess.Popen(["sh", ANI_CLI] + args, env=env, cwd=workdir,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    if on_line:
        on_line(None, proc)
    out, buf = [], b""
    deadline = time.time() + timeout
    while True:
        chunk = proc.stdout.read1(4096)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for raw in parts:
            line = clean(raw.decode("utf-8", "replace"))
            if not line:
                continue
            if on_line:
                on_line(line, proc)
            if not PROGRESS_RE.search(line):
                out.append(line)
        if time.time() > deadline:
            kill_tree(proc)
            out.append("timed out")
            break
    proc.wait()

    menu = None
    try:
        with open(env["CAP_FILE"]) as f:
            lines = f.read().splitlines()
        menu = {"prompt": lines[0].removeprefix("PROMPT=").strip(), "items": [l for l in lines[1:] if l]}
    except (OSError, IndexError):
        pass
    hist = None
    try:
        with open(os.path.join(workdir, "ani-hsts")) as f:
            ep, anime_id, title = f.readline().rstrip("\n").split("\t")
            hist = {"episode": ep, "id": anime_id, "title": title}
    except (OSError, ValueError):
        pass
    return proc.returncode, out[-30:], menu, hist


def last_error(out):
    for line in reversed(out):
        if line and not line.startswith("Checking dependencies"):
            return line
    return "unknown error"


def search(query):
    query = check_query(query)
    with tempfile.TemporaryDirectory() as wd:
        _, out, menu, _ = run_ani([query], "debug", wd)
        if menu and menu["prompt"].startswith("Select anime"):
            results = []
            for item in menu["items"]:
                num, _, title = item.partition(" ")
                results.append({"index": int(num), "title": title})
            return results
        if menu and menu["prompt"].startswith("Select episode"):
            # exactly one result: ani-cli skipped the anime menu. Pick an episode
            # so it records the title in its history, then read it back.
            with tempfile.TemporaryDirectory() as wd2:
                _, _, _, hist = run_ani([query], "debug", wd2, pick_first=True)
            if hist:
                return [{"index": 1, "title": hist["title"]}]
        if any("No results" in l for l in out):
            return []
        raise RuntimeError(last_error(out))


def episodes(query, index):
    query = check_query(query)
    with tempfile.TemporaryDirectory() as wd:
        _, out, menu, hist = run_ani(["-S", str(int(index)), query], "debug", wd)
        if menu and menu["prompt"].startswith("Select episode"):
            return [e for e in menu["items"] if EPISODE_RE.match(e)]
        if hist:  # a single episode is auto-selected without a menu
            return [hist["episode"]]
        if any("Invalid episode selection" in l for l in out):  # no episodes out yet, so no menu
            return []
        raise RuntimeError(last_error(out))


def target_path(show, season, ep):
    if re.match(r"^\d+$", ep):
        folder = os.path.join(ANIME_DIR, show, "Season %02d" % season)
        base = "%s S%02dE%02d" % (show, season, int(ep))
    else:  # half episodes like 12.5 -> specials
        folder = os.path.join(ANIME_DIR, show, "Specials")
        base = "%s - Episode %s" % (show, ep)
    path = os.path.realpath(os.path.join(folder, base))
    if not path.startswith(os.path.realpath(ANIME_DIR) + os.sep):
        raise ValueError("path escapes library")
    return path


def refresh_jellyfin():
    if not (JELLYFIN_URL and JELLYFIN_API_KEY):
        return
    req = urllib.request.Request(JELLYFIN_URL + "/Library/Refresh", method="POST", data=b"", headers={
        "Authorization": 'MediaBrowser Token="%s"' % JELLYFIN_API_KEY,
        "X-Emby-Token": JELLYFIN_API_KEY,
    })
    try:
        urllib.request.urlopen(req, timeout=15).close()
        print("jellyfin: library refresh requested", flush=True)
    except Exception as e:  # never let Jellyfin trouble break downloads
        print("jellyfin refresh failed:", e, flush=True)


# ---------- job store ----------

def save_jobs():
    tmp = JOBS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(jobs, f)
    os.replace(tmp, JOBS_FILE)


def load_jobs():
    global jobs
    try:
        with open(JOBS_FILE) as f:
            jobs = json.load(f)
    except (OSError, ValueError):
        jobs = []
    for job in jobs:
        for ep in job["episodes"]:
            if ep["status"] == "downloading":
                ep["status"], ep["progress"] = "queued", 0


def queued_count():
    return sum(1 for j in jobs for e in j["episodes"] if e["status"] == "queued")


def find_job(job_id):
    for job in jobs:
        if job["id"] == job_id:
            return job
    raise LookupError("no such job")


def create_job(body):
    query = check_query(body.get("query"))
    index = int(body.get("index", 1))
    title = str(body.get("title") or "").strip()
    if not title:
        raise ValueError("title is required (from the search result)")
    show = safe_name(body.get("show") or title)
    season = int(body.get("season", 1))
    if not 0 <= season <= 99:
        raise ValueError("season must be 0-99")
    dub = bool(body.get("dub", False))

    available = episodes(query, index)
    if not available:
        raise ValueError("no episodes released yet")
    wanted = body.get("episodes", "all")
    if wanted == "all":
        chosen = available
    elif wanted == "latest":
        chosen = available[-1:]
    else:
        if isinstance(wanted, str):  # "1-12" or "3" or "1,2,5"
            chosen = []
            for part in wanted.split(","):
                part = part.strip()
                if "-" in part:
                    a, b = part.split("-", 1)
                    chosen += [e for e in available if float(a) <= float(e) <= float(b)]
                elif part:
                    chosen.append(part)
        else:
            chosen = [str(e) for e in wanted]
        missing = [e for e in chosen if e not in available]
        if missing:
            raise ValueError("episodes not available: " + ", ".join(missing[:10]))
    chosen = list(dict.fromkeys(chosen))
    if not chosen:
        raise ValueError("no episodes selected")

    job = {"id": uuid.uuid4().hex[:10], "created": now(), "query": query, "index": index,
           "title": title, "show": show, "season": season, "dub": dub,
           "episodes": [{"ep": e, "status": "queued", "progress": 0, "error": ""} for e in chosen]}
    with lock:
        if queued_count() + len(chosen) > MAX_QUEUED:
            raise ValueError("queue limit reached (%d episodes)" % MAX_QUEUED)
        jobs.append(job)
        save_jobs()
    wakeup.set()
    return job


# ---------- worker ----------

def next_episode():
    for job in jobs:
        for ep in job["episodes"]:
            if ep["status"] == "queued":
                return job, ep
    return None, None


def download(job, ep):
    target = target_path(job["show"], job["season"], ep["ep"])
    if os.path.exists(target + ".mp4"):
        return "skipped", "already in library"

    os.makedirs(INCOMING, exist_ok=True)
    wd = tempfile.mkdtemp(prefix=job["id"] + "-", dir=INCOMING)
    try:
        def on_line(line, proc):
            with lock:
                current["proc"] = proc
                if line and (m := PROGRESS_RE.search(line)):
                    ep["progress"] = float(m.group(1))

        args = ["-S", str(job["index"]), "-e", ep["ep"]] + (["--dub"] if job["dub"] else []) + [job["query"]]
        code, out, _, hist = run_ani(args, "download", wd, timeout=3 * 3600, on_line=on_line)
        with lock:
            current["proc"] = None
            if ep["status"] == "cancelled":
                return "cancelled", ""
        videos = glob.glob(os.path.join(wd, "*.mp4"))
        if code != 0 or not videos:
            return "failed", last_error(out)
        if not hist or hist["title"] != job["title"]:
            return "failed", "search results changed (got %r); search again" % (hist or {}).get("title")

        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target + ".mp4"):
            return "skipped", "already in library"
        shutil.move(videos[0], target + ".mp4")
        for sub in glob.glob(os.path.join(wd, "*.vtt"))[:1]:
            if not os.path.exists(target + "." + SUB_LANG + ".vtt"):
                shutil.move(sub, target + "." + SUB_LANG + ".vtt")
        return "done", ""
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def worker():
    pending_refresh, last_refresh = False, 0
    while True:
        with lock:
            job, ep = next_episode()
            if ep:
                ep["status"], ep["progress"], ep["error"] = "downloading", 0, ""
                current["job"] = job["id"]
                save_jobs()
        if not ep:
            if pending_refresh:
                refresh_jellyfin()
                pending_refresh, last_refresh = False, time.time()
            wakeup.wait(60)
            wakeup.clear()
            continue

        print("downloading %s ep %s" % (job["title"], ep["ep"]), flush=True)
        try:
            status, error = download(job, ep)
        except Exception as e:
            status, error = "failed", str(e)
        with lock:
            if ep["status"] != "cancelled":
                ep["status"], ep["error"] = status, error
                if status == "done":
                    ep["progress"] = 100
            current["job"] = None
            save_jobs()
        print("  -> %s %s" % (status, error), flush=True)
        if status == "done":
            pending_refresh = True
            if time.time() - last_refresh > 600:  # keep Jellyfin current during long batches
                refresh_jellyfin()
                pending_refresh, last_refresh = False, time.time()


# ---------- HTTP ----------

def library():
    shows = []
    for show in sorted(os.listdir(ANIME_DIR)):
        path = os.path.join(ANIME_DIR, show)
        if show.startswith(".") or not os.path.isdir(path):
            continue
        count = len(glob.glob(os.path.join(glob.escape(path), "*", "*.mp4")))
        shows.append({"show": show, "episodes": count})
    return shows


class Handler(BaseHTTPRequestHandler):
    server_version = "anime-dl"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if given and hmac.compare_digest(given.encode(), API_TOKEN.encode()):
            return True
        time.sleep(1)
        self.send(401, {"error": "bad or missing token"})
        return False

    def body(self):
        length = min(int(self.headers.get("Content-Length") or 0), 65536)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(APP_DIR, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if not self.path.startswith("/api/") or not self.authorized():
            return None if self.path.startswith("/api/") else self.send(404, {"error": "not found"})
        if self.path == "/api/jobs":
            with lock:
                return self.send(200, {"jobs": list(reversed(jobs))})
        if self.path == "/api/library":
            return self.send(200, {"shows": library()})
        if self.path == "/api/status":
            with lock:
                return self.send(200, {
                    "ani_cli": ".".join(map(str, ani_cli_version(ANI_CLI))),
                    "queued": queued_count(), "max_queued": MAX_QUEUED,
                    "jellyfin": bool(JELLYFIN_URL and JELLYFIN_API_KEY)})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/api/") or not self.authorized():
            return None if self.path.startswith("/api/") else self.send(404, {"error": "not found"})
        try:
            body = self.body()
            if self.path == "/api/search":
                return self.send(200, {"results": search(body.get("query"))})
            if self.path == "/api/episodes":
                return self.send(200, {"episodes": episodes(body.get("query"), body.get("index", 1))})
            if self.path == "/api/download":
                return self.send(201, create_job(body))
            if self.path == "/api/jobs/clear":
                with lock:
                    jobs[:] = [j for j in jobs if any(e["status"] in ("queued", "downloading") for e in j["episodes"])]
                    save_jobs()
                return self.send(200, {"ok": True})
            if m := re.match(r"^/api/jobs/([0-9a-f]+)/(cancel|retry)$", self.path):
                with lock:
                    job = find_job(m.group(1))
                    for ep in job["episodes"]:
                        if m.group(2) == "cancel" and ep["status"] in ("queued", "downloading"):
                            if ep["status"] == "downloading" and current["proc"]:
                                kill_tree(current["proc"])
                            ep["status"] = "cancelled"
                        elif m.group(2) == "retry" and ep["status"] in ("failed", "cancelled"):
                            ep["status"], ep["progress"], ep["error"] = "queued", 0, ""
                    save_jobs()
                wakeup.set()
                return self.send(200, job)
            if self.path == "/api/update-ani-cli":
                before = ani_cli_version(ANI_CLI)
                r = subprocess.run(["sh", ANI_CLI, "-U"], capture_output=True, text=True, timeout=60,
                                   env={**os.environ, "TERM": "dumb", "ANI_CLI_PLAYER": "debug",
                                        "ANI_CLI_MENU": "capmenu", "PATH": APP_DIR + ":" + os.environ["PATH"]})
                return self.send(200, {"before": ".".join(map(str, before)),
                                       "after": ".".join(map(str, ani_cli_version(ANI_CLI))),
                                       "output": clean(r.stdout + r.stderr)[-500:]})
            self.send(404, {"error": "not found"})
        except (ValueError, LookupError, json.JSONDecodeError) as e:
            self.send(400, {"error": str(e)})
        except Exception as e:
            self.send(502, {"error": str(e)})


def main():
    if len(API_TOKEN) < 16:
        raise SystemExit("API_TOKEN must be set (16+ characters)")
    if not os.path.isdir(ANIME_DIR):
        raise SystemExit("%s is not mounted" % ANIME_DIR)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    install_ani_cli()
    shutil.rmtree(INCOMING, ignore_errors=True)  # leftovers from an interrupted download
    load_jobs()
    threading.Thread(target=worker, daemon=True).start()
    print("anime-dl listening on :%d (ani-cli %s)" % (PORT, ".".join(map(str, ani_cli_version(ANI_CLI)))), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
