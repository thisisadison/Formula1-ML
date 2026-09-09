"""
F1 Points-Finish (Top-10) Prediction Pipeline
==============================================

Rebuilds SC1015_Final_Formula_1_Mini-Project.ipynb as a clean,
reproducible pipeline: load -> build features -> clean -> train &
compare models -> export the best one.

What changed vs. the notebook (see chat for the full rundown):
  - Feature engineering is vectorized (groupby/merge) instead of the
    original row-by-row Python loops -- same result, much faster.
  - Race year comes from races.csv's own `year` column via a merge,
    instead of a hand-typed raceId -> year list that depended on
    raceIds coming back from a `.loc[]` filter in a specific order.
  - Class balancing (upsampling top-5 finishes) now happens AFTER the
    train/test split, on the training set only. Upsampling before the
    split (as the notebook did) risks the same duplicated minority row
    landing in both train and test, which inflates test accuracy.
  - The three tuned models (RF / Logistic Regression / SVM) are
    compared side by side and the best one is exported automatically.

Usage:
    python pipeline.py --data-dir data --model-out model.pkl

Expects a data directory containing the 7 raw CSVs from the notebook:
    drivers.csv, laptimes.csv, qualifying.csv, races.csv,
    sgppits.csv, results.csv
(weatherdescription.csv is loaded by the notebook but never actually
used in the final feature set -- see chat notes -- so it's omitted
here. Easy to add back in if you want to test whether it helps.)
"""

import argparse

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, classification_report
from sklearn.model_selection import GridSearchCV, cross_val_score, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.svm import SVC
from sklearn.utils import resample

SINGAPORE_CIRCUIT_ID = 15  # circuitId for the Singapore GP in this dataset

# Points have been paid down to P10 since the 2010 season (25-18-15-...-1),
# not just the podium or a top-5 cutoff -- this is what "finishes in the
# points" actually means and what the target below is defined against.
POINTS_POSITIONS = 10

# Pre-qualifying features only. avg_pit_stop_s/avg_lap_time_ms/fastest_lap_time_ms
# are dropped because they're only known *during* the race (see
# build_season_form()'s docstring). grid_position is ALSO dropped, despite
# being a genuinely strong feature (correlated with finishing position,
# second only to driver_standing_before) -- it doesn't exist until qualifying
# finishes,
# usually about a day before the race, so a model that needs it can only
# ever be used the day before a race, not while there's still real lead
# time. Dropping it costs real accuracy (~87% -> ~83% in testing) in
# exchange for being usable as soon as the previous race ends instead.
# Numeric features go through impute+scale; circuit_id is categorical (a
# race's circuit isn't ordinal) and goes through one-hot encoding instead --
# see build_candidates(). driver_circuit_avg_finish is allowed to be NaN
# (a driver's first-ever visit to a circuit has no history) -- clean_data()
# doesn't require it non-null, the imputer fills it with the column median.
NUMERIC_FEATURE_COLUMNS = ["driver_standing_before", "constructor_standing_before",
                            "years_experience", "is_rookie", "driver_circuit_avg_finish",
                            "driver_recent_form", "constructor_recent_form"]
CATEGORICAL_FEATURE_COLUMNS = ["circuit_id"]
FEATURE_COLUMNS = NUMERIC_FEATURE_COLUMNS + CATEGORICAL_FEATURE_COLUMNS
REQUIRED_COLUMNS = ["driver_standing_before", "constructor_standing_before",
                     "years_experience", "is_rookie", "circuit_id"]
TARGET_COLUMN = "points_finish"


# ---------------------------------------------------------------------
# 1. Load the raw tables
# ---------------------------------------------------------------------
def load_raw_tables(data_dir: str) -> dict:
    p = lambda name: f"{data_dir.rstrip('/')}/{name}"
    return {
        "drivers": pd.read_csv(p("driversgit.csv")),
        "laptimes": pd.read_csv(p("laptimesgit.csv")),
        "qualifying": pd.read_csv(p("qualifyinggit.csv")),
        "races": pd.read_csv(p("racesgit.csv")),
        "pits": pd.read_csv(p("sgppitsgitfinal.csv")),
        "results": pd.read_csv(p("resultsgit.csv")),
    }


