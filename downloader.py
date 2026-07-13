"""
downloader.py — download engine wrapper for the Bulk Video Downloader.

Two general-purpose engines, picked automatically per URL:
  * yt-dlp    -> videos (YouTube, TikTok, generic sites, FB/IG video, …)
  * gallery-dl -> images & galleries (Instagram photos, Pinterest pins,
                  Facebook photos, …)

Responsibilities:
  * Locate ffmpeg (merge video+audio / extract mp3 for the video engine).
  * Build yt-dlp options for a chosen quality preset.
  * Turn a channel / username / playlist URL into a flat list of individual
    entries (so they can be shown in the queue and "select all"ed).
  * Download a single video URL, or a whole gallery, with progress callbacks.
  * Optionally use the user's browser login cookies (needed for private or
    login-walled content on Instagram / Facebook / Pinterest).

This module contains NO site-specific scraping and NO DRM handling. It relies
entirely on the public, general-purpose extractors of yt-dlp and gallery-dl and
works on openly-served (non-DRM) content only. What you point it at, and whether
you have the right to download it, is your responsibility.
"""

import os
import re
import sys
import glob
import json
import time
import shutil
import webbrowser
from urllib.parse import urlparse, quote, unquote

from yt_dlp import YoutubeDL


# --------------------------------------------------------------------------- #
# ffmpeg discovery
# --------------------------------------------------------------------------- #
def find_ffmpeg_dir():
    """Return the directory containing ffmpeg.exe, or None if not found."""
    # 1. Bundled alongside the frozen exe (PyInstaller unpacks to _MEIPASS).
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled and os.path.exists(os.path.join(bundled, "ffmpeg.exe")):
        return bundled

    # 2. On PATH.
    onpath = shutil.which("ffmpeg")
    if onpath:
        return os.path.dirname(onpath)

    # 3. Common winget install location (Gyan.FFmpeg package).
    base = os.path.join(
        os.path.expanduser("~"),
        "AppData", "Local", "Microsoft", "WinGet", "Packages",
    )
    hits = glob.glob(
        os.path.join(base, "Gyan.FFmpeg*", "**", "ffmpeg.exe"),
        recursive=True,
    )
    if hits:
        return os.path.dirname(hits[0])
    return None


FFMPEG_DIR = find_ffmpeg_dir()


# --------------------------------------------------------------------------- #
# Quality presets  (label shown in the UI  ->  yt-dlp format string)
# --------------------------------------------------------------------------- #
QUALITY_PRESETS = {
    "Best available": "bestvideo*+bestaudio/best",
    "4K (2160p)": "bestvideo[height<=2160]+bestaudio/best[height<=2160]/best",
    "1440p": "bestvideo[height<=1440]+bestaudio/best[height<=1440]/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480p":  "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "Audio only (mp3)": "bestaudio/best",
}
# The trailing "/best" fallback matters for Instagram/Facebook: their reels don't
# expose height-filtered streams, so a strict height cap alone would error out;
# the fallback grabs the single available stream instead of failing.
# "Best available" grabs the highest resolution the source offers (4K/8K if
# present); the numbered presets cap at that height.
DEFAULT_QUALITY = "Best available"


PLATFORMS = ["YouTube", "TikTok", "Instagram", "Facebook", "Pinterest"]

# Browser choices for the "login" cookies option (label -> yt-dlp/gallery-dl
# browser name). Needed to reach private / login-walled content.
COOKIE_BROWSERS = {
    "No login": None,
    "Chrome": "chrome",
    "Edge": "edge",
    "Firefox": "firefox",
    "Brave": "brave",
    "Opera": "opera",
}


# Platforms that are login-walled — content usually needs the user to be
# logged in, so we open their login page and read the browser's cookies.
LOGIN_WALLED = {"Instagram", "Facebook", "Pinterest"}
LOGIN_URLS = {
    "Instagram": "https://www.instagram.com/accounts/login/",
    "Facebook":  "https://www.facebook.com/login/",
    "Pinterest": "https://www.pinterest.com/login/",
}


