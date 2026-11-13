"""Download API backend for @alirzzabot on Render (free web service).

Same /info + /file contract as the Railway ytdlp-service, so the
trendify-agent Worker can point at this service instead:
  GET /            -> {"ok": true}
  GET /info?url=..&token=..   -> yt-dlp metadata + formats
  GET /file?url=..&quality=720&token=.. -> downloads, merges to mp4,
        and serves the file itself (link delivery; the file is deleted
        after serving — Render free disk is ephemeral anyway).
        quality=mp3 -> best audio extracted to MP3 192k (SoundCloud,
        YouTube audio). Video qualities: 360/480/720/1080 (default 720);
        direct-media links use 1080 for the highest available quality.
  GET /dlall?url=..&limit=200&token=.. -> flat playlist listing of a
        TikTok/Instagram profile: {"count": N, "videos": [{id, title,
        url, uploader, duration}]} — no media downloaded.
  GET /jav?code=MIDA-860&token=.. -> studio-code JAV lookup across
        123av + javmost: returns the first VERIFIED direct HLS stream
        {"code","title","source","stream_url","height","duration",
        "page_url","embed_url"} (a JAV file is 1-3 GB, over the /file
        cap, so the direct stream link is returned for browser/IDM
        download instead of a served file).

Auth: ?token=<TOKEN env>. No proxy: Render egress is direct.
YouTube cookies (optional): /etc/secrets/youtube_cookies.txt
(Render Secret File) — needed when YouTube flags the datacenter IP.
Deno (optional, for YouTube JS challenges): DENO_BIN env or
~/.deno/bin/deno, installed by the build command.
ffmpeg: imageio-ffmpeg bundle (already in requirements.txt).
"""
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import yt_dlp

TOKEN = os.environ.get("TOKEN", "")
PORT = int(os.environ.get("PORT", "10000"))

# Optional VLESS proxy (env VLESS_URL, a vless:// link). sing-box is
# downloaded at build time to sing-box-bin/sing-box; at startup it is
# launched with a generated config and ALL yt-dlp traffic goes through
# its local SOCKS5 (the extractors also call sibling API hosts such as
# tiktokv.com / googlevideo.com that a per-host allowlist would miss).
# Everything falls back to direct if the proxy is down.
VLESS_URL = os.environ.get("VLESS_URL", "")
SINGBOX_BIN = os.environ.get("SINGBOX_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sing-box-bin", "sing-box")
PROXY_SOCKS = "socks5h://127.0.0.1:1080"
PROXY_UP = False


def _parse_vless(url):
    u = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    raw_path = unquote(q.get("path", "/"))
    ed = 0
    path = raw_path
    if "?ed=" in raw_path:
        path, ed_s = raw_path.split("?ed=", 1)
        ed = int(ed_s)
    host = q.get("host", "") or q.get("sni", "")
    sni = q.get("sni", "") or host
    return {"server": u.hostname, "port": u.port or 443, "uuid": u.username,
            "path": path, "ed": ed, "host": host, "sni": sni}


