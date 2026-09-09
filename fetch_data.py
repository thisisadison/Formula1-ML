"""
Fetch recent Singapore GP data from Jolpica-F1 (a community-run,
Ergast-schema-compatible API: https://api.jolpi.ca/ergast) to fill in
seasons missing from data/*.csv (which stops at 2022).

Produces a feature table in the exact same shape as pipeline.py's
build_singapore_dataset() output, so it can be concatenated straight
onto the historical data -- see pipeline.py's build_full_dataset().

The API has no numeric driverId/raceId compatible with the historical
CSVs (it uses driver codes like "NOR" and circuit slugs like
"marina_bay"), so freshly-fetched rows use the driver's 3-letter code
as driverId and the season year as raceId. Neither raceId nor driverId
is a model feature (see pipeline.py's FEATURE_COLUMNS), so this is
only ever used for display/traceability, not for joins against the
historical tables.

Usage:
    python fetch_data.py --years 2023 2024 2025 --out data/singapore_fresh.csv
"""

import argparse
import time

import pandas as pd
import requests

from pipeline import POINTS_POSITIONS, TARGET_COLUMN, _parse_lap_time_to_ms

API = "https://api.jolpi.ca/ergast/f1"
CIRCUIT_ID = "marina_bay"  # Jolpica-F1's slug for the Singapore GP circuit