def detect_default_browser():
    """Best-effort: return the yt-dlp/gallery-dl name of the default browser."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations"
            r"\UrlAssociations\https\UserChoice")
        progid = winreg.QueryValueEx(key, "ProgId")[0].lower()
        for needle, name in (("firefox", "firefox"), ("brave", "brave"),
                             ("opera", "opera"), ("chrome", "chrome"),
                             ("msedge", "edge"), ("edge", "edge")):
            if needle in progid:
                return name
    except Exception:
        pass
    return "edge"          # Windows' out-of-the-box default


def open_login_page(platform):
    """Open the platform's login page in the user's default browser."""
    url = LOGIN_URLS.get(platform)
    if url:
        try:
            webbrowser.open(url, new=2)
            return True
        except Exception:
            return False
    return False


def build_channel_url(platform, username):
    """Turn a platform + username into a listing URL of that user's content."""
    u = username.strip().lstrip("@")
    if not u:
        return ""
    if platform == "YouTube":
        return f"https://www.youtube.com/@{u}/videos"
    if platform == "TikTok":
        return f"https://www.tiktok.com/@{u}"
    if platform == "Instagram":
        return f"https://www.instagram.com/{u}/"
    if platform == "Facebook":
        return f"https://www.facebook.com/{u}"
    if platform == "Pinterest":
        return f"https://www.pinterest.com/{u}/"
    return username


# --------------------------------------------------------------------------- #
# Engine routing
# --------------------------------------------------------------------------- #
# Hosts that are primarily image / mixed galleries -> gallery-dl handles both
# their photos and videos. Everything else goes to yt-dlp.
_GALLERY_HOSTS = (
    "instagram.com", "pinterest.com", "pinterest.", "pin.it",
    "facebook.com", "fb.watch", "fb.com",
)


# Path markers that identify a *single* video/reel on Facebook (yt-dlp can
# grab these directly, even though it can't enumerate a page's reels list).
_FB_VIDEO_MARKERS = ("/reel/", "/videos/", "/watch", "/story", "/video.php")


# --------------------------------------------------------------------------- #
# Facebook Reels list — no open extractor enumerates a page's reels, so we fetch
# the reels tab HTML ourselves (needs full browser headers + FB login cookies)
# and pull the reel IDs out of it. Each reel then downloads via yt-dlp's single
# facebook:reel extractor. This mirrors how browser-based tools scrape FB.
# --------------------------------------------------------------------------- #
_FB_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
}


def _fb_cookiejar(cookies_file=None, cookies_browser=None):
    import http.cookiejar
    if cookies_file:
        cj = http.cookiejar.MozillaCookieJar()
        cj.load(cookies_file, ignore_discard=True, ignore_expires=True)
        return cj
    if cookies_browser:
        try:
            from yt_dlp.cookies import extract_cookies_from_browser
            jar = extract_cookies_from_browser(cookies_browser)
            cj = http.cookiejar.CookieJar()
            for c in jar:
                cj.set_cookie(c)
            return cj
        except Exception:
            pass
    return None


def _fb_reels_url(query):
    """Turn a username / profile URL into the reels-tab URL."""
    q = query.strip()
    if q.lower().startswith(("http://", "https://")):
        if "/reels" in q.lower():
            return q
        if "profile.php" in q.lower():        # numeric profile -> ?sk=reels_tab
            sep = "&" if "?" in q else "?"
            return q.split("#")[0] + sep + "sk=reels_tab"
        return q.split("?")[0].rstrip("/") + "/reels/"
    return f"https://www.facebook.com/{q.lstrip('@')}/reels/"


def _pw_proxy(proxy):
    """Convert a proxy URL (optionally http://user:pass@host:port) into the dict
    Playwright wants: {server, username, password}."""
    if not proxy:
        return None
    parts = urlparse(proxy if "://" in proxy else "http://" + proxy)
    server = f"{parts.scheme or 'http'}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    d = {"server": server}
    if parts.username:
        d["username"] = unquote(parts.username)
    if parts.password:
        d["password"] = unquote(parts.password)
    return d


