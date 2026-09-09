"""
F1 Race Agent -- a minimal tool-using agent built with the Claude API.

Architecture
------------
- Reuses pipeline.py's build_singapore_dataset() so the agent looks up
  drivers against the exact same feature table the model was trained on.
- Two tools: get_driver_stats() reads directly from that table,
  predict_top5() feeds the same row into the trained model.
- Claude decides which tool(s) to call based on the user's question.
  This script implements the full tool-use loop: ask -> Claude picks a
  tool -> we run it locally -> we send the result back -> Claude answers.

Setup
-----
1. pip install anthropic pandas scikit-learn joblib
2. Get an API key from the Claude Developer Platform (console.anthropic.com)
   and set it: export ANTHROPIC_API_KEY="sk-ant-..."
3. Run `python pipeline.py --data-dir data` first to produce model.pkl
4. python f1_agent.py
"""

import json

import joblib
import pandas as pd
from anthropic import Anthropic

from pipeline import FEATURE_COLUMNS, build_singapore_dataset, load_raw_tables

client = Anthropic()  # reads ANTHROPIC_API_KEY from your environment

# ---------------------------------------------------------------------
# 1. Build the same feature table the pipeline trained on, once, at
#    startup -- and load the model it exported.
# ---------------------------------------------------------------------
# TODO: point this at your real data directory (containing the 7 CSVs)
DATA_DIR = "data"

_raw = load_raw_tables(DATA_DIR)
SGP_DF = build_singapore_dataset(_raw)
MODEL = joblib.load("model.pkl")  # produced by `python pipeline.py`


def _lookup(driver: str, year: int):
    row = SGP_DF[
        (SGP_DF["code"].str.lower() == driver.lower()) & (SGP_DF["year"] == year)
    ]
    return row.iloc[0] if not row.empty else None


# ---------------------------------------------------------------------
# 2. The actual Python functions the agent is allowed to call
# ---------------------------------------------------------------------

def get_driver_stats(driver: str, year: int) -> str:
    """Look up a driver's avg lap time, avg pit stop time, and starting
    grid position for a given year's Singapore GP."""
    row = _lookup(driver, year)
    if row is None:
        return f"No data found for {driver} in {year}."
    return json.dumps({
        "driver": driver,
        "year": year,
        # cast off numpy dtypes -- json.dumps can't serialize np.int64/float64
        "avg_pit_stop_s": float(row["avg_pit_stop_s"]),
        "avg_lap_time_ms": float(row["avg_lap_time_ms"]),
        "grid_position": int(row["grid_position"]),
    })


def predict_top5(driver: str, year: int) -> str:
    """Run the trained model (whichever of RF / Logistic Regression / SVM
    pipeline.py selected) to predict whether a driver finishes top 5."""
    row = _lookup(driver, year)
    if row is None:
        return f"No data found for {driver} in {year}."
    # the trained pipeline's ColumnTransformer selects columns by name, so
    # this needs to be a one-row DataFrame with matching column names --
    # a plain list/array fails with "columns using strings is only
    # supported for dataframes"
    features = pd.DataFrame([row[FEATURE_COLUMNS]], columns=FEATURE_COLUMNS)
    prediction = MODEL.predict(features)[0]
    return json.dumps({
        "driver": driver,
        "year": year,
        "predicted_top5": bool(prediction),
    })


# ---------------------------------------------------------------------
# 3. Describe the tools to Claude (this is what lets it "decide")
# ---------------------------------------------------------------------
tools = [
    {
        "name": "get_driver_stats",
        "description": "Get a driver's race stats (avg pit stop time, avg lap time, grid position) for a given year's Singapore GP.",
        "input_schema": {
            "type": "object",
            "properties": {
                "driver": {"type": "string", "description": "Driver code, e.g. 'HAM' or 'VER'"},
                "year": {"type": "integer", "description": "Season year, e.g. 2019"},
            },
            "required": ["driver", "year"],
        },
    },
    {
        "name": "predict_top5",
        "description": "Predict whether a driver will finish in the top 5 of the Singapore GP, using the trained ML model.",
        "input_schema": {
            "type": "object",
            "properties": {
                "driver": {"type": "string"},
                "year": {"type": "integer"},
            },
            "required": ["driver", "year"],
        },
    },
]

TOOL_FUNCTIONS = {
    "get_driver_stats": get_driver_stats,
    "predict_top5": predict_top5,
}


# ---------------------------------------------------------------------
# 4. The agent loop
# ---------------------------------------------------------------------

def ask_agent(question: str, max_steps: int = 5) -> str:
    messages = [{"role": "user", "content": question}]

    for _ in range(max_steps):
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            return "".join(
                block.text for block in response.content if block.type == "text"
            )

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = TOOL_FUNCTIONS[block.name]
                result = fn(**block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": tool_results})

    return "Ran out of steps -- the agent didn't converge on an answer."


if __name__ == "__main__":
    while True:
        q = input("\nAsk about F1 (or 'quit'): ")
        if q.lower() == "quit":
            break
        print(ask_agent(q))
