"""The agentic half of the site, and the only place an LLM is called.

Same shape as f1_agent.py's tool-use loop -- ask, let Claude pick a tool,
run it locally, hand the result back, repeat -- but the tools here read
through PredictionService, so the assistant and the dashboard are always
quoting the same model and the same feature table. Claude never sees the
raw CSVs and never computes a prediction itself; it can only call these
functions and summarise what they return.

Kept deliberately separate from predictions.py: nothing on the dashboard
imports this module, so a missing API key or a slow API call can't affect
the deterministic path.
"""

import json
import os

from webapp.reference import CIRCUITS

MODEL = os.environ.get("F1_AGENT_MODEL", "claude-sonnet-5")
MAX_STEPS = 6

SYSTEM_PROMPT = """You are the race analyst built into an F1 prediction site.

The site's model predicts whether a driver finishes in the points (top 10),
from pre-qualifying information only: championship standing going into the
race, constructor standing, seasons of experience, rookie status, the
driver's average finish at that circuit, and recent form over the last
three races for both driver and team. It deliberately does not use grid
position or anything from during the race.

Answer using the tools. Never invent a probability, a standing, or a
result -- if a tool did not return it, say you do not have it. Quote
probabilities as percentages. Be concise and specific: a couple of short
paragraphs at most, and lead with the answer rather than the method.
When a prediction looks surprising, explain which of the features drove
it. Note when the underlying data stops before the race being asked
about."""