def _pw_cookies(cookies_file):
    """Convert a Netscape cookies.txt into Playwright's cookie format."""
    import http.cookiejar
    cj = http.cookiejar.MozillaCookieJar()
    cj.load(cookies_file, ignore_discard=True, ignore_expires=True)
    out = []
    for c in cj:
        out.append({"name": c.name, "value": c.value, "domain": c.domain,
                    "path": c.path or "/",
                    "expires": float(c.expires) if c.expires else -1,
                    "httpOnly": False, "secure": bool(c.secure), "sameSite": "Lax"})
    return out


def _fb_reels_ids_html(query, cookies_file=None, cookies_browser=None, proxy=None):
    """Lightweight fallback: reel IDs present in the reels-tab HTML (~latest 10)."""
    import urllib.request
    cj = _fb_cookiejar(cookies_file, cookies_browser)
    handlers = [urllib.request.HTTPCookieProcessor(cj)] if cj else []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(_fb_reels_url(query), headers=_FB_HEADERS)
    html = opener.open(req, timeout=45).read().decode("utf-8", "ignore")
    seen, ids = set(), []
    for rid in re.findall(r"/reel/(\d+)", html):
        if rid not in seen:
            seen.add(rid)
            ids.append(rid)
    return ids


def _fb_reels_ids_browser(query, cookies_file, limit=0, time_budget=170, log=None,
                          proxy=None):
    """Scroll the reels tab in a real headless browser (like ViDownloader) to load
    ALL reels — not just the initial ~10. Returns an ordered reel-id list.
    Reel IDs are read from the whole page each round (page HTML retains the ids
    from FB's loaded GraphQL data — more complete than the visible anchors, which
    FB virtualises away)."""
    import time
    from playwright.sync_api import sync_playwright
    url = _fb_reels_url(query)
    seen, order, titles = set(), [], {}
    t0 = time.time()
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, proxy=_pw_proxy(proxy))
        try:
            # force English locale so the reels page renders the same regardless
            # of the account's FB language (reel IDs are numeric either way, but
            # this keeps layout/scroll behaviour consistent)
            ctx = b.new_context(user_agent=_FB_HEADERS["User-Agent"],
                                locale="en-US",
                                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
            if cookies_file:
                ctx.add_cookies(_pw_cookies(cookies_file))
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(3000)
            # JS: reel-id -> caption from the currently rendered cards (aria-label /
            # thumbnail alt / link text). Collected each round before FB virtualises
            # the cards away, so most reels get a readable title.
            JS_TITLES = (
                "Array.from(document.querySelectorAll(\"a[href*='/reel/']\"))"
                ".map(a=>{const m=a.href.match(/\\/reel\\/(\\d+)/); if(!m) return null;"
                "const img=a.querySelector('img');"
                "const t=(a.getAttribute('aria-label')||(img&&img.getAttribute('alt'))"
                "||a.innerText||'').trim().replace(/\\s+/g,' ');"
                "return [m[1], t.slice(0,140)];}).filter(x=>x&&x[1])")
            stall = 0
            while True:
                for rid in re.findall(r"/reel/(\d+)", pg.content()):
                    if rid not in seen:
                        seen.add(rid)
                        order.append(rid)
                try:
                    for rid, cap in pg.evaluate(JS_TITLES):
                        if cap and rid not in titles:
                            titles[rid] = cap
                except Exception:
                    pass
                if limit and len(order) >= limit:
                    break
                if time.time() - t0 > time_budget:
                    break
                before = len(order)
                # real wheel events trigger FB's infinite scroll (a programmatic
                # window.scrollTo does NOT — FB ignores it and stops loading).
                # Two nudges + a generous wait: FB throttles the reels feed under
                # repeated/fast loading, so give each batch time before deciding
                # it's stalled (else we stop early with far fewer reels).
                pg.mouse.wheel(0, 20000)
                pg.wait_for_timeout(700)
                pg.mouse.wheel(0, 20000)
                pg.wait_for_timeout(2800)
                stall = stall + 1 if len(order) == before else 0
                if stall >= 14:                # very patient: FB throttles mid-scroll
                    break
        finally:
            b.close()
    return order, titles


def facebook_reels(query, cookies_file=None, cookies_browser=None, limit=0,
                   proxy=None):
    """Return a Facebook page/profile's reels as individual {url,title,engine}
    rows (yt-dlp downloads each). Uses a real headless browser to scroll and load
    ALL reels; falls back to the initial-HTML method if Playwright is unavailable."""
    ids, titles = None, {}
    if cookies_file:                       # browser scroll needs the cookies file
        try:
            ids, titles = _fb_reels_ids_browser(query, cookies_file,
                                                limit=limit, proxy=proxy)
        except Exception:
            ids = None
    if not ids:                            # fallback: just the initial ~10
        ids = _fb_reels_ids_html(query, cookies_file, cookies_browser, proxy=proxy)
    if limit and limit > 0:
        ids = ids[:int(limit)]
    return [{"url": f"https://www.facebook.com/reel/{rid}",
             "title": titles.get(rid) or f"Facebook reel {rid}",
             "engine": "video"} for rid in ids]


def pick_engine(url):
    """Return 'gallery' for image/gallery hosts, else 'video'."""
    parts = urlparse(url)
    host = (parts.netloc or "").lower()
    path = (parts.path or "").lower()

    # A single Facebook reel/video URL -> yt-dlp handles it well.
    if "facebook.com" in host or "fb.watch" in host or "fb.com" in host:
        if "fb.watch" in host or any(m in (path or url.lower())
                                     for m in _FB_VIDEO_MARKERS):
            return "video"
        return "gallery"

    if any(h in host for h in _GALLERY_HOSTS):
        return "gallery"
    return "video"


# --------------------------------------------------------------------------- #
# Content types  (dropdown label -> how to turn a username/query into a source)
# --------------------------------------------------------------------------- #
# engine:
#   "video"   -> yt-dlp enumerates individual videos
#   "gallery" -> gallery-dl enumerates individual photos/reels
# engine: "video" (yt-dlp) | "gallery" (gallery-dl) | "auto" (pick by host)
# media:  optional filter applied to gallery results — "video" | "image"
CONTENT_TYPES = {
    "TikTok Videos":      {"engine": "video",
                            "url": lambda u: f"https://www.tiktok.com/@{u}"},
    "YouTube Videos":     {"engine": "video",
                            "url": lambda u: f"https://www.youtube.com/@{u}/videos"},
    "YouTube Shorts":     {"engine": "video",
                            "url": lambda u: f"https://www.youtube.com/@{u}/shorts"},
    "Instagram Reels":    {"engine": "gallery", "media": "video",
                            "url": lambda u: f"https://www.instagram.com/{u}/"},
    "Instagram Pictures": {"engine": "gallery", "media": "image",
                            "url": lambda u: f"https://www.instagram.com/{u}/"},
    "Pinterest Pictures": {"engine": "gallery", "media": "image",
                            "url": lambda u: f"https://www.pinterest.com/{u}/"},
    "Search Pinterest":   {"engine": "gallery", "media": "image",
                            "url": lambda q: f"https://www.pinterest.com/search/pins/?q={quote(q)}"},
    "Paste URL (any)":    {"engine": "auto", "url": lambda u: u},
}
DEFAULT_CONTENT_TYPE = "TikTok Videos"

_VIDEO_EXT = (".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi")
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


def _media_kind(item):
    path = urlparse(item.get("url", "")).path.lower()
    if path.endswith(_VIDEO_EXT):
        return "video"
    if path.endswith(_IMAGE_EXT):
        return "image"
    return None


# --------------------------------------------------------------------------- #
# cobalt resolver — a self-hosted cobalt instance turns ANY single video/reel
# link (Instagram / YouTube / Facebook / TikTok / etc.) into a direct media
# URL. It uses guest/embed endpoints, so it works from a datacenter IP with NO
# cookies — the thing yt-dlp/gallery-dl get blocked on. Set COBALT_URL to point
# at your own instance (falls back to the deployed one).
# --------------------------------------------------------------------------- #
COBALT_API = (os.environ.get("COBALT_URL")
              or "https://cobalt-api-cahs.onrender.com").strip().rstrip("/")


def _cobalt_post(page_url, proxy=None, tries=3):
    """POST a link to the cobalt instance; return parsed JSON dict or None.
    Retries a few times: on Render's free plan the instance sleeps after 15 min,
    so the first call may 502/timeout while it wakes (~30-50s). Retrying makes the
    very first user request succeed instead of silently failing."""
    if not COBALT_API:
        return None
    import urllib.request
    body = json.dumps({"url": page_url, "filenameStyle": "basic"}).encode("utf-8")
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    for attempt in range(tries):
        req = urllib.request.Request(
            COBALT_API + "/", data=body, method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     "User-Agent": _UA})
        try:
            with opener.open(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception:
            if attempt < tries - 1:
                time.sleep(5)   # let a sleeping instance boot, then retry
    return None


def cobalt_items(page_url, proxy=None):
    """Resolve a link via cobalt into FETCH-step item(s), or [] if it can't.
    Single media -> one 'cobalt' item (re-resolved fresh at download time so the
    signed URL can't expire). Carousel/picker -> direct 'http' items."""
    data = _cobalt_post(page_url, proxy=proxy)
    if not data:
        return []
    st = data.get("status")
    if st in ("redirect", "tunnel", "stream"):
        return [{"url": page_url, "title": data.get("filename") or page_url,
                 "engine": "cobalt"}]
    if st == "picker":
        out = []
        for i, p in enumerate(data.get("picker") or []):
            u = p.get("url")
            if u:
                out.append({"url": u, "engine": "http",
                            "title": p.get("filename") or f"{page_url}#{i + 1}"})
        return out
    return []


def download_cobalt(page_url, outdir, progress_hook=None, proxy=None):
    """Resolve a single link via cobalt at download time (fresh URL) + fetch it."""
    data = _cobalt_post(page_url, proxy=proxy)
    st = (data or {}).get("status")
    if st in ("redirect", "tunnel", "stream"):
        return download_http(data["url"], outdir, progress_hook=progress_hook,
                             filename=data.get("filename"), proxy=proxy)
    if st == "picker":
        picks = [p.get("url") for p in (data.get("picker") or []) if p.get("url")]
        if picks:
            return download_http(picks[0], outdir, progress_hook=progress_hook, proxy=proxy)
    raise RuntimeError("cobalt could not resolve this link (%s)." % (st or "no response"))


def scrape(content_type, query, cookies_browser=None, limit=0, cookies_file=None,
           errors_out=None, proxy=None):
    """
    Enumerate individual items for a content type + username/URL/query.

    Returns a list of {'url', 'title', 'engine'} dicts (one row per item).
      * video items download via yt-dlp
      * http  items are direct media (image/clip) enumerated by gallery-dl
    """
    ct = CONTENT_TYPES.get(content_type, CONTENT_TYPES["Paste URL (any)"])
    q = query.strip()

    # If the user pasted a full URL, use it verbatim (don't wrap it again).
    if q.lower().startswith(("http://", "https://")):
        url = q
        # cobalt first: fixes Instagram/YouTube/Facebook single links on cloud
        # (datacenter IP + no cookies). Falls through to yt-dlp/gallery-dl if it
        # can't handle the link (e.g. a whole channel/profile to enumerate).
        cob = cobalt_items(url, proxy=proxy)
        if cob:
            return cob
        # For hosts that ONLY work via cobalt on the cloud (Instagram/Facebook —
        # gallery-dl can't enumerate them without cookies), don't fall through to
        # the broken path and show a misleading "kuch nahi mila". Tell the user to
        # retry (usually a cold-start; the retry lands on a woken instance).
        host = urlparse(url).netloc.lower()
        if any(h in host for h in ("instagram.com", "facebook.com", "fb.watch", "fb.com")):
            raise RuntimeError("Server abhi jaag raha hai ya link private/unsupported hai - "
                               "20-30 sec ruk kar dobara 'Fetch' dabayein.")
    else:
        url = ct["url"](q.lstrip("@"))

    engine = ct["engine"]
    if engine == "fb_reels":
        return facebook_reels(q, cookies_file=cookies_file,
                              cookies_browser=cookies_browser, limit=limit,
                              proxy=proxy)
    if engine == "auto":
        engine = pick_engine(url)

    # Any gallery host (Instagram/Pinterest/Facebook) must be enumerated by
    # gallery-dl so we get individual photos/reels, not a single echoed URL.
    if engine == "gallery" or pick_engine(url) == "gallery":
        items = _gallery_enumerate(url, cookies_browser=cookies_browser,
                                   limit=limit, cookies_file=cookies_file,
                                   errors_out=errors_out, proxy=proxy)
        want = ct.get("media")
        if want:
            filtered = [it for it in items if _media_kind(it) == want]
            if filtered:            # only apply if it didn't wipe everything
                items = filtered
        return items
    return expand_source(url, cookies_browser=cookies_browser,
                         cookies_file=cookies_file, proxy=proxy)


# --------------------------------------------------------------------------- #
# Expanding a source (channel / playlist / single video) into video entries
# --------------------------------------------------------------------------- #
def _flat_extract(url, cookies_browser=None, cookies_file=None, proxy=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
    }
    if proxy:
        opts["proxy"] = proxy
    if cookies_file:
        opts["cookiefile"] = cookies_file
    elif cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def expand_source(url, label=None, max_depth=4, cookies_browser=None,
                  cookies_file=None, proxy=None):
    """
    Resolve a URL to a flat list of {'url', 'title', 'engine'} dicts.

    Video hosts: a channel *root* URL expands into its Videos / Shorts / Live
    tabs -> individual videos; playlists expand fully; a single video -> one
    entry. Nested playlists that aren't inlined are re-extracted; unavailable
    items are skipped and duplicates removed.

    Gallery hosts are handled by scrape() via gallery-dl, not here.
    """
    info = _flat_extract(url, cookies_browser=cookies_browser,
                         cookies_file=cookies_file, proxy=proxy)
    results = []
    seen = set()

    def add_leaf(node):
        link = node.get("url") or node.get("webpage_url")
        if not link or link in seen:
            return
        seen.add(link)
        results.append({
            "url": link,
            "title": node.get("title") or node.get("id") or link,
            "engine": "video",
        })

    def walk(node, depth):
        if not node:
            return
        entries = node.get("entries")
        if entries:
            for child in entries:
                walk(child, depth + 1)
            return
        # A playlist/channel node that wasn't inlined -> re-extract it.
        if node.get("_type") == "playlist" and depth < max_depth:
            link = node.get("webpage_url") or node.get("url")
            if link:
                walk(_flat_extract(link, cookies_browser=cookies_browser,
                                   cookies_file=cookies_file, proxy=proxy), depth + 1)
            return
        add_leaf(node)

    walk(info, 0)

    # Fallback: a bare single-video URL that produced no entries.
    if not results and info:
        add_leaf(info)
    return results


# --------------------------------------------------------------------------- #
# Downloading
# --------------------------------------------------------------------------- #
def build_ydl_opts(quality, outdir, progress_hook, cookies_browser=None,
                   cookies_file=None, proxy=None):
    fmt = QUALITY_PRESETS.get(quality, QUALITY_PRESETS[DEFAULT_QUALITY])
    opts = {
        "format": fmt,
        "outtmpl": os.path.join(outdir, "%(title).200B.%(ext)s"),
        "progress_hooks": [progress_hook],
        "ignoreerrors": True,
        "noplaylist": True,          # each queue item is already a single video
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "restrictfilenames": False,
        "windowsfilenames": True,
    }
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    if proxy:
        opts["proxy"] = proxy
    if cookies_file:
        opts["cookiefile"] = cookies_file          # a cookies.txt path wins
    elif cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)

    if quality.startswith("Audio"):
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        opts["merge_output_format"] = "mp4"
    return opts


