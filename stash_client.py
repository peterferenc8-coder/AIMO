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
"""

from __future__ import annotations

import json
import logging
import random
import threading
import urllib.error
import urllib.request
from typing import Any, Optional

log = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15.0


class StashClient:
    """GraphQL + media accessor for a single Stash server."""

    def __init__(self, url: str = "", api_key: str = "", tag: str = ""):
        self._lock = threading.Lock()
        self._scenes: list[dict] = []
        self.configure(url, api_key, tag)

    # ── Configuration ────────────────────────────────────────────────────────

    def configure(self, url: str = "", api_key: str = "", tag: str = "") -> None:
        with self._lock:
            self.url = (url or "").rstrip("/")
            self.api_key = api_key or ""
            self.tag = (tag or "").strip()
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
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
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
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
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
        # urlopen returns the response for 2xx (including 206 Partial Content);
        # HTTPError (also a response object) is raised for >=400.
        try:
            return urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT)
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
