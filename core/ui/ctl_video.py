"""The video player: DLSS 5 on any file, a YouTube link, a webcam or a screen.

Logic only - vidpage draws it. The player is a portable MPC-HC with the feed
installed into it, so it is also a card in the library and installs like a
game; this page is everything else it can do. Every listing (cameras,
screens) and every download or render runs on a worker - the 1.9 window
listed screens on the Tk thread.
"""
from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path

from .. import log, sources, video


class VideoControl:
    def _video_init(self) -> None:
        self.video_busy = ""          # "", "preparing", "downloading", "rendering"
        self.video_progress: tuple | None = None
        self._vprog_job = None
        self.cameras: list[str] | None = None
        self.screens: list[str] | None = None
        self.camera = ""
        self.screen = ""
        self.video_url = ""
        self.video_full = False
        self.video_scale = "native"
        self.video_style = 0

    def player(self):
        return video.known()

    # ------------------------------------------------------------ set up
    def video_setup(self) -> None:
        if self.busy or self.video_busy:
            return
        known = video.known()
        folder = known.install_dir if known else video.default_dir()
        if not known:
            if not self.shell.ask("set up the player",
                                  f"The video player ({video.PLAYER}, portable) goes into:\n\n{folder}\n\n"
                                  f"Nothing is written anywhere else.", "use this folder", "choose another"):
                from tkinter import filedialog
                d = filedialog.askdirectory(title="folder for the video player (a new, empty one is fine)",
                                            parent=self.root)
                if not d:
                    return
                folder = Path(d)
        self.video_busy, self.video_progress = "preparing", (0, "starting")
        self.write("")
        self.write("=== video player ===", "head")
        self.write(f"> fetching {video.PLAYER} and yt-dlp into {folder}")

        def work():
            try:
                g = video.prepare(folder, on_prog=lambda p, m: self.q.put(("vprog", (p, m))),
                                  on_log=lambda t: self.q.put(("log", t)))
                self.q.put(("video_ready", g))
            except (sources.RateLimited, sources.Unavailable) as e:
                self.q.put(("vfail", str(e)))
            except Exception:
                log.exception("setting up the video player")
                self.q.put(("vfail", traceback.format_exc()))
        threading.Thread(target=work, daemon=True).start()
        self.refresh("video", soft=True)

    def _on_vprog(self, payload) -> None:
        """Downloads report per chunk (170 MB of ffmpeg is ~680 of these), and
        each redrew the whole page. Kept, and painted at most every 100 ms,
        in place when the page's progress button is on screen."""
        self.video_progress = payload
        if getattr(self, "_vprog_job", None) is not None:
            return

        def paint():
            self._vprog_job = None
            page = getattr(self.shell, "page", None)
            try:
                if page is not None and page.name == "video" and page.paint_progress():
                    return
            except Exception:
                pass
            self.refresh("video", soft=True)
        try:
            self._vprog_job = self.root.after(100, paint)
        except Exception:
            self._vprog_job = None
            self.refresh("video", soft=True)

    def _on_vfail(self, text) -> None:
        self.video_busy, self.video_progress = "", None
        text = str(text)
        self.write(text, "err")
        self.shell.error("video", text.strip().splitlines()[-1] if text.strip() else "it stopped")
        if "Traceback" in text:
            self.offer_crash_report()
        self.refresh("video")

    def _on_video_ready(self, g) -> None:
        self.video_busy, self.video_progress = "", None
        self.all_games = [x for x in self.all_games if x.install_dir != g.install_dir]
        self.all_games.insert(0, g)
        self.write("> player ready. install puts dlss 5 into it.", "ok")
        self.remember_library()
        self.open_game(g)

    # ------------------------------------------------------------ playing
    def _need_player(self):
        g = video.known()
        if g is None:
            self.shell.info("video", "Set up the player first.")
        return g

    def video_open_player(self) -> None:
        g = self._need_player()
        if g:
            try:
                video.launch(g.install_dir)
            except Exception as e:
                self.shell.error("the player", f"could not start the player:\n{e}")

    def video_play_url(self, url: str) -> None:
        g = self._need_player()
        if not g:
            return
        u = (url or "").strip() or video.clipboard_url(self.root)
        if not video.looks_like_url(u):
            self.shell.info("play a link", "Paste a link first (https://...).")
            return
        self.video_url = u
        if not g.installed:
            self.write("!! dlss 5 is not installed into the player yet - install it first; the link still plays, "
                       "but plain", "warn")
        try:
            video.play_url(g.install_dir, u)
            self.write(f"> playing {u[:90]}", "ok")
            self.shell.status("playing the link in the player")
        except Exception as e:
            self.shell.error("the player", f"could not start the player:\n{e}")

    def video_open_file(self) -> None:
        g = self._need_player()
        if not g:
            return
        from tkinter import filedialog
        f = filedialog.askopenfilename(title="video to play through DLSS 5", parent=self.root,
                                       filetypes=[("video", "*.mp4 *.mkv *.mov *.webm *.avi *.m4v *.ts *.wmv"),
                                                  ("all files", "*.*")])
        if not f:
            return
        try:
            video.launch(g.install_dir, f)
            self.write(f"> playing {Path(f).name} - F6 toggles neural rendering", "ok")
        except Exception as e:
            self.shell.error("the player", f"could not start the player:\n{e}")

    def video_download(self, url: str) -> None:
        g = self._need_player()
        if not g or self.video_busy:
            return
        u = (url or "").strip() or video.clipboard_url(self.root)
        if not video.looks_like_url(u):
            self.shell.info("download", "Paste a link first (https://...).")
            return
        self.video_url = u
        folder, full = g.install_dir, self.video_full
        self.video_busy, self.video_progress = "downloading", (0, "starting")
        self.write("")
        self.write(f"=== download: {u[:90]} ===", "head")
        self.write(f"> saving under {folder / video.DOWNLOADS}")

        def work():
            try:
                if not video.has_ffmpeg(folder):
                    self.q.put(("log", "      fetching ffmpeg once (170 MB) - youtube serves video and audio "
                                       "apart, it joins them"))
                    video.ensure_ffmpeg(folder, on_prog=lambda p, m: self.q.put(("vprog", (p, m))),
                                        on_log=lambda t: self.q.put(("log", t)))
                f_ = video.download(folder, u, full_quality=full,
                                    on_prog=lambda p, m: self.q.put(("vprog", (p, m))),
                                    on_log=lambda t: self.q.put(("log", t)))
                self.q.put(("vdone", ("downloaded", f_)))
            except Exception as e:
                log.exception("downloading a video")
                self.q.put(("vfail", str(e)))
        threading.Thread(target=work, daemon=True).start()
        self.refresh("video", soft=True)

    def video_render(self) -> None:
        g = self._need_player()
        if not g or self.video_busy:
            return
        if not g.installed:
            self.shell.info("render", "Install first - the render uses the DLSS runtimes the install puts beside "
                                      "the player.")
            return
        from tkinter import filedialog
        f = filedialog.askopenfilename(title="video to render through DLSS 5", parent=self.root,
                                       filetypes=[("video", "*.mp4 *.mkv *.mov *.webm *.avi *.m4v *.ts"),
                                                  ("all files", "*.*")])
        if not f:
            return
        folder, scale, style = g.install_dir, self.video_scale, self.video_style
        self.video_busy, self.video_progress = "rendering", (0, "starting")
        self.write("")
        self.write(f"=== render: {Path(f).name} ===", "head")
        self.write("> this runs the model on every frame; a minute of 1080p takes a minute or two on an RTX 4060 Ti.")

        def work():
            try:
                out = video.process(folder, Path(f), scale=scale, style=style,
                                    on_prog=lambda p, m: self.q.put(("vprog", (p, m))),
                                    on_log=lambda t: self.q.put(("log", t)))
                self.q.put(("vdone", ("rendered", out)))
            except Exception as e:
                log.exception("rendering a video")
                self.q.put(("vfail", str(e)))
        threading.Thread(target=work, daemon=True).start()
        self.refresh("video", soft=True)

    def _on_vdone(self, payload) -> None:
        what, path = payload
        self.video_busy, self.video_progress = "", None
        self.write(f"> {what}: {path}", "ok")
        g = video.known()
        try:
            if g:
                video.launch(g.install_dir, str(path))
                self.write("> opening it in the player", "ok")
        except Exception as e:
            self.write(f"!! could not start the player: {e}", "err")
        self.shell.status(f"{what} - opening it in the player")
        self.refresh("video")

    def video_folder(self, which: str) -> None:
        import webbrowser
        g = self._need_player()
        if not g:
            return
        d = g.install_dir / (video.DOWNLOADS if which == "downloads" else video.PROCESSED)
        d.mkdir(exist_ok=True)
        webbrowser.open(str(d))

    def video_toggle(self) -> None:
        if not video.toggle_nr():
            self.shell.info("neural rendering", "The player is not running - open it and start a video first. F6 "
                                                "inside the player does the same thing.")

    # ------------------------------------------------------------ capture
    def list_sources(self) -> None:
        g = video.known()
        if g is None:
            return
        folder = g.install_dir

        def work():
            try:
                cams = video.list_cameras(folder)
            except Exception:
                cams = []
            try:
                scr = video.list_screens()
            except Exception:
                scr = []
            self.q.put(("sources", (cams, scr)))
        threading.Thread(target=work, daemon=True).start()

    def _on_sources(self, payload) -> None:
        cams, scr = payload
        self.cameras, self.screens = list(cams or []), list(scr or [])
        if self.cameras and self.camera not in self.cameras:
            self.camera = self.cameras[0]
        if self.screens and self.screen not in self.screens:
            self.screen = self.screens[0]
        self.refresh("video", soft=True)

    def capture_start(self, kind: str) -> None:
        g = self._need_player()
        if not g:
            return
        target = self.camera if kind == "webcam" else self.screen
        if not target:
            self.shell.info(kind, "Nothing to capture was found." if kind == "screen" else
                            "No camera was found - ffmpeg must be in tools/; one download fetches it.")
            return
        folder, t0 = g.install_dir, time.time()
        self.write(f"> '{target}': starting the capture...")

        def work():
            try:
                (video.start_webcam if kind == "webcam" else video.start_screen)(folder, target)
                self.q.put(("captured", (target, t0)))
            except Exception as e:
                self.q.put(("capfail", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _on_captured(self, payload) -> None:
        target, t0 = payload
        self.capturing = target
        self.write(f"> '{target}' started - checking in 10 s whether the feed is processing it...")
        g = video.known()
        if g is None:
            return
        folder = g.install_dir

        def verify():
            frames, mv = video.feed_frames_since(folder, t0)
            sent = video.capture_frames_sent()
            if sent:
                self.write(f"> capture: {sent} frames sent to the player so far")
            if frames >= 3:
                self.write(f"> '{target}' through DLSS 5: yes - {frames} frames processed so far"
                           + (", motion alive" if mv else "") + ". F6 toggles it.", "ok")
                self.shell.status(f"'{target}' through DLSS 5: yes")
            else:
                self.write("!! the feed has not processed any frames yet. if the player shows the picture, give it "
                           "a few seconds; if it shows nothing, another app may hold the camera, or the window is "
                           "minimised.", "warn")
        self.root.after(10000, verify)
        self.refresh("video", soft=True)

    def _on_capfail(self, text) -> None:
        self.write(f"!! capture: {text}", "err")
        self.shell.error("capture", str(text))

    def capture_stop(self) -> None:
        video.stop_webcam()
        self.capturing = ""
        self.write("> stream stopped")
        self.refresh("video", soft=True)
