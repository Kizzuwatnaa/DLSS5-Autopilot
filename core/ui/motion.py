"""Animation: one job per key, eased, measured against the clock.

A frame is scheduled every ~12 ms but the position comes from elapsed time,
so a slow frame makes the motion skip ahead rather than run long. Starting an
animation under a key that is already moving replaces it - hovering from one
cover to the next never leaves two fades fighting over the same item.
"""
from __future__ import annotations

import time


def ease_out(k: float) -> float:
    return 1 - (1 - k) ** 3


def mix(a: str, b: str, k: float) -> str:
    """The colour k of the way from a to b (#rrggbb)."""
    k = max(0.0, min(1.0, k))
    ra, ga, ba = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    rb, gb, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    return "#%02x%02x%02x" % (round(ra + (rb - ra) * k), round(ga + (gb - ga) * k),
                              round(ba + (bb - ba) * k))


def ink_on(bg: str) -> str:
    """Dark or light text, whichever reads on this background."""
    r, g, b = int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16)
    return "#111111" if (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 > 0.5 else "#ffffff"


class Motion:
    def __init__(self, widget):
        self.widget = widget
        self.jobs: dict[str, str] = {}

    def run(self, key: str, ms: int, step, done=None) -> None:
        self.stop(key)
        start = time.perf_counter()
        ms = max(1, ms)

        def tick():
            k = min(1.0, (time.perf_counter() - start) * 1000.0 / ms)
            try:
                step(ease_out(k))
            except Exception as e:
                # A canvas item that went away under us (the page changed) is
                # the normal end of an animation; anything else is a bug and
                # goes to the log rather than vanishing with the frame.
                self.jobs.pop(key, None)
                import tkinter as _tk
                if not isinstance(e, _tk.TclError):
                    try:
                        from .. import log
                        log.exception(f"animation '{key}'", e)
                    except Exception:
                        pass
                return
            if k < 1.0:
                self.jobs[key] = self.widget.after(12, tick)
            else:
                self.jobs.pop(key, None)
                if done:
                    done()
        tick()

    def stop(self, key: str) -> None:
        job = self.jobs.pop(key, None)
        if job:
            try:
                self.widget.after_cancel(job)
            except Exception:
                pass

    def stop_all(self, keep: tuple = ()) -> None:
        """Stop every animation except those whose key starts with one of
        `keep` - the shell's own (the log drawer, a toast, the wheel's glide)
        outlive a page being redrawn under them."""
        for key in list(self.jobs):
            if not any(key.startswith(k) for k in keep):
                self.stop(key)

    def busy(self, key: str) -> bool:
        return key in self.jobs
