"""IPTV playlist + HLS proxy.

Fetches an upstream M3U playlist (with a custom User-Agent), rewrites every
stream URL so it points back at this server, and proxies the streams. HLS
manifests are rewritten on the fly so variant playlists, segments, keys and
init maps are all fetched through the proxy as well.

Endpoints
  GET /playlist.m3u   rewritten playlist (point TiviMate here)
  GET /s/<token>[.ext] proxied stream / manifest / segment
  GET /healthz        liveness check
"""

import asyncio
import base64
import hmac
import logging
import os
import re
import time
from urllib.parse import quote, urljoin, urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web

UPSTREAM_PLAYLIST_URL = os.environ.get("UPSTREAM_PLAYLIST_URL", "").strip()
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 OnDemandIPTV AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.5 Safari/604.1",
)
PLAYLIST_TTL = float(os.environ.get("PLAYLIST_TTL", "60"))  # seconds
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
ACCESS_KEY = os.environ.get("ACCESS_KEY", "").strip()
PORT = int(os.environ.get("PORT", "8080"))
MAX_MANIFEST_BYTES = 8 * 1024 * 1024

log = logging.getLogger("iptv-proxy")

MANIFEST_MAGIC = b"#EXTM3U"
URI_ATTR = re.compile(r'URI="([^"]*)"')
EXT_HINTS = {".m3u8", ".ts", ".m4s", ".mp4", ".aac", ".key", ".vtt", ".mpd"}
PASS_HEADERS = ("Content-Type", "Content-Range", "Accept-Ranges", "Content-Disposition")


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def enc(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def dec(token: str) -> str:
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()


def proxy_base(request: web.Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    return f"{proto}://{request.host}"


def proxy_url(target: str, base: str) -> str:
    """Build the proxied URL for an absolute upstream URL."""
    ext = os.path.splitext(urlparse(target).path)[1].lower()
    suffix = ext if ext in EXT_HINTS else ""  # helps players pick a demuxer
    out = f"{base}/s/{enc(target)}{suffix}"
    if ACCESS_KEY:
        out += "?k=" + quote(ACCESS_KEY, safe="")
    return out


def rewrite_m3u(text: str, origin_url: str, base: str) -> str:
    """Rewrite a channel playlist or HLS manifest so all URLs go via the proxy."""

    def swap(raw: str) -> str:
        raw = raw.strip()
        absolute = urljoin(origin_url, raw)
        if urlparse(absolute).scheme not in ("http", "https"):
            return raw  # data: URIs etc. are left alone
        return proxy_url(absolute, base)

    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            if "URI=" in stripped:
                line = URI_ATTR.sub(lambda m: f'URI="{swap(m.group(1))}"', line)
            out.append(line)
        else:
            out.append(swap(stripped))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
@web.middleware
async def auth(request: web.Request, handler):
    if ACCESS_KEY and request.path != "/healthz":
        supplied = request.query.get("k", "")
        if not hmac.compare_digest(supplied.encode(), ACCESS_KEY.encode()):
            raise web.HTTPForbidden(text="forbidden\n")
    return await handler(request)


# --------------------------------------------------------------------------- #
# Upstream playlist (cached, stale-on-error)
# --------------------------------------------------------------------------- #
class PlaylistCache:
    def __init__(self):
        self.text = None
        self.url = UPSTREAM_PLAYLIST_URL
        self.fetched = 0.0
        self.lock = asyncio.Lock()


async def get_playlist(app: web.Application):
    cache: PlaylistCache = app["playlist_cache"]
    async with cache.lock:
        fresh = cache.text and (time.monotonic() - cache.fetched) < PLAYLIST_TTL
        if fresh:
            return cache.text, cache.url
        try:
            async with app["session"].get(
                UPSTREAM_PLAYLIST_URL,
                headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            ) as r:
                r.raise_for_status()
                body = (await r.read()).decode("utf-8", "replace")
                if MANIFEST_MAGIC.decode() not in body[:1024]:
                    raise ValueError("upstream did not return an M3U playlist")
                cache.text, cache.url = body, str(r.url)
                cache.fetched = time.monotonic()
                log.info("refreshed upstream playlist (%d bytes)", len(body))
        except (ClientError, asyncio.TimeoutError, ValueError) as e:
            if cache.text:
                log.warning("upstream playlist refresh failed (%s); serving stale", e)
                return cache.text, cache.url
            raise web.HTTPBadGateway(text=f"upstream playlist unavailable: {e}\n")
        return cache.text, cache.url


async def playlist(request: web.Request) -> web.Response:
    text, origin = await get_playlist(request.app)
    body = rewrite_m3u(text, origin, proxy_base(request))
    return web.Response(
        text=body,
        content_type="audio/x-mpegurl",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------- #
# Stream / manifest / segment proxy
# --------------------------------------------------------------------------- #
async def read_head(content, want: int = 16) -> bytes:
    """Read at least `want` bytes (or until EOF) so we can sniff the payload."""
    buf = b""
    while len(buf) < want:
        chunk = await content.read(4096)
        if not chunk:
            break
        buf += chunk
    return buf


async def stream(request: web.Request) -> web.StreamResponse:
    token = request.match_info["tok"].split(".")[0]
    try:
        target = dec(token)
    except Exception:
        raise web.HTTPBadRequest(text="bad token\n")
    if urlparse(target).scheme not in ("http", "https"):
        raise web.HTTPBadRequest(text="bad target\n")

    headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]

    try:
        upstream = await request.app["session"].get(target, headers=headers)
    except (ClientError, asyncio.TimeoutError) as e:
        log.warning("upstream connect failed %s: %s", target, e)
        raise web.HTTPBadGateway(text=f"upstream error: {e}\n")

    async with upstream:
        if upstream.status >= 400:
            log.warning("upstream %s -> HTTP %s", target, upstream.status)
            return web.Response(status=upstream.status, text="upstream error\n")

        try:
            head = await read_head(upstream.content)
        except (ClientError, asyncio.TimeoutError) as e:
            raise web.HTTPBadGateway(text=f"upstream read failed: {e}\n")

        # ---- HLS manifest: buffer, rewrite, return -------------------------
        if head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(MANIFEST_MAGIC):
            chunks, size = [head], len(head)
            try:
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_MANIFEST_BYTES:
                        raise web.HTTPBadGateway(text="manifest too large\n")
            except (ClientError, asyncio.TimeoutError) as e:
                raise web.HTTPBadGateway(text=f"upstream read failed: {e}\n")
            text = b"".join(chunks).decode("utf-8", "replace")
            body = rewrite_m3u(text, str(upstream.url), proxy_base(request))
            return web.Response(
                text=body,
                content_type="application/vnd.apple.mpegurl",
                charset="utf-8",
                headers={"Cache-Control": "no-store"},
            )

        # ---- Anything else (segments, keys, raw TS): pass bytes through ----
        resp = web.StreamResponse(status=upstream.status)
        for h in PASS_HEADERS:
            if h in upstream.headers:
                resp.headers[h] = upstream.headers[h]
        if "Content-Length" in upstream.headers and not upstream.headers.get("Content-Encoding"):
            resp.content_length = int(upstream.headers["Content-Length"])
        await resp.prepare(request)
        try:
            if head:
                await resp.write(head)
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await resp.write(chunk)
            await resp.write_eof()
        except (ConnectionResetError, ClientError, asyncio.TimeoutError):
            pass  # client went away or upstream stalled; nothing more to send
        return resp


async def healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok\n")


async def index(request: web.Request) -> web.Response:
    return web.Response(text=f"IPTV proxy running. Playlist: {proxy_base(request)}/playlist.m3u\n")


# --------------------------------------------------------------------------- #
# App wiring
# --------------------------------------------------------------------------- #
async def on_startup(app: web.Application):
    app["session"] = ClientSession(
        timeout=ClientTimeout(total=None, connect=10, sock_connect=10, sock_read=30),
        connector=TCPConnector(limit=200, ttl_dns_cache=300),
    )
    app["playlist_cache"] = PlaylistCache()


async def on_cleanup(app: web.Application):
    await app["session"].close()


def make_app() -> web.Application:
    app = web.Application(middlewares=[auth])
    app.add_routes(
        [
            web.get("/", index),
            web.get("/healthz", healthz),
            web.get("/playlist.m3u", playlist),
            web.get("/playlist", playlist),
            web.get("/s/{tok}", stream),
        ]
    )
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not UPSTREAM_PLAYLIST_URL:
        raise SystemExit("UPSTREAM_PLAYLIST_URL is required")
    web.run_app(make_app(), host="0.0.0.0", port=PORT, access_log=None)
