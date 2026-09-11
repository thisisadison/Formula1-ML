"""Find the photo file for a driver or a team, whatever it's been named.

The frontend used to build a URL by convention (`/photos/drivers/VER.jpg`)
and let a 404 mean "no photo". That only works if every file is named the
one way the convention expects, which real uploads aren't: the bundle in
this repo arrived named by full driver name, with an uppercase `.JPG`
extension, an umlaut in Hulkenberg, an accent in Perez, and a transposed
`valterri_bottas`. Renaming 22 files to codes would fix it once and break
again the next time someone adds one.

So the directory is the source of truth instead. Every file registers
under a normalised key -- lowercased, accents stripped, non-alphanumerics
dropped -- plus a second key for its last name-ish token, and a lookup
tries the most specific form first:

    kimi_antonelli.JPG   ->  "kimiantonelli"  and  "antonelli"
    VER.jpg              ->  "ver"
    Red Bull Racing.png  ->  "redbullracing"  and  "racing"

A driver then resolves by full name, then surname, then code, so
"Andrea Kimi Antonelli" still finds `kimi_antonelli.JPG` (surname), and
"Valtteri Bottas" still finds the misspelled `valterri_bottas.JPG` for
the same reason. Nothing matches -> None, and the UI shows its monogram
exactly as it did for a 404, minus the failed request.

The index re-reads itself when the directory's mtime changes, so dropping
a new file in shows up on the next request without a restart.
"""

import os
import unicodedata

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif"}


def normalise(text) -> str:
    """Lowercase, accent-stripped, alphanumerics only.

    'Nico Hulkenberg', 'Nico Hülkenberg' and 'nico_hulkenberg.JPG' all have
    to land on the same key, and so do 'VER' and 'ver'.
    """
    decomposed = unicodedata.normalize("NFKD", str(text))
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(ch for ch in without_marks.lower() if ch.isalnum())


def _tokens(stem: str) -> list:
    """Filename stem split on any separator: `kimi_antonelli` -> two parts."""
    for separator in ("_", "-", " "):
        stem = stem.replace(separator, " ")
    return stem.split()


class PhotoIndex:
    """One directory of images, looked up by any reasonable name for them.

    strip_tokens drops a trailing tag from the stem before keys are built.
    The headshot bundle is named `max_verstappen_hs.jpg`, and without this
    every file in it would key on "hs" as its last token and answer for
    every driver at once.
    """

    def __init__(self, subdirectory: str, strip_tokens=()):
        self.directory = os.path.join(STATIC_DIR, "photos", subdirectory)
        self.url_prefix = f"/photos/{subdirectory}"
        self.strip_tokens = {token.lower() for token in strip_tokens}
        self._index = {}
        self._mtime = None
        self.refresh()

    def refresh(self) -> None:
        """Re-scan only when the directory has actually changed. A stat call
        per lookup is cheap enough to keep the bundle hot-swappable without
        a restart; re-listing it every time would not be."""
        try:
            mtime = os.stat(self.directory).st_mtime
        except OSError:
            self._index, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return

        index = {}
        secondary = {}
        for filename in sorted(os.listdir(self.directory)):
            stem, extension = os.path.splitext(filename)
            if extension.lower() not in EXTENSIONS:
                continue
            # The URL keeps the filename verbatim -- StaticFiles matches
            # case-sensitively on Linux, so a lowercased ".jpg" would 404
            # against a file actually saved as ".JPG".
            url = f"{self.url_prefix}/{filename}"
            tokens = _tokens(stem)
            while len(tokens) > 1 and tokens[-1].lower() in self.strip_tokens:
                tokens.pop()
            if not tokens:
                continue
            index.setdefault(normalise("".join(tokens)), url)
            secondary.setdefault(normalise(tokens[-1]), url)

        # Full-stem keys win over surname keys, so two drivers sharing a
        # surname still resolve correctly whenever both files carry a full
        # name -- the surname key is only ever a fallback.
        for key, url in secondary.items():
            index.setdefault(key, url)

        self._index, self._mtime = index, mtime

    def lookup(self, *candidates) -> str:
        """First candidate with a file wins; None if none of them match."""
        self.refresh()
        for candidate in candidates:
            if not candidate:
                continue
            url = self._index.get(normalise(candidate))
            if url:
                return url
        return None

    def __len__(self) -> int:
        self.refresh()
        return len(self._index)


def surname(full_name: str) -> str:
    """Last word of a display name -- good enough as a surname for lookup
    ('Nyck de Vries' -> 'Vries', which is what a `..._de_vries` filename
    reduces to as well)."""
    parts = str(full_name or "").split()
    return parts[-1] if parts else ""
