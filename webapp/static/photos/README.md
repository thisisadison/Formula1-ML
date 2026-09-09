# Local photo bundle

Static files, served directly by FastAPI's StaticFiles mount -- no code
change needed to add a photo, just drop the file in with the right name.

Replaces the earlier live Wikipedia lookup (webapp/images.py, removed):
that depended on a third-party API returning a working URL on every
request, which repeatedly didn't (bad host, dead cache, wrong hostname).
A file that's just *there* can't have that problem.

## Naming (exact, case-sensitive)

- `drivers/<CODE>.jpg` -- the driver's 3-letter code as shown in the UI
  (VER, NOR, HAM, PIA, LEC, RUS, ...). Landscape or portrait both work;
  the frontend crops to fit. Missing files are fine -- the driver falls
  back to their colored initials, no broken-image icon.
- `teams/<id>.png` -- the constructor id (red_bull, mclaren, ferrari,
  mercedes, aston_martin, alpine, williams, rb, sauber, alfa, haas,
  cadillac, audi, ...). PNG so a transparent-background logo works;
  missing files fall back to no badge.

## Why photos only show on the most recent / next race

Driver codes get reused across eras -- VER is both Max Verstappen today
and Jean-Éric Vergne in 2012-2014 (see reference.py's CURRENT_GRID for
the full list of collisions). A photo keyed only by code would show the
wrong person's face on an old replay. The frontend only attempts a driver
photo when showing the current grid (the "Next race" tab, or a replay of
the single most recent completed race) -- never on an older replay,
where a code might not mean who it means today. Team badges don't have
this problem (a constructor id is unique across eras), so those show
everywhere.
