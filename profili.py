"""Profili creati dall'utente per le schede «Sostituisci sigla» e «Dividi puntate».

Un profilo è un insieme di impostazioni per una situazione particolare che si ripete (una serie fatta in un certo
modo, per esempio): si crea, si modifica, si duplica, si elimina, si porta su un altro PC (esporta/importa).
In ogni scheda c'è sempre anche il profilo «Standard», che è parte del programma e non si cancella.

Ogni profilo è un file .json in %LOCALAPPDATA%\\ClipMerger\\profili\\<scheda>\\ (più, per «Dividi puntate», un
campione .npz con lo stesso nome): resta lì quando il programma si aggiorna e non fa parte del programma."""
from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
import zipfile
from pathlib import Path

import applog

KINDS = ("sostituisci", "dividi")


class ProfiliError(Exception):
    pass


def base_folder() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "ClipMerger" / "profili"


def folder(kind: str) -> Path:
    d = base_folder() / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def slug(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text[:40] or "profilo"


def _json_path(kind: str, key: str) -> Path:
    return folder(kind) / f"{key}.json"


def sample_path(kind: str, key: str) -> Path:
    return folder(kind) / f"{key}.npz"


def all_dicts(kind: str) -> dict[str, dict]:
    """Every saved profile, by key, sorted by name. A file that cannot be read is skipped (and logged)."""
    found: dict[str, dict] = {}
    for f in folder(kind).glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and str(data.get("nome", "")).strip():
                found[f.stem] = data
        except (OSError, ValueError) as exc:
            applog.log.warning("Profilo %s non leggibile: %s", f.name, exc)
    return dict(sorted(found.items(), key=lambda kv: kv[1]["nome"].lower()))


def new_key(kind: str, name: str) -> str:
    base = slug(name)
    key, n = base, 2
    while _json_path(kind, key).exists():
        key, n = f"{base}_{n}", n + 1
    return key


def save(kind: str, data: dict, key: str | None = None) -> str:
    """Writes the profile (a new one when `key` is None); returns its key."""
    data = dict(data)
    data["nome"] = str(data.get("nome", "")).strip()
    if not data["nome"]:
        raise ProfiliError("Il profilo ha bisogno di un nome.")
    key = key or new_key(kind, data["nome"])
    data["tipo"] = kind
    try:
        _json_path(kind, key).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise ProfiliError(f"Non riesco a salvare il profilo: {exc}") from exc
    return key


def name_taken(kind: str, name: str, except_key: str | None = None) -> bool:
    wanted = name.strip().lower()
    return any(k != except_key and d["nome"].strip().lower() == wanted for k, d in all_dicts(kind).items())


def delete(kind: str, key: str) -> None:
    for f in (_json_path(kind, key), sample_path(kind, key)):
        try:
            f.unlink(missing_ok=True)
        except OSError as exc:
            raise ProfiliError(f"Non riesco a eliminare {f.name}: {exc}") from exc


def duplicate(kind: str, key: str, new_name: str) -> str:
    data = all_dicts(kind).get(key)
    if data is None:
        raise ProfiliError("Profilo non trovato.")
    data = dict(data, nome=new_name)
    new = new_key(kind, new_name)
    if sample_path(kind, key).is_file():
        shutil.copyfile(sample_path(kind, key), sample_path(kind, new))
        data["campione"] = sample_path(kind, new).name
    return save(kind, data, new)


def export_to(kind: str, key: str, dest: Path) -> None:
    """A single .zip with the profile (and its sample)."""
    if key not in all_dicts(kind):
        raise ProfiliError("Profilo non trovato.")
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(_json_path(kind, key), "profilo.json")
            if sample_path(kind, key).is_file():
                z.write(sample_path(kind, key), "campione.npz")
    except OSError as exc:
        raise ProfiliError(f"Non riesco a scrivere il file: {exc}") from exc


def import_from(kind: str, src: Path) -> str:
    """Reads a profile exported with export_to; returns the key it was saved under."""
    try:
        with zipfile.ZipFile(src) as z:
            data = json.loads(z.read("profilo.json").decode("utf-8"))
            sample = z.read("campione.npz") if "campione.npz" in z.namelist() else None
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise ProfiliError("Questo file non è un profilo di ClipMerger.") from exc
    if not isinstance(data, dict) or data.get("tipo") != kind or not str(data.get("nome", "")).strip():
        raise ProfiliError("Questo profilo è di un'altra scheda (o non è valido).")
    name = str(data["nome"]).strip()
    if name_taken(kind, name):
        name = f"{name} (importato)"
    data["nome"] = name
    key = new_key(kind, name)
    if sample is not None:
        sample_path(kind, key).write_bytes(sample)
        data["campione"] = sample_path(kind, key).name
    else:
        data.pop("campione", None)
    return save(kind, data, key)
