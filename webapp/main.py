"""FastAPI app: JSON API plus the static frontend, in one process.

Run it with:
    uvicorn webapp.main:app --reload

Two clearly separate code paths, as reflected in the routes:
  /api/predictions/*  deterministic -- pipeline.py features into model.pkl
  /api/chat           agentic       -- Claude choosing tools over the same data
"""

import datetime
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import pipeline
from webapp.agent import RaceAnalyst
from webapp.images import ImageResolver
from webapp.live_data import LiveData
from webapp.model_store import ModelStore
from webapp.news import NewsService
from webapp.predictions import PredictionService
from webapp.reference import CIRCUITS, Reference
from webapp.scheduler import AutoUpdateScheduler

DATA_DIR = os.environ.get("F1_DATA_DIR", "data")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = FastAPI(title="F1 Points-Finish Predictor")

reference = Reference(pipeline.load_raw_tables(DATA_DIR))
model_store = ModelStore()
service = PredictionService(DATA_DIR, model_store, reference)
analyst = RaceAnalyst(service)
live = LiveData(DATA_DIR)
news = NewsService()


def _refresh_roster() -> None:
    """Pull this season's driver/constructor names into the reference
    tables, so anyone who joined the grid recently shows a real name (and
    therefore gets a photo) instead of a bare three-letter code."""
    roster = live.season_roster(datetime.date.today().year)
    if roster:
        reference.augment(roster)


def _rebuild_service() -> None:
    """Re-read the CSVs after a refresh has written new rows into them.

    PredictionService caches the whole combined timeline and its feature
    table at construction, so a refresh that isn't followed by this would
    keep serving predictions from the pre-refresh data.
    """
    global service, analyst
    _refresh_roster()
    service = PredictionService(DATA_DIR, model_store, reference)
    analyst = RaceAnalyst(service)


images = ImageResolver(os.path.join(DATA_DIR, "cache", "images.json"))

auto_update = AutoUpdateScheduler(live, DATA_DIR, model_store, _rebuild_service, PROJECT_ROOT)
auto_update.start()

# Off the startup path: a name lookup must never delay the server coming up,
# and the static tables are a perfectly good answer until it lands.
threading.Thread(target=_refresh_roster, daemon=True, name="f1-roster").start()


class ChatRequest(BaseModel):
    question: str


class ImageRequest(BaseModel):
    titles: list[str]


@app.get("/api/status")
def status():
    cutoff = service.data_cutoff()
    year, round_ = service.next_round_slot()
    return {
        "model": {
            "ready": model_store.ready,
            "name": model_store.name,
            "error": model_store.error,
            "features": pipeline.FEATURE_COLUMNS,
        },
        "data": {
            "cutoff": cutoff,
            "next_slot": {"year": year, "round": round_},
            "races": len(service.race_catalog()),
        },
        "live": live.status(),
        "auto_update": auto_update.status(),
        "assistant": {
            "available": analyst.available,
            "reason": analyst.unavailable_reason,
        },
    }


@app.get("/api/circuits")
def circuits():
    return [
        {"id": key, "name": name, "locality": locality, "country": country}
        for key, (name, locality, country) in sorted(CIRCUITS.items(), key=lambda kv: kv[1][0])
    ]


@app.get("/api/races")
def races(limit: int = 60):
    return service.race_catalog()[:limit]


@app.get("/api/schedule")
def schedule(year: int = None):
    """The season calendar with a completed/upcoming marker per round, so
    the UI can show what's already happened and what's next.

    Falls back to reconstructing a completed-only calendar from local
    data (no round names/dates for anything still to come) when Jolpica
    is unreachable -- same offline-first stance as the rest of the app.
    """
    year = year or datetime.date.today().year
    completed_by_round = {
        race["round"]: race for race in service.race_catalog() if race["year"] == year
    }
    next_year, next_round = service.next_round_slot()

    calendar = live.schedule(year)
    source = "live"
    if calendar is None:
        source = "local"
        calendar = [
            {"round": race["round"], "circuit_id": race["circuit"]["id"], "name": race["circuit"]["name"], "date": None}
            for race in completed_by_round.values()
        ]

    rounds = []
    for item in sorted(calendar, key=lambda entry: entry["round"]):
        completed_race = completed_by_round.get(item["round"])
        rounds.append({
            "round": item["round"],
            "name": item.get("name"),
            "date": item.get("date"),
            "circuit": reference.circuit(item["circuit_id"]),
            "completed": completed_race is not None,
            "race_id": completed_race["race_id"] if completed_race else None,
            "is_next": year == next_year and item["round"] == next_round,
        })
    return {"year": year, "rounds": rounds, "source": source}


