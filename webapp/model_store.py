"""Loads the model pipeline.py exported and runs it.

Deliberately does NOT train: training is pipeline.py's job (two
GridSearchCV sweeps, minutes of CPU), and doing it lazily inside a web
request would be a trap. If model.pkl is missing or was pickled by a
different scikit-learn version, the app still starts -- the dashboard
reports that the model needs rebuilding instead of failing to boot, so
the news and assistant tabs keep working.
"""

import os

import joblib
import pandas as pd

from pipeline import FEATURE_COLUMNS

DEFAULT_MODEL_PATH = os.environ.get("F1_MODEL_PATH", "model.pkl")


class ModelStore:
    def __init__(self, path: str = DEFAULT_MODEL_PATH):
        self.path = path
        self.model = None
        self.error = None
        try:
            self.model = joblib.load(path)
        except FileNotFoundError:
            self.error = (
                f"{path} not found. Build it with: python pipeline.py --data-dir data"
            )
        except Exception as exc:  # unpickling across sklearn versions
            self.error = (
                f"{path} could not be loaded ({type(exc).__name__}: {exc}). "
                "It was most likely pickled by a different scikit-learn version -- "
                "rebuild it with: python pipeline.py --data-dir data"
            )

    @property
    def ready(self) -> bool:
        return self.model is not None

    @property
    def name(self) -> str:
        if not self.ready:
            return "unavailable"
        return type(self.model.named_steps["model"]).__name__

    def predict_top5_proba(self, features: pd.DataFrame) -> pd.Series:
        """P(top-5 finish) for each row, indexed like the input.

        The exported object is a full sklearn Pipeline whose
        ColumnTransformer selects by column name, so the input has to be
        a DataFrame with FEATURE_COLUMNS -- passing a bare array fails.
        """
        if not self.ready:
            raise RuntimeError(self.error)
        ordered = features[FEATURE_COLUMNS]
        proba = self.model.predict_proba(ordered)
        top5_column = list(self.model.classes_).index(1)
        return pd.Series(proba[:, top5_column], index=features.index)
