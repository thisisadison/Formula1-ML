"""The deterministic half of the site: feature building and model
predictions. No LLM is involved anywhere in this module.

Every feature is built by calling pipeline.py's own builders rather than
reimplementing them, which is what keeps the site honest -- the numbers
on screen come from the same code path that produced the numbers the
model was trained and scored on.

Predicting a race that hasn't happened yet works because all four
builders are already strictly pre-race: build_season_form subtracts a
row's own points before ranking, and the circuit/recent/constructor form
builders all .shift(1) so a row only ever sees races before it. So a
placeholder row -- the right driver, the right constructor, the right
circuit, no result -- run through those same builders yields exactly the
features that race would have had. Nothing about the future leaks in,
because there is no future to leak.
"""

import functools

import pandas as pd

import pipeline
from pipeline import FEATURE_COLUMNS

# Placeholder finishing position, matching how a DNF/unknown result is
# spelled in the source CSVs -- pd.to_numeric(errors="coerce") turns it
# into NaN, which every builder already excludes from its averages.
NO_RESULT = "\\N"


def normalize_circuit_id(circuit_id):
    """Put a circuit on the same footing the model was trained on.

    load_multi_circuit_fresh_data() folds Jolpica's "marina_bay" slug into
    the historical numeric circuitId 15, so Singapore has one identity
    across both eras. Injecting a placeholder race under the raw slug
    instead would make it a circuit the data has never seen: circuit
    history comes back empty and the one-hot encoder drops it.

    Returns the id in the RAW scheme (int for Singapore, slug otherwise),
    not stringified -- build_circuit_history groups on this column before
    anything casts it, so an int 15 and a str "15" would be two circuits.
    Stringification happens later, once, for the model's encoder.
    """
    if str(circuit_id) in ("marina_bay", str(pipeline.SINGAPORE_CIRCUIT_ID)):
        return pipeline.SINGAPORE_CIRCUIT_ID
    return circuit_id

FEATURE_LABELS = {
    "driver_standing_before": "Championship position",
    "constructor_standing_before": "Constructor position",
    "driver_circuit_avg_finish": "Avg finish at this circuit",
    "driver_recent_form": "Recent form (last 3)",
    "constructor_recent_form": "Team form (last 3)",
    "years_experience": "Seasons in F1",
    "is_rookie": "Rookie season",
}


