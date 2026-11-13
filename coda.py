"""La coda unica: tutti gli strumenti vi mettono i loro lavori, e partono uno dopo l'altro.

Un lavoro (Job) è quello che uno strumento ha preparato: tanti elementi (Unit: un episodio, un pezzo...) con
tutto ciò che serve per eseguirli, scritto in forma semplice (testo e numeri), così la coda si può salvare su
disco e riprendere dopo una chiusura. Gli elementi di un lavoro girano in parallelo (quanti, lo dice la
codifica scelta); i lavori uno alla volta, nell'ordine della coda."""
from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

import applog
import encoding
import merger
from encoding import EncodingOptions

TOOL_TITLES = {"unisci": "Unisci sigle", "sostituisci": "Sostituisci sigla", "dividi": "Dividi puntate", "tracce": "Modifica tracce"}
FINISHED = ("done", "skipped", "error")  # states of a unit that needs nothing more


def store_file() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = Path(base) / "ClipMerger"
    d.mkdir(parents=True, exist_ok=True)
    return d / "coda.json"


@dataclass
class Unit:
    label: str
    params: dict
    weight: float = 1.0       # how much work it is, for the estimate (seconds of video, usually)
    state: str = "waiting"    # waiting | running | done | skipped | error
    pct: float = 0.0
    note: str = ""            # what to say next to the state: a remark, or the error
    output: str = ""


@dataclass
class Job:
    tool: str
    title: str
    detail: str
    parallel: int
    units: list[Unit]
    output_dir: str = ""
    id: int = 0
    state: str = "waiting"    # waiting | running | stopped | done
    created: float = field(default_factory=time.time)
    finished_at: float = 0.0
    logged: bool = False      # the start of this job is in the log

    def count(self, *states: str) -> int:
        return sum(1 for u in self.units if u.state in states)

    @property
    def pending(self) -> bool:
        return any(u.state in ("waiting", "running") for u in self.units)


def unit_status_text(u: Unit) -> str:
    """How a queue element is described in a tool's own table."""
    if u.state == "running":
        return f"In elaborazione {int(u.pct * 100)}%"
    if u.state == "done":
        return "Completato" + (f" — {u.note}" if u.note else "")
    if u.state == "skipped":
        return "Già presente, saltato" + (f" — {u.note}" if u.note else "")
    if u.state == "error":
        return f"Errore: {u.note}"
    return "In coda"


# ---------------------------------------------------------------------------- doing one unit
def tracks_to_dict(ft) -> dict:
    d = dataclasses.asdict(ft)
    d["path"] = str(ft.path)
    return d


def tracks_from_dict(d: dict):
    import tracce
    def track(t: dict):
        t = dict(t)
        t["flags"] = tuple(t.get("flags", ()))
        return tracce.Track(**t)
    return tracce.FileTracks(Path(d["path"]), d.get("duration", 0.0), [track(t) for t in d.get("audio", [])],
                             [track(t) for t in d.get("subs", [])], d.get("attachments", 0), d.get("error"))


def plan_to_dict(plan) -> dict:
    return {"keep_start": plan.keep_start, "keep_end": plan.keep_end, "mute_head": plan.mute_head,
            "card": list(plan.card) if plan.card else None}


