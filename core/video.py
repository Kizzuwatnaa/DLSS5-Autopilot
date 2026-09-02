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
TOGGLE_KEY = "F6"


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
    have_section = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
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
DOWNLOADS = "downloads"


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
        member = next((n for n in zf.namelist() if n.endswith("/bin/ffmpeg.exe")), None)
        if not member:
            raise RuntimeError("ffmpeg.exe not in the zip")
        with zf.open(member) as src, open(dst, "wb") as out:
            out.write(src.read())
    say(f"      ffmpeg -> {dst}")
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
    "3. Home opens the ReShade overlay if you want the DLSS 5 panel",
    "!  there is no depth buffer in a video, the feed runs on colour and "
    "motion only - 'depth is flat' in the log is expected here",
)