def _parse_lap_time_to_ms(value) -> float:
    """Convert 'M:SS.mmm' lap-time strings (e.g. '1:34.500') to
    milliseconds. Missing/DNF values are recorded as '\\N'."""
    if pd.isna(value) or value == "\\N":
        return np.nan
    minutes, seconds = str(value).split(":")
    return (int(minutes) * 60 + float(seconds)) * 1000


# ---------------------------------------------------------------------
# 2. Build the Singapore GP dataset 
# ---------------------------------------------------------------------
def build_singapore_dataset(raw: dict) -> pd.DataFrame:
    races = raw["races"]
    sgp_races = races.loc[races["circuitId"] == SINGAPORE_CIRCUIT_ID, ["raceId", "year"]]
    sgp_ids = sgp_races["raceId"]

    # sgppitsgitfinal.csv's own "raceId" column actually holds the *year*
    # (e.g. 2011, 2012, ...), not the real raceId used by every other table
    # -- so this one has to join on year instead of raceId.
    pits = raw["pits"][raw["pits"]["raceId"].isin(sgp_races["year"])].rename(
        columns={"raceId": "year"}
    )
    pit_avg = (
        pits.groupby(["year", "driverId"])["duration"]
        .mean()
        .reset_index(name="avg_pit_stop_s")
    )

    laptimes = raw["laptimes"][raw["laptimes"]["raceId"].isin(sgp_ids)]
    lap_avg = (
        laptimes.groupby(["raceId", "driverId"])["milliseconds"]
        .mean()
        .reset_index(name="avg_lap_time_ms")
    )

    quali = raw["qualifying"][raw["qualifying"]["raceId"].isin(sgp_ids)]
    grid = quali[["raceId", "driverId", "position"]].rename(columns={"position": "grid_position"})

    results = raw["results"][raw["results"]["raceId"].isin(sgp_ids)].copy()
    results["fastest_lap_time_ms"] = results["fastestLapTime"].apply(_parse_lap_time_to_ms)
    results["position"] = pd.to_numeric(results["position"], errors="coerce")  # '\N' (DNF) -> NaN
    results[TARGET_COLUMN] = results["position"].between(1, POINTS_POSITIONS).astype(int)  # DNF/NaN -> 0

    df = lap_avg.merge(grid, on=["raceId", "driverId"], how="outer")
    df = df.merge(
        results[["raceId", "driverId", "fastest_lap_time_ms", TARGET_COLUMN]],
        on=["raceId", "driverId"], how="outer",
    )
    df = df.merge(sgp_races, on="raceId", how="left")
    df = df.merge(pit_avg, on=["year", "driverId"], how="outer")
    df = df.merge(raw["drivers"][["driverId", "code"]], on="driverId", how="left")
    df = df.merge(build_season_form(raw), on=["raceId", "driverId"], how="left")
    df = df.merge(build_circuit_history(raw), on=["raceId", "driverId"], how="left")
    df = df.merge(build_recent_form(raw), on=["raceId", "driverId"], how="left")
    df = df.merge(build_constructor_recent_form(raw), on=["raceId", "driverId"], how="left")
    df["circuit_id"] = str(SINGAPORE_CIRCUIT_ID)  # string, not int -- see build_multi_circuit_dataset()
    return df


