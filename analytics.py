"""Season EDA over the exact features the model is trained on.

Single source of truth for the Analytics page AND the notebook. Both call
compute() on the same feature frame, so a number shown on the site and the
same number in the notebook can't drift apart -- the alternative (the page
querying one way, the notebook recomputing another) is how two "official"
answers to the same question appear.

Scoped to ONE season by default -- the most recent one present in the data.
A rolling-3-race form value or a championship standing only means anything
inside the season it was computed in, and a reader looking at the analytics
to reason about the next race cares about the current competitive order,
not a 2008 field that shares no drivers, cars, or regulations with it.

    from analytics import build_season_frame, compute
    frame, year = build_season_frame("data")     # notebook entry point
    report = compute(frame, year)
"""

import numpy as np
import pandas as pd

import pipeline

# The seven model inputs, in the order they're shown everywhere downstream.
# circuit_id is excluded on purpose: it's the one categorical feature, so it
# has no distribution/correlation reading in the sense the others do.
FEATURES = [column for column in pipeline.FEATURE_COLUMNS if column != "circuit_id"]

FEATURE_LABELS = {
    "driver_standing_before": "Championship position",
    "constructor_standing_before": "Constructor position",
    "driver_circuit_avg_finish": "Avg finish at circuit",
    "driver_recent_form": "Recent form (last 3)",
    "constructor_recent_form": "Team form (last 3)",
    "years_experience": "Seasons in F1",
    "is_rookie": "Rookie season",
}

# Lower is better for every one of these (P1 beats P10, a 2.0 average finish
# beats 8.0) EXCEPT experience. Stated once here so the UI can explain a
# negative correlation as "lower value, more likely to score" rather than
# leaving the reader to work out the sign convention per feature.
LOWER_IS_BETTER = {
    "driver_standing_before": True,
    "constructor_standing_before": True,
    "driver_circuit_avg_finish": True,
    "driver_recent_form": True,
    "constructor_recent_form": True,
    "years_experience": False,
    "is_rookie": False,
}

HISTOGRAM_BINS = 12
BUCKET_COUNT = 5

# Named states for the one binary feature, keyed by its raw 0/1 value -- used
# everywhere a chart would otherwise print the ambiguous "No"/"Yes" (which
# reads as "no" to WHAT unless you already know the column name).
BINARY_LABELS = {"is_rookie": {0: "Non-rookie", 1: "Rookie"}}


def build_season_frame(data_dir: str = "data", year: int = None):
    """Feature frame for one season, built the same way the model's training
    data is. Standalone entry point -- the notebook has no PredictionService,
    so this reconstructs the frame from pipeline.py directly."""
    raw = pipeline.load_raw_tables(data_dir)
    combined = pipeline.build_combined_raw(raw, f"{data_dir.rstrip('/')}/multi_circuit_fresh.csv")

    base = combined["results"][["raceId", "driverId", "constructorId"]].copy()
    base = base.merge(
        combined["races"][["raceId", "year", "round", "circuitId"]], on="raceId", how="left"
    )
    base = base[base["year"] >= pipeline.MULTI_CIRCUIT_MIN_YEAR]
    for builder in (
        pipeline.build_season_form,
        pipeline.build_circuit_history,
        pipeline.build_recent_form,
        pipeline.build_constructor_recent_form,
    ):
        base = base.merge(builder(combined), on=["raceId", "driverId"], how="left")

    actual = combined["results"][["raceId", "driverId", "position"]].copy()
    actual["actual_position"] = pd.to_numeric(actual["position"], errors="coerce")
    base = base.merge(
        actual[["raceId", "driverId", "actual_position"]], on=["raceId", "driverId"], how="left"
    )

    year = int(year or base["year"].max())
    return base[base["year"] == year].copy(), year


def _scored(frame: pd.DataFrame) -> pd.Series:
    """Did this driver finish in the points? NaN for a DNF/no-result row --
    those are excluded from rates rather than silently counted as failures,
    which would conflate "wasn't fast enough" with "didn't finish"."""
    return frame["actual_position"].between(1, pipeline.POINTS_POSITIONS)


def _histogram(values: pd.Series, feature: str = "") -> dict:
    clean = values.dropna()
    if clean.empty:
        return {"bins": [], "counts": []}
    if clean.nunique() <= 2:  # is_rookie and anything else effectively binary
        counts = clean.value_counts().sort_index()
        # A 0/1 flag reads as a quantity on an axis; name the two states
        # concretely (not "Yes"/"No") so a chart never needs a caption to
        # say which value means what.
        return {
            "bins": [BINARY_LABELS.get(feature, {}).get(value, f"{value:g}")
                     for value in counts.index],
            "counts": [int(count) for count in counts.values],
        }
    counts, edges = np.histogram(clean, bins=HISTOGRAM_BINS)
    return {
        "bins": [f"{edges[i]:.0f}–{edges[i + 1]:.0f}" for i in range(len(counts))],
        "counts": [int(count) for count in counts],
    }


