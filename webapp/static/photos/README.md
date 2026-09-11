# Local photo bundle

Static files, served directly by FastAPI's StaticFiles mount -- no code
change needed to add a photo, just drop the file in with the right name.

Replaces the earlier live Wikipedia lookup (webapp/images.py, removed):
that depended on a third-party API returning a working URL on every
request, which repeatedly didn't (bad host, dead cache, wrong hostname).
A file that's just *there* can't have that problem.

## The three directories

| Directory | Used for | Shown when |
|---|---|---|
| `driver_headshot/` | the circular icon on every driver row | any race in the **current season** |
| `drivers/` | the large image in the expanded panel | the **single most recent race** only |
| `teams/` | the small constructor badge | everywhere |

The two driver bundles are deliberately separate: a headshot crops well
to a 46px circle, a trackside action shot doesn't, and the reverse is
true of the big panel image. Headshot files carry an `_hs` suffix, which
is stripped before matching (see below), so `max_verstappen_hs.jpg` and
`max_verstappen.JPG` both resolve to Max Verstappen from their own
directory.

Both are gated because driver codes are reused across eras — see "Why
photos only show on recent races" below.

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

## Why photos only show on recent races

Driver codes get reused across eras -- VER is both Max Verstappen today
and Jean-Éric Vergne in 2012-2014 (see reference.py's CURRENT_GRID for
the full list of collisions). An image keyed only by code would show the
wrong person's face on an old replay, so the frontend never attempts one
outside the window where a code still means who it means today.

Headshots get the wider window of the two: every round of the CURRENT
season was raced by the drivers on today's grid, so the face is right for
all of them. The large panel image is narrower still -- the single most
recent race -- which is also the only view where those five leading cards
are expanded on arrival.

Team badges don't have this problem (a constructor id is unique across
eras), so those show everywhere.

## Adding a driver the reference tables don't know yet

A driver who joined too recently to be in `driversgit.csv` needs a line
in `CURRENT_GRID` in `webapp/reference.py`, or their name resolves to the
bare code and neither bundle can match it -- that is what kept Arvid
Lindblad (`LIN`) faceless until his entry was added.