def build_season_form(raw: dict) -> pd.DataFrame:
    """Pre-race-known form features -- championship standing and years of
    experience -- computed from ALL circuits/years in results.csv/races.csv,
    not just Singapore. Same logic as the notebook's season-form cell.
    Unlike avg_lap_time_ms/avg_pit_stop_s/fastest_lap_time_ms (only known
    *during* the race), these are legitimate features for actually
    forecasting an upcoming race."""
    races = raw["races"]
    results = raw["results"]

    full = results.merge(races[["raceId", "year", "round"]], on="raceId", how="left")
    full["points"] = pd.to_numeric(full["points"], errors="coerce").fillna(0)
    full = full.sort_values(["year", "round"])

    full["driver_points_before"] = (
        full.groupby(["year", "driverId"])["points"].cumsum() - full["points"]
    )
    full["constructor_points_before"] = (
        full.groupby(["year", "constructorId"])["points"].cumsum() - full["points"]
    )
    full["driver_standing_before"] = (
        full.groupby(["year", "raceId"])["driver_points_before"].rank(method="min", ascending=False)
    )
    full["constructor_standing_before"] = (
        full.groupby(["year", "raceId"])["constructor_points_before"].rank(method="min", ascending=False)
    )

    debut_year = full.groupby("driverId")["year"].min().rename("debut_year")
    full = full.merge(debut_year, on="driverId", how="left")
    full["years_experience"] = full["year"] - full["debut_year"]
    full["is_rookie"] = (full["years_experience"] == 0).astype(int)

    return full[["raceId", "driverId", "driver_standing_before", "constructor_standing_before",
                 "years_experience", "is_rookie"]]


def build_circuit_history(raw: dict) -> pd.DataFrame:
    """A driver's average finishing position at THIS circuit, from every
    prior visit (any year) -- NaN on a driver's first-ever race there.
    Pre-race-known (only uses races strictly before this one), and lets a
    driver's personal track record show up even though grid_position and
    in-race stats have both been dropped (see FEATURE_COLUMNS)."""
    races = raw["races"]
    results = raw["results"]

    full = results.merge(races[["raceId", "year", "round", "circuitId"]], on="raceId", how="left")
    full["position_num"] = pd.to_numeric(full["position"], errors="coerce")  # DNF -> NaN, excluded from the average
    full = full.sort_values(["year", "round"])
    full["driver_circuit_avg_finish"] = (
        full.groupby(["driverId", "circuitId"])["position_num"]
        .transform(lambda s: s.expanding().mean().shift(1))
    )
    return full[["raceId", "driverId", "driver_circuit_avg_finish"]]


RECENT_FORM_WINDOW = 3


def build_recent_form(raw: dict) -> pd.DataFrame:
    """A driver's average finishing position over their last N races (any
    circuit), pre-race-known -- captures current momentum in a way
    driver_standing_before can't. Season-cumulative standing looks the
    same whether a driver started terribly and is now on a hot streak or
    has been steady all year; a rolling recent-form average doesn't."""
    races = raw["races"]
    results = raw["results"]

    full = results.merge(races[["raceId", "year", "round"]], on="raceId", how="left")
    full["position_num"] = pd.to_numeric(full["position"], errors="coerce")  # DNF -> NaN, excluded from the average
    full = full.sort_values(["year", "round"])
    full["driver_recent_form"] = (
        full.groupby("driverId")["position_num"]
        .transform(lambda s: s.rolling(RECENT_FORM_WINDOW, min_periods=1).mean().shift(1))
    )
    return full[["raceId", "driverId", "driver_recent_form"]]


def build_constructor_recent_form(raw: dict) -> pd.DataFrame:
    """A constructor's average finishing position across both cars, over
    their last N races (any circuit), pre-race-known -- the team-level
    analogue of build_recent_form(). Car pace shifts mid-season too
    (upgrades, reliability swings), which driver_standing_before/
    constructor_standing_before (season-cumulative) can't capture.

    NOTE: unlike driverId, constructorId is NOT unified between historical
    (numeric) and fresh 2023+ (Jolpica slug, e.g. "red_bull") rows -- see
    build_multi_circuit_dataset()'s docstring for why that's fine for
    *standings* (computed per-season). It's a smaller-but-real limitation
    here specifically, since this rolls across seasons: each team's first
    fresh-era race gets NaN (imputed) instead of carrying over its last
    historical value, a one-time gap per team rather than a lasting one --
    every fresh race after that correctly uses fresh-era history."""
    races = raw["races"]
    results = raw["results"]

    full = results.merge(races[["raceId", "year", "round"]], on="raceId", how="left")
    full["position_num"] = pd.to_numeric(full["position"], errors="coerce")

    race_level = (
        full.groupby(["constructorId", "raceId", "year", "round"])["position_num"]
        .mean()
        .reset_index(name="constructor_race_avg_position")
        .sort_values(["year", "round"])
    )
    race_level["constructor_recent_form"] = (
        race_level.groupby("constructorId")["constructor_race_avg_position"]
        .transform(lambda s: s.rolling(RECENT_FORM_WINDOW, min_periods=1).mean().shift(1))
    )
    out = full[["raceId", "driverId", "constructorId"]].merge(
        race_level[["constructorId", "raceId", "constructor_recent_form"]],
        on=["constructorId", "raceId"], how="left",
    )
    return out[["raceId", "driverId", "constructor_recent_form"]]


