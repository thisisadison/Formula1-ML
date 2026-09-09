# Formula 1 Data Science Project🚀

# Project Objective
- The attached contains files documenting my Machine Learning Semester Project. 
- Our project allows for real-time predictions for whether a Formula 1 driver will finish in the Top 5 of the Singapore Grand Prix using live predictors such as "Average Lap Times", "Average Pit Stop Times", "Fastest Lap Time" and "Starting Grid Positions".
- This model is trained on all data that exists for every iteration of the Singapore Grand Prix since it began in 2008. Teams can utilise this to determine what the outcome of a driver's race will be and the factors they need to improve on during the race to achieve better results.

# Web App (webapp/)
- A website over the trained model: predictions dashboard, an agentic race analyst, and an F1 news tab.
- Setup:
    pip install -r requirements.txt
    python pipeline.py --data-dir data     # produces model.pkl -- required
    uvicorn webapp.main:app --reload       # http://127.0.0.1:8000
- Predictions tab (deterministic, no LLM): a season strip up top marks every round DONE or NEXT, so
  you can see at a glance what's already happened and what to watch for. Click a completed round to
  replay it (what the model would have said beforehand vs. what actually happened, side by side with
  a called-right/missed verdict), or an upcoming one for a live prediction against the current grid
  -- either way every driver shows a photo (from Wikipedia, Commons-licensed) next to a small team
  badge, and expands to the feature values behind their number. The 5 highest-probability drivers are
  marked with a red edge -- that's the model's actual top-5 pick, exactly 5 of them, not a raw
  percentage cutoff (which could mark 3 or 8 drivers "predicted" depending on how confident the model
  is, and disagree with what's visibly in the top 5 rows). Or skip the strip and pick any circuit
  freely via the chips below it.
- Assistant tab (agentic): a Claude agent with read-only tools over the same model and feature table.
  Needs ANTHROPIC_API_KEY; the rest of the site works without it.
- News tab: RSS from Formula1.com, Autosport, Motorsport.com and BBC Sport, read server-side and
  linked out to, with filter chips by TOPIC (Drivers / Teams / Regulations / Race Weekend / Other) --
  each headline is tagged by keyword match against its title and summary, not sorted by which outlet
  wrote it. Refetched at most every 3.5 days (F1_NEWS_TTL_SECONDS to change it) -- headlines don't
  need to be as fresh as race data, so this is deliberately much lazier than the race-data refresh
  below.
- Staying current, automatically: while the server is running, a background thread checks the
  Jolpica-F1 API every 3 hours (F1_CHECK_INTERVAL_SECONDS to change it) for a newly-completed race.
  If one is found, it fetches the results AND retrains the model (runs pipeline.py as a subprocess,
  ~20-30 min), then hot-swaps the new model.pkl into the running server -- no restart needed. Poll
  /api/status's "auto_update" field (state: checking / retraining / idle) for progress, or the nav bar
  dot shows "Retraining on latest race..." live. For this to actually run after every race, the
  server process needs to be left running continuously (e.g. `uvicorn webapp.main:app`, without
  --reload, kept up on your machine) -- it only checks while it's up.
  POST /api/auto-update/check-now?force=true triggers a check immediately instead of waiting for the
  next scheduled one (useful for testing). POST /api/refresh?year=YYYY does the data-fetch half only,
  without retraining, if that's ever what you want on its own.

# Project Overview
- Project demonstrates cleaning, filtering and visualising of large datasets using Python libraries such as Pandas and self-wrote helper functions.
- Performed Exploratory Data Analysis for extraction of best predictors to maximise model performance sing Python libraries such as MatplotLib and Seaborn.
- Application of Basic Machine Learning Models such as Random Forest Regression, Logistic Regression and Support Vector Machines (SVM) using Scikit-Learn and Numpy.
- Refer to the Project Slides on Microsoft PPT for project and results summary.
- Data used in the attached project is taken from Kaggle and Official Formula 1.

