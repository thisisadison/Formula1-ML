"""Keeps the site current by topping the local CSVs up from Jolpica-F1.

Wraps fetch_all_circuits_data.py rather than reimplementing it, adds an
on-disk cache so a page load never depends on a live API round trip, and
degrades to the bundled CSVs when the network is unavailable. The app
must start and stay useful with no connectivity at all -- refreshing is
an enhancement, never a requirement.
"""

import json
import os
import threading
import time

import pandas as pd
import requests

from fetch_all_circuits_data import fetch_season_results

CACHE_TTL_SECONDS = 60 * 60 * 6
API = "https://api.jolpi.ca/ergast/f1"


class LiveData:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir.rstrip("/")
        self.cache_dir = f"{self.data_dir}/cache"
        os.makedirs(self.cache_dir, exist_ok=True)
        self._lock = threading.Lock()
        self.last_refresh = None
        self.last_error = None
        self.online = None  # unknown until something is attempted

    # -----------------------------------------------------------------
    def _cache_path(self, key: str) -> str:
        return f"{self.cache_dir}/{key}.json"

    def _read_cache(self, key: str, max_age: int = CACHE_TTL_SECONDS):
        path = self._cache_path(key)
        try:
            if time.time() - os.path.getmtime(path) > max_age:
                return None
            with open(path) as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def _write_cache(self, key: str, payload) -> None:
        try:
            with open(self._cache_path(key), "w") as handle:
                json.dump(payload, handle)
        except OSError:
            pass  # a read-only data dir shouldn't break a request

    # -----------------------------------------------------------------
    def schedule(self, year: int):
        """That season's calendar. Returns None when unreachable and
        nothing usable is cached."""
        key = f"schedule_{year}"
        cached = self._read_cache(key)
        if cached is not None:
            return cached
        try:
            response = requests.get(f"{API}/{year}.json", params={"limit": 100}, timeout=15)
            response.raise_for_status()
            races = response.json()["MRData"]["RaceTable"]["Races"]
            payload = [{
                "round": int(race["round"]),
                "circuit_id": race["Circuit"]["circuitId"],
                "name": race["raceName"],
                "date": race.get("date"),
            } for race in races]
            self._write_cache(key, payload)
            self.online = True
            return payload
        except Exception as exc:
            self.online = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    def refresh_season(self, year: int) -> dict:
        """Pull a season's results and merge them into
        data/multi_circuit_fresh.csv, which is what the feature builders
        read. Returns a short status dict for the UI."""
        with self._lock:
            try:
                fresh = fetch_season_results(year)
            except Exception as exc:
                self.online = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                return {"ok": False, "error": self.last_error, "year": year}

            if fresh.empty:
                return {"ok": True, "year": year, "races": 0, "rows": 0, "changed": False}

            path = f"{self.data_dir}/multi_circuit_fresh.csv"
            try:
                existing = pd.read_csv(path)
            except FileNotFoundError:
                existing = pd.DataFrame(columns=fresh.columns)

            before = len(existing)
            combined = pd.concat([existing[existing["year"] != year], fresh], ignore_index=True)
            combined = combined.sort_values(["year", "round"]).reset_index(drop=True)
            combined.to_csv(path, index=False)

            self.online = True
            self.last_refresh = time.time()
            self.last_error = None
            return {
                "ok": True,
                "year": year,
                "races": int(fresh["raceId"].nunique()),
                "rows": len(fresh),
                "changed": len(combined) != before,
            }

    def status(self) -> dict:
        return {
            "online": self.online,
            "last_refresh": self.last_refresh,
            "last_error": self.last_error,
            "api": API,
        }