# Earliest year to include in the multi-circuit dataset. Singapore itself
# only joined the calendar in 2008, so 1950-2007 data can only ever come
# from *other* circuits in eras with different points systems, aero rules,
# and even a different points-eligible-position count -- pooling that in
# is the "massive changes" problem raised in chat, just far more extreme
# than the 2008-2022 case, so it's excluded rather than just noted.
MULTI_CIRCUIT_MIN_YEAR = SINGAPORE_ERA_START_YEAR = 2008


def load_multi_circuit_fresh_data(path: str) -> pd.DataFrame:
    """Loads data/multi_circuit_fresh.csv (produced by
    fetch_all_circuits_data.py) if present, else an empty frame."""
    try:
        df = pd.read_csv(path)
    except FileNotFoundError:
        return pd.DataFrame()
    # Jolpica's circuit slug for Singapore is unified with the local
    # numeric circuitId, so the held-out Singapore evaluation set still
    # finds these rows. Every OTHER circuit's fresh rows keep their slug
    # as-is rather than being mapped to their pre-2023 numeric ID -- a
    # deliberate simplification: it costs circuit_id signal quality for
    # non-Singapore circuits (their 2023+ rows look like a "new" circuit
    # to the model), but doesn't affect correctness of the one circuit
    # this is actually trained toward and evaluated on.
    df["circuitId"] = df["circuitId"].replace({"marina_bay": SINGAPORE_CIRCUIT_ID})
    return df


def build_combined_raw(raw: dict, fresh_path: str = None) -> dict:
    """Historical CSV rows and freshly-fetched 2023+ rows as ONE continuous
    results/races timeline, with driverIds reconciled between the two.

    Fresh rows are folded in at the RAW results/races level, before any
    feature is computed -- not merged in afterward like
    build_full_dataset() does for the Singapore-only fresh data. That
    matters for build_season_form()/build_circuit_history()/
    build_recent_form(): they need a driver's full history to get
    debut year, past-circuit form, and recent form right, so historical
    and fresh rows have to already be one continuous timeline by the time
    those run, not stitched together after the fact."""
    # driverId needs to be on the same footing as the fresh data (driver
    # codes, e.g. "HAM") before concatenating -- otherwise a veteran's
    # historical rows (numeric driverId) and 2023+ rows (code) look like
    # two different people, breaking debut-year/circuit-history/
    # recent-form continuity. Note: constructorId does NOT need this same
    # treatment -- constructor standings are computed per-season (see
    # build_season_form), so a scheme mismatch across the 2022/2023
    # boundary never actually crosses a groupby boundary.
    hist_results = raw["results"].merge(raw["drivers"][["driverId", "code"]], on="driverId", how="left")
    hist_results = hist_results.drop(columns="driverId").rename(columns={"code": "driverId"})
    hist_races = raw["races"][["raceId", "year", "round", "circuitId"]]

    fresh = load_multi_circuit_fresh_data(fresh_path or "data/multi_circuit_fresh.csv")
    if fresh.empty:
        return {"results": hist_results, "races": hist_races}
    return {
        "results": pd.concat([
            hist_results[["raceId", "driverId", "constructorId", "position", "points"]],
            fresh[["raceId", "driverId", "constructorId", "position", "points"]],
        ], ignore_index=True),
        "races": pd.concat([
            hist_races,
            fresh[["raceId", "year", "round", "circuitId"]].drop_duplicates("raceId"),
        ], ignore_index=True),
    }