def _rate_by_bucket(frame: pd.DataFrame, feature: str) -> dict:
    """Points-finish rate across ordered buckets of one feature -- the chart
    that actually answers "does this feature separate scorers from
    non-scorers?", which a distribution alone cannot.

    Quantile buckets, not equal-width: championship standing is uniform but
    circuit-average-finish clusters hard, and equal-width bins there produce
    near-empty extremes whose rates swing on two or three drivers."""
    usable = frame[[feature]].join(_scored(frame).rename("scored"))
    usable = usable.dropna(subset=[feature, "scored"])
    if usable.empty:
        return {"labels": [], "rates": [], "counts": []}

    distinct = usable[feature].nunique()
    if distinct <= 2:
        grouped = usable.groupby(feature)["scored"]
        return {
            "labels": [BINARY_LABELS.get(feature, {}).get(value, f"{value:g}")
                       for value in grouped.mean().index],
            "rates": [round(float(rate), 4) for rate in grouped.mean().values],
            "counts": [int(count) for count in grouped.size().values],
        }

    buckets = min(BUCKET_COUNT, distinct)
    try:
        binned = pd.qcut(usable[feature], q=buckets, duplicates="drop")
    except ValueError:
        return {"labels": [], "rates": [], "counts": []}

    grouped = usable.groupby(binned, observed=True)["scored"]
    return {
        "labels": [f"{interval.left:.0f}–{interval.right:.0f}" for interval in grouped.mean().index],
        "rates": [round(float(rate), 4) for rate in grouped.mean().values],
        "counts": [int(count) for count in grouped.size().values],
    }


def _correlations(frame: pd.DataFrame) -> list:
    """Point-biserial correlation of each feature with the binary outcome --
    which is just Pearson with one binary side, so pandas' .corr() is exact
    here, not an approximation."""
    scored = _scored(frame).astype(float)
    rows = []
    for feature in FEATURES:
        series = frame[feature]
        paired = pd.DataFrame({"x": series, "y": scored}).dropna()
        value = paired["x"].corr(paired["y"]) if len(paired) > 2 and paired["x"].nunique() > 1 else np.nan
        rows.append({
            "feature": feature,
            "label": FEATURE_LABELS.get(feature, feature),
            "correlation": None if pd.isna(value) else round(float(value), 4),
            "lower_is_better": LOWER_IS_BETTER.get(feature, False),
            "coverage": int(paired.shape[0]),
        })
    rows.sort(key=lambda row: abs(row["correlation"] or 0), reverse=True)
    return rows


def _missingness(frame: pd.DataFrame) -> list:
    """Which features are actually populated this season. A rookie has no
    circuit history and no prior-3-race form, so these are legitimately NaN
    rather than broken -- but the reader should see how much of the season
    the model is imputing before trusting a feature's chart.

    Every row is still returned (nothing here is dropped) -- most features
    run at 100% most seasons, and it's the UI's job to decide whether a
    fully-covered row is worth a chart or just a one-line mention, not
    this function's."""
    total = len(frame)
    return [{
        "feature": feature,
        "label": FEATURE_LABELS.get(feature, feature),
        "missing": int(frame[feature].isna().sum()),
        "missing_pct": round(float(frame[feature].isna().mean()), 4) if total else 0.0,
    } for feature in FEATURES]


def _by_team(frame: pd.DataFrame) -> list:
    scored = _scored(frame)
    usable = frame[["constructorId"]].join(scored.rename("scored")).dropna(subset=["scored"])
    if usable.empty:
        return []
    grouped = usable.groupby("constructorId")["scored"]
    total_scores = int(grouped.sum().sum())
    rows = [{
        "team": str(team),
        "rate": round(float(rate), 4),
        "entries": int(grouped.size()[team]),
        "scores": int(grouped.sum()[team]),
        # Share of the season's total points-finishes, not of this team's own
        # entries -- a different question from "rate" (how good is this team
        # per opportunity) that "who is actually racking up the points"
        # answers, and the two can rank teams differently when field sizes
        # differ (a two-car team can't out-total a stronger one at the same
        # rate).
        "share": round(int(grouped.sum()[team]) / total_scores, 4) if total_scores else 0.0,
    } for team, rate in grouped.mean().items()]
    rows.sort(key=lambda row: row["rate"], reverse=True)
    return rows