def run_unit(tool: str, p: dict, ffmpeg: str, ffprobe: str, progress_cb, cancel_event) -> merger.MergeResult:
    """Does one unit of work, whatever the tool. Never raises: a failure comes back as a result."""
    opt = lambda name: Path(p[name]) if p.get(name) else None
    try:
        if tool == "unisci":
            settings = merger.MergeSettings(**p["settings"])
            opts = EncodingOptions.from_dict(p["opts"])
            episode = Path(p["episode"])
            info = merger.probe(ffprobe, episode)
            interlaced = encoding.resolve_interlaced(opts, ffmpeg, episode, info)
            return merger.merge_episode(ffmpeg, ffprobe, opt("intro"), episode, opt("outro"), Path(p["output"]), settings,
                                        progress_cb=progress_cb, cancel_event=cancel_event, episode_interlaced=interlaced)
        if tool == "sostituisci":
            import sostituisci
            plan = sostituisci.CutPlan(p["plan"]["keep_start"], p["plan"]["keep_end"], p["plan"]["mute_head"], None, None,
                                       card=tuple(p["plan"]["card"]) if p["plan"].get("card") else None)
            ana = sostituisci.EpisodeAnalysis(Path(p["episode"]), plan=plan)
            return sostituisci.elabora_episodio(ffmpeg, ffprobe, ana, opt("new_intro"), opt("new_outro"), Path(p["output_dir"]),
                                                EncodingOptions.from_dict(p["opts"]), p.get("hw") or {},
                                                progress_cb=progress_cb, cancel_event=cancel_event)
        if tool == "dividi":
            import dividi
            return dividi.cut_piece(ffmpeg, ffprobe, Path(p["src"]), p["start"], p["end"], Path(p["output"]),
                                    EncodingOptions.from_dict(p["opts"]), p.get("hw") or {}, p.get("title", ""),
                                    progress_cb=progress_cb, cancel_event=cancel_event,
                                    tail=tuple(p["tail"]) if p.get("tail") else None, deinterlace=bool(p.get("deinterlace")),
                                    bars=tuple(p["bars"]) if p.get("bars") else None)
        if tool == "tracce":
            import tracce
            return tracce.remux(ffmpeg, ffprobe, tracks_from_dict(p["source"]), tracks_from_dict(p["plan"]), Path(p["output_dir"]),
                                progress_cb=progress_cb, cancel_event=cancel_event)
        raise ValueError(f"strumento sconosciuto: {tool}")
    except merger.Cancelled:
        return merger.MergeResult(output_path=Path(), success=False, error="Annullato")
    except Exception as exc:  # never let a worker thread die silently
        applog.log_exception(TOOL_TITLES.get(tool, tool), exc)
        return merger.MergeResult(output_path=Path(), success=False, error=applog.describe_exception(exc))


class _Signals(QObject):
    started = Signal(int, int, int)
    progress = Signal(int, int, int, float)
    finished = Signal(int, int, int, object)


class _Runnable(QRunnable):
    def __init__(self, token: int, job: Job, idx: int, ffmpeg: str, ffprobe: str, signals: _Signals, cancel_event: threading.Event):
        super().__init__()
        self.token, self.job_id, self.idx, self.tool = token, job.id, idx, job.tool
        self.params = job.units[idx].params
        self.ffmpeg, self.ffprobe, self.signals, self.cancel_event = ffmpeg, ffprobe, signals, cancel_event
        self.setAutoDelete(True)

    def run(self):
        if self.cancel_event.is_set():
            return
        self.signals.started.emit(self.token, self.job_id, self.idx)
        result = run_unit(self.tool, self.params, self.ffmpeg, self.ffprobe,
                          lambda pct: self.signals.progress.emit(self.token, self.job_id, self.idx, pct), self.cancel_event)
        self.signals.finished.emit(self.token, self.job_id, self.idx, result)