def _start_singbox():
    """Launch sing-box with the VLESS config. Returns True if the local
    SOCKS5 answers, else False (callers fall back to direct)."""
    global PROXY_UP
    if not VLESS_URL or not os.path.exists(SINGBOX_BIN):
        return False
    try:
        v = _parse_vless(VLESS_URL)
        cfg = {
            "log": {"level": "error"},
            "inbounds": [{"type": "socks", "tag": "socks-in",
                          "listen": "127.0.0.1", "listen_port": 1080}],
            "outbounds": [{
                "type": "vless", "tag": "proxy",
                "server": v["server"], "server_port": v["port"],
                "uuid": v["uuid"], "flow": "",
                "tls": {"enabled": True, "server_name": v["sni"],
                        "insecure": True, "alpn": ["http/1.1"],
                        "utls": {"enabled": True, "fingerprint": "chrome"}},
                "transport": {"type": "ws", "path": v["path"],
                              "headers": {"Host": v["host"]},
                              "max_early_data": v["ed"],
                              "early_data_header_name": "Sec-WebSocket-Protocol"},
            }],
        }
        cfg_path = os.path.join(tempfile.gettempdir(), "sing-box-render.json")
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)
        os.chmod(cfg_path, 0o600)
        subprocess.Popen([SINGBOX_BIN, "run", "-c", cfg_path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            try:
                with socket.create_connection(("127.0.0.1", 1080), timeout=1):
                    PROXY_UP = True
                    return True
            except OSError:
                time.sleep(1)
    except Exception:
        pass
    return False


def _proxy_for(url):
    # Route ALL yt-dlp traffic through the proxy when it is up: the
    # extractors also call sibling API hosts (tiktokv.com, googlevideo.com)
    # that a per-host allowlist would miss and that fail direct from
    # Render's IP. Falls back to direct when the proxy is down.
    return PROXY_SOCKS if PROXY_UP else None

YOUTUBE_COOKIES = "/etc/secrets/youtube_cookies.txt"
INSTAGRAM_COOKIES = "/etc/secrets/instagram_cookies.txt"

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = None

DENO_BIN = os.environ.get("DENO_BIN") or os.path.expanduser("~/.deno/bin/deno")
JS_RUNTIMES = {"deno": {"path": DENO_BIN}} if os.path.exists(DENO_BIN) else None


def _base_opts():
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 30, "retries": 2}
    # VM-local egress proxy MITMs TLS (self-signed chain); Render egress is
    # direct, so this stays OFF there unless explicitly enabled.
    if os.environ.get("YTDLP_COMPAT_NO_CERTIFI") == "1":
        opts["compat_opts"] = ["no-certifi"]
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
    if JS_RUNTIMES:
        opts["js_runtimes"] = JS_RUNTIMES
    return opts


def _cookies_for(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if ("youtube.com" in host or "youtu.be" in host) and os.path.exists(YOUTUBE_COOKIES):
        return YOUTUBE_COOKIES
    if ("instagram.com" in host or "instagr.am" in host) and os.path.exists(INSTAGRAM_COOKIES):
        return INSTAGRAM_COOKIES
    return None


def _normalize_url(url):
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        if host == "pornhub.com" or host.endswith(".pornhub.com"):
            return u._replace(netloc="www.pornhub.org").geturl()
        if host == "pornhub.net" or host.endswith(".pornhub.net"):
            return u._replace(netloc="www.pornhub.org").geturl()
    except Exception:
        pass
    return url


FMT_KEYS = ("format_id", "ext", "height", "width", "fps", "vcodec", "acodec",
            "abr", "tbr", "filesize", "filesize_approx", "protocol", "url", "http_headers")
MAX_FILE_BYTES = 1024 * 1024 * 1024  # 1 GB cap


def extract(url):
    url = _normalize_url(url)
    last_err = None
    for _ in range(3):
        try:
            opts = _base_opts()
            opts["skip_download"] = True
            opts["socket_timeout"] = 20
            opts["proxy"] = _proxy_for(url)
            ck = _cookies_for(url)
            if ck:
                opts["cookiefile"] = ck
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except Exception as e:
            last_err = e
            time.sleep(2)
    else:
        raise last_err
    formats = [{k: f.get(k) for k in FMT_KEYS}
               for f in (info.get("formats") or []) if f.get("url")]
    return {"id": info.get("id"), "title": info.get("title"),
            "uploader": info.get("uploader"), "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"), "webpage_url": info.get("webpage_url"),
            "url": info.get("url"), "formats": formats}


FORMAT_FOR_QUALITY = {
    "360": "bv*[height<=360]+ba/b[height<=360]/b",
    "480": "bv*[height<=480]+ba/b[height<=480]/b",
    "720": "bv*[height<=720]+ba/b[height<=720]/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]/b",
}
DEFAULT_FMT = "bv*[height<=720]+ba/b[height<=720]/b"


def list_profile(url, limit):
    """Flat playlist listing of a TikTok/Instagram profile: video ids,
    titles and page URLs only — no media downloaded. Used by /dlall.
    Tries the proxy first, then direct: TikTok's profile page rejects
    some egress IPs with an empty response while video pages pass."""
    url = _normalize_url(url)
    opts = _base_opts()
    opts["noplaylist"] = False  # _base_opts sets noplaylist=True for single
    # videos; a profile IS a playlist — without this the extractor parses
    # the page as one video and fails with "Failed to parse JSON".
    opts["extract_flat"] = True
    opts["playlistend"] = limit
    opts["skip_download"] = True
    last_err = None
    for proxy in (_proxy_for(url), None):
        opts["proxy"] = proxy
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except Exception as e:
            last_err = e
            continue
    else:
        raise last_err
    videos = []
    for e in (info.get("entries") or []):
        if not e:
            continue
        videos.append({"id": e.get("id"), "title": e.get("title"),
                       "url": e.get("webpage_url") or e.get("url") or "",
                       "uploader": e.get("uploader"),
                       "duration": e.get("duration")})
    return {"count": len(videos), "videos": videos}


# ---------------------------------------------------------------------------
# JAV downloader (Part 1) — studio code -> highest-quality direct stream link.
# Only professional STUDIO codes (e.g. MIDA-860, SSIS-001). Amateur/FC2
# sections are never touched; the resolver only opens the studio-code page.
# v1 sources (each is a plug-in resolver; more sites add a new function):
#  1) 123av.com — the video page embeds a player JSON with the episode
#     (file-host embed) URL; the embed host exposes /stream?id=<hash>
#     returning {"media": {"stream": "<m3u8>"}}. The m3u8 is the HIGHEST
#     quality that source offers (720p for MIDA-860). Verified live
#     2026-11-13 from the VM (full 151.5-min stream downloaded, valid
#     MPEG-TS, ffprobe-confirmed).
#  2) javmost.ws — the page has part buttons; a form-POST to
#     /ri3123o235r/ returns file-host embed URLs (DoodStream etc.).
#     v1 returns the embed URL as a fallback link (browser-playable);
#     resolving those hosts to a direct stream is a future step.
# A JAV video is typically 1-3 GB — over the 1 GB /file cap and over
# Telegram's 2 GB limit — so /jav returns the direct stream LINK
# (HLS .m3u8) for the user to download with a browser/IDM; it does NOT
# download+serve the file. The link is resolved fresh per request, so
# its CDN token is fresh when the bot sends it.
# Egress: the JAV hosts Cloudflare-challenge datacenter IPs, so each
# fetch tries DIRECT first, then the VLESS SOCKS proxy. The response
# reports which path worked ("via") for diagnostics.
# ---------------------------------------------------------------------------
try:
    from curl_cffi import requests as _cf_requests
except Exception:
    _cf_requests = None

_JAV_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_JAV_CODE_RE = re.compile(r"^[A-Za-z]{2,8}-?\d{2,6}$")


def _jav_fetch(url, headers=None, timeout=25):
    """GET with curl_cffi (Chrome impersonation). Tries direct first,
    then the VLESS SOCKS proxy. Returns (text, via). Raises on failure."""
    if _cf_requests is None:
        raise RuntimeError("curl_cffi not installed")
    h = {"User-Agent": _JAV_UA}
    if headers:
        h.update(headers)
    attempts = [(None, "direct")]
    if PROXY_UP:
        attempts.append(({"http": PROXY_SOCKS, "https": PROXY_SOCKS}, "proxy"))
    last_err = None
    for proxies, via in attempts:
        try:
            r = _cf_requests.get(url, impersonate="chrome", proxies=proxies,
                                 headers=h, timeout=timeout)
            if r.status_code == 200:
                return r.text, via
            last_err = RuntimeError(f"HTTP {r.status_code} via {via}")
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError("fetch failed")


def _jav_post_form(url, data, headers=None, timeout=25):
    """Form-POST with curl_cffi. Same direct-then-proxy order."""
    if _cf_requests is None:
        raise RuntimeError("curl_cffi not installed")
    h = {"User-Agent": _JAV_UA}
    if headers:
        h.update(headers)
    attempts = [(None, "direct")]
    if PROXY_UP:
        attempts.append(({"http": PROXY_SOCKS, "https": PROXY_SOCKS}, "proxy"))
    last_err = None
    for proxies, via in attempts:
        try:
            r = _cf_requests.post(url, data=data, impersonate="chrome",
                                  proxies=proxies, headers=h, timeout=timeout)
            if r.status_code == 200:
                return r.text, via
            last_err = RuntimeError(f"HTTP {r.status_code} via {via}")
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError("post failed")


def _jav_123av(code):
    """Resolve a studio code on 123av.com to a verified HLS stream."""
    import html as _html
    slug = code.strip().lower()
    page_url = f"https://123av.com/en/v/{slug}"
    html_text, via = _jav_fetch(page_url)
    m = re.search(r"<title>([^<]*)</title>", html_text)
    title = m.group(1).strip() if m else code.upper()
    # The title is HTML-entity encoded TWICE (&amp;quot;), so unescape
    # until it stops changing.
    for _ in range(3):
        t2 = _html.unescape(title)
        if t2 == title:
            break
        title = t2
    title = re.sub(r"\s*[—–-]\s*123AV\s*$", "", title).strip()
    pm = re.search(r"player\(JSON\.parse\('(.*?)'\)", html_text, re.S)
    if not pm:
        raise RuntimeError("no player data on 123av page")
    # The page stores the episode JSON as a JS string literal, so it is
    # escaped TWICE: \u0022 for quotes (JS level) inside JSON. Decode the
    # JS string first, then parse the JSON — exactly what the browser
    # does with JSON.parse('...').
    try:
        episodes = json.loads(json.loads('"' + pm.group(1) + '"'))
    except Exception:
        try:
            episodes = json.loads(pm.group(1).replace("\\u0022", '"')
                                  .replace("\\/", "/"))
        except Exception:
            raise RuntimeError("player data is not valid JSON")
    if not episodes or not isinstance(episodes, list):
        raise RuntimeError("no episodes on 123av page")
    last_err = None
    for ep in episodes:
        embed_url = (ep or {}).get("url") or ""
        if not embed_url.startswith("http"):
            continue
        try:
            return _jav_123av_embed(code, page_url, title, embed_url, via)
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError("no playable episode on 123av")


def _jav_123av_embed(code, page_url, title, embed_url, via):
    """Given a 123av episode embed URL, resolve its /stream API to the
    verified master .m3u8 and read height/duration from the playlist."""
    from urllib.parse import urlencode, parse_qsl
    eu = urlparse(embed_url)
    m = re.search(r"/e/([A-Za-z0-9_]+)", eu.path)
    if not m:
        raise RuntimeError("embed URL has no /e/<hash>")
    q = dict(parse_qsl(eu.query))
    q["id"] = m.group(1)
    stream_api = f"{eu.scheme}://{eu.netloc}/stream?{urlencode(q)}"
    body, via2 = _jav_fetch(stream_api, headers={"Referer": embed_url})
    try:
        media = json.loads(body).get("media") or {}
        stream_url = media.get("stream") or ""
    except Exception:
        stream_url = ""
    if not stream_url.startswith("http"):
        raise RuntimeError("embed /stream returned no stream URL")
    master, _ = _jav_fetch(stream_url, headers={"Referer": embed_url})
    if "#EXTM3U" not in master:
        raise RuntimeError("stream URL is not an HLS playlist")
    height = None
    bandwidth = None
    variant = None
    vm = re.search(r"#EXT-X-STREAM-INF:([^\n]*)\n([^\n]+)", master)
    if vm:
        attrs = vm.group(1)
        rm = re.search(r"RESOLUTION=\d+x(\d+)", attrs)
        bm = re.search(r"BANDWIDTH=(\d+)", attrs)
        height = int(rm.group(1)) if rm else None
        bandwidth = int(bm.group(1)) if bm else None
        variant = vm.group(2).strip()
    duration = None
    if variant:
        from urllib.parse import urljoin
        try:
            media_pl, _ = _jav_fetch(urljoin(stream_url, variant),
                                     headers={"Referer": embed_url})
            duration = round(sum(float(x) for x in
                                 re.findall(r"#EXTINF:([\d.]+)", media_pl)))
        except Exception:
            duration = None
    return {"source": "123av", "code": code.upper(), "title": title,
            "page_url": page_url, "embed_url": embed_url,
            "stream_url": stream_url, "height": height,
            "bandwidth": bandwidth, "duration": duration, "via": via2}


def _jav_javmost(code):
    """Resolve a studio code on javmost.ws to its file-host embed URL.
    v1 returns the embed link only (browser-playable fallback); the
    embed host's direct stream is not resolved here yet."""
    page_url = f"https://www.javmost.ws/{code.strip().upper()}/"
    html_text, via = _jav_fetch(page_url)
    btns = re.findall(
        r"part\('(\d+)','(\d+)',this,'(parent|child)','([^']+)','([^']+)','([^']+)'\)",
        html_text)
    if not btns:
        raise RuntimeError("no part buttons on javmost page")
    vm = re.search(r"var YWRzMQo\s*=\s*'([^']+)'", html_text)
    if not vm:
        raise RuntimeError("no YWRzMQo token on javmost page")
    btns.sort(key=lambda b: 0 if b[2] == "parent" else 1)
    last_err = None
    for part, group, _typ, tok1, tok2, tok3 in btns:
        data = {"group": group, "part": part, "code": tok1,
                "code2": tok2, "code3": tok3, "value": vm.group(1),
                "sound": "av"}
        try:
            body, via2 = _jav_post_form(
                "https://www.javmost.ws/ri3123o235r/", data,
                headers={"Referer": page_url,
                         "X-Requested-With": "XMLHttpRequest"})
            d = json.loads(body).get("data") or []
            if d and str(d[0]).startswith("http"):
                return {"source": "javmost", "code": code.upper(),
                        "page_url": page_url, "embed_url": str(d[0]),
                        "via": via2}
            last_err = RuntimeError("javmost returned no embed URL")
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError("javmost lookup failed")


def jav_lookup(code):
    """Try each JAV source in order; return the first VERIFIED direct
    stream (with title/height/duration). Raises RuntimeError with a
    per-source report when nothing verifies."""
    code = (code or "").strip()
    if not _JAV_CODE_RE.match(code):
        raise ValueError("bad JAV code (expected like MIDA-860)")
    tried = []
    try:
        return _jav_123av(code)
    except Exception as e:
        tried.append({"source": "123av", "error": str(e)[:300]})
    try:
        jm = _jav_javmost(code)
        tried.append({"source": "javmost", "embed_url": jm.get("embed_url"),
                      "note": "embed found; direct stream not resolved in v1"})
    except Exception as e:
        tried.append({"source": "javmost", "error": str(e)[:300]})
    err = RuntimeError(f"no verified stream for {code.upper()}")
    err.tried = tried
    raise err


def download_file(url, fmt, audio=False):
    url = _normalize_url(url)
    tmp = tempfile.mkdtemp(prefix="ytdl-")
    try:
        opts = _base_opts()
        opts["format"] = fmt
        opts["proxy"] = _proxy_for(url)
        if audio:
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                       "preferredcodec": "mp3",
                                       "preferredquality": "192"}]
        else:
            opts["merge_output_format"] = "mp4"
        opts["outtmpl"] = os.path.join(tmp, "%(id)s.%(ext)s")
        ck = _cookies_for(url)
        if ck:
            opts["cookiefile"] = ck
        last_err = None
        for _ in range(3):
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                break
            except Exception as e:
                last_err = e
                time.sleep(2)
                for f in os.listdir(tmp):
                    fp = os.path.join(tmp, f)
                    if os.path.isfile(fp):
                        os.remove(fp)
        else:
            raise last_err
        files = [os.path.join(tmp, f) for f in os.listdir(tmp)
                 if os.path.isfile(os.path.join(tmp, f))
                 and not f.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError("download produced no file")
        path = max(files, key=os.path.getsize)
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            raise RuntimeError("file too large")
        title = info.get("title") or info.get("id") or "video"
        return tmp, path, title, size
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, filename, size, content_type="video/mp4", ext="mp4"):
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(size))
        safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename)[:120]
        self.send_header("content-disposition", f'attachment; filename="{safe}.{ext}"')
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, length=1024 * 256)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            return self._send(200, {"ok": True, "proxy": PROXY_UP})
        if u.path not in ("/info", "/file", "/dlall", "/jav"):
            return self._send(404, {"error": "not found"})
        if TOKEN and not hmac.compare_digest(q.get("token", [""])[0], TOKEN):
            return self._send(401, {"error": "unauthorized"})
        if u.path == "/jav":
            code = q.get("code", [""])[0]
            if not code:
                return self._send(400, {"error": "missing code (use ?code=MIDA-860)"})
            try:
                return self._send(200, jav_lookup(code))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                tried = getattr(e, "tried", None)
                body = {"error": str(e)[:500]}
                if tried:
                    body["tried"] = tried
                return self._send(502, body)
        url = q.get("url", [""])[0]
        if not url.startswith(("http://", "https://")):
            return self._send(400, {"error": "bad url"})
        if u.path == "/dlall":
            try:
                limit = int(q.get("limit", ["200"])[0])
            except Exception:
                limit = 200
            limit = max(1, min(200, limit))
            try:
                self._send(200, list_profile(url, limit))
            except Exception as e:
                self._send(502, {"error": str(e)[:500], "proxy": PROXY_UP})
            return
        if u.path == "/info":
            try:
                self._send(200, extract(url))
            except Exception as e:
                self._send(502, {"error": str(e)[:500]})
            return
        quality = q.get("quality", [""])[0]
        if quality == "mp3":
            try:
                tmp, path, title, size = download_file(url, "ba/bestaudio", audio=True)
            except Exception as e:
                return self._send(502, {"error": str(e)[:500]})
            try:
                self._send_file(path, title, size, "audio/mpeg", "mp3")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            return
        fmt = FORMAT_FOR_QUALITY.get(quality, DEFAULT_FMT)
        try:
            tmp, path, title, size = download_file(url, fmt)
        except Exception as e:
            return self._send(502, {"error": str(e)[:500]})
        try:
            self._send_file(path, title, size)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    _start_singbox()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
