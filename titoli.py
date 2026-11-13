"""Titoli e numeri degli episodi da TMDB (themoviedb.org) e confronto con i titoli letti sullo schermo.

La lista di una stagione si scarica una volta con la chiave personale dell'utente e si tiene in una
copia sul PC (%LOCALAPPDATA%\\ClipMerger\\tmdb): da lì in poi funziona anche senza internet e senza chiave.
Questo prodotto usa le API di TMDB ma non è approvato né certificato da TMDB."""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import applog

API = "https://api.themoviedb.org/3"
SURE = 0.85  # similarity from which a read title counts as certain
MAYBE = 0.50  # below this, nothing sensible can be proposed
_ARTICLES = ("IL", "LO", "LA", "I", "GLI", "LE", "L", "UN", "UNO", "UNA", "UN")


class TitoliError(Exception):
    pass


@dataclass(frozen=True)
class Episodio:
    number: int
    title: str


def cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = Path(base) / "ClipMerger" / "tmdb"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_file(series_id: int, season: int) -> Path:
    return cache_dir() / f"serie_{series_id}_stagione_{season}.json"


def key_from_file() -> str:
    """The key kept in a «tmdb_key.txt» next to the program (handy when working on it); "" when absent."""
    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    f = here / "tmdb_key.txt"
    try:
        return f.read_text(encoding="utf-8").strip() if f.is_file() else ""
    except OSError:
        return ""