TOOLS = [
    {
        "name": "predict_race",
        "description": (
            "Run the trained model over the current grid for a race at a given circuit "
            "and return each driver's probability of finishing in the points (top 10), plus "
            "the feature values behind it. Use this for any question about who will do well "
            "in an upcoming or hypothetical race."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "circuit": {
                    "type": "string",
                    "description": "Circuit id, e.g. 'marina_bay', 'monza', 'silverstone'.",
                },
                "top_n": {
                    "type": "integer",
                    "description": "How many drivers to return, highest probability first. Default 10.",
                },
            },
            "required": ["circuit"],
        },
    },
    {
        "name": "get_driver_form",
        "description": (
            "Current pre-race picture for one driver: championship and constructor "
            "standing, recent form over the last three races, team form, seasons of "
            "experience. Use for questions about how a driver is going right now."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "driver": {"type": "string", "description": "Three-letter code, e.g. 'VER', 'NOR'."},
            },
            "required": ["driver"],
        },
    },
    {
        "name": "get_circuit_history",
        "description": (
            "A driver's record at one circuit: every finishing position they have "
            "recorded there in the data, and their average. Use for questions about "
            "whether a track suits someone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "driver": {"type": "string", "description": "Three-letter code, e.g. 'HAM'."},
                "circuit": {"type": "string", "description": "Circuit id, e.g. 'marina_bay'."},
            },
            "required": ["driver", "circuit"],
        },
    },
    {
        "name": "get_past_race",
        "description": (
            "What the model predicted for a race that has already run, alongside what "
            "actually happened. Use to answer questions about the model's accuracy or "
            "about a specific past result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer"},
                "circuit": {"type": "string", "description": "Circuit id, e.g. 'marina_bay'."},
            },
            "required": ["year", "circuit"],
        },
    },
    {
        "name": "list_options",
        "description": (
            "The circuits and driver codes this data actually covers, and where the "
            "data stops. Call this first if a name in the question might not resolve."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


class RaceAnalyst:
    """Owns the tool implementations and the conversation loop."""

    def __init__(self, service):
        self.service = service
        self._client = None
        self._client_error = None

    # -----------------------------------------------------------------
    # Tools -- every one of these is a thin read over PredictionService
    # -----------------------------------------------------------------
    def _upcoming_rows(self, circuit: str) -> list:
        entrants = self.service.latest_entry_list()
        year, round_ = self.service.next_round_slot()
        return self.service.predict_upcoming(circuit, entrants, year, round_)

    def tool_predict_race(self, circuit: str, top_n: int = 10) -> dict:
        rows = self._upcoming_rows(circuit)
        if not rows:
            return {"error": f"No grid available to predict a race at '{circuit}'."}
        return {
            "circuit": self.service.reference.circuit(circuit),
            "prediction_only": "This race has not been run; there are no actual results.",
            "form_as_of": self.service.data_cutoff()["label"],
            "drivers": [{
                "driver": row["driver"]["name"],
                "code": row["driver"]["code"],
                "team": row["team"]["name"],
                "points_probability": round(row["points_probability"], 3),
                "features": {k: (None if v is None else round(v, 2)) for k, v in row["features"].items()},
            } for row in rows[:max(1, min(int(top_n), 20))]],
        }

    def tool_get_driver_form(self, driver: str) -> dict:
        code = driver.upper()
        rows = self._upcoming_rows("marina_bay")  # circuit only affects circuit history
        match = next((row for row in rows if row["driver"]["code"] == code), None)
        if match is None:
            return {"error": f"'{driver}' is not on the current grid in this data."}
        return {
            "driver": match["driver"]["name"],
            "code": code,
            "team": match["team"]["name"],
            "features": {k: (None if v is None else round(v, 2)) for k, v in match["features"].items()},
        }

    def tool_get_circuit_history(self, driver: str, circuit: str) -> dict:
        code = driver.upper()
        history = self.service.driver_circuit_record(code, circuit)
        if not history["appearances"]:
            return {
                "driver": code,
                "circuit": self.service.reference.circuit(circuit)["name"],
                "appearances": [],
                "note": "No recorded races for this driver at this circuit in the data.",
            }
        return history

    def tool_get_past_race(self, year: int, circuit: str) -> dict:
        race = self.service.find_race(int(year), circuit)
        if race is None:
            return {"error": f"No {year} race at '{circuit}' in the data."}
        rows = self.service.predict_known_race(race["race_id"])
        return {
            "race": race["label"],
            "drivers": [{
                "driver": row["driver"]["name"],
                "code": row["driver"]["code"],
                "team": row["team"]["name"],
                "points_probability": round(row["points_probability"], 3),
                "predicted_points": row["predicted_points"],
                "actual_position": row["actual_position"],
                "actual_points": row["actual_points"],
            } for row in rows],
        }

    def tool_list_options(self) -> dict:
        return {
            "circuits": [
                {"id": key, "name": name} for key, (name, _, _) in sorted(CIRCUITS.items())
            ],
            "drivers": [
                {"code": code, "name": self.service.reference.driver(code)["name"]}
                for code, _ in self.service.latest_entry_list()
            ],
            "data_stops_after": self.service.data_cutoff()["label"],
        }

    def _run_tool(self, name: str, payload: dict) -> str:
        handlers = {
            "predict_race": self.tool_predict_race,
            "get_driver_form": self.tool_get_driver_form,
            "get_circuit_history": self.tool_get_circuit_history,
            "get_past_race": self.tool_get_past_race,
            "list_options": self.tool_list_options,
        }
        handler = handlers.get(name)
        if handler is None:
            return json.dumps({"error": f"Unknown tool '{name}'."})
        try:
            return json.dumps(handler(**payload), default=str)
        except Exception as exc:
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

    # -----------------------------------------------------------------
    # Conversation
    # -----------------------------------------------------------------
    @property
    def client(self):
        if self._client is None and self._client_error is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                self._client_error = (
                    "ANTHROPIC_API_KEY is not set, so the assistant is offline. "
                    "The predictions dashboard does not need it."
                )
            else:
                try:
                    from anthropic import Anthropic

                    self._client = Anthropic()
                except Exception as exc:
                    self._client_error = f"Could not start the Anthropic client: {exc}"
        return self._client

    @property
    def available(self) -> bool:
        return self.client is not None

    @property
    def unavailable_reason(self):
        self.client  # resolve lazily so the reason is populated
        return self._client_error

    def ask(self, question: str, history: list = None) -> dict:
        """One turn. Returns the answer plus which tools were called, so
        the UI can show its working."""
        if not self.available:
            return {"answer": None, "error": self.unavailable_reason, "tool_calls": []}

        messages = list(history or [])
        messages.append({"role": "user", "content": question})
        tool_calls = []

        for _ in range(MAX_STEPS):
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            if response.stop_reason != "tool_use":
                answer = "".join(
                    block.text for block in response.content if block.type == "text"
                )
                return {"answer": answer.strip(), "error": None, "tool_calls": tool_calls}

            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool_calls.append({"name": block.name, "input": block.input})
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": self._run_tool(block.name, dict(block.input)),
                })
            messages.append({"role": "user", "content": results})

        return {
            "answer": None,
            "error": "The assistant kept calling tools without reaching an answer.",
            "tool_calls": tool_calls,
        }
