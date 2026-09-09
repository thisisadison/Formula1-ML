"""F1 headlines from official and established motorsport outlets.

Reads each outlet's own RSS/Atom feed server-side, caches the result, and
surfaces headline + short summary + link. It deliberately links out
rather than republishing article bodies, which is what those feeds are
published for.

Summaries arrive as HTML from every one of these feeds, so tags are
stripped here before the text ever reaches a browser -- the frontend
also renders them as text nodes, so neither layer alone is load-bearing.
"""

import concurrent.futures
import html
import os
import re
import threading
import time

import feedparser
import requests

from webapp.reference import CURRENT_GRID, TEAMS

FEEDS = [
    ("Formula 1", "https://www.formula1.com/content/fom-website/en/latest/all.xml"),
    ("Autosport", "https://www.autosport.com/rss/f1/news/"),
    ("Motorsport.com", "https://www.motorsport.com/rss/f1/news/"),
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/formula1/rss.xml"),
]

# Headlines don't need to be fresher than this -- a news tab isn't the
# race-data path, so it's fine (and much lighter on every outlet's feed)
# to only actually refetch every few days rather than every request.
# F1_NEWS_TTL_SECONDS overrides; default matches "refresh every 3-4 days".
CACHE_TTL_SECONDS = int(os.environ.get("F1_NEWS_TTL_SECONDS", 3.5 * 24 * 60 * 60))
MAX_PER_FEED = 12
SUMMARY_CHARS = 260
FEED_TIMEOUT_SECONDS = 12
USER_AGENT = "Apex-F1/1.0 (+https://github.com/thisisadison/Formula1-ML)"

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")

# Topic filtering, not outlet filtering -- readers want "show me driver
# news" or "show me regulation news," not "show me BBC's coverage." Plain
# keyword matching against each headline's title+summary: no per-article
# LLM call, no external classifier, just a lookup against words that
# reliably indicate what a story is actually about. A story can land in
# more than one bucket (a driver signing for a new team is both), and one
# that matches nothing still surfaces under "Other" rather than vanishing.
CATEGORIES = ["Drivers", "Teams", "Regulations", "Race Weekend", "Other"]

DRIVER_KEYWORDS = {surname.lower() for _, surname, _ in CURRENT_GRID.values()} | {
    "driver", "drivers", "rookie", "seat", "signs", "signing", "contract",
    "line-up", "lineup", "retire", "retirement", "debut",
}
TEAM_KEYWORDS = {name.lower() for name, _ in TEAMS.values()} | {
    "team", "constructor", "livery", "sponsor", "factory", "upgrade",
    "car launch", "power unit", "engine deal", "principal",
}
REGULATION_KEYWORDS = {
    "fia", "regulation", "regulations", "rule change", "technical directive",
    "penalty", "penalised", "penalized", "steward", "stewards", "protest",
    "budget cap", "disqualified", "disqualification", "appeal", "rules",
}
RACE_WEEKEND_KEYWORDS = {
    "grand prix", "qualifying", "practice", "pole position", "podium",
    "victory", "wins", "crash", "crashes", "safety car", "pit stop",
    "sprint race", "fastest lap", "race result", "results",
}


def _categorize(title: str, summary: str) -> list:
    text = f"{title} {summary}".lower()
    hits = []
    if any(keyword in text for keyword in DRIVER_KEYWORDS):
        hits.append("Drivers")
    if any(keyword in text for keyword in TEAM_KEYWORDS):
        hits.append("Teams")
    if any(keyword in text for keyword in REGULATION_KEYWORDS):
        hits.append("Regulations")
    if any(keyword in text for keyword in RACE_WEEKEND_KEYWORDS):
        hits.append("Race Weekend")
    return hits or ["Other"]


def _clean(text: str) -> str:
    if not text:
        return ""
    return _WHITESPACE.sub(" ", html.unescape(_TAG.sub(" ", text))).strip()


def _image_for(entry) -> str:
    """Feeds advertise images in three different places depending on the
    outlet; take whichever is present."""
    for media in entry.get("media_content", []) or []:
        if media.get("url"):
            return media["url"]
    for thumb in entry.get("media_thumbnail", []) or []:
        if thumb.get("url"):
            return thumb["url"]
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image"):
            return link.get("href")
    return None


def _published_ts(entry) -> float:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return time.mktime(parsed)
    return 0.0


def _fetch_one(source: str, url: str) -> list:
    # Fetched with requests rather than handing the URL to feedparser,
    # which has no timeout of its own -- one unresponsive outlet would
    # otherwise hang the whole news request.
    response = requests.get(
        url, timeout=FEED_TIMEOUT_SECONDS, headers={"user-agent": USER_AGENT}
    )
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    items = []
    for entry in parsed.entries[:MAX_PER_FEED]:
        title = _clean(entry.get("title"))
        link = entry.get("link")
        if not title or not link:
            continue
        summary = _clean(entry.get("summary") or entry.get("description"))[:SUMMARY_CHARS]
        items.append({
            "source": source,
            "title": title,
            "link": link,
            "summary": summary,
            "image": _image_for(entry),
            "published": _published_ts(entry),
            "categories": _categorize(title, summary),
        })
    return items


class NewsService:
    def __init__(self):
        self._lock = threading.Lock()
        self._items = []
        self._fetched_at = 0.0
        self._errors = []

    def _refresh(self) -> None:
        items, errors = [], []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
            futures = {pool.submit(_fetch_one, name, url): name for name, url in FEEDS}
            for future in concurrent.futures.as_completed(futures, timeout=25):
                name = futures[future]
                try:
                    items.extend(future.result())
                except Exception as exc:
                    # The status code / URL / reason, not just the exception
                    # class -- "Autosport: HTTPError" doesn't tell anyone
                    # (including me, debugging this later) whether that feed
                    # 403'd, 404'd, moved, or timed out.
                    errors.append(f"{name}: {type(exc).__name__}: {exc}")
        items.sort(key=lambda item: item["published"], reverse=True)
        self._items = items
        self._errors = errors
        self._fetched_at = time.time()

    def headlines(self, force: bool = False) -> dict:
        with self._lock:
            stale = time.time() - self._fetched_at > CACHE_TTL_SECONDS
            if force or stale or not self._items:
                try:
                    self._refresh()
                except Exception as exc:
                    self._errors = [f"{type(exc).__name__}: {exc}"]
        return {
            "items": self._items,
            "sources": [name for name, _ in FEEDS],
            "categories": CATEGORIES,
            "fetched_at": self._fetched_at or None,
            "errors": self._errors,
        }