def download_one(url, quality, outdir, progress_hook, cookies_browser=None,
                 cookies_file=None, proxy=None):
    """Download a single video URL. Raises on hard failure; hook reports progress."""
    os.makedirs(outdir, exist_ok=True)
    opts = build_ydl_opts(quality, outdir, progress_hook, cookies_browser,
                          cookies_file=cookies_file, proxy=proxy)
    with YoutubeDL(opts) as ydl:
        ydl.download([url])


# --------------------------------------------------------------------------- #
# Gallery downloading (images + videos on Instagram / Pinterest / Facebook)
# --------------------------------------------------------------------------- #
class GalleryStop(Exception):
    """Raised inside the gallery-dl per-file hook to abort a gallery download."""


def download_gallery(url, outdir, on_file=None, stop_check=None,
                     cookies_browser=None, cookies_file=None, proxy=None):
    """
    Download every photo/video in a gallery/profile via gallery-dl (in-process,
    so it works inside the frozen exe). Returns the number of files downloaded.

    on_file(count) is called after each file; stop_check() -> True aborts.
    Files land under <outdir>/<site>/… (gallery-dl's own tidy folder layout).
    """
    from gallery_dl import job, config

    os.makedirs(outdir, exist_ok=True)
    config.load()                       # honour any user gallery-dl config
    config.set((), "base-directory", outdir)
    config.set(("output",), "mode", "null")
    if proxy:
        config.set(("extractor",), "proxy", proxy)
    if cookies_file:
        config.set(("extractor",), "cookies", cookies_file)   # path string
    elif cookies_browser:
        config.set(("extractor",), "cookies", [cookies_browser])

    class _CountingJob(job.DownloadJob):
        count = 0

        def handle_url(self, u, kwdict):
            if stop_check and stop_check():
                raise GalleryStop()
            super().handle_url(u, kwdict)
            _CountingJob.count += 1
            if on_file:
                on_file(_CountingJob.count)

    _CountingJob.count = 0
    _CountingJob(url).run()
    return _CountingJob.count


