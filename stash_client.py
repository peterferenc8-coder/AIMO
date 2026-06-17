"""
stash_client.py
---------------
Thin client for a Stash media server (https://stashapp.cc).

Responsibilities:
  - Query scenes carrying a configured tag via the GraphQL API.
  - Pick a random playable scene (preferring interactive ones, which carry a
    funscript).
  - Fetch a scene's funscript JSON (for driving the device).
  - Open a scene's video stream for proxying through Flask (so the Stash API
    key never reaches the browser, and Range requests still work for seeking).

Uses only the standard library so no new dependency is required.

Optionally tunnels every request through a SOCKS5 proxy (configured in the
Settings tab) — useful when the Stash server is only reachable via a proxy.
The SOCKS5 handshake is implemented with the stdlib socket module so no extra
dependency (e.g. PySocks) is needed.
"""

from __future__ import annotations

import functools
import http.client
import json
import logging
import random
import socket
import threading
import urllib.error
import urllib.request
from typing import Any, Optional

log = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15.0


# ── SOCKS5 proxy support (stdlib only) ─────────────────────────────────────────

def parse_proxy_address(address: str) -> Optional[tuple[str, int]]:
    """Parse a ``host:port`` proxy address into ``(host, port)`` or None."""
    address = (address or "").strip()
    if not address:
        return None
    # Tolerate an accidental scheme prefix like "socks5://".
    if "://" in address:
        address = address.split("://", 1)[1]
    if ":" not in address:
        return None
    host, _, port = address.rpartition(":")
    host = host.strip()
    try:
        port_num = int(port.strip())
    except ValueError:
        return None
    if not host or not (0 < port_num < 65536):
        return None
    return host, port_num


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise OSError("SOCKS5 proxy closed the connection unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _socks5_connect(proxy: tuple[str, int], dest_host: str, dest_port: int,
                    timeout: float | None) -> socket.socket:
    """Open a TCP socket to ``dest`` tunnelled through a SOCKS5 proxy (no auth)."""
    sock = socket.create_connection(proxy, timeout=timeout)
    try:
        # Greeting: version 5, one method, "no authentication required" (0x00).
        sock.sendall(b"\x05\x01\x00")
        ver, method = _recv_exact(sock, 2)
        if ver != 0x05:
            raise OSError("SOCKS5 proxy returned an unexpected version")
        if method != 0x00:
            raise OSError("SOCKS5 proxy requires authentication (unsupported)")

        # CONNECT request using a domain-name address so the proxy resolves DNS.
        host_bytes = dest_host.encode("idna")
        if len(host_bytes) > 255:
            raise OSError("Destination host name is too long for SOCKS5")
        request = (
            b"\x05\x01\x00\x03"
            + bytes([len(host_bytes)])
            + host_bytes
            + int(dest_port).to_bytes(2, "big")
        )
        sock.sendall(request)

        ver, rep, _rsv, atyp = _recv_exact(sock, 4)
        if rep != 0x00:
            raise OSError(f"SOCKS5 proxy refused the connection (code {rep})")
        # Drain the bound address + port from the reply so the stream is clean.
        if atyp == 0x01:      # IPv4
            _recv_exact(sock, 4)
        elif atyp == 0x03:    # domain name
            length = _recv_exact(sock, 1)[0]
            _recv_exact(sock, length)
        elif atyp == 0x04:    # IPv6
            _recv_exact(sock, 16)
        else:
            raise OSError("SOCKS5 proxy returned an unknown address type")
        _recv_exact(sock, 2)  # bound port
        return sock
    except Exception:
        sock.close()
        raise


class _Socks5HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, _proxy: tuple[str, int], **kwargs):
        super().__init__(*args, **kwargs)
        self._proxy = _proxy

    def connect(self) -> None:
        self.sock = _socks5_connect(self._proxy, self.host, self.port, self.timeout)
        if self._tunnel_host:
            self._tunnel()


