"""A video player set up so films and YouTube go through DLSS 5.

The feeder does not care what draws the frame. A video player that renders
through Direct3D 11 is, to ReShade and the feed, a game with no depth buffer:
the colour goes in, LumeniteFX estimates motion from the picture itself, and
the DLSS 5 add-on runs its neural pass on every frame. Tested 2026-09-02 on
MPC-HC 2.8.1 with a local file and with a YouTube link opened live through
yt-dlp: 60 fps, the feed costing about 5% of the frame.

MPC-HC was picked because it is portable (a plain zip, settings in an ini
next to the exe), ships the MPC Video Renderer (D3D11), and opens a YouTube
URL directly when yt-dlp.exe sits beside it - no download, no ffmpeg.

Two things have to be put right before the normal install runs:

  - the renderer. MPC-HC starts on EVR (Direct3D 9) by default, and ReShade
    then attaches to a D3D9 device where the feed cannot work. The ini
    selects the MPC Video Renderer instead.
  - the first-run "check for updates?" dialog, which stops playback until
    someone answers it. The ini answers it.

MPC-HC also bundles an old D3DCompiler_47.dll that rejects the cs_5_1 target
the neural pass is compiled with; the installer moves that aside for every
game (see installer.SIDELINE), so it is not handled here.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from . import games, log, net, prefs, sources

PLAYER = "MPC-HC"
PLAYER_EXE = "mpc-hc64.exe"
INI = "mpc-hc64.ini"
YTDLP = "yt-dlp.exe"
# deno and ffmpeg live in a subfolder: a program started from the player
# folder itself can load ReShade's dxgi.dll (ffmpeg does, for D3D11 hardware
# decoding) and overwrite the player's logs with a no-swapchain session.
# yt-dlp.exe itself stays beside the player: MPC-HC only finds it there
# (its YDLExePath setting made URL playback fail outright, tested
# 2026-09-02), and yt-dlp does not touch DXGI.
TOOLS = "tools"
PREF_KEY = "video_player_dir"

MPC_API = "https://api.github.com/repos/clsid2/mpc-hc/releases/latest"
YTDLP_API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"

# MPC-HC reads these from <exe name>.ini when that file exists next to the
# executable (portable mode). DSVidRen 14 = MPC Video Renderer (D3D11);
# the enum is in the project's AppSettings.h.
SETTINGS = {
    "DSVidRen": "14",
    "UpdaterAutoCheck": "0",
    "RememberWindowPos": "1",
    # 1440p keeps YouTube's VP9/AV1 streams within what one 60 fps neural
    # pass copes with comfortably; the player's options can raise it.
    "YDLMaxHeight": "1440",
}

# The panel hotkeys the finished-install notes point at. F6 is the DLSS 5
# add-on's neural-rendering toggle (renodx-dlss5 4.6+), Home the overlay.
# Checked against MPC-HC 2.8.1's own bindings (its [Commands2] table, ids
# from resource.h): F6 is unbound, F5 = "Save image" (harmless beside the
# add-on's own F5 screenshot), but Home = ID_PLAY_SEEKSET, "jump to the
# start" - opening ReShade's overlay restarted the video. That binding is
# taken away below; the menu still offers the command.
TOGGLE_KEY = "F6"
UNBIND_COMMANDS = {"996": "Home (jump to start) - ReShade's overlay key"}


def default_dir() -> Path:
    return Path.home() / "Videos" / "DLSS5 Player"


def is_player(folder: Path) -> bool:
    return (Path(folder) / PLAYER_EXE).is_file()


def resolve_player() -> tuple[str, str]:
    """(tag, url) of the newest MPC-HC x64 portable zip."""
    data = sources._json(MPC_API)
    for a in data.get("assets", []):
        n = a.get("name", "")
        if n.lower().endswith("x64.zip"):
            return data.get("tag_name", "?"), a["browser_download_url"]
    raise RuntimeError("MPC-HC release has no x64 zip")


def resolve_ytdlp() -> tuple[str, str]:
    """(tag, url) of the newest yt-dlp.exe (64-bit)."""
    data = sources._json(YTDLP_API)
    for a in data.get("assets", []):
        if a.get("name") == YTDLP:
            return data.get("tag_name", "?"), a["browser_download_url"]
    raise RuntimeError("yt-dlp release has no yt-dlp.exe")


def tools_dir(folder: Path) -> Path:
    return Path(folder) / TOOLS


def _write_ini(folder: Path) -> None:
    """Write the portable settings, keeping anything the user changed.

    Byte-level on purpose: MPC-HC writes the file with a UTF-8 BOM and CRLF,
    and a text-mode rewrite on Windows turns every CRLF into CR CR LF. Each
    rewrite then doubled the blank lines, and with enough of them MPC-HC
    crashed the moment a URL was opened (0xc000041d in KERNELBASE, seen
    2026-09-02). So: read bytes, split on any line ending, drop blank lines,
    write CRLF with the BOM kept.
    """
    p = folder / INI
    settings = dict(SETTINGS)
    raw = b""
    if p.is_file():
        try:
            raw = p.read_bytes()
        except OSError:
            raw = b""
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = (raw[3:] if bom else raw).decode("utf8", "replace")
    lines = [ln for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
             if ln.strip()]
    seen: set[str] = set()
    out: list[str] = []
    in_settings = False
    in_commands = False
    have_section = False
    for ln in lines:
        s = ln.strip()
        if in_commands and s.startswith("CommandMod") and "=" in s:
            # "CommandModN=<id> <mod> <vk> ..." - drop the key of the
            # commands that collide with the add-on / ReShade keys.
            key, _, val = s.partition("=")
            parts = val.split(" ")
            if len(parts) >= 3 and parts[0] in UNBIND_COMMANDS and parts[2] != "0":
                parts[2] = "0"
                out.append(f"{key}={' '.join(parts)}")
                continue
        if s.startswith("[") and s.endswith("]"):
            in_commands = s.lower() == "[commands2]"
            if in_settings:
                # Leaving [Settings]: append the keys it did not have.
                for k, v in settings.items():
                    if k not in seen:
                        out.append(f"{k}={v}")
            in_settings = s.lower() == "[settings]"
            have_section = have_section or in_settings
            out.append(ln)
            continue
        if in_settings and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k == "YDLExePath":
                continue            # an earlier build wrote it; it breaks URLs
            if k in settings:
                seen.add(k)
                # The renderer and the update prompt are what make this
                # work; a user-changed renderer would silently break it.
                if k in ("DSVidRen", "UpdaterAutoCheck"):
                    out.append(f"{k}={settings[k]}")
                    continue
        out.append(ln)
    if in_settings:
        for k, v in settings.items():
            if k not in seen:
                out.append(f"{k}={v}")
    if not have_section:
        out.append("[Settings]")
        out += [f"{k}={v}" for k, v in settings.items()]
    data = "\r\n".join(out) + "\r\n"
    p.write_bytes((b"\xef\xbb\xbf" if bom or not raw else b"") + data.encode("utf8"))


def prepare(folder: Path, on_prog=None, on_log=None) -> games.Game:
    """Put a ready-to-use player in `folder` and return it as a Game.

    Everything lands inside `folder`; nothing is written anywhere else. The
    player's own files are only extracted when they are not there yet, so
    a second run keeps the user's playlists and settings.
    """
    folder = Path(folder)
    prog = on_prog or (lambda *_: None)
    say = on_log or (lambda *_: None)
    folder.mkdir(parents=True, exist_ok=True)

    def dl(url: str, fname: str) -> Path:
        def p(done: int, total: int) -> None:
            pct = int(done * 100 / total) if total else 0
            prog(pct, f"{fname} - {net.human(done)}"
                      + (f" / {net.human(total)}" if total else ""))
        return net.download(url, fname, progress=p)

    if is_player(folder):
        say(f"      {PLAYER} is already in {folder}")
    else:
        tag, url = resolve_player()
        say(f"      {PLAYER} {tag}")
        z = dl(url, f"mpc-hc-{tag}-x64.zip")
        with zipfile.ZipFile(z) as zf:
            names = zf.namelist()
            # The zip may or may not wrap everything in one top folder.
            top = ""
            if names and "/" in names[0]:
                first = names[0].split("/", 1)[0] + "/"
                if all(n.startswith(first) for n in names):
                    top = first
            for n in names:
                if n.endswith("/"):
                    continue
                rel = n[len(top):] if top else n
                if not rel:
                    continue
                dst = folder / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(n) as src, open(dst, "wb") as out:
                    out.write(src.read())
        say(f"      {len(names)} files -> {folder}")
    if not is_player(folder):
        raise RuntimeError(f"{PLAYER_EXE} did not appear in {folder}")

    _write_ini(folder)
    say("      renderer: MPC Video Renderer (D3D11); update prompt off")

    try:
        tag, url = resolve_ytdlp()
        src = dl(url, f"yt-dlp-{tag}.exe")
        dst = folder / YTDLP
        # An earlier build put it under tools/, where the player never looked.
        stray = tools_dir(folder) / YTDLP
        if stray.is_file():
            stray.unlink()
        if not dst.is_file() or dst.stat().st_size != src.stat().st_size:
            dst.write_bytes(src.read_bytes())
        say(f"      yt-dlp {tag} - YouTube links open straight in the player")
        try:
            ensure_deno(folder, on_prog=on_prog, on_log=on_log)
        except Exception as e:
            log.write(f"deno: {e}", "warn")
            say(f"      !! deno could not be fetched ({e}); YouTube may offer "
                f"fewer formats")
    except Exception as e:                      # the player still works
        log.write(f"yt-dlp: {e}", "warn")
        say(f"      !! yt-dlp could not be fetched ({e}); local files only "
            f"until it is dropped into the folder by hand")

    try:
        prefs.set_(PREF_KEY, str(folder))
    except Exception:
        pass
    return as_game(folder)


def as_game(folder: Path) -> games.Game:
    g = games.manual(Path(folder) / PLAYER_EXE)
    g.name = f"Video player ({PLAYER})"
    g.kind = "video"
    g.source = "Video"
    return g


def known() -> games.Game | None:
    """The player set up earlier, if it is still there."""
    d = prefs.get(PREF_KEY)
    if d and is_player(Path(d)):
        return as_game(Path(d))
    return None


def launch(folder: Path, target: str = "") -> None:
    """Start the player, optionally on a file or URL."""
    import subprocess
    exe = Path(folder) / PLAYER_EXE
    args = [str(exe)]
    if target:
        args += [target, "/play"]
    subprocess.Popen(args, cwd=str(folder))


FFMPEG_API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
FFMPEG_ASSET = "ffmpeg-master-latest-win64-gpl.zip"
FFMPEG = "ffmpeg.exe"
FFPROBE = "ffprobe.exe"
DOWNLOADS = "downloads"

# Offline processing: DaniilSokolyuk/video2dlssnr, a pure D3D12 command-line
# tool that takes raw RGBA frames on stdin and returns them neural-rendered
# (and optionally DLSS-upscaled) on stdout. The "light" release is the exe
# plus its NGX forwarder only (a quarter of a megabyte); the NVIDIA runtimes
# it needs are the ones the feeder install already put beside the player,
# handed over with --dll-dir. ffmpeg decodes and encodes around it, exactly
# as the project's own nr_video.py does.
PROCESSOR_API = "https://api.github.com/repos/DaniilSokolyuk/video2dlssnr/releases/latest"
PROCESSOR_ASSET = "video2dlssnr_release_light.zip"
PROCESSOR_LATEST = ("https://github.com/DaniilSokolyuk/video2dlssnr/releases/latest/"
                    "download/video2dlssnr_release_light.zip")
PROCESSOR_DIR = "video2dlssnr"
PROCESSOR_EXE = "video2dlssnr.exe"
PROCESSED = "processed"
# NR styles as the tool numbers them.
STYLES = {0: "default", 1: "natural", 2: "cinematic"}
SCALES = {"native": 1.0, "2x": 2.0, "4K (3840 wide)": 0.0}


def has_processor(folder: Path) -> bool:
    d = tools_dir(folder) / PROCESSOR_DIR
    return (d / PROCESSOR_EXE).is_file() and (d / "nvngx.dll_dlssnr.dll").is_file()


def ensure_processor(folder: Path, on_prog=None, on_log=None) -> Path:
    """Put video2dlssnr.exe (+ its forwarder) under tools/, fetch ffmpeg too."""
    folder = Path(folder)
    say = on_log or (lambda *_: None)
    d = tools_dir(folder) / PROCESSOR_DIR
    if not has_processor(folder):
        url = PROCESSOR_LATEST
        try:
            data = sources._json(PROCESSOR_API)
            url = next((a["browser_download_url"] for a in data.get("assets", [])
                        if a.get("name") == PROCESSOR_ASSET), url)
        except Exception:
            pass

        def p(done: int, total: int) -> None:
            if on_prog:
                on_prog(int(done * 100 / total) if total else 0,
                        f"{PROCESSOR_ASSET} - {net.human(done)}")
        z = net.download(url, PROCESSOR_ASSET, progress=p)
        d.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(z) as zf:
            for n in zf.namelist():
                base = n.rsplit("/", 1)[-1]
                if base.lower() in (PROCESSOR_EXE, "nvngx.dll_dlssnr.dll"):
                    with zf.open(n) as src, open(d / base, "wb") as out:
                        out.write(src.read())
        say(f"      video2dlssnr -> {d}")
    if not (has_ffmpeg(folder) and (tools_dir(folder) / FFPROBE).is_file()):
        # an older tools/ has ffmpeg without ffprobe: fetch the pair again
        try:
            (tools_dir(folder) / FFMPEG).unlink()
        except OSError:
            pass
        ensure_ffmpeg(folder, on_prog=on_prog, on_log=on_log)
    for dll in ("nvngx_dlssnr.dll", "nvngx_dlss.dll"):
        if not (folder / dll).is_file():
            raise RuntimeError(f"{dll} is not beside the player - press INSTALL "
                               f"first, the processor uses the same runtimes")
    return d / PROCESSOR_EXE


def _probe(folder: Path, src: Path) -> tuple[int, int, float, int]:
    import subprocess
    out = subprocess.run(
        [str(tools_dir(folder) / FFPROBE), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
         "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1",
         str(src)], capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    d = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if "width" not in d:
        raise RuntimeError("ffprobe could not read the video")
    num, den = (d.get("r_frame_rate", "30/1").split("/") + ["1"])[:2]
    fps = float(num) / float(den or 1)
    frames = int(d.get("nb_frames") or 0)
    if frames <= 0:
        try:
            frames = round(float(d.get("duration") or 0) * fps)
        except ValueError:
            frames = 0
    return int(d["width"]), int(d["height"]), fps, frames


def process(folder: Path, src: Path, scale: str = "native", style: int = 0,
            intensity: float = 1.0, on_prog=None, on_log=None) -> Path:
    """Neural-render a clip on disk into <folder>/processed/<name>_nr.mp4.

    ffmpeg decodes to raw RGBA, video2dlssnr runs DLSS SR (when scaling) and
    the neural pass on every frame on the GPU, ffmpeg re-encodes with NVENC
    and copies the audio across. Progress comes from the tool's NRPROG
    lines on stderr.
    """
    import re
    import subprocess
    import threading
    folder = Path(folder)
    src = Path(src)
    say = on_log or (lambda *_: None)
    prog = on_prog or (lambda *_: None)
    exe = ensure_processor(folder, on_prog=on_prog, on_log=on_log)
    ffmpeg = str(tools_dir(folder) / FFMPEG)
    in_w, in_h, fps, total = _probe(folder, src)
    factor = SCALES.get(scale, 1.0)
    if factor == 0.0:                      # 4K: pin the width, keep aspect
        out_w, out_h = 3840, round(in_h * 3840 / in_w)
    else:
        out_w, out_h = round(in_w * factor), round(in_h * factor)
    out_w -= out_w % 2
    out_h -= out_h % 2
    out_dir = folder / PROCESSED
    out_dir.mkdir(exist_ok=True)
    dst = out_dir / f"{src.stem}_nr{'' if factor == 1.0 else '_' + str(out_w)}.mp4"
    say(f"      {in_w}x{in_h} @ {fps:.2f} fps, {total or '?'} frames -> "
        f"{out_w}x{out_h}, style {STYLES.get(style, style)}, intensity {intensity}")
    say(f"      saving as {dst}")

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    vf = "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709,format=rgba"
    dec = [ffmpeg, "-v", "error", "-i", str(src), "-vf", vf, "-f", "rawvideo", "-"]
    tool = [str(exe), "--nr-video", "--nr-in", f"{in_w}x{in_h}",
            "--nr-style", str(int(style)), "--nr-intensity", str(float(intensity)),
            "--nr-ui-correction", "0", "--nr-motion", "1",
            "--dll-dir", str(folder)]
    if (out_w, out_h) != (in_w, in_h):
        tool += ["--nr-width", str(out_w), "--nr-height", str(out_h)]
    enc = [ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba",
           "-s", f"{out_w}x{out_h}", "-r", f"{fps}", "-i", "-",
           "-i", str(src), "-map", "0:v:0", "-map", "1:a:0?",
           "-c:v", "hevc_nvenc", "-preset", "p5", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest",
           str(dst)]
    p1 = subprocess.Popen(dec, stdout=subprocess.PIPE, creationflags=flags)
    p2 = subprocess.Popen(tool, stdin=p1.stdout, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, cwd=str(exe.parent), creationflags=flags)
    p1.stdout.close()
    p3 = subprocess.Popen(enc, stdin=p2.stdout, stderr=subprocess.PIPE, creationflags=flags)
    p2.stdout.close()
    errs: list[str] = []
    rx = re.compile(rb"^NRPROG (\d+) ([\d.]+)")

    def pump() -> None:
        assert p2.stderr is not None
        for raw in iter(p2.stderr.readline, b""):
            m = rx.match(raw)
            if m:
                n, f = int(m.group(1)), float(m.group(2))
                pct = int(n * 100 / total) if total else 0
                left = (total - n) / f if (total and f > 0) else 0
                prog(min(pct, 99), f"frame {n}/{total or '?'} at {f:.1f} fps"
                                   + (f", {int(left // 60)}:{int(left % 60):02d} left"
                                      if left else ""))
            else:
                line = raw.decode("utf8", "replace").strip()
                if line and not line.startswith("done:"):
                    errs.append(line)
    t = threading.Thread(target=pump, daemon=True)
    t.start()
    rc3 = p3.wait()
    p2.wait()
    p1.wait()
    t.join(timeout=5)
    enc_err = (p3.stderr.read().decode("utf8", "replace").strip() if p3.stderr else "")
    if rc3 != 0 or p2.returncode not in (0, None) or not dst.is_file():
        raise RuntimeError("processing failed: " + " | ".join((errs[-3:] + [enc_err])[-3:])
                           or "no output written")
    prog(100, dst.name)
    return dst



DENO_API = "https://api.github.com/repos/denoland/deno/releases/latest"
DENO_ASSET = "deno-x86_64-pc-windows-msvc.zip"
DENO = "deno.exe"
YTDLP_CONF = "yt-dlp.conf"


def ensure_deno(folder: Path, on_prog=None, on_log=None) -> Path | None:
    """yt-dlp (2026) needs a JavaScript runtime to read YouTube's player.

    Without one it warns and most formats go missing - the player still
    streams something, but downloads fail with "requested format is not
    available". deno is the runtime yt-dlp enables by default; it goes into
    the player folder and yt-dlp.conf (read from the exe's own folder) points
    at it, so both the player's streaming and our downloads use it.
    """
    folder = Path(folder)
    tools_dir(folder).mkdir(exist_ok=True)
    dst = tools_dir(folder) / DENO
    say = on_log or (lambda *_: None)
    if not dst.is_file():
        data = sources._json(DENO_API)
        url = next((a["browser_download_url"] for a in data.get("assets", [])
                    if a.get("name") == DENO_ASSET), None)
        if not url:
            return None

        def p(done: int, total: int) -> None:
            if on_prog:
                on_prog(int(done * 100 / total) if total else 0,
                        f"{DENO_ASSET} - {net.human(done)}"
                        + (f" / {net.human(total)}" if total else ""))
        z = net.download(url, f"deno-{data.get('tag_name', '')}.zip", progress=p)
        with zipfile.ZipFile(z) as zf:
            member = next((n for n in zf.namelist() if n.endswith("deno.exe")), None)
            if not member:
                return None
            with zf.open(member) as src, open(dst, "wb") as out:
                out.write(src.read())
        say(f"      deno {data.get('tag_name', '')} - JavaScript runtime for yt-dlp")
    conf = folder / YTDLP_CONF           # read from yt-dlp.exe's own folder
    stray = tools_dir(folder) / YTDLP_CONF
    if stray.is_file():
        stray.unlink()
    # yt-dlp splits the config file like a POSIX shell: backslashes escape
    # and spaces separate, so the path goes quoted with forward slashes.
    line = f'--js-runtimes "deno:{dst.as_posix()}"'
    try:
        cur = conf.read_text(encoding="utf8") if conf.is_file() else ""
    except OSError:
        cur = ""
    if line not in cur:
        keep = [ln for ln in cur.splitlines() if not ln.startswith("--js-runtimes")]
        conf.write_text("\n".join(keep + [line]) + "\n", encoding="utf8")
    return dst


def looks_like_url(text: str) -> bool:
    t = (text or "").strip().lower()
    return t.startswith(("http://", "https://")) and " " not in t and len(t) < 2048


def clipboard_url(root) -> str:
    """A link sitting on the clipboard, or "". Never raises."""
    try:
        t = root.clipboard_get()
    except Exception:
        return ""
    return t.strip() if looks_like_url(t) else ""


def play_url(folder: Path, url: str) -> None:
    """Open a link straight in the player (yt-dlp resolves the stream)."""
    if not looks_like_url(url):
        raise ValueError("that is not a link")
    launch(folder, url.strip())


def has_ffmpeg(folder: Path) -> bool:
    return (tools_dir(folder) / FFMPEG).is_file()


def ensure_ffmpeg(folder: Path, on_prog=None, on_log=None) -> Path:
    """Fetch ffmpeg.exe once; yt-dlp needs it to merge video and audio.

    YouTube offers no combined file any more, so every download is a merge.
    The build is 170 MB and is fetched on the first download, not with the
    player, so that people who only stream never pay for it.
    """
    folder = tools_dir(folder)
    folder.mkdir(exist_ok=True)
    dst = folder / FFMPEG
    if dst.is_file():
        return dst
    say = on_log or (lambda *_: None)
    data = sources._json(FFMPEG_API)
    url = next((a["browser_download_url"] for a in data.get("assets", [])
                if a.get("name") == FFMPEG_ASSET), None)
    if not url:
        raise RuntimeError("ffmpeg build not found on GitHub")

    def p(done: int, total: int) -> None:
        if on_prog:
            on_prog(int(done * 100 / total) if total else 0,
                    f"{FFMPEG_ASSET} - {net.human(done)}"
                    + (f" / {net.human(total)}" if total else ""))
    z = net.download(url, FFMPEG_ASSET, progress=p)
    with zipfile.ZipFile(z) as zf:
        for exe_name in (FFMPEG, FFPROBE):
            member = next((n for n in zf.namelist()
                           if n.endswith("/bin/" + exe_name)), None)
            if not member:
                raise RuntimeError(f"{exe_name} not in the zip")
            with zf.open(member) as src, open(folder / exe_name, "wb") as out:
                out.write(src.read())
    say(f"      ffmpeg + ffprobe -> {folder}")
    return dst


def download(folder: Path, url: str, on_prog=None, on_log=None,
             full_quality: bool = False) -> Path:
    """Save a link as a file in <folder>/downloads and return the path.

    yt-dlp does the work; its progress lines are turned into the tool's
    progress bar. The file is then opened in the player by the caller.
    """
    folder = Path(folder)
    if not looks_like_url(url):
        raise ValueError("that is not a link")
    exe = folder / YTDLP
    if not exe.is_file():
        raise RuntimeError("yt-dlp.exe is missing from the player folder - run "
                           "'set up the video player' again")
    out_dir = folder / DOWNLOADS
    out_dir.mkdir(exist_ok=True)
    say = on_log or (lambda *_: None)
    prog = on_prog or (lambda *_: None)
    import re
    import subprocess
    # YouTube stopped offering combined video+audio files (checked
    # 2026-09-02: every format is video-only or audio-only), so a download
    # is always a merge and ffmpeg is not optional.
    if not has_ffmpeg(folder):
        raise RuntimeError("ffmpeg.exe is missing from the player folder; the "
                           "download button fetches it once")
    cap = 2160 if full_quality else 1440
    fmt = f"bv*[ext=mp4][height<={cap}]+ba[ext=m4a]/bv*[height<={cap}]+ba/b"
    args = [str(exe), "--no-playlist", "--newline", "-f", fmt,
            "--merge-output-format", "mp4",
            "-o", str(out_dir / "%(title).80s [%(id)s].%(ext)s"),
            "--print", "after_move:filepath", "--no-simulate", "--no-quiet",
            "--progress"]
    args += ["--ffmpeg-location", str(tools_dir(folder))]
    args.append(url.strip())
    say(f"      yt-dlp: best video up to {cap}p + audio, merged to mp4")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(args, cwd=str(tools_dir(folder)), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf8", errors="replace",
                            creationflags=creationflags)
    result = None
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        tail = tail[-8:]
        m = re.search(r"\[download\]\s+([\d.]+)%(.*)", line)
        if m:
            prog(int(float(m.group(1))), "downloading" + m.group(2)[:60])
            continue
        if line.startswith("[") and "ERROR" not in line:
            say(f"      {line[:120]}")
        elif "ERROR" in line:
            say(f"      !! {line[:160]}")
        else:
            candidate = Path(line)
            if candidate.suffix and str(out_dir).lower() in line.lower():
                result = candidate
    proc.wait()
    if proc.returncode != 0 or result is None or not result.is_file():
        raise RuntimeError("yt-dlp failed: " + " | ".join(tail[-3:]))
    prog(100, result.name)
    return result


# Webcam: the player's own "Open Device" needs a camera picked in its
# options first, so ffmpeg (already in tools/) reads the camera through
# DirectShow and hands it to the player as a local MPEG-TS stream over UDP.
# Half a second of latency, and the feed sees it like any other video.
WEBCAM_PORT = 47321
WEBCAM_URL = f"udp://@127.0.0.1:{WEBCAM_PORT}"
_webcam_proc = None


def list_cameras(folder: Path) -> list[str]:
    """DirectShow video devices, as ffmpeg names them."""
    import re
    import subprocess
    ff = tools_dir(folder) / FFMPEG
    if not ff.is_file():
        return []
    try:
        out = subprocess.run([str(ff), "-hide_banner", "-list_devices", "true",
                              "-f", "dshow", "-i", "dummy"],
                             capture_output=True, text=True, encoding="utf8",
                             errors="replace", timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return []
    cams: list[str] = []
    for line in ((out.stderr or "") + "\n" + (out.stdout or "")).splitlines():
        m = re.search(r'"([^"]+)"\s+\(video\)', line)
        if m and m.group(1) not in cams:
            cams.append(m.group(1))
    return cams


def start_webcam(folder: Path, camera: str, size: str = "1280x720", fps: int = 30):
    """Start the camera stream and the player on it. Returns the ffmpeg process."""
    import subprocess
    global _webcam_proc
    stop_webcam()
    ff = tools_dir(folder) / FFMPEG
    if not ff.is_file():
        raise RuntimeError("ffmpeg is not in the player's tools folder yet - "
                           "run one download first, or press 'set up the video "
                           "player' again")
    args = [str(ff), "-hide_banner", "-loglevel", "error", "-f", "dshow",
            "-rtbufsize", "64M", "-video_size", size, "-framerate", str(fps),
            "-i", f"video={camera}",
            "-vcodec", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-g", str(fps), "-pix_fmt", "yuv420p", "-f", "mpegts",
            f"udp://127.0.0.1:{WEBCAM_PORT}?pkt_size=1316"]
    _webcam_proc = subprocess.Popen(args, cwd=str(tools_dir(folder)),
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    import time
    time.sleep(2.0)
    if _webcam_proc.poll() is not None:
        # The size/rate was refused: try the camera's own default.
        args = [a for a in args if a not in ("-video_size", size, "-framerate", str(fps))]
        _webcam_proc = subprocess.Popen(args, cwd=str(tools_dir(folder)),
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        time.sleep(2.0)
        if _webcam_proc.poll() is not None:
            _webcam_proc = None
            raise RuntimeError(f"ffmpeg could not open the camera '{camera}' - is "
                               f"another app using it?")
    launch(folder, WEBCAM_URL)
    return _webcam_proc


def stop_webcam() -> None:
    global _webcam_proc
    if _webcam_proc is not None:
        try:
            _webcam_proc.kill()
            _webcam_proc.wait(timeout=3)
        except Exception:
            pass
        _webcam_proc = None


WINDOW_CLASS = "MediaPlayerClassicW"


def toggle_nr() -> bool:
    """Press the add-on's toggle key in the running player.

    The DLSS 5 add-on reads its hotkey through ReShade's input hook, which
    only sees real keyboard input aimed at the player's window - a posted
    message is ignored. So the player is brought to the front and F6 is
    pressed for real; the person sees the picture change right there.
    Returns False when no player window exists.
    """
    import ctypes
    u = ctypes.windll.user32
    hwnd = u.FindWindowW(WINDOW_CLASS, None)
    if not hwnd:
        return False
    VK_F6, KEYUP, SW_RESTORE = 0x75, 0x0002, 9
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, SW_RESTORE)
    u.SetForegroundWindow(hwnd)
    ctypes.windll.kernel32.Sleep(250)
    u.keybd_event(VK_F6, 0x40, 0, 0)
    ctypes.windll.kernel32.Sleep(80)
    u.keybd_event(VK_F6, 0x40, KEYUP, 0)
    return True


CHECKLIST = (
    "1. open the player; File > Open File, or File > Open URL with a "
    "YouTube link (yt-dlp fetches the stream, nothing is downloaded)",
    f"2. {TOGGLE_KEY} switches neural rendering on and off while it plays - "
    f"compare for yourself",
    "3. Home opens the ReShade overlay if you want the DLSS 5 panel (the "
    "player's own Home = 'jump to start' is unbound for that reason)",
    "!  there is no depth buffer in a video, the feed runs on colour and "
    "motion only - 'depth is flat' in the log is expected here",
)