# --------------------------------------------------------------------------- #
# Gallery enumeration (list individual photos/clips as separate rows)
# --------------------------------------------------------------------------- #
def _gallery_enumerate(url, cookies_browser=None, limit=0, cookies_file=None,
                       errors_out=None, proxy=None):
    """
    Use gallery-dl to list every media item under a profile/board/search as
    individual {'url', 'title', 'engine': 'http'} rows (direct media URLs).
    Raises RuntimeError with gallery-dl's message if extraction fails (e.g.
    login required), so the UI can show a helpful hint. If some items were
    found but pagination hit an error (e.g. IG rate-limit), the list is
    returned as-is and the error text is appended to errors_out (if given).
    """
    from gallery_dl import job, config

    config.load()
    # Throttle so Instagram doesn't rate-limit pagination and silently truncate a
    # big profile. Facebook's extractor already makes many requests per post (it's
    # naturally slow), and an extra sleep pushed 8 items to ~12 min — so for FB we
    # skip the throttle. Host-aware.
    fb = any(h in url.lower() for h in ("facebook.com", "fb.com", "fb.watch"))
    config.set(("extractor",), "sleep-request", 0 if fb else "1.0-2.5")
    config.set(("extractor",), "retries", 4)
    if proxy:
        config.set(("extractor",), "proxy", proxy)
    if limit and limit > 0:
        config.set(("extractor",), "image-range", f"1-{int(limit)}")
    if cookies_file:
        config.set(("extractor",), "cookies", cookies_file)   # path string
    elif cookies_browser:
        config.set(("extractor",), "cookies", [cookies_browser])

    items = []
    errors = []
    seen = set()
    CAP = 3000          # safety cap so a huge profile can't run away

    import io

    def run(u, depth):
        if len(items) >= CAP:
            return
        dj = job.DataJob(u, file=io.StringIO())    # suppress stdout JSON dump
        dj.run()
        for msg in (dj.data or []):
            if len(items) >= CAP:
                break
            if not isinstance(msg, (list, tuple)) or not msg:
                continue
            code = msg[0]
            if code == 3 and len(msg) >= 2:             # Url (direct media)
                media = msg[1]
                if media in seen:
                    continue
                seen.add(media)
                kw = msg[2] if len(msg) > 2 else {}
                title = (kw.get("description") or kw.get("caption")
                         or kw.get("content") or kw.get("filename") or media)
                items.append({
                    "url": media,
                    "title": str(title).replace("\n", " ")[:140] or media,
                    "engine": "http",
                    "referer": u,
                })
            elif code == 6 and len(msg) >= 2:           # Queue (sub-page)
                sub = msg[1]
                if sub in seen:
                    continue
                seen.add(sub)
                if depth < 2:
                    run(sub, depth + 1)                 # recurse into sub-page
                else:
                    kw = msg[2] if len(msg) > 2 else {}
                    title = kw.get("title") or kw.get("description") or sub
                    items.append({
                        "url": sub,
                        "title": str(title).replace("\n", " ")[:140] or sub,
                        "engine": "gallery-single",
                    })
            elif code == -1:                            # error tuple
                info = msg[1] if len(msg) > 1 else {}
                errors.append(str(info.get("message", info))
                              if isinstance(info, dict) else str(info))

    run(url, 0)
    if not items and errors:
        raise RuntimeError(errors[0].strip()[:200])
    if errors and errors_out is not None:   # partial list (e.g. rate-limited)
        errors_out.append(errors[0].strip()[:200])
    return items


