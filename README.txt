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
- Predictions tab (deterministic, no LLM): pick a circuit for a "next race" prediction against the
  current grid, or replay any past race to see what the model would have said beforehand versus what
  actually happened. Every driver expands to show the feature values behind their number.
- Assistant tab (agentic): a Claude agent with read-only tools over the same model and feature table.
  Needs ANTHROPIC_API_KEY; the rest of the site works without it.
- News tab: RSS from Formula1.com, Autosport, Motorsport.com and BBC Sport, read server-side and
  linked out to.
- Staying current: POST /api/refresh?year=YYYY pulls that season from the Jolpica-F1 API into
  data/multi_circuit_fresh.csv and rebuilds the feature table. Everything degrades to the bundled
  CSVs when offline.

# Project Overview
- Project demonstrates cleaning, filtering and visualising of large datasets using Python libraries such as Pandas and self-wrote helper functions.
- Performed Exploratory Data Analysis for extraction of best predictors to maximise model performance sing Python libraries such as MatplotLib and Seaborn.
- Application of Basic Machine Learning Models such as Random Forest Regression, Logistic Regression and Support Vector Machines (SVM) using Scikit-Learn and Numpy.
- Refer to the Project Slides on Microsoft PPT for project and results summary.
- Data used in the attached project is taken from Kaggle and Official Formula 1.