class PredictionService:
    """Owns the combined history and turns a race into per-driver rows."""

    def __init__(self, data_dir: str, model_store, reference):
        self.data_dir = data_dir
        self.model_store = model_store
        self.reference = reference
        self.raw = pipeline.load_raw_tables(data_dir)
        self.combined = pipeline.build_combined_raw(
            self.raw, f"{data_dir.rstrip('/')}/multi_circuit_fresh.csv"
        )
        self._known_features = None

    # -----------------------------------------------------------------
    # Feature building
    # -----------------------------------------------------------------
    @staticmethod
    def _apply_builders(scoped_raw: dict) -> pd.DataFrame:
        """Run pipeline's four pre-race feature builders over a
        results/races timeline and return one row per (race, driver)."""
        base = scoped_raw["results"][["raceId", "driverId", "constructorId"]].copy()
        base = base.merge(
            scoped_raw["races"][["raceId", "year", "round", "circuitId"]],
            on="raceId", how="left",
        )
        # Narrow to the modelled era BEFORE merging, exactly as
        # build_multi_circuit_dataset does. Pre-1990s drivers have no
        # 3-letter code in driversgit.csv (they come through as "\N"), so
        # dozens of them collapse onto one (raceId, driverId) key -- left
        # in, each builder merge would multiply those rows against each
        # other and blow up into hundreds of millions of rows. The
        # builders themselves still see the full timeline, which is what
        # debut year and circuit history need.
        base = base[(base["year"] >= pipeline.MULTI_CIRCUIT_MIN_YEAR) & (base["driverId"] != NO_RESULT)]
        for builder in (
            pipeline.build_season_form,
            pipeline.build_circuit_history,
            pipeline.build_recent_form,
            pipeline.build_constructor_recent_form,
        ):
            base = base.merge(builder(scoped_raw), on=["raceId", "driverId"], how="left")
        # Same string-typing as build_multi_circuit_dataset: the encoder
        # can't sort a column mixing numeric historical ids with slugs.
        base["circuit_id"] = base["circuitId"].astype(str)
        return base

    def known_features(self) -> pd.DataFrame:
        """Features for every race already present in the data. Computed
        once -- the builders sweep the whole timeline, so this is far too
        expensive to redo per request."""
        if self._known_features is None:
            features = self._apply_builders(self.combined)
            actual = self.combined["results"][["raceId", "driverId", "position"]].copy()
            actual["actual_position"] = pd.to_numeric(actual["position"], errors="coerce")
            features = features.merge(
                actual[["raceId", "driverId", "actual_position"]],
                on=["raceId", "driverId"], how="left",
            )
            self._known_features = features
        return self._known_features

    def upcoming_features(self, circuit_id: str, entrants: tuple, year: int, round_: int) -> pd.DataFrame:
        """Features for a race that hasn't been run, by appending a
        resultless placeholder row per entrant and re-running the same
        builders over the extended timeline."""
        circuit_id = normalize_circuit_id(circuit_id)
        race_id = f"upcoming_{year}_{round_}_{circuit_id}"
        placeholder_results = pd.DataFrame([
            {
                "raceId": race_id,
                "driverId": code,
                "constructorId": constructor,
                "position": NO_RESULT,
                "points": 0,
            }
            for code, constructor in entrants
        ])
        placeholder_race = pd.DataFrame([{
            "raceId": race_id, "year": year, "round": round_, "circuitId": circuit_id,
        }])

        columns = ["raceId", "driverId", "constructorId", "position", "points"]
        scoped = {
            "results": pd.concat(
                [self.combined["results"][columns], placeholder_results], ignore_index=True
            ),
            "races": pd.concat(
                [self.combined["races"], placeholder_race], ignore_index=True
            ),
        }
        features = self._apply_builders(scoped)
        return features[features["raceId"] == race_id].copy()

    # -----------------------------------------------------------------
    # Prediction
    # -----------------------------------------------------------------
    def _to_rows(self, features: pd.DataFrame) -> list:
        features = features.copy()
        features["top5_probability"] = self.model_store.predict_top5_proba(features)
        features = features.sort_values("top5_probability", ascending=False)

        rows = []
        for position, (_, row) in enumerate(features.iterrows()):
            driver = self.reference.driver(row["driverId"])
            team = self.reference.team(row["constructorId"])
            actual = row.get("actual_position")
            rows.append({
                "driver": driver,
                "team": team,
                "top5_probability": float(row["top5_probability"]),
                # Rank-based, NOT a >=50% cutoff: a real top-5 finish always
                # names exactly 5 drivers, so the prediction should too, and
                # this is what "5th place in the list" already looks like on
                # screen -- a probability threshold can silently disagree
                # with that (a borderline 5th-place driver at 42% reads as
                # "not predicted top 5" under a cutoff despite being shown
                # right there in the top 5 rows), which is exactly what
                # made a real miss display as "called right."
                "predicted_top5": position < 5,
                "actual_position": None if pd.isna(actual) else int(actual),
                "actual_top5": None if pd.isna(actual) else bool(1 <= actual <= 5),
                "features": {
                    key: (None if pd.isna(row[key]) else float(row[key]))
                    for key in FEATURE_COLUMNS if key != "circuit_id"
                },
            })
        return rows

    def predict_known_race(self, race_id) -> list:
        features = self.known_features()
        race_rows = features[features["raceId"].astype(str) == str(race_id)]
        if race_rows.empty:
            return []
        return self._to_rows(race_rows)

    @functools.lru_cache(maxsize=16)
    def _predict_upcoming_cached(self, circuit_id: str, entrants: tuple, year: int, round_: int) -> tuple:
        features = self.upcoming_features(circuit_id, entrants, year, round_)
        return tuple(self._to_rows(features))

    def predict_upcoming(self, circuit_id: str, entrants: list, year: int, round_: int) -> list:
        return list(self._predict_upcoming_cached(
            circuit_id, tuple(sorted(entrants)), year, round_
        ))

    # -----------------------------------------------------------------
    # Catalog
    # -----------------------------------------------------------------
    def race_catalog(self) -> list:
        """Every race in the data that has a result, newest first."""
        races = self.combined["races"].copy()
        results = self.combined["results"]
        raced = set(results.loc[pd.to_numeric(results["position"], errors="coerce").notna(), "raceId"])
        races = races[races["raceId"].isin(raced)]
        races = races[races["year"] >= pipeline.MULTI_CIRCUIT_MIN_YEAR]
        races = races.sort_values(["year", "round"], ascending=False)

        catalog = []
        for _, row in races.iterrows():
            circuit = self.reference.circuit(row["circuitId"])
            catalog.append({
                "race_id": str(row["raceId"]),
                "year": int(row["year"]),
                "round": int(row["round"]),
                "circuit": circuit,
                "label": f"{int(row['year'])} · {circuit['name']}",
            })
        return catalog

    def latest_race(self) -> dict:
        catalog = self.race_catalog()
        return catalog[0] if catalog else None

    def latest_entry_list(self) -> list:
        """(driver code, constructor) pairs from the most recent race --
        the default grid for an upcoming-race prediction."""
        latest = self.latest_race()
        if latest is None:
            return []
        results = self.combined["results"]
        entries = results[results["raceId"].astype(str) == latest["race_id"]]
        return sorted({
            (row["driverId"], str(row["constructorId"]))
            for _, row in entries.iterrows()
            if isinstance(row["driverId"], str)
        })

    def find_race(self, year: int, circuit_id: str):
        """The catalog entry for one year's race at a circuit, if it ran."""
        target = str(normalize_circuit_id(circuit_id))
        for race in self.race_catalog():
            if race["year"] == year and str(normalize_circuit_id(race["circuit"]["id"])) == target:
                return race
        return None

    def driver_circuit_record(self, code: str, circuit_id: str) -> dict:
        """Every finish a driver has recorded at one circuit, newest
        first, plus their average -- the raw evidence behind the
        driver_circuit_avg_finish feature."""
        circuit = self.reference.circuit(circuit_id)
        target = normalize_circuit_id(circuit_id)
        races = self.combined["races"]
        matching = races[races["circuitId"].astype(str) == str(target)]

        results = self.combined["results"]
        entries = results[
            (results["driverId"] == code) & (results["raceId"].isin(matching["raceId"]))
        ].merge(matching[["raceId", "year"]], on="raceId", how="left")

        appearances = []
        for _, row in entries.sort_values("year", ascending=False).iterrows():
            position = pd.to_numeric(row["position"], errors="coerce")
            appearances.append({
                "year": int(row["year"]),
                "position": None if pd.isna(position) else int(position),
            })
        finishes = [item["position"] for item in appearances if item["position"] is not None]
        return {
            "driver": self.reference.driver(code)["name"],
            "code": code,
            "circuit": circuit["name"],
            "appearances": appearances,
            "average_finish": round(sum(finishes) / len(finishes), 2) if finishes else None,
        }

    def next_round_slot(self) -> tuple:
        """Where an unraced event slots into the timeline: the round after
        the last one with results, in the season already under way.

        Slotting it as the next round of the CURRENT season rather than
        round 1 of the next one is what makes the standings features
        meaningful -- a season opener has everyone on zero points and
        therefore tied at the top, which tells the model nothing. This
        way the prediction runs against the championship as it actually
        stands, both offline (the latest season in the CSVs) and with
        live data (the real season in progress).
        """
        latest = self.latest_race()
        if latest is None:
            return (pipeline.MULTI_CIRCUIT_MIN_YEAR, 1)
        return (latest["year"], latest["round"] + 1)

    def data_cutoff(self) -> dict:
        latest = self.latest_race()
        if latest is None:
            return {"year": None, "round": None, "label": None}
        return {"year": latest["year"], "round": latest["round"], "label": latest["label"]}
