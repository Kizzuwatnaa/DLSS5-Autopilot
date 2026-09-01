# DLSS 5 Autopilot

A Windows tool that puts DLSS 5 neural rendering into your games. It scans
your library, works out each game's architecture and graphics API, picks the
route that fits the game **and your card**, fetches every component from its
publisher, and writes the configuration. One executable, no installation.

**[→ Download the latest release](../../releases/latest)**

> **This repository contains no game files, no NVIDIA binaries and no
> third-party redistributables.** It is installer logic only. Everything it
> needs is downloaded at run time from the original publishers. See
> [Credits and licensing](#credits-and-licensing).

---

## What is DLSS 5, in one paragraph

DLSS 5 "neural rendering" is a model that runs over a finished frame and
re-lights it - materials, skin, tone. NVIDIA launches it on **3 September 2026
in NBA 2K27, for RTX 50 only.** The runtime leaked a week early from that
game's build, and the modding community wired it into other games through
ReShade add-ons and an OptiScaler fork, then patched builds of the runtime so
RTX 40, 30 and 20 cards can run it too. This tool automates that whole setup.
It is unofficial, it is early, and the components change daily.

---

## Who does what: the five routes

Every game gets one of these. The tool picks the best fit and says why; the
dropdown shows every route the game allows, marks the recommended one, and
marks the ones your card cannot use. You can always pick another.

| Route | What it is | Where it fits | Performance dial |
|---|---|---|---|
| **native** | Krish's `renodx-dlss5` ReShade add-on hooks the game's own DLSS calls on D3D12. | 64-bit D3D12 games that ship DLSS. The most proven route. | The game's own DLSS quality mode |
| **optiscaler** | Dagherbou's OptiScaler fork replaces the upscaler and runs the model over its output. No ReShade. | 64-bit D3D12 (D3D11 works with FSR underneath), game must already use DLSS. Author tested RTX 50; runs on RTX 20/30/40 with the per-card runtime the tool installs. | **Model resolution 25-100%** - the biggest fps lever there is |
| **renodx-dlss** | ShortFuse's `renodx-dlss` add-on (the "SF" build) hooks D3D9, D3D11 and D3D12 in-process. No bridge, no shaders. | 64-bit D3D9 / D3D11 / D3D12, with or without DLSS. Newest component (1 September). | The game's own DLSS mode where it has one |
| **bridge** | NIGos' `dlss5-bridge` reproduces the DLSS contract on a private D3D12 session. | Vulkan games with DLSS (mirror). D3D11 fallback. Author has stopped at 1.3.0. | The game's own DLSS mode |
| **feeder** | jlrouzies-fr's `DLSS5-Feeder` builds a DLAA contract out of ReShade's depth buffer and shader motion vectors. | Games with **no** DLSS: D3D11, D3D12, Vulkan, OpenGL, and the only route for **32-bit** games (host64 helper) and DX9 (via dgVoodoo2). | `work_resolution` 50-100% (64-bit D3D11 only) |

**How the recommendation is made:**

- D3D12 game with DLSS → **optiscaler** (the fps dial, which matters most on the cards where the pass is heaviest). Native is one click away.
- D3D11 game with DLSS → **renodx-dlss**; bridge and optiscaler as alternatives.
- No DLSS in the game (D3D11/12) → **feeder**; renodx-dlss (no shaders, no motion vectors) as the simpler alternative.
- Vulkan with DLSS → **bridge**; Vulkan without → **feeder**.
- OpenGL, 32-bit, DX9 (32-bit) → **feeder**.
- 64-bit DX9 → **renodx-dlss** (nothing else reaches it).
- **DirectX 10 is not supported by anything.** The tool says so instead of installing.

### Support matrix

| Path | Status | How |
|---|---|---|
| 64-bit D3D12 with DLSS | reliable | native / optiscaler / renodx-dlss |
| 64-bit D3D11 with DLSS | beta | renodx-dlss / bridge |
| 64-bit D3D11 / D3D12 without DLSS | reliable | feeder (ReShade + shaders) |
| Vulkan (64-bit) | beta | ReShade as a Vulkan layer + bridge or feeder |
| OpenGL | often fails | feeder, ReShade as `opengl32.dll` |
| 32-bit D3D11 / D3D12 | often fails | feeder + `host64\` helper process |
| DirectX 9 (32-bit) | often fails | dgVoodoo2 → D3D11 → feeder |
| DirectX 9 (64-bit) | beta | renodx-dlss |
| DirectX 10 | not supported | nothing hooks D3D10 |
| Emulators | reliable* | D3D11/12 backend; Vulkan is the beta path |

\* set the emulator's renderer to Direct3D 11/12. Vulkan works through the
layer registration; OpenGL is the least reliable.

---

## Your graphics card, honestly

The neural-rendering runtime (`nvngx_dlssnr.dll`) is compiled per GPU
architecture. NVIDIA's own build carries FP8 kernels for RTX 50 only; the
community re-targeted it for older cards. The tool detects your card, picks
the right build, then **opens the downloaded file and checks the CUDA fatbin
records** to confirm it really contains code for your architecture.

| Card | Build the tool installs | What to expect |
|---|---|---|
| **RTX 50** | `310.8.0` - NVIDIA's original, FP8 | Full speed. The 3 September Game Ready driver ships this same runtime. |
| **RTX 40** | `310.8.0-RTX40` - community, re-targeted to sm_89 | Works. Moderate frame-time cost. |
| **RTX 20 / 30** | `310.8.SF` / `SF-v2` - community, FP16 path | Works. **Heavy**: roughly half your fps at full model resolution. Use the resolution dial. |
| GTX / RTX below 20 | - | Does not run. |

**"I have an RTX 50, can I just swap the DLSS DLL?"** No. The driver brings
the runtime, but a game has to *ask* for neural rendering, and outside NBA
2K27 none do. That is what the add-on (native / renodx-dlss) or OptiScaler is
for, on every card. What RTX 50 saves you is the patched runtime and the
frame-time penalty.

The table above is not hard-coded - it is read from the files - so new
builds work too. The install log tells you which tier you are on.

---

## Using it

1. Run the executable
2. **Step 1** - pick an architecture filter (or "Show everything")
3. **Step 2** - pick your game (there is a search box)
4. **Step 3** - check the route and the dials, press **INSTALL**

The tool then tells you, per route, what to press in the game. In short:

| Route | In game |
|---|---|
| optiscaler | **Insert** opens the overlay. Neural rendering is already on; the model-resolution slider is live. |
| native / bridge | **Home** opens ReShade → DLSS 5 tab → turn neural rendering on (F5 in 4.6+ builds). Keep the game's DLSS on. |
| renodx-dlss | **Home** → RenoDX DLSS tab. Neural rendering is already on. |
| feeder | **Home** → tick `LUMENITE: Kernel 2.0` and `DLSS 5 Feed`, **Kernel above the feed** → DLSS 5 panel → neural rendering on. |

Everywhere: turn the game's own **MSAA/SSAA off**.

Press **Esc** to jump back to the start at any time; the step rail on the
left is clickable too.

### Settings you should know about

- **Set your resolution before turning neural rendering on.** The feature is
  created for one backbuffer size. Changing resolution, display mode or DLSS
  settings while it runs forces a rebuild that can freeze or crash the game.
- Prefer **borderless** over exclusive fullscreen - swapchain recreation on
  alt-tab can crash.
- **Model resolution (optiscaler)** - cost falls with the square: 75% is about
  half the cost of 100%, 50% a quarter. The frame itself stays full detail;
  only the model's contribution is computed small and enlarged. Default 75%.
- **Work area (feeder)** - 50-100%, 64-bit D3D11 only. Ignored elsewhere, so
  the slider is disabled there rather than pretending.
- **NVIDIA Smooth Motion** and the feeder do not mix. Turn it off per game if
  the picture flickers.
- **V-sync at 60 Hz** can pin you to 30 fps once the pass costs a few
  milliseconds. Turn v-sync off or lower the dial.
- **Feeder pre-release** (checkbox) - the feeder's stable release only works
  with DLSS 5 add-on 4.55, and the tool pins it there. Its pre-releases
  support the newer 4.6/4.7 add-on builds; tick the box to use them.
- **Do not use any of this in online games.** ReShade with add-ons and
  anti-cheat do not coexist. The tool detects BattlEye, EAC and Vanguard and
  marks those games blocked.

### Updating

**The tool updates itself.** When a newer release exists it downloads it in
the background, checks it is a 64-bit Windows executable of a sane size **and
that its SHA-256 matches the `SHA256SUMS.txt` GitHub published with the
release**, and the top bar offers **restart into it** - one click. The previous build is
kept next to it as `.old.exe`. Set `"auto_update": false` in
`%LOCALAPPDATA%\dlss5-autopilot\settings.json` to keep the download manual.

**The components update too.** After every scan, games you set up earlier
are checked against what their publishers offer now; a game with newer parts
shows **update (N newer)** in the list. Press install again on it - your
settings and backups are kept. **check versions** on the install page shows
the per-component detail.

### Something crashed, or it does nothing?

Nothing is sent anywhere by itself - there is no telemetry. What there is:

- **report a bug** (left rail) opens a GitHub issue **already filled in**:
  version, card, driver, game, route, the last diagnosis, the last error and
  the tail of the log. You see the text in your browser and decide whether
  to post it, and you can edit it first.
- When the tool hits an internal error it says so in the top bar with a
  **report it** button that does the same.
- After **did it work?** finds a problem, the diagnosis goes into the
  report too.

- **suggest a feature** (left rail) opens an issue labelled *enhancement*.

That is where fixes and the next features come from. A fix ships as a new release, and every
copy out there offers to restart into it the next time it is opened.

### Command line

```
dlss5-autopilot.exe "D:\Games\Game"            install
dlss5-autopilot.exe "D:\Games\Game" --check    detect only, write nothing
dlss5-autopilot.exe "D:\Games\Game" --remove   uninstall
```

---

## What makes it more than a copy script

**It finds your games.** Steam, Epic, GOG, EA app, Ubisoft Connect,
Battle.net, Rockstar, Amazon Games, itch, Heroic, Xbox/Game Pass folders,
plain `D:\Games\*` folders, and eighteen emulators (DuckStation, PCSX2,
Dolphin, PPSSPP, Xenia, Cemu, RPCS3, Ryujinx, yuzu/suyu/Eden, shadPS4,
Azahar/Citra, melonDS, Flycast, xemu, Vita3K, RetroArch, mGBA, Snes9x,
Play!). Anything else: **Choose folder…** and point at the folder the `.exe`
is actually in.

**The right executable.** Files must sit next to the executable that
actually runs - many games keep it in a subfolder (`Bin\Win64…\Game.exe`).
When a folder has several candidates you can pick which one, and an install
made earlier is found again even if the ranking changes.

**Nothing is overwritten without a backup.** Every file the tool replaces -
the game's own `nvngx_dlss.dll`, a tuned `OptiScaler.ini`, an existing
`ReShade.ini` - is saved alongside and restored on uninstall.

**Uninstall removes exactly what was installed.** Everything written is
recorded in `dlss5-autopilot.json` in the game folder; the logs the
components write later are known too. A file the game or its launcher is
still holding is retried, reported by name, and kept in the record so the
next uninstall finishes the job - it is never silently left behind.

**Switching routes is clean.** Installing another route removes the previous
one first, and no two routes' add-ons are ever left in one folder (ReShade
loads every add-on it finds, and two of them fighting over NGX is the
classic "game exits before the first frame").

**Versions that go together.** The feeder's stable release conflicts with
DLSS 5 add-on builds newer than 4.55 - the DLSS feature dies in
`CreateFeature` - so the tool pins them together and says so in the log.

**"Did it work?"** After you have played, press it. The tool reads the
components' logs back and tells you in plain words what happened - whether
the shader loaded, whether motion vectors are alive, whether the DLSS
feature was created, and whether frames are actually being delivered. The
usual failure is silent: the game simply looks unchanged.

**It keeps working when GitHub rate-limits you.** GitHub allows 60 anonymous
API calls an hour per address. Every API answer is cached on disk and reused
when a live call fails, so an install still completes.

---

## Requirements

Windows, an NVIDIA RTX 20 series card or newer, and a recent driver
(OptiScaler's DLSS-NR needs **616.56+**; the tool checks). The first install
downloads roughly 150 MB (`nvngx_dlssnr.dll` alone unpacks to 165 MB) into
`%LOCALAPPDATA%\dlss5-autopilot\cache`; later games install instantly.

---

## Troubleshooting

`dlss5-feed.log` / `ReShade.log` / `OptiScaler.log` in the game folder are
the first places to look, and **did it work?** reads them for you.

| Line | Meaning |
|---|---|
| `feature ready … DLAA` | the contract was established |
| `frame N delivered` | frames are being processed |
| `MV probe … N% non-zero` | should not be 0% while moving |
| `CreateFeature raised exception 0xC0000005` | add-on / feeder version mismatch (see above), or the runtime does not match the card |

### It worked, then stopped after I changed display mode

The contract is built out of ReShade's depth buffer, chosen by matching it
against the back buffer. Borderless instead of fullscreen, Windows display
scaling, or an in-game render scale below 100% can leave nothing selected:
everything sets up and no frame is ever produced. Open the ReShade overlay,
**Add-ons** tab, depth buffer list - one entry has to be selected. If none
is, turn **"Use aspect ratio heuristics"** off there.

### The tool does not list my game

Not every launcher can be found from the registry, and an executable locked
at the moment of the scan (antivirus, a running updater, OneDrive
placeholders) cannot be read. The scan log (**open log file**) says what
each store returned. **Choose folder…** always works.

### Antivirus quarantined a file after install

`renodx-dlss5.addon64`, `nvngx_dlssnr.dll` and OptiScaler are unsigned,
freshly built, uncommon, and hook graphics APIs - everything heuristics look
for. The install reports success and then the game does nothing. The tool
notices the missing file and tells you; restore it from quarantine and add
the game folder to your exclusions.

---

## Network access

The tool contacts these hosts and nothing else:

```
reshade.me
raw.githubusercontent.com
api.github.com  ·  github.com  ·  objects.githubusercontent.com
codeload.github.com
```

All download URLs live in a single file, [`core/sources.py`](core/sources.py).

---

## Is it safe? How to check for yourself

Fair question to ask about any .exe from a Discord link. Do not take
anyone's word for it - here is what you can check.

**Every release is built by GitHub, not uploaded by a person.** Pushing a
version tag runs [`release.yml`](.github/workflows/release.yml) on GitHub's
own Windows runner: it builds the .exe from the commit you can read, writes
`SHA256SUMS.txt`, and attaches a **signed provenance attestation** that ties
the file to that exact commit and build log. Both are on every release page,
with the hashes at the top of the notes.

```
certutil -hashfile dlss5-autopilot.exe SHA256
gh attestation verify dlss5-autopilot.exe --repo Kizzuwatnaa/DLSS5-Autopilot
```

If the hash differs from the release page, the file did not come from here.

**The source is all here, and there is nothing else in it.** Plain Python,
standard library and tkinter only; nothing from PyPI ends up in the build.
Run it from source if you would rather not take the .exe at all:

```
git clone https://github.com/Kizzuwatnaa/DLSS5-Autopilot
cd DLSS5-Autopilot
python dlss5_autopilot.py
```

**About antivirus warnings.** The build is packed with PyInstaller. Its
stock bootloader is the same wrapper a lot of real malware uses, so heuristic
scanners flag it - `Trojan.Generic`, `Wacatac.C!ml`, on *any* PyInstaller
build. Since v1.3.0 the release workflow **compiles a fresh bootloader on the
runner** rather than using the one every scanner has a signature for, which
removes most of these. What it cannot remove is SmartScreen's
*"Windows protected your PC"* on first run: that is about the missing
publisher certificate, not the file. **More info → Run anyway**, once. A
code-signing certificate costs money the project does not have.

Windows Defender's **Block at First Sight** can also flag a brand-new
release for a day and clear it after enough machines have seen it, without
the file changing. Report a false positive at
<https://www.microsoft.com/en-us/wdsi/filesubmission>; a confirmed one is
corrected for everyone.

**What it actually does to your machine**, all of which you can read:

- writes only into the game folder you pick, and backs up anything it replaces
- the one exception is Vulkan: ReShade's layer is a per-user registry value, the tool says so before writing it and removes it with the last Vulkan game
- keeps its settings and cache in `%LOCALAPPDATA%\dlss5-autopilot`
- never needs administrator rights, and never asks for them
- downloads only from the hosts listed above
- sends nothing anywhere: no telemetry, no analytics, no account

---

## Credits and licensing

This tool is a downloader and configurator. It bundles nothing. Each
component is fetched at run time from its own publisher and remains under
its own licence:

| Component | Project | Licence |
|---|---|---|
| ReShade | [crosire/reshade](https://github.com/crosire/reshade) | BSD-3-Clause |
| Shader headers | [crosire/reshade-shaders](https://github.com/crosire/reshade-shaders) | per-file |
| DLSS5-Feeder | [jlrouzies-fr/DLSS5-Feeder](https://github.com/jlrouzies-fr/DLSS5-Feeder) | see repository |
| dlss5-bridge | [NIGos/dlss5-bridge](https://github.com/NIGos/dlss5-bridge) | see repository |
| OptiScaler DLSS-NR fork | [Dagherbou/OptiScaler_DLSSNR](https://github.com/Dagherbou/OptiScaler_DLSSNR) | GPL-3.0 |
| LumeniteFX | [umar-afzaal/LumeniteFX](https://github.com/umar-afzaal/LumeniteFX) | AGNYA |
| dgVoodoo2 | [dege-diosg/dgVoodoo2](https://github.com/dege-diosg/dgVoodoo2) | freely redistributed by its author |
| RenoDX DLSS 5 add-ons (Krish, ShortFuse), NVIDIA NGX runtimes | community-distributed | **proprietary, no public licence** |

The DLSS 5 add-ons and the NVIDIA NGX runtimes are closed-source software
with no published licence. **They are not in this repository, not in the
release archive, and not redistributed by this project.** The tool downloads
them from a public community mirror, exactly as a person would by hand. If
you are not comfortable with that, do not use this tool.

Nothing here is affiliated with or endorsed by NVIDIA, ReShade, RenoDX,
OptiScaler or any of the projects above. Use at your own risk.

The installer's own source code is MIT licensed - see [LICENSE](LICENSE).

If you are a rights holder and want something changed or removed, open an
issue and it will be addressed.

---

## Building from source

```
build.bat
```

Needs Python 3.10+ and `pip install pyinstaller`. The release build is the
one GitHub makes; a local build behaves the same but carries the stock
bootloader.

### Tests

```
python test_reshade_ini.py     ReShade ini/preset logic, including ordering
python test_install.py         end-to-end install + uninstall in temp folders
python test_clean_machine.py   empty cache, no local files: completeness check
python test_all.py             the whole suite against the current tree
```

### Layout

```
dlss5_autopilot.py    entry point (GUI + CLI)
core/pe.py            PE parsing: bitness, imports, API detection, exe ranking
core/games.py         Steam / Epic / GOG / EA / Ubisoft / Battle.net / Rockstar /
                      Amazon / itch / Heroic / Xbox / folder scanning
core/emulators.py     emulator profiles and discovery
core/gpu.py           GPU + driver detection, CUDA architecture check, build tiers
core/dlss.py          which route fits the game and the card
core/sources.py       every download URL, in one place; version pins
core/net.py           downloading, caching, zip extraction
core/prefs.py         persistent settings, local add-on discovery
core/reshade_ini.py   ReShade.ini / preset writing and technique ordering
core/feedcfg.py       dlss5-feed.cfg and dlss5-bridge.cfg
core/optiscaler.py    the OptiScaler route and its [DlssNr] dials
core/vulkan.py        ReShade as a Vulkan implicit layer
core/dgvoodoo.py      DX9 to D3D11 via dgVoodoo2
core/anticheat.py     BattlEye / EAC / Vanguard detection
core/installer.py     install engine, route switching, uninstall
core/components.py    are the installed parts still current?
core/update.py        update check
core/selfupdate.py    download, verify and swap in a new build
core/diagnose.py      reading the logs back into an answer
core/gui.py           interface
```
