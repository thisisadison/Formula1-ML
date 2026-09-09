"""Background auto-update loop for the always-on deployment.

Runs as a daemon thread inside the same process as the web server --
there is no separate cron/launchd job to configure, because the running
server IS the always-on process this is meant to piggyback on. Every
CHECK_INTERVAL_SECONDS it asks Jolpica for the current season's results;
if a race has completed that isn't in data/multi_circuit_fresh.csv yet,
it fetches it and retrains.

Retraining runs pipeline.py as a SEPARATE PROCESS, not an in-process
function call. Two reasons: it's a ~20-30 minute CPU-bound GridSearchCV
sweep that would otherwise pin one core and starve FastAPI's event loop
for the whole duration, and a crash or an out-of-memory kill in the
subprocess can't take the web server down with it. The new model is
written to a temp path and moved into place with os.replace(), which is
atomic on the same filesystem -- a request arriving mid-retrain always
sees either the complete old file or the complete new one, never a
partial write.
"""

import datetime
import os
import subprocess
import sys
import threading
import time

CHECK_INTERVAL_SECONDS = int(os.environ.get("F1_CHECK_INTERVAL_SECONDS", 3 * 60 * 60))
RETRAIN_TIMEOUT_SECONDS = 60 * 60  # generous headroom over the ~20-30 min real run


class AutoUpdateScheduler:
    def __init__(self, live_data, data_dir: str, model_store, rebuild_service, project_root: str):
        self.live = live_data
        self.data_dir = data_dir
        self.model_store = model_store
        self.rebuild_service = rebuild_service
        self.project_root = project_root

        self._stop = threading.Event()
        self._busy = threading.Lock()
        self._thread = None

        self.state = "idle"  # idle | checking | retraining
        self.last_checked = None
        self.last_data_refresh = None  # new races published (precedes the retrain)
        self.last_retrained = None     # model rebuilt from them (much later)
        self.last_error = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="f1-auto-update")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {
            "state": self.state,
            "last_checked": self.last_checked,
            "last_data_refresh": self.last_data_refresh,
            "last_retrained": self.last_retrained,
            "last_error": self.last_error,
            "check_interval_seconds": CHECK_INTERVAL_SECONDS,
        }

    # -----------------------------------------------------------------
    def _loop(self) -> None:
        # Checks once immediately on startup (covers "the app was off
        # when the last race finished"), then on the regular interval.
        while not self._stop.is_set():
            self.check_now()
            self._stop.wait(CHECK_INTERVAL_SECONDS)

    def check_now(self, force: bool = False) -> dict:
        """Fetch the latest results; retrain if anything changed (or
        unconditionally, if force=True).

        Safe to call from multiple threads -- a manual trigger while the
        scheduled loop is already mid-retrain reports busy rather than
        running a second retrain in parallel, which would otherwise let
        two subprocesses race to write the same temp path.
        """
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "busy": True, "state": self.state}
        try:
            self.state = "checking"
            self.last_checked = time.time()
            year = datetime.date.today().year
            result = self.live.refresh_season(year)

            if not result.get("ok"):
                self.last_error = result.get("error")
                self.state = "idle"
                return {"ok": False, "error": self.last_error}

            if not (result.get("changed") or force):
                self.state = "idle"
                return {"ok": True, "changed": False}

            # Publish the new race data NOW, before the ~20-30 minute
            # retrain rather than after it. The rows are already on disk;
            # gating them behind training means the site reports a stale
            # cutoff and predicts on last week's standings for half an
            # hour after a race it has already downloaded. The existing
            # model applies perfectly well to fresher features -- it just
            # hasn't learned from the new race yet, which the retrain
            # below fixes on its own schedule.
            self.rebuild_service()
            self.last_data_refresh = time.time()

            self._retrain()
            return {"ok": self.last_error is None, "changed": True}
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.state = "idle"
            return {"ok": False, "error": self.last_error}
        finally:
            self._busy.release()

    def _retrain(self) -> None:
        self.state = "retraining"
        tmp_path = f"{self.model_store.path}.new"
        command = [
            sys.executable, "pipeline.py",
            "--data-dir", self.data_dir,
            "--model-out", tmp_path,
        ]
        try:
            subprocess.run(
                command, cwd=self.project_root, check=True,
                timeout=RETRAIN_TIMEOUT_SECONDS, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as exc:
            self.last_error = f"Retrain failed: {(exc.stderr or '')[-1500:]}"
            self.state = "idle"
            return
        except subprocess.TimeoutExpired:
            self.last_error = f"Retrain exceeded {RETRAIN_TIMEOUT_SECONDS}s and was aborted."
            self.state = "idle"
            return

        os.replace(tmp_path, self.model_store.path)
        reload_ok = self.model_store.reload()
        # reload() swaps the model in place on the shared ModelStore, so
        # every existing component already sees it; this rebuild is for the
        # cached feature tables, which were built before the retrain.
        self.rebuild_service()
        self.last_error = None if reload_ok else self.model_store.error
        self.last_retrained = time.time()
        self.state = "idle"