def cached_season(series_id: int, season: int) -> list[Episodio] | None:
    f = _cache_file(series_id, season)
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return [Episodio(int(n), str(t)) for n, t in data["episodi"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def cached_series_name(series_id: int) -> str:
    """The series' name saved with any of its downloaded seasons ("" when none has it)."""
    for f in sorted(cache_dir().glob(f"serie_{series_id}_stagione_*.json")):
        try:
            name = str(json.loads(f.read_text(encoding="utf-8")).get("nome") or "").strip()
        except (OSError, ValueError):
            continue
        if name:
            return name
    return ""


def download_season(series_id: int, season: int, key: str) -> list[Episodio]:
    """Downloads the list from TMDB (Italian titles) and stores the copy. Never writes the key anywhere."""
    key = key.strip()
    if not key:
        raise TitoliError("Manca la chiave di TMDB: scrivila nel campo qui sopra.")
    url = f"{API}/tv/{series_id}/season/{season}?language=it-IT&api_key={key}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ClipMerger"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise TitoliError("TMDB non accetta la chiave (controlla di averla copiata tutta).") from exc
        if exc.code == 404:
            raise TitoliError(f"TMDB non trova la serie {series_id}, stagione {season}.") from exc
        raise TitoliError(f"TMDB ha risposto con un errore ({exc.code}).") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise TitoliError("Impossibile contattare TMDB: controlla la connessione a internet.") from exc
    series_name = ""
    try:  # the series' own name (non-essential: the list is useful without it)
        info_req = urllib.request.Request(f"{API}/tv/{series_id}?language=it-IT&api_key={key}",
                                          headers={"Accept": "application/json", "User-Agent": "ClipMerger"})
        with urllib.request.urlopen(info_req, timeout=30) as resp:
            series_name = str(json.load(resp).get("name") or "").strip()
    except (urllib.error.URLError, OSError, ValueError):
        pass
    episodes = [Episodio(int(e["episode_number"]), str(e.get("name") or "").strip()) for e in data.get("episodes", [])]
    episodes = [e for e in episodes if e.title]
    if not episodes:
        raise TitoliError(f"TMDB non ha episodi per la serie {series_id}, stagione {season}.")
    _cache_file(series_id, season).write_text(
        json.dumps({"serie": series_id, "nome": series_name, "stagione": season,
                    "episodi": [[e.number, e.title] for e in episodes]}, ensure_ascii=False), encoding="utf-8")
    applog.log.info("Lista TMDB scaricata: serie %s stagione %s, %d episodi", series_id, season, len(episodes))
    return episodes


# ---------------------------------------------------------------------------- finding a series by its title
COUNTRIES = {
    "IT": "Italia", "FR": "Francia", "ES": "Spagna", "DE": "Germania", "GB": "Regno Unito", "US": "Stati Uniti",
    "CA": "Canada", "JP": "Giappone", "KR": "Corea del Sud", "CN": "Cina", "BR": "Brasile", "MX": "Messico",
    "AR": "Argentina", "AU": "Australia", "PL": "Polonia", "GR": "Grecia", "PT": "Portogallo", "NL": "Paesi Bassi",
    "BE": "Belgio", "SE": "Svezia", "NO": "Norvegia", "DK": "Danimarca", "FI": "Finlandia", "RU": "Russia",
    "TR": "Turchia", "IN": "India", "CH": "Svizzera", "AT": "Austria", "IE": "Irlanda", "CZ": "Cechia", "HU": "Ungheria",
    "RO": "Romania", "IL": "Israele", "TW": "Taiwan", "HK": "Hong Kong", "NZ": "Nuova Zelanda", "ZA": "Sudafrica",
}
LANGUAGES = {
    "it": "italiano", "fr": "francese", "es": "spagnolo", "de": "tedesco", "en": "inglese", "ja": "giapponese",
    "ko": "coreano", "zh": "cinese", "pt": "portoghese", "nl": "olandese", "sv": "svedese", "no": "norvegese",
    "da": "danese", "fi": "finlandese", "ru": "russo", "tr": "turco", "hi": "hindi", "pl": "polacco", "el": "greco",
    "cs": "ceco", "hu": "ungherese", "ro": "rumeno", "he": "ebraico",
}


@dataclass
class Serie:
    id: int
    name: str                 # the title in Italian when TMDB has one
    original_name: str = ""
    year: str = ""
    countries: tuple = ()
    language: str = ""
    overview: str = ""
    seasons: int | None = None
    episodes: int | None = None

    @property
    def country_text(self) -> str:
        return ", ".join(COUNTRIES.get(c, c) for c in self.countries)

    @property
    def language_text(self) -> str:
        return LANGUAGES.get(self.language, self.language)

    def label(self) -> str:
        """«Nome della serie · 2003 · Italia · 5 stagioni», to show what was chosen."""
        parts = [self.name, self.year, self.country_text]
        if self.seasons:
            parts.append(f"{self.seasons} {'stagione' if self.seasons == 1 else 'stagioni'}")
        return " · ".join(p for p in parts if p)


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ClipMerger"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise TitoliError("TMDB non accetta la chiave (controlla di averla copiata tutta).") from exc
        raise TitoliError(f"TMDB ha risposto con un errore ({exc.code}).") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise TitoliError("Impossibile contattare TMDB: controlla la connessione a internet.") from exc


def search_series(query: str, key: str, limit: int = 10) -> list[Serie]:
    """The series TMDB finds for a title, best first, with enough detail to tell namesakes apart (year, country,
    language, number of seasons and episodes). Never writes the key anywhere."""
    key, query = key.strip(), query.strip()
    if not key:
        raise TitoliError("Manca la chiave di TMDB: scrivila nel campo qui accanto.")
    if not query:
        raise TitoliError("Scrivi il titolo della serie.")
    data = _get_json(f"{API}/search/tv?language=it-IT&query={urllib.parse.quote(query)}&api_key={key}")
    found = []
    for r in data.get("results", [])[:limit]:
        found.append(Serie(
            int(r["id"]), str(r.get("name") or r.get("original_name") or "").strip(), str(r.get("original_name") or "").strip(),
            str(r.get("first_air_date") or "")[:4], tuple(r.get("origin_country") or ()), str(r.get("original_language") or ""),
            str(r.get("overview") or "").strip()))

    def details(s: Serie) -> None:  # seasons and episodes are not in the search result: one small request each
        try:
            d = _get_json(f"{API}/tv/{s.id}?language=it-IT&api_key={key}")
            s.seasons = int(d.get("number_of_seasons") or 0) or None
            s.episodes = int(d.get("number_of_episodes") or 0) or None
        except (TitoliError, ValueError, TypeError):
            pass

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(details, found))
    return found


def series_by_id(series_id: int, key: str) -> Serie | None:
    """One series by its number (used to show the name of a profile's series); None when TMDB does not answer."""
    try:
        r = _get_json(f"{API}/tv/{series_id}?language=it-IT&api_key={key.strip()}")
    except TitoliError:
        return None
    return Serie(int(r["id"]), str(r.get("name") or "").strip(), str(r.get("original_name") or "").strip(),
                 str(r.get("first_air_date") or "")[:4], tuple(r.get("origin_country") or ()),
                 str(r.get("original_language") or ""), str(r.get("overview") or "").strip(),
                 int(r.get("number_of_seasons") or 0) or None, int(r.get("number_of_episodes") or 0) or None)


# ---------------------------------------------------------------------------- matching
def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().upper()
    text = re.sub(r"[^A-Z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _no_article(norm: str) -> str:
    words = norm.split()
    while len(words) > 1 and words[0] in _ARTICLES:
        words = words[1:]
    return " ".join(words)


def similarity(read: str, title: str) -> float:
    """How well a (possibly garbled) read text fits a catalogue title, 0..1. The article is optional
    ("LOOK" fits "Il look") and a read text that is just the first word(s) of the title counts a bit less."""
    a, b = normalize(read), normalize(title)
    if len(a) < 3 or not b:
        return 0.0
    best = max(difflib.SequenceMatcher(None, a, b).ratio(),
               difflib.SequenceMatcher(None, _no_article(a), _no_article(b)).ratio())
    if len(a) >= 4 and b.startswith(a + " "):
        best = max(best, 0.72)
    return best


@dataclass
class Proposta:
    number: int | None
    title: str
    score: float           # 0..1 similarity with the chosen catalogue title (0 when nothing fits)
    ambiguous: bool = False  # the same title exists under more than one number

    @property
    def sure(self) -> bool:
        return self.score >= SURE and not self.ambiguous


def best_match(lines: list[str], catalogue: list[Episodio], used: set[int] | None = None) -> Proposta:
    """Picks the catalogue episode that the read text lines fit best. `lines` are every text read from the
    caption (one entry per line of every frame); consecutive pairs are tried too (two-line titles)."""
    lines = [l for l in lines if l and l.strip()]
    texts = list(lines) + [a + " " + b for a, b in zip(lines, lines[1:])]
    if not texts or not catalogue:
        return Proposta(None, "", 0.0)
    best_score, best_title = 0.0, ""
    for text in texts:
        for ep in catalogue:
            s = similarity(text, ep.title)
            if s > best_score:
                best_score, best_title = s, ep.title
    if best_score < MAYBE:
        raw = max(lines, key=len, default="").strip()
        return Proposta(None, raw.title() if raw else "", 0.0)
    same = [e for e in catalogue if normalize(e.title) == normalize(best_title)]
    if len(same) > 1:  # the same title under more than one number: take the first not yet used
        free = [e for e in same if used is None or e.number not in used]
        chosen = (free or same)[0]
        return Proposta(chosen.number, chosen.title, best_score, ambiguous=True)
    return Proposta(same[0].number, same[0].title, best_score)


def find_by_title(text: str, catalogue: list[Episodio]) -> Episodio | None:
    """The one episode whose title is exactly `text` (ignoring case/accents); None when none or several."""
    norm = normalize(text)
    found = [e for e in catalogue if normalize(e.title) == norm]
    return found[0] if len(found) == 1 else None


_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


NAME_STYLES = [
    ("filebot", "Serie - S01E005 - Titolo   (il formato di FileBot, Plex e Jellyfin)"),
    ("x", "Serie - 1x005 - Titolo"),
    ("short", "1x005 Titolo   (senza il nome della serie)"),
]


def file_stem(season: int, number: int | None, title: str, series: str = "", style: str = "filebot") -> str:
    """The file name without extension: "Serie - S01E318 - Titolo" in the default style. The episode
    number has at least three digits, so that files sort properly in a season of hundreds of episodes."""
    clean = lambda t: re.sub(r"\s+", " ", _FORBIDDEN.sub("", t)).strip(" .")
    title, series = clean(title), clean(series)
    if number is None:
        return clean(f"{series} - {title}" if series and style != "short" else title) or "senza titolo"
    code = {"filebot": f"S{season:02d}E{number:03d}", "x": f"{season}x{number:03d}", "short": f"{season}x{number:03d}"}[style]
    if style == "short" or not series:
        return f"{code} {title}".strip()
    return f"{series} - {code} - {title}".strip(" -")


def season_from_names(names: list[str]) -> int | None:
    """The season written in the file names (S01E05...), when all of them agree."""
    found = {int(m.group(1)) for n in names if (m := re.search(r"[Ss](\d{1,2})[Ee]\d", n))}
    return found.pop() if len(found) == 1 else None
