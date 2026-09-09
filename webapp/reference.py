"""Display metadata: who a driver code refers to, what a circuit slug is
called, what colour a team races in.

None of this feeds the model -- it exists so the UI can show "Max
Verstappen, Red Bull, Marina Bay Street Circuit" instead of "VER,
red_bull, marina_bay".

Driver codes are NOT unique across eras in driversgit.csv: VER is both
Max Verstappen and Jean-Eric Vergne, ALB is both Alexander Albon and
Christijan Albers, MAG is both Kevin and Jan Magnussen. The pipeline
joins on code regardless (see build_combined_raw), so a code always
resolves to one person here too -- CURRENT_GRID pins the modern driver
for every code on the 2023+ grid, and anything else falls back to the
most recently born CSV driver holding that code.
"""

import pandas as pd

# Codes on the 2023+ grid, including the ones that never appear in
# driversgit.csv (it stops at 2022) and the ones that would otherwise
# resolve to a same-code driver from an earlier era.
CURRENT_GRID = {
    "ALB": ("Alexander", "Albon", "Thai"),
    "ALO": ("Fernando", "Alonso", "Spanish"),
    "ANT": ("Andrea Kimi", "Antonelli", "Italian"),
    "BEA": ("Oliver", "Bearman", "British"),
    "BOR": ("Gabriel", "Bortoleto", "Brazilian"),
    "BOT": ("Valtteri", "Bottas", "Finnish"),
    "COL": ("Franco", "Colapinto", "Argentine"),
    "DEV": ("Nyck", "de Vries", "Dutch"),
    "DOO": ("Jack", "Doohan", "Australian"),
    "GAS": ("Pierre", "Gasly", "French"),
    "HAD": ("Isack", "Hadjar", "French"),
    "HAM": ("Lewis", "Hamilton", "British"),
    "HUL": ("Nico", "Hulkenberg", "German"),
    "LAW": ("Liam", "Lawson", "New Zealander"),
    "LEC": ("Charles", "Leclerc", "Monegasque"),
    "MAG": ("Kevin", "Magnussen", "Danish"),
    "NOR": ("Lando", "Norris", "British"),
    "OCO": ("Esteban", "Ocon", "French"),
    "PER": ("Sergio", "Perez", "Mexican"),
    "PIA": ("Oscar", "Piastri", "Australian"),
    "RIC": ("Daniel", "Ricciardo", "Australian"),
    "RUS": ("George", "Russell", "British"),
    "SAI": ("Carlos", "Sainz", "Spanish"),
    "SAR": ("Logan", "Sargeant", "American"),
    "STR": ("Lance", "Stroll", "Canadian"),
    "TSU": ("Yuki", "Tsunoda", "Japanese"),
    "VER": ("Max", "Verstappen", "Dutch"),
    "ZHO": ("Guanyu", "Zhou", "Chinese"),
}

# Constructor identity is split across two schemes: historical rows use
# numeric constructorIds, 2023+ rows use Jolpica slugs (see
# build_combined_raw). Both are keyed here so either resolves.
TEAMS = {
    "red_bull": ("Red Bull Racing", "#3671C6"),
    "ferrari": ("Ferrari", "#E8002D"),
    "mercedes": ("Mercedes", "#27F4D2"),
    "mclaren": ("McLaren", "#FF8000"),
    "aston_martin": ("Aston Martin", "#229971"),
    "alpine": ("Alpine", "#00A1E8"),
    "williams": ("Williams", "#1868DB"),
    "rb": ("RB", "#6692FF"),
    "alphatauri": ("AlphaTauri", "#6692FF"),
    "sauber": ("Kick Sauber", "#01C00E"),
    "alfa": ("Alfa Romeo", "#B12039"),
    "haas": ("Haas", "#9C9FA2"),
}

NEUTRAL_TEAM_COLOR = "#8E8E93"


def _fallback_team_color(constructor_id: str) -> str:
    """A stable, distinct colour for a team that isn't in TEAMS yet.

    A team that joins the grid after this file was written would otherwise
    render in the same flat grey as every other unknown, making two new
    teams indistinguishable. Derived from the id so it never changes
    between reloads. Deliberately not a guess at the real livery -- add the
    team to TEAMS to pin its actual colour.
    """
    # FNV-1a, then spread across the wheel in large steps. A plain
    # multiply-and-mod lands similar-length ids on near-identical hues
    # ("cadillac" and "audi" came out 4 degrees apart), which is the one
    # thing this function exists to avoid.
    digest = 2166136261
    for char in constructor_id:
        digest = ((digest ^ ord(char)) * 16777619) & 0xFFFFFFFF
    return f"hsl({(digest % 24) * 15}, 62%, 55%)"