class _Socks5HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, _proxy: tuple[str, int], **kwargs):
        super().__init__(*args, **kwargs)
        self._proxy = _proxy

    def connect(self) -> None:
        sock = _socks5_connect(self._proxy, self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _Socks5HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, proxy: tuple[str, int]):
        super().__init__()
        self._proxy = proxy

    def http_open(self, req):
        return self.do_open(
            functools.partial(_Socks5HTTPConnection, _proxy=self._proxy), req
        )


class _Socks5HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, proxy: tuple[str, int]):
        super().__init__()
        self._proxy = proxy

    def https_open(self, req):
        return self.do_open(
            functools.partial(_Socks5HTTPSConnection, _proxy=self._proxy), req
        )


def _build_opener(proxy: Optional[tuple[str, int]]) -> urllib.request.OpenerDirector:
    """Build a urllib opener that tunnels through ``proxy`` when set."""
    if proxy is None:
        return urllib.request.build_opener()
    return urllib.request.build_opener(
        _Socks5HTTPHandler(proxy), _Socks5HTTPSHandler(proxy)
    )


class StashClient:
    """GraphQL + media accessor for a single Stash server."""

    def __init__(self, url: str = "", api_key: str = "", tag: str = "",
                 proxy_enabled: bool = False, proxy_address: str = ""):
        self._lock = threading.Lock()
        self._scenes: list[dict] = []
        self._opener = _build_opener(None)
        self.configure(url, api_key, tag, proxy_enabled, proxy_address)

    # ── Configuration ────────────────────────────────────────────────────────

    def configure(self, url: str = "", api_key: str = "", tag: str = "",
                  proxy_enabled: bool = False, proxy_address: str = "") -> None:
        with self._lock:
            self.url = (url or "").rstrip("/")
            self.api_key = api_key or ""
            self.tag = (tag or "").strip()
            self.proxy_enabled = bool(proxy_enabled)
            self.proxy_address = (proxy_address or "").strip()
            proxy = parse_proxy_address(self.proxy_address) if self.proxy_enabled else None
            if self.proxy_enabled and proxy is None:
                log.warning("Stash: proxy enabled but address %r is invalid; "
                            "connecting directly", self.proxy_address)
            self._opener = _build_opener(proxy)
            self._scenes = []  # invalidate cache on any config change

    def is_configured(self) -> bool:
        return bool(self.url and self.tag)

    # ── HTTP helpers ─────────────────────────────────────────────────────────

    def _headers(self, extra: Optional[dict] = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["ApiKey"] = self.api_key
        if extra:
            headers.update(extra)
        return headers

    def _graphql(self, query: str, variables: dict | None = None) -> dict:
        if not self.url:
            raise RuntimeError("Stash URL is not configured")
        body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/graphql",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        with self._opener.open(req, timeout=_HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("errors"):
            raise RuntimeError(f"Stash GraphQL error: {payload['errors']}")
        return payload.get("data", {}) or {}

    # ── Scene queries ────────────────────────────────────────────────────────

    def _resolve_tag_id(self, name: str) -> Optional[str]:
        query = """
        query($name: String!) {
          findTags(tag_filter: {name: {value: $name, modifier: EQUALS}}) {
            tags { id name }
          }
        }
        """
        data = self._graphql(query, {"name": name})
        tags = (data.get("findTags") or {}).get("tags") or []
        if not tags:
            log.warning("Stash: no tag named %r found", name)
            return None
        return tags[0]["id"]

    def refresh_scenes(self) -> list[dict]:
        """Query Stash for all scenes carrying the configured tag and cache them."""
        if not self.is_configured():
            raise RuntimeError("Stash is not configured (need url + tag)")

        tag_id = self._resolve_tag_id(self.tag)
        if tag_id is None:
            with self._lock:
                self._scenes = []
            return []

        query = """
        query($tagIds: [ID!]) {
          findScenes(
            scene_filter: {tags: {value: $tagIds, modifier: INCLUDES_ALL}}
            filter: {per_page: -1}
          ) {
            scenes {
              id
              title
              interactive
              files { duration }
            }
          }
        }
        """
        data = self._graphql(query, {"tagIds": [tag_id]})
        raw = (data.get("findScenes") or {}).get("scenes") or []

        scenes: list[dict] = []
        for s in raw:
            files = s.get("files") or []
            duration_s = float(files[0]["duration"]) if files and files[0].get("duration") else 0.0
            interactive = bool(s.get("interactive"))
            scenes.append({
                "id": str(s["id"]),
                "title": s.get("title") or f"Scene {s['id']}",
                "interactive": interactive,
                "has_funscript": interactive,
                "duration_ms": int(duration_s * 1000),
            })

        with self._lock:
            self._scenes = scenes
        log.info("Stash: cached %d scene(s) for tag %r", len(scenes), self.tag)
        return scenes

    def get_scenes(self, force: bool = False) -> list[dict]:
        """Return cached tagged scenes, refreshing from Stash if needed."""
        with self._lock:
            cached = list(self._scenes)
        if cached and not force:
            return cached
        return self.refresh_scenes()

    def get_scene(self, scene_id: str) -> Optional[dict]:
        for scene in self.get_scenes():
            if scene["id"] == str(scene_id):
                return scene
        return None

    def pick_random_scene(self, prefer_interactive: bool = True) -> Optional[dict]:
        """Pick a random tagged scene, preferring ones that carry a funscript."""
        scenes = self.get_scenes()
        if not scenes:
            return None
        if prefer_interactive:
            interactive = [s for s in scenes if s["has_funscript"]]
            if interactive:
                return random.choice(interactive)
        return random.choice(scenes)

    # ── Media accessors ──────────────────────────────────────────────────────

    def stream_url(self, scene_id: str) -> str:
        return f"{self.url}/scene/{scene_id}/stream"

    def funscript_url(self, scene_id: str) -> str:
        return f"{self.url}/scene/{scene_id}/funscript"

    def fetch_funscript(self, scene_id: str) -> dict:
        """Fetch a scene's funscript JSON from Stash."""
        req = urllib.request.Request(
            self.funscript_url(scene_id),
            headers=self._headers(),
            method="GET",
        )
        with self._opener.open(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def open_stream(self, scene_id: str, range_header: Optional[str] = None):
        """
        Open the upstream video stream for proxying.

        Returns the raw urllib response object (a file-like with .status,
        .headers and .read()). The caller is responsible for closing it.
        Forwards a Range header when present so the browser can seek.
        """
        headers: dict[str, str] = {}
        if self.api_key:
            headers["ApiKey"] = self.api_key
        if range_header:
            headers["Range"] = range_header
        req = urllib.request.Request(
            self.stream_url(scene_id),
            headers=headers,
            method="GET",
        )
        # open() returns the response for 2xx (including 206 Partial Content);
        # HTTPError (also a response object) is raised for >=400.
        try:
            return self._opener.open(req, timeout=_HTTP_TIMEOUT)
        except urllib.error.HTTPError as exc:
            return exc

    # ── Validation (for the Settings tab) ──────────────────────────────────────

    def validate(self) -> dict[str, Any]:
        """Check connectivity + tag and return a status dict for the UI."""
        if not self.url:
            return {"ok": False, "message": "No Stash URL set", "scene_count": 0}
        if not self.tag:
            return {"ok": False, "message": "No tag set", "scene_count": 0}
        try:
            scenes = self.refresh_scenes()
        except urllib.error.URLError as exc:
            return {"ok": False, "message": f"Cannot reach Stash: {exc.reason}", "scene_count": 0}
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            return {"ok": False, "message": str(exc), "scene_count": 0}
        return {
            "ok": True,
            "message": f"{len(scenes)} scene(s) tagged {self.tag!r}",
            "scene_count": len(scenes),
        }
