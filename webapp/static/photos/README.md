# Local photo bundle

Static files, served directly by FastAPI's StaticFiles mount -- no code
change needed to add a photo, just drop the file in with the right name.

Replaces the earlier live Wikipedia lookup (webapp/images.py, removed):
that depended on a third-party API returning a working URL on every
request, which repeatedly didn't (bad host, dead cache, wrong hostname).
A file that's just *there* can't have that problem.

## Naming

Name a file after the driver or the team and it will be found --
`webapp/photos.py` scans these directories and matches on a normalised
key, so all of these work and none of them need a code change:

- `drivers/max_verstappen.JPG`, `drivers/Max Verstappen.jpg`,
  `drivers/VER.jpg` -- full name, surname alone, or the 3-letter code.
- Any of `.jpg .jpeg .png .webp .avif`, upper or lower case.
- Accents and umlauts are folded, so `nico_hulkenberg.JPG` matches
  "Nico Hülkenberg" and `sergio_perez.JPG` matches "Sergio Pérez".
- A surname on its own is enough, which is what rescues a file whose
  forename is spelled differently from the grid entry
  (`kimi_antonelli.JPG` for "Andrea Kimi Antonelli", `alex_albon.JPG`
  for "Alexander Albon", and even a typo like `valterri_bottas.JPG`).

Teams work the same way, matched on constructor id or team name:
`teams/red_bull.png`, `teams/Red Bull Racing.png`. PNG so a
transparent-background logo works.

Full names beat surnames, so two drivers sharing a surname resolve
correctly as long as both files carry a full name.

Landscape or portrait both work -- the frontend crops to fit. Missing
files are fine and cost nothing: no file matched means no request is
made at all, and the driver falls back to their coloured initials.

Drop a new file in and it appears on the next request -- the index
re-reads itself whenever the directory changes, no restart needed.

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
