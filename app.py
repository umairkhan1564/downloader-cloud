# -*- coding: utf-8 -*-
"""Bulk Downloader Web — local Flask server.
Reuses the proven desktop engine (downloader.py). Two-step flow like the app:
FETCH (enumerate items) -> pick -> DOWNLOAD (yt-dlp / gallery-dl), per-item
progress, then serve each file to the browser (+ optional output folder copy).
Runs on THIS PC (residential IP + your bandwidth) — the right place for a
yt-dlp based downloader."""
import os
import re
import uuid
import shutil
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from flask import Flask, request, jsonify, send_from_directory, render_template

import downloader as dl

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = None

BASE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.join(BASE, "outputs")
os.makedirs(OUTPUTS, exist_ok=True)

# --- ViDownloader-style server-side login --------------------------------- #
# Drop the operator's OWN exported cookies.txt file(s) here (from a logged-in
# Instagram/Facebook browser). Every visitor's IG/FB download then uses these
# automatically — the site NEVER asks a normal user for cookies. One combined
# cookies.txt can hold both instagram.com + facebook.com cookies.
SERVER_COOKIES_DIR = os.path.join(BASE, "server_cookies")
os.makedirs(SERVER_COOKIES_DIR, exist_ok=True)


def _server_cookie_file():
    """Newest operator cookies file used for all users (server-side login).
    Looked up in, in order: an explicit COOKIES_FILE env path, Render's mounted
    Secret Files dir (/etc/secrets), and the local server_cookies/ folder.
    Only picks files named cookies*.txt so the README isn't mistaken for one."""
    import glob
    env_path = (os.environ.get("COOKIES_FILE") or "").strip()
    if env_path and os.path.isfile(env_path):
        return env_path
    dirs = ["/etc/secrets", SERVER_COOKIES_DIR]   # Render secret files, then local
    files = []
    for d in dirs:
        files += glob.glob(os.path.join(d, "cookies*.txt"))
    files = [f for f in files if os.path.isfile(f)]
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0] if files else None


# --- Rotating proxies (defeat FB/IG throttling) --------------------------- #
# Put one proxy per line in proxies.txt, e.g.  http://user:pass@host:port
# Each request/download picks a random one so the IP keeps changing.
PROXIES_FILE = os.path.join(BASE, "proxies.txt")


