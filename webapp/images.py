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
import re
import threading

import requests

SUMMARY_API = "https://en.wikipedia.org/api/rest_v1/page/summary/"
THUMB_WIDTH = 320
TIMEOUT_SECONDS = 8

# Wikimedia's API policy requires a descriptive User-Agent and answers 403
# to the default python-requests one -- without this every lookup fails and
# the site shows no photographs at all.
USER_AGENT = "Apex-F1/1.0 (https://github.com/thisisadison/Formula1-ML) python-requests"

# Bumped when a bug could have written bad entries; caches from an older
# version are dropped rather than trusted. v1 cached 403s and rate limits
# as permanent "this page has no photo" misses. v2 cached thumb.wikimedia.org
# URLs as successful hits -- a valid API response, but a dead host that
# doesn't actually serve the file, so every image already resolved under
# v2 needs re-resolving through the upload.wikimedia.org rewrite below.
CACHE_VERSION = 3


class ImageResolver:
    def __init__(self, cache_path: str):
        self.cache_path = cache_path
        self._lock = threading.Lock()
        self._cache = self._load()

    def _load(self) -> dict:
        try:
            with open(self.cache_path) as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            return {}
        if not isinstance(stored, dict) or stored.get("version") != CACHE_VERSION:
            return {}  # written by a version whose entries can't be trusted
        entries = stored.get("entries")
        return entries if isinstance(entries, dict) else {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w") as handle:
                json.dump({"version": CACHE_VERSION, "entries": self._cache}, handle)
        except OSError:
            pass

    def lookup(self, title: str) -> dict:
        """{"image": url|None, "attribution": page url|None} for a
        Wikipedia page title.

        Only a genuinely successful lookup is ever cached. A 403, a rate
        limit or a network blip must NOT be remembered: caching those
        would let one bad afternoon permanently blank every photo on the
        site, with no TTL to recover from.
        """
        if not title:
            return {"image": None, "attribution": None}
        with self._lock:
            if title in self._cache:
                return self._cache[title]

        miss = {"image": None, "attribution": None}
        try:
            response = requests.get(
                SUMMARY_API + title.replace(" ", "_"),
                timeout=TIMEOUT_SECONDS,
                headers={"accept": "application/json", "user-agent": USER_AGENT},
            )
            if not response.ok:
                return miss  # transient (403/429/5xx) or absent -- retry next time
            payload = response.json()
        except Exception:
            return miss

        result = {"image": None, "attribution": None}
        thumbnail = payload.get("thumbnail") or {}
        source = thumbnail.get("source")
        if source:
            # The summary API can hand back a thumb.wikimedia.org URL --
            # same exact Commons path structure, but that host doesn't
            # actually serve the file (confirmed: loading one directly in a
            # browser fails outright, not a CORS/embedding issue, a genuine
            # dead load). upload.wikimedia.org is Wikimedia's real,
            # long-standing, hotlink-friendly media CDN and serves the
            # identical path -- forcing it here fixes the request rather
            # than trusting whichever host the API happened to return.
            source = re.sub(r"^https?://thumb\.wikimedia\.org/", "https://upload.wikimedia.org/", source)
            # Ask for a consistent width rather than whatever the summary
            # happens to return, so cards don't jump as photos land. Also
            # drops the API's own ?utm_source=... tracking params -- dead
            # weight on a direct CDN fetch.
            result["image"] = source.replace(
                f"/{thumbnail.get('width', THUMB_WIDTH)}px-", f"/{THUMB_WIDTH}px-"
            ).split("?", 1)[0]
        result["attribution"] = payload.get("content_urls", {}).get("desktop", {}).get("page")

        # A page that genuinely has no photo IS worth caching -- that answer
        # won't change on a retry, unlike the failures above.
        with self._lock:
            self._cache[title] = result
            self._save()
        return result
