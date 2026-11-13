"""Download API backend for @alirzzabot on Render (free web service).

Same /info + /file contract as the Railway ytdlp-service, so the
trendify-agent Worker can point at this service instead:
  GET /            -> {"ok": true}
  GET /info?url=..&token=..   -> yt-dlp metadata + formats
  GET /file?url=..&quality=720&token=.. -> downloads, merges to mp4,
        and serves the file itself (link delivery; the file is deleted
        after serving — Render free disk is ephemeral anyway).

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
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import yt_dlp

TOKEN = os.environ.get("TOKEN", "")
PORT = int(os.environ.get("PORT", "10000"))

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


def download_file(url, fmt):
    url = _normalize_url(url)
    tmp = tempfile.mkdtemp(prefix="ytdl-")
    try:
        opts = _base_opts()
        opts["format"] = fmt
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

    def _send_file(self, path, filename, size):
        self.send_response(200)
        self.send_header("content-type", "video/mp4")
        self.send_header("content-length", str(size))
        safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename)[:120]
        self.send_header("content-disposition", f'attachment; filename="{safe}.mp4"')
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, length=1024 * 256)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            return self._send(200, {"ok": True})
        if u.path not in ("/info", "/file"):
            return self._send(404, {"error": "not found"})
        if TOKEN and not hmac.compare_digest(q.get("token", [""])[0], TOKEN):
            return self._send(401, {"error": "unauthorized"})
        url = q.get("url", [""])[0]
        if not url.startswith(("http://", "https://")):
            return self._send(400, {"error": "bad url"})
        if u.path == "/info":
            try:
                self._send(200, extract(url))
            except Exception as e:
                self._send(502, {"error": str(e)[:500]})
            return
        quality = q.get("quality", [""])[0]
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
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