def _load_proxies():
    if not os.path.isfile(PROXIES_FILE):
        return []
    out = []
    with open(PROXIES_FILE, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
    return out


def _pick_proxy():
    """A random proxy for this call, or None if none are configured."""
    import random
    proxies = _load_proxies()
    return random.choice(proxies) if proxies else None


JOBS = {}  # job_id -> {status, items:[{url,title,engine,pct,status,file,error}], log}

# Shared worker pool for enumeration (with per-call timeouts). Not a `with`
# block, so a timed-out worker is abandoned instead of joined.
_FETCH_POOL = ThreadPoolExecutor(max_workers=4)


@app.route("/")
def index():
    scf = _server_cookie_file()
    return render_template(
        "index.html",
        content_types=list(dl.CONTENT_TYPES),
        qualities=list(dl.QUALITY_PRESETS),
        browsers=list(dl.COOKIE_BROWSERS),
        default_ct=dl.DEFAULT_CONTENT_TYPE,
        default_q=dl.DEFAULT_QUALITY,
        server_login=os.path.basename(scf) if scf else None,
        proxy_count=len(_load_proxies()),
    )


def _cookies(name):
    return dl.COOKIE_BROWSERS.get(name or "No login")


def _cookies_file(path):
    """Resolve which cookies.txt to use. Prefer the user's own path (local power
    users); otherwise fall back to the operator's server-side cookies so public
    visitors never have to supply anything (ViDownloader-style)."""
    p = (path or "").strip().strip('"')
    if p and os.path.isfile(p):
        return p
    return _server_cookie_file()


@app.route("/fetch", methods=["POST"])
def fetch():
    """Enumerate individual items for a content type + query/URL(s)."""
    d = request.get_json(force=True)
    ct = d.get("content_type", dl.DEFAULT_CONTENT_TYPE)
    query = (d.get("query") or "").strip()
    cookies = _cookies(d.get("cookies"))
    cfile = _cookies_file(d.get("cookies_file"))
    try:
        limit = int(d.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    if not query:
        return jsonify(error="URL ya username daalein."), 400

    items = []
    errors = []
    # Support several URLs / usernames, one per line.
    for line in [x.strip() for x in query.splitlines() if x.strip()]:
        try:
            partial = []
            # Facebook/Instagram enumeration can occasionally hang — run it in a
            # worker with a hard timeout so one bad URL can't freeze the request.
            # NOTE: submit to a shared pool (not a `with` block) — a `with`
            # ThreadPoolExecutor joins the worker on exit, which would defeat the
            # timeout. On timeout we just abandon the worker.
            fut = _FETCH_POOL.submit(dl.scrape, ct, line, cookies_browser=cookies,
                                     limit=limit, cookies_file=cfile,
                                     errors_out=partial, proxy=_pick_proxy())
            items.extend(fut.result(timeout=210))
            if partial:
                errors.append("List adhoori aa sakti hai (rate-limit): "
                              + partial[0][:120])
        except FuturesTimeout:
            errors.append(f"{line[:50]} → time out (FB/IG ne jawab nahi diya, "
                          "dobara try karein ya cookies check karein)")
        except Exception as e:
            errors.append(f"{line[:60]} → {e}")
    # de-dup by url, keep order
    seen, uniq = set(), []
    for it in items:
        u = it.get("url")
        if u and u not in seen:
            seen.add(u)
            uniq.append({"url": u, "title": it.get("title") or u,
                         "engine": it.get("engine", "video"),
                         "referer": it.get("referer")})
    if not uniq:
        msg = "Kuch nahi mila."
        if errors:
            msg += " (" + " | ".join(errors[:3]) + ")"
        return jsonify(error=msg), 400
    return jsonify(items=uniq, errors=errors)


@app.route("/download", methods=["POST"])
def download():
    d = request.get_json(force=True)
    items = d.get("items") or []
    if not items:
        return jsonify(error="Koi item select nahi kiya."), 400
    quality = d.get("quality", dl.DEFAULT_QUALITY)
    cookies = _cookies(d.get("cookies"))
    cfile = _cookies_file(d.get("cookies_file"))
    out_folder = (d.get("out_folder") or "").strip()
    try:
        threads = max(1, min(6, int(d.get("threads") or 3)))
    except (TypeError, ValueError):
        threads = 3

    job = uuid.uuid4().hex[:12]
    for i, it in enumerate(items):
        it["index"] = i
        it["pct"] = "0%"
        it["status"] = "queued"
        it["file"] = None
    JOBS[job] = {"status": "running", "items": items, "log": [],
                 "outdir": os.path.join(OUTPUTS, job), "out_folder": out_folder}
    threading.Thread(target=_run, args=(job, quality, cookies, threads, cfile),
                     daemon=True).start()
    return jsonify(job=job)


def _newest_media(folder):
    """Pick the produced media file in a per-item folder (largest, recursive)."""
    best, best_sz = None, -1
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if f.endswith(".part") or f.endswith(".ytdl"):
                continue
            p = os.path.join(root, f)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            if sz > best_sz:
                best, best_sz = p, sz
    return best


def _run(job, quality, cookies, threads, cfile=None):
    j = JOBS[job]

    def log(m):
        j["log"].append(str(m))

    def dl_item(it):
        it["status"] = "downloading"
        itemdir = os.path.join(j["outdir"], str(it["index"]))
        os.makedirs(itemdir, exist_ok=True)

        def hook(d):
            if isinstance(d, dict):
                if d.get("status") == "finished":
                    it["pct"] = "100%"
                else:
                    p = (d.get("_percent_str") or "").strip()
                    if not p and d.get("total_bytes"):
                        p = f"{d['downloaded_bytes'] / d['total_bytes'] * 100:.1f}%"
                    it["pct"] = re.sub(r"\x1b\[[0-9;]*m", "", p) or it["pct"]
            else:  # http/gallery hooks pass a plain string
                it["pct"] = str(d)

        try:
            eng = it.get("engine", "video")
            url = it["url"]
            px = _pick_proxy()             # rotate IP per item
            # gallery-dl marks "let yt-dlp fetch this" media with a ytdl: prefix
            # (common for Instagram/Facebook reels). Strip it and use yt-dlp.
            if url.startswith("ytdl:"):
                url, eng = url[5:], "video"
            if eng == "cobalt":
                dl.download_cobalt(url, itemdir, progress_hook=hook, proxy=px)
            elif eng == "http":
                dl.download_http(url, itemdir, progress_hook=hook,
                                 referer=it.get("referer"), cookies_browser=cookies,
                                 cookies_file=cfile, proxy=px)
            elif eng in ("gallery", "gallery-single"):
                dl.download_gallery(url, itemdir,
                                    on_file=lambda n: it.__setitem__("pct", f"{n} files"),
                                    cookies_browser=cookies, cookies_file=cfile, proxy=px)
            else:
                dl.download_one(url, quality, itemdir, hook,
                                cookies_browser=cookies, cookies_file=cfile, proxy=px)
            f = _newest_media(itemdir)
            if not f:
                raise RuntimeError("No file produced (private/blocked?).")
            it["file"] = os.path.relpath(f, j["outdir"]).replace("\\", "/")
            it["name"] = os.path.basename(f)
            it["pct"] = "100%"
            it["status"] = "done"
            _copy_out(j, f, log)
            log(f"OK  {it['name']}")
        except Exception as e:
            it["status"] = "error"
            it["error"] = str(e)[:200]
            log(f"ERR {it['url'][:60]} → {e}")

    try:
        with ThreadPoolExecutor(max_workers=threads) as ex:
            list(ex.map(dl_item, j["items"]))
    except Exception as e:
        log("FATAL: " + str(e))
        log(traceback.format_exc()[-800:])
    j["status"] = "done"


def _copy_out(j, filepath, log):
    of = j.get("out_folder")
    if not of:
        return
    try:
        os.makedirs(of, exist_ok=True)
        shutil.copy(filepath, os.path.join(of, os.path.basename(filepath)))
    except Exception as e:
        log("Output folder copy failed: " + str(e))


@app.route("/status/<job>")
def status(job):
    j = JOBS.get(job)
    if not j:
        return jsonify(error="unknown job"), 404
    items = [{"index": it["index"], "title": it.get("title"), "pct": it.get("pct"),
              "status": it.get("status"), "file": it.get("file"),
              "name": it.get("name"), "error": it.get("error")}
             for it in j["items"]]
    return jsonify(status=j["status"], items=items, log=j["log"][-80:])


@app.route("/file/<job>/<path:name>")
def file(job, name):
    return send_from_directory(os.path.join(OUTPUTS, job), name, as_attachment=True)


if __name__ == "__main__":
    # Local run; on a cloud host gunicorn serves `app` and sets $PORT.
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, threaded=True)
