"""
F1 Top-5 Finish Prediction Pipeline
====================================

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
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, classification_report
from sklearn.model_selection import cross_val_score, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
from sklearn.utils import resample

SINGAPORE_CIRCUIT_ID = 15  # circuitId for the Singapore GP in this dataset

FEATURE_COLUMNS = ["avg_pit_stop_s", "avg_lap_time_ms", "fastest_lap_time_ms", "grid_position"]
TARGET_COLUMN = "top5"


# ---------------------------------------------------------------------
# 1. Load the raw tables
# ---------------------------------------------------------------------
def load_raw_tables(data_dir: str) -> dict:
    p = lambda name: f"{data_dir.rstrip('/')}/{name}"
    return {
        "drivers": pd.read_csv(p("drivers.csv")),
        "laptimes": pd.read_csv(p("laptimes.csv")),
        "qualifying": pd.read_csv(p("qualifying.csv")),
        "races": pd.read_csv(p("races.csv")),
        "pits": pd.read_csv(p("sgppits.csv")),
        "results": pd.read_csv(p("results.csv")),
    }


def _parse_lap_time_to_ms(value) -> float:
    """Convert 'M:SS.mmm' lap-time strings (e.g. '1:34.500') to
    milliseconds. Missing/DNF values are recorded as '\\N'."""
    if pd.isna(value) or value == "\\N":
        return np.nan
    minutes, seconds = str(value).split(":")
    return (int(minutes) * 60 + float(seconds)) * 1000


# ---------------------------------------------------------------------
# 2. Build the Singapore GP dataset (replaces the notebook's loops)
# ---------------------------------------------------------------------
def build_singapore_dataset(raw: dict) -> pd.DataFrame:
    races = raw["races"]
    sgp_races = races.loc[races["circuitId"] == SINGAPORE_CIRCUIT_ID, ["raceId", "year"]]
    sgp_ids = sgp_races["raceId"]

    pits = raw["pits"][raw["pits"]["raceId"].isin(sgp_ids)]
    pit_avg = (
        pits.groupby(["raceId", "driverId"])["duration"]
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
    results[TARGET_COLUMN] = results["position"].between(1, 5).astype(int)  # DNF/NaN -> 0, same as the notebook

    df = pit_avg.merge(lap_avg, on=["raceId", "driverId"], how="outer")
    df = df.merge(grid, on=["raceId", "driverId"], how="outer")
    df = df.merge(
        results[["raceId", "driverId", "fastest_lap_time_ms", TARGET_COLUMN]],
        on=["raceId", "driverId"], how="outer",
    )
    df = df.merge(sgp_races, on="raceId", how="left")
    df = df.merge(raw["drivers"][["driverId", "code"]], on="driverId", how="left")
    return df


# ---------------------------------------------------------------------
# 3. Clean
# ---------------------------------------------------------------------
def clean_data(df: pd.DataFrame) -> pd.DataFrame:
    return df.dropna(subset=FEATURE_COLUMNS + [TARGET_COLUMN])


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
        ]), FEATURE_COLUMNS),
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
    }


# ---------------------------------------------------------------------
# 6. Train, compare, evaluate
# ---------------------------------------------------------------------
def train_and_select(df: pd.DataFrame):
    X = df[FEATURE_COLUMNS]
    y = df[TARGET_COLUMN]
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=42, stratify=y
    )

    train_df = X_train.copy()
    train_df[TARGET_COLUMN] = y_train.values
    train_df = balance_classes(train_df)
    X_train_bal = train_df[FEATURE_COLUMNS]
    y_train_bal = train_df[TARGET_COLUMN]

    candidates = build_candidates()
    results = {}

    for name, pipe in candidates.items():
        cv_scores = cross_val_score(pipe, X_train_bal, y_train_bal, cv=5, scoring="accuracy")
        pipe.fit(X_train_bal, y_train_bal)
        test_preds = pipe.predict(X_test)
        test_acc = accuracy_score(y_test, test_preds)
        results[name] = {"pipeline": pipe, "test_accuracy": test_acc}

        print(f"\n{name}")
        print(f"  CV accuracy (balanced train): {cv_scores.mean():.3f} (+/- {cv_scores.std():.3f})")
        print(f"  Test accuracy (real distribution): {test_acc:.3f}")
        print(classification_report(y_test, test_preds, zero_division=0))

    best_name = max(results, key=lambda n: results[n]["test_accuracy"])
    print(f"\nBest model: {best_name}")
    return results[best_name]["pipeline"], best_name


# ---------------------------------------------------------------------
# 7. Export for f1_agent.py's predict_top5() tool to load
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
    df = build_singapore_dataset(raw)
    df = clean_data(df)
    best_pipeline, _ = train_and_select(df)
    export_model(best_pipeline, args.model_out)


if __name__ == "__main__":
    main()