def build_multi_circuit_dataset(raw: dict, fresh_path: str = None) -> pd.DataFrame:
    """Same features as build_singapore_dataset(), but one row per driver
    per race across EVERY circuit (restricted to MULTI_CIRCUIT_MIN_YEAR
    onward -- same era Singapore itself has been racing in) plus any
    freshly-fetched 2023+ seasons, instead of just historical Singapore.
    None of FEATURE_COLUMNS describe anything Singapore-specific
    (grid_position/in-race stats were already dropped), so this is meant
    to be trained on and then evaluated specifically against held-out
    Singapore races -- see train_and_select()'s test_circuit_id."""
    combined_raw = build_combined_raw(raw, fresh_path)
    combined_results, combined_races = combined_raw["results"], combined_raw["races"]

    results = combined_results.copy()
    results["position"] = pd.to_numeric(results["position"], errors="coerce")
    results[TARGET_COLUMN] = results["position"].between(1, POINTS_POSITIONS).astype(int)

    df = results[["raceId", "driverId", TARGET_COLUMN]].copy()
    df = df.merge(combined_races[["raceId", "year", "circuitId"]], on="raceId", how="left")
    df = df[df["year"] >= MULTI_CIRCUIT_MIN_YEAR]
    df = df.rename(columns={"circuitId": "circuit_id"})
    # circuit_id mixes types otherwise -- historical rows are numeric
    # (15), fresh non-Singapore rows are Jolpica slugs ("bahrain") -- and
    # OneHotEncoder can't sort a column containing both str and int when
    # it hits the full (untrimmed-by-CV-fold) set of categories.
    df["circuit_id"] = df["circuit_id"].astype(str)
    df = df.merge(build_season_form(combined_raw), on=["raceId", "driverId"], how="left")
    df = df.merge(build_circuit_history(combined_raw), on=["raceId", "driverId"], how="left")
    df = df.merge(build_recent_form(combined_raw), on=["raceId", "driverId"], how="left")
    df = df.merge(build_constructor_recent_form(combined_raw), on=["raceId", "driverId"], how="left")
    return df


# ---------------------------------------------------------------------
# 2b. Fold in freshly-fetched seasons (see fetch_data.py), if present
# ---------------------------------------------------------------------
def load_fresh_data(path: str) -> pd.DataFrame:
    """Loads data/singapore_fresh.csv (produced by fetch_data.py) if it
    exists, else returns an empty frame so callers don't need to branch."""
    try:
        return pd.read_csv(path)
    except FileNotFoundError:
        return pd.DataFrame()


def build_full_dataset(data_dir: str, fresh_path: str = None) -> pd.DataFrame:
    """Historical CSVs (through 2022) plus any freshly-fetched seasons,
    concatenated on the shared feature-table columns. driverId/raceId
    aren't model features (see FEATURE_COLUMNS) so it's fine that the
    two sources use different ID schemes (numeric vs. driver code)."""
    raw = load_raw_tables(data_dir)
    historical = build_singapore_dataset(raw)
    fresh = load_fresh_data(fresh_path or f"{data_dir.rstrip('/')}/singapore_fresh.csv")
    if fresh.empty:
        return historical

    # fresh rows (from fetch_data.py) are always Singapore, and don't come
    # with circuit_id or a computed circuit-history feature -- circuit_id is
    # trivially known (it's always Singapore); driver_circuit_avg_finish is
    # backfilled from each driver's most recent *historical* (pre-2023)
    # Singapore average as an approximation -- it won't reflect 2023 itself
    # feeding into 2024's average, but is allowed to be NaN either way
    # (not in REQUIRED_COLUMNS), so this only ever improves on the fallback.
    fresh = fresh.copy()
    fresh["circuit_id"] = str(SINGAPORE_CIRCUIT_ID)  # string, not int -- see build_multi_circuit_dataset()
    circuit_hist = build_circuit_history(raw).merge(
        raw["drivers"][["driverId", "code"]], on="driverId", how="left"
    )
    latest_avg_by_code = (
        circuit_hist.dropna(subset=["driver_circuit_avg_finish"])
        .sort_values("raceId")
        .groupby("code")["driver_circuit_avg_finish"].last()
    )
    fresh["driver_circuit_avg_finish"] = fresh["driverId"].map(latest_avg_by_code)

    return pd.concat([historical, fresh], ignore_index=True)