def _require_model():
    if not model_store.ready:
        raise HTTPException(status_code=503, detail=model_store.error)


@app.get("/api/predictions/upcoming")
def predictions_upcoming(
    circuit: str,
    year: int = None,
    round_num: int = Query(None, alias="round"),
):
    """Predicted for the current grid at `circuit`, optionally labelled
    for a specific future round (year/round, as the season strip sends).

    The features are ALWAYS computed at the slot immediately after the
    last completed race, whatever round is being asked about. That slot
    is the only one where the championship-standing features mean
    anything: build_season_form ranks drivers on points accumulated
    earlier in the SAME season, so slotting a placeholder into a season
    with no completed races yet gives every driver zero points and ties
    the entire grid at P1 -- silently destroying the model's strongest
    feature and leaving it to guess from circuit history alone.

    Nothing between now and a later round is knowable anyway, so today's
    standings are the honest input for any future race; only the label
    changes.
    """
    _require_model()
    entrants = service.latest_entry_list()
    slot_year, slot_round = service.next_round_slot()
    rows = service.predict_upcoming(circuit, entrants, slot_year, slot_round)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No grid available for '{circuit}'.")
    return {
        "mode": "upcoming",
        "circuit": reference.circuit(circuit),
        "predicted_for": {
            "year": year if year is not None else slot_year,
            "round": round_num if round_num is not None else slot_round,
        },
        "form_as_of": service.data_cutoff(),
        "based_on": service.data_cutoff(),
        "drivers": rows,
    }


@app.get("/api/predictions/race/{race_id}")
def predictions_race(race_id: str):
    _require_model()
    rows = service.predict_known_race(race_id)
    race = next((item for item in service.race_catalog() if item["race_id"] == race_id), None)
    if not rows or race is None:
        raise HTTPException(status_code=404, detail=f"No race '{race_id}'.")
    scored = [row for row in rows if row["actual_points"] is not None]
    correct = sum(1 for row in scored if row["predicted_points"] == row["actual_points"])
    return {
        "mode": "replay",
        "race": race,
        "accuracy": round(correct / len(scored), 3) if scored else None,
        "drivers": rows,
    }


@app.post("/api/refresh")
def refresh(year: int):
    result = live.refresh_season(year)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "Refresh failed."))
    if result.get("changed"):
        _rebuild_service()
    return {**result, "cutoff": service.data_cutoff()}


@app.post("/api/auto-update/check-now")
def auto_update_check_now(force: bool = False):
    """Trigger the same check-and-retrain cycle the background scheduler
    runs every F1_CHECK_INTERVAL_SECONDS, without waiting for it.

    Retraining takes ~20-30 minutes, so this returns immediately and the
    work happens on its own thread -- poll /api/status's auto_update
    field (state: "checking" | "retraining" | "idle") for progress.
    force=true retrains even if no new race was found, mainly useful for
    verifying the pipeline end-to-end without waiting for a real race.
    """
    if auto_update.state != "idle":
        return {"started": False, "reason": "busy", "state": auto_update.state}
    threading.Thread(target=auto_update.check_now, kwargs={"force": force}, daemon=True).start()
    return {"started": True}


@app.post("/api/images")
def resolve_images(request: ImageRequest):
    """Batch Wikipedia thumbnail lookup, called after the page has
    already rendered -- photos fill in, they are never waited on."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        titles = request.titles[:40]
        resolved = list(pool.map(images.lookup, titles))
    return dict(zip(titles, resolved))


@app.get("/api/news")
def get_news(force: bool = False):
    return news.headlines(force=force)


@app.post("/api/chat")
def chat(request: ChatRequest):
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Ask something first.")
    return analyst.ask(question)


@app.on_event("shutdown")
def _stop_scheduler():
    auto_update.stop()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