def _get(path: str, **params) -> dict:
    resp = requests.get(f"{API}/{path}", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()["MRData"]


def _get_paginated(path: str, races_key: str, limit: int = 100) -> list:
    """Collects every page of a list endpoint (pitstops/laps can span
    hundreds of rows per race)."""
    items, offset = [], 0
    while True:
        data = _get(path, limit=limit, offset=offset)
        races = data["RaceTable"]["Races"]
        items.extend(races[0][races_key] if races else [])
        offset += limit
        if offset >= int(data["total"]):
            return items


def _parse_pit_duration_to_s(value) -> float:
    """Pit stop "duration" is usually plain seconds ("30.032"), but long
    stops (drive-throughs, damage repairs) come back as "M:SS.mmm"
    instead, same format as lap times."""
    text = str(value)
    if ":" in text:
        minutes, seconds = text.split(":")
        return int(minutes) * 60 + float(seconds)
    return float(text)


def fetch_singapore_round(year: int):
    """Returns the round number for that year's Singapore GP, or None if
    it hasn't been held yet (e.g. a future season)."""
    data = _get(f"{year}/circuits/{CIRCUIT_ID}/results.json")
    races = data["RaceTable"]["Races"]
    return races[0]["round"] if races else None


def fetch_standings_before(year: int, round_: str) -> tuple:
    """Championship standings after the round *before* Singapore -- i.e.
    exactly what a driver/constructor's standing was going into that race.
    Returns (driver_standing_by_code, constructor_standing_by_constructor_id),
    each mapping to {"points": float, "standing": int}."""
    prior_round = int(round_) - 1
    if prior_round < 1:
        return {}, {}  # Singapore was somehow round 1 -- no prior standings exist

    driver_data = _get(f"{year}/{prior_round}/driverStandings.json")
    driver_lists = driver_data["StandingsTable"]["StandingsLists"]
    driver_standing = {
        s["Driver"]["code"]: {"points": float(s["points"]), "standing": int(s["position"])}
        for s in (driver_lists[0]["DriverStandings"] if driver_lists else [])
    }

    constructor_data = _get(f"{year}/{prior_round}/constructorStandings.json")
    constructor_lists = constructor_data["StandingsTable"]["StandingsLists"]
    constructor_standing = {
        s["Constructor"]["constructorId"]: {"points": float(s["points"]), "standing": int(s["position"])}
        for s in (constructor_lists[0]["ConstructorStandings"] if constructor_lists else [])
    }
    return driver_standing, constructor_standing


_debut_year_cache = {}


def fetch_debut_year(driver_id: str) -> int:
    """A driver's first F1 season, across their whole career (not just
    Singapore) -- cached since the same driver reappears across years."""
    if driver_id not in _debut_year_cache:
        data = _get(f"drivers/{driver_id}/seasons.json", limit=1)
        seasons = data["SeasonTable"]["Seasons"]
        _debut_year_cache[driver_id] = int(seasons[0]["season"]) if seasons else None
    return _debut_year_cache[driver_id]


def build_features_for_year(year: int) -> pd.DataFrame:
    """One row per driver for that year's Singapore GP, in the same
    column shape as pipeline.py's build_singapore_dataset()."""
    round_ = fetch_singapore_round(year)
    if round_ is None:
        return pd.DataFrame()

    results = _get(f"{year}/{round_}/results.json")["RaceTable"]["Races"][0]["Results"]
    qualifying = _get_paginated(f"{year}/{round_}/qualifying.json", "QualifyingResults")
    pitstops = _get_paginated(f"{year}/{round_}/pitstops.json", "PitStops")
    laps = _get_paginated(f"{year}/{round_}/laps.json", "Laps")

    code_by_driver_id = {r["Driver"]["driverId"]: r["Driver"]["code"] for r in results}

    results_df = pd.DataFrame([{
        "driverId": r["Driver"]["code"],
        "grid_position": int(r["grid"]) if r["grid"] not in ("", None) else None,
        "fastest_lap_time_ms": _parse_lap_time_to_ms(
            r.get("FastestLap", {}).get("Time", {}).get("time")
        ),
        # Imported from pipeline.py rather than hardcoded here: build_full_dataset()
        # concats this frame straight onto the historical one by column name, so a
        # column named or thresholded differently from pipeline.py's own TARGET_COLUMN
        # doesn't raise -- it silently produces two half-empty target columns, and
        # every fresh row gets dropped as "no target" by clean_data(). (That's exactly
        # what a stale "top5"/<=5 literal here did after the target moved to top 10.)
        TARGET_COLUMN: int(r["position"].isdigit() and 1 <= int(r["position"]) <= POINTS_POSITIONS),
    } for r in results])

    # qualifying position is what the notebook/pipeline call "grid" --
    # falls back to the results endpoint's grid if a driver has no
    # qualifying row (e.g. a late replacement)
    quali_df = pd.DataFrame([{
        "driverId": q["Driver"]["code"],
        "grid_position": int(q["position"]),
    } for q in qualifying])
    if not quali_df.empty:
        results_df = results_df.drop(columns="grid_position").merge(
            quali_df, on="driverId", how="left"
        )

    pit_df = pd.DataFrame([{
        "driverId": code_by_driver_id.get(p["driverId"], p["driverId"]),
        "duration": _parse_pit_duration_to_s(p["duration"]),
    } for p in pitstops])
    pit_avg = (
        pit_df.groupby("driverId")["duration"].mean().reset_index(name="avg_pit_stop_s")
        if not pit_df.empty else pd.DataFrame(columns=["driverId", "avg_pit_stop_s"])
    )

    lap_rows = [
        {"driverId": code_by_driver_id.get(t["driverId"], t["driverId"]),
         "milliseconds": _parse_lap_time_to_ms(t["time"])}
        for lap in laps for t in lap["Timings"]
    ]
    lap_df = pd.DataFrame(lap_rows)
    lap_avg = (
        lap_df.groupby("driverId")["milliseconds"].mean().reset_index(name="avg_lap_time_ms")
        if not lap_df.empty else pd.DataFrame(columns=["driverId", "avg_lap_time_ms"])
    )

    driver_standing, constructor_standing = fetch_standings_before(year, round_)
    constructor_by_code = {r["Driver"]["code"]: r["Constructor"]["constructorId"] for r in results}

    form_rows = []
    for r in results:
        code = r["Driver"]["code"]
        driver_id = r["Driver"]["driverId"]
        constructor_id = constructor_by_code.get(code)
        debut_year = fetch_debut_year(driver_id)
        years_experience = year - debut_year if debut_year is not None else None
        form_rows.append({
            "driverId": code,
            "driver_standing_before": driver_standing.get(code, {}).get("standing"),
            "constructor_standing_before": constructor_standing.get(constructor_id, {}).get("standing"),
            "years_experience": years_experience,
            "is_rookie": int(years_experience == 0) if years_experience is not None else None,
        })
        time.sleep(0.2)  # one seasons.json call per driver -- stay polite
    form_df = pd.DataFrame(form_rows)

    df = results_df.merge(pit_avg, on="driverId", how="left")
    df = df.merge(lap_avg, on="driverId", how="left")
    df = df.merge(form_df, on="driverId", how="left")
    df["raceId"] = year  # synthetic: historical raceIds are small ints that never collide with a season year
    df["year"] = year
    df["code"] = df["driverId"]
    return df


def fetch_singapore_years(years: list) -> pd.DataFrame:
    frames = []
    for year in years:
        print(f"Fetching {year} Singapore GP...")
        frame = build_features_for_year(year)
        if frame.empty:
            print(f"  no race found for {year} (not held yet?) -- skipping")
            continue
        print(f"  {len(frame)} drivers")
        frames.append(frame)
        time.sleep(0.5)  # be polite to a free, community-run API
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, nargs="+", required=True)
    parser.add_argument("--out", default="data/singapore_fresh.csv")
    args = parser.parse_args()

    df = fetch_singapore_years(args.years)
    if df.empty:
        print("Nothing fetched.")
        return
    df.to_csv(args.out, index=False)
    print(f"Saved {len(df)} rows to {args.out}")


if __name__ == "__main__":
    main()