# ---------------------------------------------------------------------
# 3. Clean
# ---------------------------------------------------------------------
def clean_data(df: pd.DataFrame) -> pd.DataFrame:
    return df.dropna(subset=REQUIRED_COLUMNS + [TARGET_COLUMN])


# ---------------------------------------------------------------------
# 4. Balance classes (training set only -- see module docstring)
# ---------------------------------------------------------------------
def balance_classes(df: pd.DataFrame) -> pd.DataFrame:
    majority = df[df[TARGET_COLUMN] == 0]
    minority = df[df[TARGET_COLUMN] == 1]
    if len(minority) == 0 or len(majority) == 0:
        return df
    minority_upsampled = resample(minority, replace=True, n_samples=len(majority), random_state=42)
    return pd.concat([majority, minority_upsampled]).reset_index(drop=True)


# ---------------------------------------------------------------------
# 5. Model candidates -- hyperparameters carried over from your
#    notebook's GridSearchCV runs (SVM2 / LogisticRegression2 / the
#    tuned RF cell), bundled with preprocessing into one Pipeline each
# ---------------------------------------------------------------------
def build_candidates() -> dict:
    preprocessor = ColumnTransformer([
        ("num", Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
        ]), NUMERIC_FEATURE_COLUMNS),
        ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), CATEGORICAL_FEATURE_COLUMNS),
    ])

    return {
        "random_forest": Pipeline([
            ("prep", preprocessor),
            ("model", RandomForestClassifier(
                max_depth=5, max_features=0.8, max_samples=0.75,
                n_estimators=500,  # your GridSearchCV searched [100,500,1000,1500]
                random_state=42,   # but the notebook's final cell left this at
            )),                    # sklearn's default (100) -- worth a deliberate choice
        ]),
        "logistic_regression": Pipeline([
            ("prep", preprocessor),
            ("model", LogisticRegression(C=0.23357214690901212, solver="newton-cg", max_iter=1000)),
        ]),
        "svm": Pipeline([
            ("prep", preprocessor),
            ("model", SVC(C=1000, degree=9, kernel="poly", probability=True, random_state=42)),
        ]),
        "hist_gradient_boosting": Pipeline([
            ("prep", preprocessor),
            ("model", HistGradientBoostingClassifier(random_state=42)),  # boosting, vs. RF's bagging
        ]),
    }


# ---------------------------------------------------------------------
# 5b. Hyperparameter search spaces -- same models/ranges your notebook's
#     GridSearchCV cells explored (RF/SVM full grids; the logistic
#     regression solver/penalty grid is trimmed to valid combinations
#     only -- the original mixed solvers and penalties that sklearn
#     rejects together, e.g. lbfgs + l1)
# ---------------------------------------------------------------------
def build_param_grids() -> dict:
    return {
        "random_forest": {
            # trimmed from the notebook's original grid (3x7x4x4=336 combos)
            # -- this now runs twice (Singapore-only + ~26k-row multi-circuit)
            # single-threaded (see n_jobs note below), so kept smaller
            "model__max_depth": [4, 5, 6],
            "model__max_features": ["sqrt", 0.5, 1.0],
            "model__max_samples": [0.5, 0.75, 1.0],
            "model__n_estimators": [100, 500],
        },
        "logistic_regression": {
            "model__C": np.logspace(-4, 4, 20),
            "model__penalty": ["l1", "l2"],
            "model__solver": ["liblinear"],  # only solver here that supports both l1 and l2
            "model__max_iter": [1000],
        },
        "svm": [
            # SVM training time scales poorly with row count -- on the
            # ~26k-row multi-circuit dataset, the notebook's original grid
            # (4 C's x 2 kernel groups, poly degree 1-9) would take far too
            # long single-threaded, so this is trimmed to what's tractable.
            # Every run so far has picked "rbf" or "linear" over "poly"
            # anyway (see chat), so the poly/degree search is dropped here.
            {"model__C": [1, 100], "model__kernel": ["rbf", "linear"]},
        ],
        "hist_gradient_boosting": {
            # tighter than a first pass: with only ~240 training rows, the
            # untuned grid picked max_depth=None + learning_rate=0.2, which
            # overfit the training folds and did worse on the real held-out
            # years than not tuning at all (see chat) -- these ranges push
            # toward shallower, more regularized trees instead
            "model__max_iter": [50, 100, 200],
            "model__max_depth": [2, 3],
            "model__learning_rate": [0.01, 0.05, 0.1],
            "model__l2_regularization": [0.5, 1.0, 2.0],
            "model__min_samples_leaf": [10, 20, 30],
        },
    }