def _by_driver(frame: pd.DataFrame) -> list:
    scored = _scored(frame)
    usable = frame[["driverId"]].join(scored.rename("scored")).dropna(subset=["scored"])
    if usable.empty:
        return []
    grouped = usable.groupby("driverId")["scored"]
    rows = [{
        "driver": str(driver),
        "rate": round(float(rate), 4),
        "entries": int(grouped.size()[driver]),
        "scores": int(grouped.sum()[driver]),
    } for driver, rate in grouped.mean().items()]
    rows.sort(key=lambda row: (row["scores"], row["rate"]), reverse=True)
    return rows


def _experience_trend(frame: pd.DataFrame) -> dict:
    """Cumulative points-finish rate through each round, rookies vs everyone
    else -- does a rookie season actually improve as the year goes on, or is
    the gap to the established grid roughly constant?

    Cumulative, not per-round: this season has only 3 rookies, so a single
    round's rate jumps between 0%, 33% and 100% on 3 results and says
    nothing. Rate-to-date over a growing sample is the only version of this
    that isn't noise dressed up as a trend."""
    scored = _scored(frame).astype(float)
    usable = frame[["round", "is_rookie"]].join(scored.rename("scored"))
    usable = usable.dropna(subset=["round", "scored"])
    if usable.empty:
        return {"rounds": [], "rookie_rate": [], "veteran_rate": [], "rookie_entries": []}

    rounds = sorted(int(r) for r in usable["round"].unique())
    rookie_rate, veteran_rate, rookie_entries = [], [], []
    for round_num in rounds:
        so_far = usable[usable["round"] <= round_num]
        rookies = so_far.loc[so_far["is_rookie"] == 1, "scored"]
        veterans = so_far.loc[so_far["is_rookie"] == 0, "scored"]
        rookie_rate.append(round(float(rookies.mean()), 4) if len(rookies) else None)
        veteran_rate.append(round(float(veterans.mean()), 4) if len(veterans) else None)
        rookie_entries.append(int(len(rookies)))

    return {
        "rounds": rounds,
        "rookie_rate": rookie_rate,
        "veteran_rate": veteran_rate,
        # Sample size behind each rookie point, so a reader can see the early
        # rounds are thinner evidence than the late ones.
        "rookie_entries": rookie_entries,
    }


def compute(frame: pd.DataFrame, year: int) -> dict:
    """The whole report for one season. Pure -- takes an already-built
    feature frame so the web app can hand over its cached one instead of
    rebuilding the timeline on every request."""
    scored = _scored(frame)
    classified = scored.notna() & frame["actual_position"].notna()
    finishers = int(classified.sum())

    return {
        "year": int(year),
        "headline": {
            "races": int(frame["raceId"].nunique()),
            "entries": int(len(frame)),
            "drivers": int(frame["driverId"].nunique()),
            "teams": int(frame["constructorId"].nunique()),
            "points_finishes": int(scored.fillna(False).sum()),
            "classified": finishers,
            "dnf_or_unclassified": int(len(frame) - finishers),
            "points_rate": round(float(scored[classified].mean()), 4) if finishers else None,
            "rookies": int(frame.loc[frame["is_rookie"] == 1, "driverId"].nunique()),
        },
        "distributions": [{
            "feature": feature,
            "label": FEATURE_LABELS.get(feature, feature),
            **_histogram(frame[feature], feature),
        } for feature in FEATURES],
        "rate_by_bucket": [{
            "feature": feature,
            "label": FEATURE_LABELS.get(feature, feature),
            "lower_is_better": LOWER_IS_BETTER.get(feature, False),
            **_rate_by_bucket(frame, feature),
        } for feature in FEATURES],
        "correlations": _correlations(frame),
        "missingness": _missingness(frame),
        "by_team": _by_team(frame),
        "by_driver": _by_driver(frame),
        "experience_trend": _experience_trend(frame),
    }


def available_years(data_dir: str = "data") -> list:
    """Seasons that could be analysed, newest first."""
    raw = pipeline.load_raw_tables(data_dir)
    combined = pipeline.build_combined_raw(raw, f"{data_dir.rstrip('/')}/multi_circuit_fresh.csv")
    years = combined["races"]["year"]
    years = years[years >= pipeline.MULTI_CIRCUIT_MIN_YEAR]
    return sorted({int(year) for year in years.dropna()}, reverse=True)
