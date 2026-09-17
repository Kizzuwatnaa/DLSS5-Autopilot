"""The watcher: which of our installed games starts, and when it closes.

`watch.Recorder` watches one folder until its game is seen once, which is
what "did it work?" needs. This keeps watching every folder we have
installed into, for as long as the tool runs (or sits in the tray), and says
two things through a callback:

    ("started", folder, sighting)   the game is up and settled; what it loaded
    ("closed", folder, seconds)     it is gone again, after `seconds` of play

On "closed" the window reads the logs back itself, so the answer to "did it
work?" is waiting when the person comes back - nobody has to remember to
press anything.

It only reads the process table and, once per start, one game's module
list. It writes nothing into any game folder; the sighting goes where
watch.remember always puts it.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import installer, log, watch

POLL = 4.0          # seconds between process snapshots
SETTLE = 8.0        # after a start, before the module list is read
MIN_PLAY = 20.0     # a start shorter than this is a crash at launch or a launcher hop


class Lookout:
    def __init__(self, on_event, poll: float = POLL, settle: float = SETTLE):
        self.on_event = on_event
        self.poll, self.settle = max(1.0, poll), max(0.0, settle)
        self._folders: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._tick_lock = threading.Lock()
        self.running: dict[str, dict] = {}     # key -> {"since", "read", "exe"}

    @staticmethod
    def _key(folder) -> str:
        return os.path.normcase(str(Path(folder)))

    def set_games(self, games) -> int:
        """Watch the install folders of these games that have our install record."""
        want = {}
        for g in games:
            try:
                if not getattr(g, "exe", None) or not g.installed:
                    continue
                root = Path(g.install_dir)
                man = installer._previous_manifest(root) or {}
                if not man:
                    continue
                want[self._key(root)] = {"folder": root, "ours": list(man.get("files") or []),
                                         "exe": str(man.get("exe") or ""), "name": g.name}
            except Exception:
                continue
        with self._lock:
            self._folders = want
        return len(want)

    def count(self) -> int:
        with self._lock:
            return len(self._folders)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive() and not self._stop.is_set():
            return
        # A fresh event per thread: switched off and on again within one poll,
        # the old thread was still alive, start() returned, and the old
        # thread then woke to its set event and ended - the rail said
        # "watching" with nothing watching. The old one now ends on its own
        # event while the new one runs on this.
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), name="dlss5-lookout",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def _run(self, stop=None) -> None:
        stop = stop or self._stop
        while not stop.is_set():
            try:
                # one tick at a time: an old thread still finishing its last
                # tick must not read the same close as the new one
                with self._tick_lock:
                    if not stop.is_set():
                        self._tick()
            except Exception:
                log.exception("the watcher")        # never takes the tool down
            stop.wait(self.poll if not any(not r["read"] for r in self.running.values())
                      else min(self.poll, 1.0))

    def _tick(self) -> None:
        with self._lock:
            jobs = dict(self._folders)
        if not jobs:
            return
        ps = watch.procs()
        now = time.monotonic()
        for key, job in jobs.items():
            try:
                up = watch.from_folder(job["folder"], ps, exe=job["exe"])
            except Exception:
                up = []
            state = self.running.get(key)
            if up and state is None:
                self.running[key] = {"since": now, "read": False, "exe": job["exe"]}
                continue
            if up and state is not None and not state["read"] and now - state["since"] >= self.settle:
                state["read"] = True
                try:
                    found = watch.inspect(job["folder"], job["ours"], exe=job["exe"])
                except Exception:
                    found = []
                if found:
                    try:
                        watch.remember(job["folder"], found[0])
                    except Exception:
                        log.exception("recording what the game loaded")
                self.on_event(("started", job["folder"], found[0] if found else None))
                continue
            if not up and state is not None:
                self.running.pop(key, None)
                played = now - state["since"]
                if played >= MIN_PLAY or state["read"]:
                    self.on_event(("closed", job["folder"], played))
