## v1.3.0 - routes per game and card, OptiScaler's fps dial, and the boring stuff fixed

The short version: the tool now knows **which of five routes fits your game
and your card**, tells you why, and lets you change it. It carries the
component changes of the last week (three of the five projects shipped major
releases since v1.2.0), updates itself, tells you when a game's parts are
out of date, finds more games, and uninstalls properly.

### Routes: who does what

| Route | Fits |
|---|---|
| **native** (renodx-dlss5) | 64-bit D3D12 games with DLSS - the most proven |
| **optiscaler** (Dagherbou's fork) | D3D12 with DLSS, any RTX. Model-resolution dial 25-100% - at 75% about half the cost, frame stays full detail |
| **renodx-dlss** (ShortFuse's SF build, new) | 64-bit D3D9 / D3D11 / D3D12, in-process, no bridge, no shaders |
| **bridge** | Vulkan games with DLSS; D3D11 fallback. Author has stopped at 1.3.0 |
| **feeder** | games with no DLSS; the only way for 32-bit, OpenGL and DX9 |

The dropdown marks the recommended route and the ones your card cannot use.
**DirectX 10 is refused honestly** - nothing supports it.

### Your card

RTX 50 gets NVIDIA's own FP8 build at full speed. RTX 40 gets the re-targeted
build (moderate cost). RTX 20/30 get the FP16 build (heavy - use the dial).
The tool picks per card, verifies the file's CUDA fatbin records, and prints
which tier you are on. And no, an RTX 50 cannot just swap the DLSS DLL: a
game has to ask for neural rendering, and outside NBA 2K27 none do.

### Fixed

- **`CreateFeature 0xC0000005` on the feeder route.** The feeder's stable
  release only works with DLSS 5 add-on **4.55**; the tool was installing
  the newest add-on (4.6/4.7) beside it. Now pinned together, with a
  checkbox to use the feeder's pre-release when you want the newer add-on.
- **"Uninstall does not remove everything."** Installs are found again even
  if the exe ranking changed; a locked file is retried, reported by name,
  and kept in the record so the next uninstall finishes; read-only files are
  handled; runtime logs are cleaned.
- **"It installs unnecessary things."** Each route installs only its own
  parts, and switching routes removes the previous one first. No two routes'
  add-ons are ever left in one folder.
- **"It does not find my game."** Added Rockstar, Amazon Games, itch,
  Heroic and plain `D:\Games\*` folders; ten more emulators; the scan log
  says what each store returned.
- **"It says the game has no DLSS."** Unreal keeps `nvngx_dlss.dll` under
  `Engine\Plugins\...`, CryEngine under `Bin\Win64Shared`; the tool only
  looked beside the executable, so those games never got the native or
  OptiScaler routes. It now looks where engines keep it.
- **"It installed and nothing happens."** A store's launch executable can be
  a stub in the root (Epic's Ghostwire Tokyo); the game runs from
  `...\Binaries\Win64`. Files went beside the stub. The real one wins now.
- A game search box.

### New

- **Auto-update**: a newer build is downloaded in the background, checked
  against the `SHA256SUMS.txt` GitHub published with it, and one click
  restarts into it. Small fixes reach everyone this way.
- **Bug reports that carry the facts**: *report a bug* opens a GitHub issue
  with version, card, driver, game, route, the last diagnosis, the last
  error and the log tail already filled in; *suggest a feature* does the
  same for ideas. Nothing is sent by itself.
- **Component updates**: after every scan, games set up earlier are checked
  against their publishers; the list shows **update (N newer)**. Press
  install again.
- OptiScaler: model resolution, model preset and style dials in the tool;
  D3D11 games get the bridged upscaler set automatically; driver version
  check (616.56+).
- Per-route in-game checklist after every install.
- Vulkan through the feeder as well as the bridge; 64-bit DX9 through
  renodx-dlss.

### Trust

Built by GitHub Actions from the tagged commit; `SHA256SUMS.txt` and a signed
provenance attestation sit beside the download. The workflow compiles a fresh
PyInstaller bootloader, which removes most antivirus false positives.
SmartScreen's first-run prompt remains (no code-signing certificate).

Built on the work of **jlrouzies-fr** (DLSS5-Feeder), **NIGos**
(dlss5-bridge), **Dagherbou** (OptiScaler DLSS-NR), **ShortFuse** and
**Krish** (RenoDX DLSS 5 add-ons), **umar-afzaal** (LumeniteFX),
**crosire** (ReShade) and the **RankFTW** mirror.
