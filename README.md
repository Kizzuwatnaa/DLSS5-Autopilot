# DLSS 5 Autopilot

A Windows tool that automates the whole DLSS5-Feeder setup. It scans your
games, detects each one's architecture and graphics API, fetches every
component, and writes the ReShade configuration in the right order.

**[→ Download the latest release](../../releases/latest)** — single file, no installation.

> **This repository contains no game files, no NVIDIA binaries and no
> third-party redistributables.** It is installer logic only. Everything it
> needs is downloaded at run time from the original publishers. See
> [Credits and licensing](#credits-and-licensing).

---

## What it supports

There are three routes into a game, and the tool picks the right one for you.

| Route | What it does | When it is used |
|---|---|---|
| **native** | The DLSS 5 add-on hooks the game's own NGX D3D12 calls | Game ships DLSS and renders with D3D12 |
| **optiscaler** | OptiScaler replaces the upscaler entirely — no ReShade — and reads the game's own DLSS depth and motion vectors | D3D12 game with DLSS, when you want the cheapest route and real upscaling |
| **bridge** | `dlss5-bridge` reproduces the DLSS contract on a private D3D12 session | D3D11 or Vulkan games, and games with no DLSS at all |
| **feeder** | DLSS5-Feeder builds a synthetic DLAA contract from ReShade's depth and shader motion vectors | Everything else |

On the **native** route your in-game DLSS quality setting (Quality / Balanced /
Performance) still applies — it is the game's own DLSS being upgraded, not a
synthetic one. The feeder route is always DLAA.

| API | Support |
|---|---|
| **64-bit DX12** | reliable — native when the game has DLSS, else feeder |
| **64-bit DX11** | reliable — bridge when the game has DLSS, else feeder |
| **Vulkan** | beta — bridge only; ReShade is registered as a Vulkan layer |
| DirectX 9 | long shot — dgVoodoo2 translates to D3D11, then bridge or feeder |
| DirectX 10 | long shot — feeder only, nothing hooks D3D10 |
| 64-bit OpenGL | long shot — feeder only |
| 32-bit anything | long shot — feeder only, via the `host64\` helper |
| Emulators | DuckStation, PCSX2, Dolphin, PPSSPP, Xenia — set them to a D3D11/12 backend |

**Vulkan is a global change.** ReShade reaches Vulkan as an *implicit layer*: a
registry value the Vulkan loader reads at start-up. Once registered it loads
into every Vulkan application on your account, not just the game. The tool
registers it under `HKEY_CURRENT_USER` (no administrator rights), says so
before doing it, reuses an existing ReShade registration rather than
duplicating it, and removes its own on uninstall.

**Be realistic:** DX11/DX12 is where this works. DirectX 9, DirectX 10, OpenGL
and 32-bit games go through extra translation or a helper process and often
fail at `CreateFeature`. The tool labels every game's outlook rather than
pretending.

Steam, Epic and GOG libraries are scanned automatically. Anything else can be
added with **Choose folder…**.

---

## Using it

1. Run the executable
2. **Step 1** — pick an architecture filter (or "Show everything")
3. **Step 2** — pick your game
4. **Step 3** — press **INSTALL**

Then in the game:

- **Home** opens the ReShade overlay
- `LUMENITE: Kernel 2.0` and `DLSS 5 Feed` must both be ticked, **Kernel above the feed**
- Enable neural rendering in the `DLSS 5 Neural Rendering` panel
- Turn the game's own **MSAA/SSAA** off

Press **Esc** to jump back to the start at any time; the step rail on the left
is clickable too.

When a newer release exists, a bar appears at the top. **Update now**
downloads it, swaps the executable and restarts — the previous build is kept
next to it as `.old.exe` in case you want to go back.

### Command line

```
dlss5-autopilot.exe "D:\Games\Game"            install
dlss5-autopilot.exe "D:\Games\Game" --check    detect only, write nothing
dlss5-autopilot.exe "D:\Games\Game" --remove   uninstall
```

---

## What makes it more than a copy script

**GPU compatibility is verified, not assumed.** The CUDA code inside the DLSS
neural-rendering runtime is compiled per architecture. The tool detects your
card, then parses the fatbin records inside the downloaded file to confirm it
actually contains code for that architecture. Measured results:

| Build | RTX 20 | RTX 30 | RTX 40 | RTX 50 |
|---|:---:|:---:|:---:|:---:|
| `310.8.0` | – | – | – | ✓ |
| `310.8.0-RTX40` | – | – | ✓ | ✓ |
| `310.8.SF` | ✓ | ✓ | ✓ | ✓ |
| `310.8.SF-v2` | ✓ | ✓ | ✓ | ✓ |

Left on **Auto**, the tool walks the list newest-first and picks the first
build that supports your card. The table is not hard-coded — it is read from
the files, so new releases work too.

**Technique ordering is handled.** The motion-vector provider's technique must
sit above `DLSS 5 Feed` or the feed never receives vectors. The tool writes
this correctly and leaves your existing ReShade settings and other shaders
alone.

**Your own files are backed up.** If a game ships its own `nvngx_dlss.dll` and
it gets replaced, the original is saved alongside and restored on uninstall.

**The right executable.** Files must sit next to the executable that actually
runs — many games keep it in a subfolder (`Bin\Win64…\Game.exe`). When a
folder has several candidates you can pick which one to target.

**"Did it work?"** After you have played, press it. The tool reads
`dlss5-feed.log` and `ReShade.log` back and tells you in plain words what
happened — whether the shader loaded, whether motion vectors are alive,
whether the DLSS feature was created, and whether frames are actually being
delivered. This matters because the usual failure is silent: the game simply
looks unchanged.

**It keeps working when GitHub rate-limits you.** GitHub allows 60 anonymous
API calls an hour per address, which is easy to hit behind a VPN, a
university network or CGNAT. Every API answer is cached on disk and reused
when a live call fails, so an install still completes — with a note saying
the version list is stale.

---

## Settings

The **Quality / speed** section writes `dlss5-feed.cfg`:

- **Work area** (`work_resolution`, 50–100%) — the performance dial. Only the
  64-bit **D3D11** path honours it; the add-on's own log line is
  `settled D3D11 work resolution=…%`. On DX12, OpenGL and the 32-bit helper
  the value is ignored, so the slider is disabled there rather than pretending.
- **DLSS preset** — if you see warping around flames or transparent objects,
  try Preset E or F (the legacy CNN).
- **HDR** — auto / force SDR / force HDR

### Why there is no "DLSS Performance mode"

The feeder path is always DLAA and cannot be otherwise. DLSS5-Feeder never
sees the game's low-resolution render; it sees the finished full-resolution
frame at the end of the ReShade chain. There is no low-resolution source to
upscale from, so Quality / Balanced / Performance have no meaning here. The
performance dial is `work_resolution`.

---

## Requirements

Windows, and an NVIDIA RTX 20 series card or newer. The first install
downloads roughly 150 MB (`nvngx_dlssnr.dll` alone unpacks to 165 MB) into
`%LOCALAPPDATA%\dlss5-autopilot\cache`; later games install instantly.

---

## Warnings

- **Do not use this in online games.** ReShade with add-ons will be flagged by
  anti-cheat. The tool detects BattlEye, Easy Anti-Cheat, Vanguard and others
  in the game folder and marks those games `blocked` in the list — that is why
  Arma 3 and Arma Reforger do nothing when set up. It is not a tool bug and
  there is no way around it.
- **Set your resolution before turning neural rendering on.** The DLSS
  feature is created for one specific backbuffer size. Changing resolution,
  display mode or DLSS settings while it is running forces a rebuild, which
  can black-screen, freeze or crash the game. Turn neural rendering off, change
  the resolution, then turn it back on.
- Prefer **borderless** over exclusive fullscreen — swapchain recreation on
  alt-tab can crash.
- Neural rendering costs several milliseconds. With v-sync on at 60 Hz you can
  drop to 30 fps; turn v-sync off or lower the work area.
- Emulators must be set to a **Direct3D 11/12** backend, and ReShade may latch
  onto the wrong depth buffer — pick the right one in its DX11/DX12 tab.

---

## Troubleshooting

`dlss5-feed.log` in the game folder is the first place to look:

| Line | Meaning |
|---|---|
| `feature ready … DLAA` | the contract was established |
| `frame N delivered` | frames are being processed |
| `MV probe … N% non-zero` | should not be 0% while moving |
| `CreateFeature raised exception 0xC0000005` | the add-on and the `nvngx_dlssnr` build do not get along — try another combination |

Everything the tool writes is recorded in `dlss5-autopilot.json` in the game
folder. **Uninstall** removes exactly those files, restores backups, and
leaves your own shaders and settings untouched.

---

## Network access

The tool contacts these hosts and nothing else:

```
reshade.me
raw.githubusercontent.com
api.github.com  ·  github.com  ·  objects.githubusercontent.com
codeload.github.com
```

All download URLs live in a single file, [`core/sources.py`](core/sources.py),
so they are easy to audit.

---

## Is it safe? How to check for yourself

Fair question to ask about any .exe from a Discord link. Do not take anyone's
word for it — here is what you can verify.

**The executable is built by GitHub, not on anyone's PC.** Every release is
compiled by the [`release` workflow](.github/workflows/release.yml) on GitHub's
own runners, from the source in this repository. The build log is public: open
the Actions tab and read exactly what was run. Nobody gets to slip anything in
between the code you can read and the file you download.

**Check the file is the one that build produced.** Every release ships a
`SHA256SUMS.txt`. Compare it against your download:

```powershell
Get-FileHash .\dlss5-autopilot.exe -Algorithm SHA256
```

If it does not match, do not run it — it did not come from here.

**Check the signed provenance**, which ties the binary to the exact commit and
build run that produced it:

```
gh attestation verify dlss5-autopilot.exe --repo Kizzuwatnaa/DLSS5-Autopilot
```

**About antivirus warnings.** The build is packed with PyInstaller, which
bundles a Python interpreter into a single self-extracting .exe. That is the
same shape a lot of real malware uses, so heuristic scanners flag it — a
handful of the ~70 engines on VirusTotal will usually say something like
`Trojan.Generic` or `Wacatac` on *any* PyInstaller build, including a script
that does nothing at all. It is a false positive, and it is the price of
shipping a single .exe with no installer.

Windows Defender, which is what almost everyone actually runs, reports no
threat. If you would rather not take the .exe at all, **run it from source** —
it is plain Python with no third-party packages:

```
git clone https://github.com/Kizzuwatnaa/DLSS5-Autopilot
cd DLSS5-Autopilot
python dlss5_autopilot.py
```

**What it actually does to your machine**, all of which you can read:

- writes only into the game folder you pick, and backs up anything it replaces
- keeps its settings and log in `%LOCALAPPDATA%\dlss5-autopilot`
- adds one registry key under `HKEY_CURRENT_USER` — and only for the Vulkan
  route, which needs it, removed again on uninstall
- never needs administrator rights, and never asks for them
- downloads only from the hosts listed above
- sends nothing anywhere: no telemetry, no analytics, no account

---

## Credits and licensing

This tool is a downloader and configurator. It bundles nothing. Each component
is fetched at run time from its own publisher and remains under its own
licence:

| Component | Project | Licence |
|---|---|---|
| dlss5-bridge | [NIGos/dlss5-bridge](https://github.com/NIGos/dlss5-bridge) | see repository |
| OptiScaler (DLSS-NR fork) | [Dagherbou/OptiScaler_DLSSNR](https://github.com/Dagherbou/OptiScaler_DLSSNR) | GPL-3.0 |
| ReShade | [crosire/reshade](https://github.com/crosire/reshade) | BSD-3-Clause |
| Shader headers | [crosire/reshade-shaders](https://github.com/crosire/reshade-shaders) | per-file |
| DLSS5-Feeder | [jlrouzies-fr/DLSS5-Feeder](https://github.com/jlrouzies-fr/DLSS5-Feeder) | see repository |
| LumeniteFX | [umar-afzaal/LumeniteFX](https://github.com/umar-afzaal/LumeniteFX) | AGNYA |
| dgVoodoo2 | [dege-diosg/dgVoodoo2](https://github.com/dege-diosg/dgVoodoo2) | freely redistributed by its author |
| RenoDX DLSS 5 add-on, NVIDIA NGX runtimes | community-distributed | **proprietary, no public licence** |

The DLSS 5 neural-rendering add-on and the NVIDIA NGX runtimes are
closed-source software with no published licence. **They are not in this
repository, not in the release archive, and not redistributed by this
project.** The tool downloads them from a public community mirror, exactly as
a person would by hand. If you are not comfortable with that, do not use this
tool.

Nothing here is affiliated with or endorsed by NVIDIA, ReShade, RenoDX or any
of the projects above. Use at your own risk.

The installer's own source code is MIT licensed — see [LICENSE](LICENSE).

If you are a rights holder and want something changed or removed, open an
issue and it will be addressed.

---

## Building from source

```
build.bat
```

Needs Python 3.10+ and `pip install pyinstaller`. The script installs
PyInstaller if it is missing and produces `dlss5-autopilot.exe`.

### Tests

```
python test_reshade_ini.py     ReShade ini/preset logic, including ordering
python test_install.py         end-to-end install + uninstall in temp folders
python test_clean_machine.py   empty cache, no local files: completeness check
```

`test_clean_machine.py` reproduces a fresh machine: it downloads everything
from scratch and asserts that all three install paths end up complete.

### Layout

```
dlss5_autopilot.py    entry point (GUI + CLI)
core/pe.py            PE parsing: bitness, imports, API detection, exe ranking
core/games.py         Steam / Epic / GOG / emulator scanning
core/emulators.py     emulator profiles and discovery
core/gpu.py           GPU detection + CUDA architecture compatibility check
core/sources.py       every download URL, in one place
core/net.py           downloading, caching, zip extraction
core/prefs.py         persistent settings, local renodx discovery
core/reshade_ini.py   ReShade.ini / preset writing and technique ordering
core/feedcfg.py       dlss5-feed.cfg
core/dgvoodoo.py      DX9 to D3D11 via dgVoodoo2
core/dlss.py          native-DLSS detection and route selection
core/vulkan.py        registering ReShade as an implicit Vulkan layer
core/anticheat.py     spotting anti-cheat before it wastes your time
core/optiscaler.py    the OptiScaler DLSS-NR route
core/installer.py     install engine and reliability assessment
core/update.py        update check
core/selfupdate.py    download and swap in a new build
core/diagnose.py      reading dlss5-feed.log back into an answer
core/gui.py           interface
```
