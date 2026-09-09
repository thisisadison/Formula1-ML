"""
Fetch full-season results (every circuit) from Jolpica-F1 for seasons
missing from the local CSVs (2023+), for the multi-circuit model.

Unlike fetch_data.py (which also pulls qualifying/pit stops/lap times for
the Singapore-only, pre-qualifying-plus-grid feature set this project
started with), the multi-circuit model only ever needs finishing position,
points, constructor, and circuit per race -- so this is a much smaller
fetch: one paginated call per season instead of one call per race per
data type.

Usage:
    python fetch_all_circuits_data.py --years 2023 2024 2025 --out data/multi_circuit_fresh.csv
"""

import argparse

import pandas as pd
import requests

API = "https://api.jolpi.ca/ergast/f1"


def _get(path: str, **params) -> dict:
    resp = requests.get(f"{API}/{path}", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()["MRData"]


def fetch_season_results(year: int) -> pd.DataFrame:
    """One row per driver per race, for every circuit that season."""
    rows, offset, limit = [], 0, 100
    while True:
        data = _get(f"{year}/results.json", limit=limit, offset=offset)
        for race in data["RaceTable"]["Races"]:
            race_id = f"{year}_{race['round']}"  # synthetic: unique per race, never collides with historical numeric raceIds
            for r in race["Results"]:
                rows.append({
                    "raceId": race_id,
                    "year": year,
                    "round": int(race["round"]),
                    "circuitId": race["Circuit"]["circuitId"],
                    "driverId": r["Driver"]["code"],
                    "constructorId": r["Constructor"]["constructorId"],
                    "position": r["position"] if r["position"].isdigit() else "\\N",
                    "points": r["points"],
                })
        offset += limit
        if offset >= int(data["total"]):
            break
    return pd.DataFrame(rows)


def fetch_seasons(years: list) -> pd.DataFrame:
    frames = []
    for year in years:
        print(f"Fetching {year} full season (all circuits)...")
        frame = fetch_season_results(year)
        if frame.empty:
            print(f"  no data for {year} -- skipping")
            continue
        print(f"  {len(frame)} results across {frame['raceId'].nunique()} races")
        frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, nargs="+", required=True)
    parser.add_argument("--out", default="data/multi_circuit_fresh.csv")
    args = parser.parse_args()

    df = fetch_seasons(args.years)
    if df.empty:
        print("Nothing fetched.")
        return
    df.to_csv(args.out, index=False)
    print(f"Saved {len(df)} rows to {args.out}")


if __name__ == "__main__":
    main()