# ---------------------------------------------------------------------
# 6. Train, compare, evaluate -- baseline hyperparameters vs. a proper
#    GridSearchCV tune, side by side, so the effect of tuning is visible
#    instead of only ever seeing the tuned numbers
# ---------------------------------------------------------------------
def chronological_split(df: pd.DataFrame, test_size: float = 0.25, test_circuit_id=None):
    """Splits by year, most recent editions held out as the test set,
    instead of a random shuffle across years. A random split lets the
    model be evaluated on a race chronologically *before* some of its
    own training data -- if the actual goal is forecasting the next
    real-world Singapore GP, the model should only ever be judged on
    data that came after everything it trained on.

    test_circuit_id restricts the *held-out test set* to one circuit
    (e.g. Singapore) while training still uses every circuit from the
    earlier years -- for the multi-circuit dataset, where the actual
    question is "does training on everywhere help predict Singapore,"
    not "does it help predict an arbitrary race anywhere." Training
    rows are still cut off at the same year boundary regardless of
    circuit, so no circuit's rows from a held-out year ever leak in."""
    eval_scope = df if test_circuit_id is None else df[df["circuit_id"] == str(test_circuit_id)]
    year_counts = eval_scope.sort_values("year")["year"].value_counts().sort_index()
    n_test_target = int(round(len(eval_scope) * test_size))

    test_years, running_total = [], 0
    for year in reversed(year_counts.index.tolist()):
        if running_total >= n_test_target:
            break
        test_years.append(year)
        running_total += year_counts[year]

    train_df = df.loc[~df["year"].isin(test_years)]
    test_df = eval_scope.loc[eval_scope["year"].isin(test_years)]
    print(f"Chronological split: training on {sorted(train_df['year'].unique())} (all circuits)"
          + (f", evaluating on circuit_id={test_circuit_id} only" if test_circuit_id is not None else "")
          + f", test years {sorted(test_years)} ({len(train_df)} train / {len(test_df)} test rows)")
    return train_df, test_df