# Slugs seen in the 2023+ data, plus the official circuit name and the
# country it sits in (for the flag chip in the UI).
CIRCUITS = {
    "albert_park": ("Albert Park Circuit", "Melbourne", "au"),
    "americas": ("Circuit of the Americas", "Austin", "us"),
    "bahrain": ("Bahrain International Circuit", "Sakhir", "bh"),
    "baku": ("Baku City Circuit", "Baku", "az"),
    "catalunya": ("Circuit de Barcelona-Catalunya", "Barcelona", "es"),
    "hungaroring": ("Hungaroring", "Budapest", "hu"),
    "imola": ("Autodromo Enzo e Dino Ferrari", "Imola", "it"),
    "interlagos": ("Autodromo Jose Carlos Pace", "Sao Paulo", "br"),
    "jeddah": ("Jeddah Corniche Circuit", "Jeddah", "sa"),
    "losail": ("Lusail International Circuit", "Lusail", "qa"),
    "marina_bay": ("Marina Bay Street Circuit", "Singapore", "sg"),
    "miami": ("Miami International Autodrome", "Miami", "us"),
    "monaco": ("Circuit de Monaco", "Monte Carlo", "mc"),
    "monza": ("Autodromo Nazionale Monza", "Monza", "it"),
    "red_bull_ring": ("Red Bull Ring", "Spielberg", "at"),
    "rodriguez": ("Autodromo Hermanos Rodriguez", "Mexico City", "mx"),
    "shanghai": ("Shanghai International Circuit", "Shanghai", "cn"),
    "silverstone": ("Silverstone Circuit", "Silverstone", "gb"),
    "spa": ("Circuit de Spa-Francorchamps", "Spa", "be"),
    "suzuka": ("Suzuka International Racing Course", "Suzuka", "jp"),
    "vegas": ("Las Vegas Strip Circuit", "Las Vegas", "us"),
    "villeneuve": ("Circuit Gilles Villeneuve", "Montreal", "ca"),
    "yas_marina": ("Yas Marina Circuit", "Abu Dhabi", "ae"),
    "zandvoort": ("Circuit Zandvoort", "Zandvoort", "nl"),
}

# The one circuit whose slug is unified with a numeric id by
# load_multi_circuit_fresh_data() -- so both spellings must resolve.
SINGAPORE_ALIASES = ("15", "marina_bay")


class Reference:
    """Everything the UI needs to render a row, built once from the CSVs."""

    def __init__(self, raw: dict):
        self._drivers = self._build_driver_directory(raw["drivers"])
        self._team_names = {}  # filled by augment() from the live roster
        self._circuit_names_by_numeric_id = self._build_circuit_directory(raw["races"])

    @staticmethod
    def _build_driver_directory(drivers: pd.DataFrame) -> dict:
        # Latest-born driver wins a contested code, then CURRENT_GRID
        # overrides outright for anyone racing in the modern era.
        ordered = drivers.dropna(subset=["code"]).sort_values("dob")
        directory = {
            row["code"]: {
                "name": f"{row['forename']} {row['surname']}",
                "nationality": row["nationality"],
                "wiki_url": None if row["url"] == "\\N" else row["url"],
            }
            for _, row in ordered.iterrows()
        }
        for code, (forename, surname, nationality) in CURRENT_GRID.items():
            directory[code] = {
                "name": f"{forename} {surname}",
                "nationality": nationality,
                "wiki_url": f"https://en.wikipedia.org/wiki/{forename}_{surname}".replace(" ", "_"),
            }
        return directory

    @staticmethod
    def _build_circuit_directory(races: pd.DataFrame) -> dict:
        """Numeric circuitId -> the Grand Prix name used at that circuit,
        taken from the most recent race held there."""
        latest = races.sort_values("year").groupby("circuitId").last()
        return {str(cid): row["name"] for cid, row in latest.iterrows()}

    def augment(self, roster: dict) -> None:
        """Merge a live season roster (see LiveData.season_roster) over the
        static tables.

        The API is authoritative for who currently holds a code, so it wins
        over CURRENT_GRID -- which is a hand-written snapshot and goes stale
        the moment the grid changes. Teams only take a name from here;
        colours stay local, since the API doesn't publish them.
        """
        for driver in roster.get("drivers", []):
            if not driver.get("code") or not driver.get("name"):
                continue
            self._drivers[driver["code"]] = {
                "name": driver["name"],
                "nationality": driver.get("nationality"),
                "wiki_url": driver.get("wiki_url"),
            }
        for team in roster.get("teams", []):
            if team.get("id") and team.get("name"):
                self._team_names[team["id"]] = team["name"]

    def driver(self, code: str) -> dict:
        info = self._drivers.get(code)
        if info is None:
            return {"code": code, "name": code, "nationality": None, "wiki_url": None}
        return {"code": code, **info}

    def team(self, constructor_id) -> dict:
        key = str(constructor_id)
        if key in TEAMS:
            name, color = TEAMS[key]
            return {"id": key, "name": name, "color": color}
        name = self._team_names.get(key, key.replace("_", " ").title())
        return {"id": key, "name": name, "color": _fallback_team_color(key)}

    def circuit(self, circuit_id) -> dict:
        key = str(circuit_id)
        if key in SINGAPORE_ALIASES:
            key = "marina_bay"
        if key in CIRCUITS:
            name, locality, country = CIRCUITS[key]
            return {"id": key, "name": name, "locality": locality, "country": country}
        # A pre-2023 numeric circuitId with no modern slug -- fall back to
        # the Grand Prix name from races.csv.
        name = self._circuit_names_by_numeric_id.get(key, key.replace("_", " ").title())
        return {"id": key, "name": name, "locality": None, "country": None}
