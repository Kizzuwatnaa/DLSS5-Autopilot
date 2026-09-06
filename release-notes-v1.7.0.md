## Anything on your screen through DLSS 5, frame generation on every card, Direct3D 10

### Your screen, your browser, any window

The video player card gets a **screen** row. Pick a monitor or a window and
what is there plays through DLSS 5 in the player, about half a second
behind: YouTube or Twitch in the browser, an emulator, a video call, a game
nothing should be injected into. The desktop is captured with the Desktop
Duplication API on the GPU and encoded by NVENC. Nothing is injected into
the source.

### Frame generation

- **Any RTX card, 2x** - on the *optiscaler* route (D3D12) a new switch turns
  on AMD's FSR 3.1 frame generation from the libraries OptiScaler already
  ships: the upscaler OptiScaler runs is the input, FSR FG the output. Three
  keys in `OptiScaler.ini`, nothing extra to download. Turn the game's own
  frame generation off; expect some latency.
- **RTX 40, 3x/4x** - on the ReShade routes (D3D12 and Vulkan), for games
  that already ship DLSS Frame Generation, a second switch places dashdogy's
  RTX40MFG-Unlock with Ultimate ASI Loader under a proxy name the
  executable really imports (`dinput8.dll`, or `version.dll` when a
  REFramework already holds the first). The multiplier is chosen in
  ReShade's **DLSS MFG** tab. Offered only on an RTX 40 and only when
  `nvngx_dlssg.dll` / `sl.dlss_g.dll` is in the folder. Research software;
  the loader's own ini is merged, not replaced, and the file it displaces
  is backed up.

### Direct3D 10

DLSS5-Feeder 0.13.1 reaches D3D10 through a private D3D11 relay device
inside the game. The tool no longer refuses these games: the feeder route
is offered, and an older feeder build picked by hand is refused with the
build number that works.

### OpenGL

Two findings from [perseval-BLR](https://github.com/perseval-BLR/dlss5-classic-games),
who verified DLSS 5 on six OpenGL games (OpenMW, ioquake3, Serious Sam,
Jedi Academy, Riddick, DOOM 3 BFG):

- `renodx-dlss5` 4.70 stalls after four frames under OpenGL (its fenced
  workset pool never recycles there). OpenGL games are pinned to 4.60.
- LumeniteFX reads 0 % motion under OpenGL. Motion vectors for OpenGL
  games come from **VORT Motion** (optical flow), which the tool installs
  and puts above the feed in the preset. VORT can also be chosen on any
  other API.

The Quake III (GOG loads `opengl32.dll` from System32 - use ioquake3) and
OpenMW notes are shown under the route card before INSTALL.

### NVIDIA driver 616.64 and newer: renodx-dlss5 pinned to 4.55

The DLSS 5 launch drivers (616.64, 616.86) route NGX feature 18 into
`nvngx_dlssnr.dll` itself, and the `renodx-dlss5` 4.6/4.7 add-on faults on
every evaluate there - the game keeps rendering and no neural frame ever
arrives. The feeder's author measured
it on an RTX 5090 (DLSS5-Feeder #54): 4.7 passes 0 of 300 evaluates on
616.64, **4.55 passes 300 of 300** on the same driver. On these drivers the
tool installs 4.55 on every route that uses the add-on and says so in the log;
*did it work?* names the fault when it sees the helper's stack
(`D3D12Core.dll <- nvngx_dlssnr.dll <- _nvngx.dll <- renodx-dlss5`). The
bridge route works around it in memory since dlss5-bridge 1.4.9. If you
installed with 1.6.x on one of these drivers, install again.

### From the first reports

- **Removable drives are skipped** by the folder scan; a USB drive holding a
  backup copy got the install instead of the real game (#18).
- **scan library at start** can be switched off on the games page; *rescan*
  and *choose folder* work on demand. A folder chosen while a scan was
  still running no longer vanishes when the scan finishes (#18).
- A game picked through a `bin` / `Win64` / `Binaries` folder is named after
  the game's own folder, not "bin" (#17).
- The diagnosis lists each loaded add-on once, not once per launch (#22),
  and a Vulkan-layer install that has not produced a log is told to check
  the renderer setting instead of a proxy DLL name (#16, #19).
- The feeder's own `0xE06D7363` crash in the D3D12 evaluate (#17) is fixed
  upstream in DLSS5-Feeder 0.14.0-beta.2; the newest release is what a
  fresh install picks.

### Bug reports

The report body starts with **Did the game start?** - yes / no / it closed
itself - so a game that never launched is a report too, and sorts apart
from one that launched and did nothing (#15). On the feeder route the file
list includes `DLSS5_Feed.fx` and the provider's shader (#13).

### Code signing

The release workflow carries a signing step through the
[SignPath Foundation](https://signpath.org). It runs once the project is
enrolled and is skipped until then; this release is unsigned.

### Updater

The self-updater accepts both release layouts: the single `.exe`, and an
`.exe` with an `_internal` folder beside it. For the folder layout the
swap copies the folder (the install may be on another drive than the
download), keeps the previous one as `_internal.old`, and rolls back if
the copy fails. This release still ships the single file.

### Route descriptions

Each route has a one-line label and a short description of what it does,
what it needs and what it costs. The bridge's description no longer calls
it unmaintained; dlss5-bridge is actively released.

### Also

- Per-executable notes under the route card (Quake III, OpenMW).