# ---------------------------------------------------------------------------- the queue
class QueueManager(QObject):
    changed = Signal()              # jobs added, removed, reordered, or a state moved
    unit_changed = Signal(object, int)  # (job, unit index): its state or progress moved
    job_finished = Signal(object)
    queue_finished = Signal(bool)   # the queue ran dry by itself (True) — not stopped by the user

    def __init__(self, tools_getter: Callable[[], tuple[str, str] | None]):
        super().__init__()
        self.tools = tools_getter
        self.jobs: list[Job] = []
        self.running = False
        self.pool = QThreadPool()
        self.signals = _Signals()
        self.signals.started.connect(self._on_started)
        self.signals.progress.connect(self._on_progress)
        self.signals.finished.connect(self._on_finished)
        self.cancel_event = threading.Event()
        self.token = 0
        self._next_id = 1
        self.run_started = 0.0
        self.run_done0 = 0.0

    # ------------------------------------------------------------------ the list
    def find(self, job_id: int) -> Job | None:
        return next((j for j in self.jobs if j.id == job_id), None)

    def add_job(self, job: Job, start_now: bool = False) -> Job:
        job.id = self._next_id
        self._next_id += 1
        self.jobs.append(job)
        self._save()
        self.changed.emit()
        if start_now and not self.running:
            self.start()
        return job

    def remove(self, job_id: int) -> None:
        job = self.find(job_id)
        if job is None:
            return
        if job.state == "running":
            self.stop()
        self.jobs.remove(job)
        self._save()
        self.changed.emit()

    def move(self, job_id: int, delta: int) -> None:
        job = self.find(job_id)
        if job is None or job.state == "running":
            return
        i = self.jobs.index(job)
        j = min(max(i + delta, 0), len(self.jobs) - 1)
        if j != i and self.jobs[j].state != "running":
            self.jobs.insert(j, self.jobs.pop(i))
            self._save()
            self.changed.emit()

    def retry(self, job_id: int) -> None:
        """The elements that failed are put back in line."""
        job = self.find(job_id)
        if job is None or job.state == "running":
            return
        for u in job.units:
            if u.state == "error":
                u.state, u.pct, u.note = "waiting", 0.0, ""
        job.state = "waiting"
        job.finished_at = 0.0
        self._save()
        self.changed.emit()

    def clear_finished(self) -> None:
        self.jobs = [j for j in self.jobs if j.state != "done"]
        self._save()
        self.changed.emit()

    @property
    def active_jobs(self) -> list[Job]:
        return [j for j in self.jobs if j.state != "done"]

    def has_unfinished(self) -> bool:
        return any(j.pending for j in self.jobs)

    # ------------------------------------------------------------------ running
    def _next_job(self) -> Job | None:
        return next((j for j in self.jobs if j.pending), None)

    def start(self) -> bool:
        """Starts (or, after a stop, carries on with) the jobs in line."""
        if self.running:
            return True
        tools = self.tools()
        job = self._next_job()
        if tools is None or job is None:
            return False
        self.running = True
        self.token += 1
        self.cancel_event = threading.Event()
        self.run_started = time.monotonic()
        self.run_done0 = self._weights()[0]
        self._start_job(job, tools)
        return True

    def _start_job(self, job: Job, tools: tuple[str, str]) -> None:
        job.state = "running"
        if not job.logged:
            job.logged = True
            applog.log_batch_start(TOOL_TITLES.get(job.tool, job.tool), job.detail, len(job.units), job.parallel)
        self.pool.setMaxThreadCount(max(job.parallel, 1))
        for idx, u in enumerate(job.units):
            if u.state == "waiting":
                u.pct = 0.0
                self.pool.start(_Runnable(self.token, job, idx, tools[0], tools[1], self.signals, self.cancel_event))
        self._save()
        self.changed.emit()

    def stop(self) -> None:
        """Stops what is running; the elements not finished stay in line, so Avvia carries on from there."""
        if not self.running:
            return
        self.running = False
        self.token += 1  # whatever the stopped tasks still report is ignored
        self.cancel_event.set()
        self.pool.clear()
        for job in self.jobs:
            for u in job.units:
                if u.state == "running":
                    u.state, u.pct = "waiting", 0.0
                    self.unit_changed.emit(job, job.units.index(u))
            if job.state == "running":
                job.state = "stopped"
        self._save()
        self.changed.emit()

    def shutdown(self) -> None:
        self.stop()
        self.pool.waitForDone(20000)
        self._save()

    def _unit(self, job_id: int, idx: int):
        job = self.find(job_id)
        return (job, job.units[idx]) if job is not None and idx < len(job.units) else (None, None)

    def _on_started(self, token: int, job_id: int, idx: int) -> None:
        job, u = self._unit(job_id, idx)
        if token != self.token or u is None:
            return
        u.state = "running"
        self.unit_changed.emit(job, idx)
        self.changed.emit()

    def _on_progress(self, token: int, job_id: int, idx: int, pct: float) -> None:
        job, u = self._unit(job_id, idx)
        if token != self.token or u is None:
            return
        changed = int(pct * 100) != int(u.pct * 100)
        u.pct = pct
        if changed:
            self.unit_changed.emit(job, idx)

    def _on_finished(self, token: int, job_id: int, idx: int, result) -> None:
        job, u = self._unit(job_id, idx)
        if token != self.token or u is None:
            return
        applog.log_result(TOOL_TITLES.get(job.tool, job.tool), u.label, result)
        u.pct = 1.0
        u.output = str(result.output_path) if result.output_path else ""
        if result.skipped:
            u.state, u.note = "skipped", result.note or ""
        elif result.success:
            u.state, u.note = "done", result.note or ""
        else:
            u.state, u.note = "error", result.error or "errore"
        pre = u.params.get("pre_notes")
        if pre and u.state in ("done", "skipped"):
            u.note = "; ".join([*pre, *([u.note] if u.note else [])])
        self.unit_changed.emit(job, idx)
        if not job.pending:
            job.state = "done"
            job.finished_at = time.time()
            self._save()
            self.job_finished.emit(job)
            nxt = self._next_job()
            tools = self.tools()
            if nxt is not None and tools is not None:
                self._start_job(nxt, tools)
                return
            self.running = False
            self.changed.emit()
            self.queue_finished.emit(True)
        else:
            self.changed.emit()

    # ------------------------------------------------------------------ numbers for the bar
    def _weights(self) -> tuple[float, float]:
        """(work done, work in total) over the jobs of the run: those not finished before it began, and those finished in it."""
        done = total = 0.0
        for j in self.jobs:
            if j.state == "done" and j.finished_at < self._run_wall_start():
                continue
            for u in j.units:
                total += u.weight
                done += u.weight * (1.0 if u.state in FINISHED else (u.pct if u.state == "running" else 0.0))
        return done, total

    def _run_wall_start(self) -> float:
        return time.time() - (time.monotonic() - self.run_started) if self.run_started else 0.0

    def summary(self) -> dict:
        """What the summary bar shows: where the queue is, how much is left, and for how long (a rough guess)."""
        done, total = self._weights()
        frac = done / total if total else 0.0
        eta = None
        left = total - self.run_done0
        if self.running and left > 0 and self.run_started:
            progressed = (done - self.run_done0) / left
            elapsed = time.monotonic() - self.run_started
            if progressed > 0.02:
                eta = elapsed * (1 - progressed) / progressed
        current = next((j for j in self.jobs if j.state == "running"), None)
        running = [(current, i, u) for i, u in enumerate(current.units) if u.state == "running"] if current else []
        waiting_units = sum(j.count("waiting", "running") for j in self.jobs)
        label = ""
        if current is not None:
            k = current.count("done", "skipped", "error") + 1
            names = ", ".join(u.label for _, _, u in running[:2]) if running else ""
            label = f"{TOOL_TITLES.get(current.tool, current.tool)} · elemento {min(k, len(current.units))} di {len(current.units)}" + (f" · {names}" if names else "")
        return {"running": self.running, "fraction": frac, "eta": eta, "label": label, "left": waiting_units,
                "jobs_left": len([j for j in self.jobs if j.pending])}

    # ------------------------------------------------------------------ remembering it
    def _save(self) -> None:
        data = {"versione": 1, "prossimo": self._next_id, "lavori": [dataclasses.asdict(j) for j in self.jobs]}
        try:
            store_file().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except (OSError, TypeError, ValueError) as exc:
            applog.log.warning("Coda non salvata: %s", exc)

    def load(self) -> None:
        """The queue of the last session. What was running is put back in line (stopped): nothing starts by itself."""
        try:
            data = json.loads(store_file().read_text(encoding="utf-8"))
            raw = data["lavori"]
        except (OSError, ValueError, KeyError, TypeError):
            return
        for r in raw:
            try:
                units = [Unit(**{k: v for k, v in u.items() if k in Unit.__dataclass_fields__}) for u in r["units"]]
                job = Job(**{**{k: v for k, v in r.items() if k in Job.__dataclass_fields__}, "units": units})
            except (KeyError, TypeError):
                continue
            for u in job.units:
                if u.state == "running":
                    u.state, u.pct = "waiting", 0.0
            if job.state == "running":
                job.state = "stopped"
            self.jobs.append(job)
            self._next_id = max(self._next_id, job.id + 1)
        self._next_id = max(self._next_id, int(data.get("prossimo", 1)))
        if self.jobs:
            self.changed.emit()