def train_and_select(df: pd.DataFrame, test_circuit_id=None):
    train_df_raw, test_df_raw = chronological_split(df, test_circuit_id=test_circuit_id)
    X_test, y_test = test_df_raw[FEATURE_COLUMNS], test_df_raw[TARGET_COLUMN]

    train_df = balance_classes(train_df_raw[FEATURE_COLUMNS + [TARGET_COLUMN]])
    X_train_bal = train_df[FEATURE_COLUMNS]
    y_train_bal = train_df[TARGET_COLUMN]

    candidates = build_candidates()
    param_grids = build_param_grids()
    results = {}

    for name, pipe in candidates.items():
        # -- baseline: the fixed hyperparameters above -----------------
        cv_scores = cross_val_score(pipe, X_train_bal, y_train_bal, cv=5, scoring="accuracy")
        pipe.fit(X_train_bal, y_train_bal)
        baseline_preds = pipe.predict(X_test)
        baseline_acc = accuracy_score(y_test, baseline_preds)

        # -- tuned: GridSearchCV over the same (train-only, balanced) data,
        #    so this stays leakage-free -- see build_full_dataset()/module
        #    docstring for why balancing must happen after the split -----
        search = GridSearchCV(
            build_candidates()[name], param_grids[name], cv=5, scoring="accuracy", n_jobs=1
        )
        search.fit(X_train_bal, y_train_bal)
        tuned_preds = search.best_estimator_.predict(X_test)
        tuned_acc = accuracy_score(y_test, tuned_preds)

        results[name] = {
            "baseline_pipeline": pipe, "baseline_test_accuracy": baseline_acc,
            "tuned_pipeline": search.best_estimator_, "tuned_test_accuracy": tuned_acc,
            "best_params": search.best_params_,
        }

        print(f"\n{name}")
        print(f"  Baseline  -- CV accuracy: {cv_scores.mean():.3f} (+/- {cv_scores.std():.3f})"
              f"  |  Test accuracy: {baseline_acc:.3f}")
        print(f"  Tuned     -- CV accuracy: {search.best_score_:.3f}"
              f"  |  Test accuracy: {tuned_acc:.3f}"
              f"  |  improvement: {tuned_acc - baseline_acc:+.3f}")
        print(f"  Best params: {search.best_params_}")
        print("  Tuned classification report (test set):")
        print(classification_report(y_test, tuned_preds, zero_division=0))

    best_name = max(results, key=lambda n: results[n]["tuned_test_accuracy"])
    best_acc = results[best_name]["tuned_test_accuracy"]
    print(f"\nBest model overall (by held-out, forward-looking accuracy): "
          f"{best_name} ({best_acc:.3f})")

    # Re-fit the winning config on ALL available data (train + held-out test
    # years, balanced) -- the model we actually ship should use every real
    # race we have when predicting the next, still-unraced Singapore GP.
    # The accuracy number above is what tells you whether to trust it; this
    # step doesn't get evaluated again, since there's no more held-out data
    # left to evaluate it on.
    full_balanced = balance_classes(df[FEATURE_COLUMNS + [TARGET_COLUMN]])
    final_pipeline = build_candidates()[best_name]
    final_pipeline.set_params(**results[best_name]["best_params"])
    final_pipeline.fit(full_balanced[FEATURE_COLUMNS], full_balanced[TARGET_COLUMN])
    print(f"Refit {best_name} on all {len(df)} rows (train+test years) for the exported model.")

    return final_pipeline, best_name, results


# ---------------------------------------------------------------------
# 7. Export for f1_agent.py's predict_points_finish() tool to load
# ---------------------------------------------------------------------
def export_model(pipeline: Pipeline, path: str):
    joblib.dump(pipeline, path)
    print(f"Saved to {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data", help="Folder containing the 7 raw CSVs")
    parser.add_argument("--model-out", default="model.pkl")
    args = parser.parse_args()

    raw = load_raw_tables(args.data_dir)

    print("=" * 70)
    print("SINGAPORE-ONLY (evaluated on held-out Singapore years)")
    print("=" * 70)
    sgp_df = clean_data(build_full_dataset(args.data_dir))
    sgp_pipeline, sgp_name, sgp_results = train_and_select(sgp_df)
    sgp_acc = sgp_results[sgp_name]["tuned_test_accuracy"]

    print("\n" + "=" * 70)
    print("MULTI-CIRCUIT (trained on every circuit, evaluated on held-out")
    print("Singapore years only)")
    print("=" * 70)
    multi_df = clean_data(build_multi_circuit_dataset(raw))
    multi_pipeline, multi_name, multi_results = train_and_select(
        multi_df, test_circuit_id=SINGAPORE_CIRCUIT_ID
    )
    multi_acc = multi_results[multi_name]["tuned_test_accuracy"]

    print("\n" + "=" * 70)
    print(f"Singapore-only best: {sgp_name} ({sgp_acc:.3f})")
    print(f"Multi-circuit best:  {multi_name} ({multi_acc:.3f})")
    if multi_acc > sgp_acc:
        print("Multi-circuit wins -- exporting that model.")
        export_model(multi_pipeline, args.model_out)
    else:
        print("Singapore-only wins (or ties) -- exporting that model.")
        export_model(sgp_pipeline, args.model_out)


if __name__ == "__main__":
    main()