# --------------------------------------------------------------------------- #
# Direct HTTP download (for individual media enumerated by gallery-dl)
# --------------------------------------------------------------------------- #
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _guess_name(url, default_ext=".jpg"):
    path = unquote(urlparse(url).path)
    name = os.path.basename(path) or "media"
    if "." not in name:
        name += default_ext
    return name


def download_http(url, outdir, progress_hook=None, referer=None,
                  cookies_browser=None, cookies_file=None, proxy=None, filename=None):
    """Stream a direct media URL to disk with byte progress. Returns filepath."""
    import urllib.request
    import http.cookiejar

    os.makedirs(outdir, exist_ok=True)
    headers = {"User-Agent": _UA}
    if referer:
        headers["Referer"] = referer

    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    if cookies_file:
        try:
            cj = http.cookiejar.MozillaCookieJar()
            cj.load(cookies_file, ignore_discard=True, ignore_expires=True)
            handlers.append(urllib.request.HTTPCookieProcessor(cj))
        except Exception:
            pass
    elif cookies_browser:
        try:
            from yt_dlp.cookies import extract_cookies_from_browser
            jar = extract_cookies_from_browser(cookies_browser)
            cj = http.cookiejar.CookieJar()
            for c in jar:
                cj.set_cookie(c)
            handlers.append(urllib.request.HTTPCookieProcessor(cj))
        except Exception:
            pass
    opener = urllib.request.build_opener(*handlers)

    req = urllib.request.Request(url, headers=headers)
    safe_name = re.sub(r'[<>:"/\\|?*]', "_", os.path.basename(filename)) if filename else None
    dest = os.path.join(outdir, safe_name or _guess_name(url))
    # avoid clobbering same-named files
    base, ext = os.path.splitext(dest)
    n = 1
    while os.path.exists(dest):
        dest = f"{base}_{n}{ext}"
        n += 1

    with opener.open(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_hook:
                    pct = f"{done / total * 100:4.1f}%" if total else "…"
                    progress_hook(pct)
    return dest
