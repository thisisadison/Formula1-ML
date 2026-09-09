"""Driver and circuit photographs from Wikipedia, resolved server-side.

Strictly a progressive enhancement. Every lookup can return None, and the
frontend is designed to look finished without a single photograph -- team
colours, monograms and generated track art carry the page on their own.
So this never raises, never blocks a render, and caches both hits and
misses so a missing photo costs one request rather than one per page
load.

Wikipedia's REST summary endpoint is used because it needs no API key and
returns a licensed thumbnail URL directly.
"""

import json
import os
import threading

import requests

SUMMARY_API = "https://en.wikipedia.org/api/rest_v1/page/summary/"
THUMB_WIDTH = 320
TIMEOUT_SECONDS = 8


class ImageResolver:
    def __init__(self, cache_path: str):
        self.cache_path = cache_path
        self._lock = threading.Lock()
        self._cache = self._load()

    def _load(self) -> dict:
        try:
            with open(self.cache_path) as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w") as handle:
                json.dump(self._cache, handle)
        except OSError:
            pass

    def lookup(self, title: str) -> dict:
        """{"image": url|None, "attribution": page url|None} for a
        Wikipedia page title."""
        if not title:
            return {"image": None, "attribution": None}
        with self._lock:
            if title in self._cache:
                return self._cache[title]

        result = {"image": None, "attribution": None}
        try:
            response = requests.get(
                SUMMARY_API + title.replace(" ", "_"),
                timeout=TIMEOUT_SECONDS,
                headers={"accept": "application/json"},
            )
            if response.ok:
                payload = response.json()
                thumbnail = payload.get("thumbnail") or {}
                source = thumbnail.get("source")
                if source:
                    # Ask for a consistent width rather than whatever the
                    # summary happens to return, so cards don't jump.
                    result["image"] = source.replace(
                        f"/{thumbnail.get('width', THUMB_WIDTH)}px-", f"/{THUMB_WIDTH}px-"
                    )
                result["attribution"] = (
                    payload.get("content_urls", {}).get("desktop", {}).get("page")
                )
        except Exception:
            # Offline, rate limited, redirected to a disambiguation page --
            # all the same outcome as far as the page is concerned.
            return {"image": None, "attribution": None}

        with self._lock:
            self._cache[title] = result
            self._save()
        return result
