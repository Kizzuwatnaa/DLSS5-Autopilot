Four ways into a game instead of one, and the tool works out which your game actually needs.

## Routes

The feeder is often the wrong answer. v1.3 reads the game folder for Streamline/NGX files and the API from the PE import table, then recommends — you can override it.

| Route | What happens | When |
|---|---|---|
| **native** | The DLSS 5 add-on hooks the game's own NGX D3D12 calls | Game ships DLSS, renders D3D12 |
| **optiscaler** | OptiScaler replaces the upscaler entirely — no ReShade — and reads the game's own DLSS depth and motion vectors | D3D12/D3D11 with DLSS, when you want the cheapest route and real upscaling |
| **bridge** | `dlss5-bridge` reproduces the contract on a private D3D12 session | D3D11, Vulkan, or no DLSS at all |
| **feeder** | The synthetic DLAA contract from ReShade's depth and shader motion vectors | Everything else |

On **native** and **optiscaler** your in-game DLSS quality setting still applies. The feeder is always DLAA.

## Vulkan works now

ReShade reaches Vulkan as an implicit layer, not a proxy DLL. The layer manifest is written and registered under `HKEY_CURRENT_USER` — no administrator rights. An existing ReShade registration is reused rather than duplicated, the tool says up front that an implicit layer loads into *every* Vulkan application, and it removes its own on uninstall — but only when the last Vulkan install goes.

## Nothing is overwritten without a backup

Audited by putting known content in every file the tool writes and checking it byte for byte after install and uninstall. Four of six were being replaced with nothing kept — `nvngx_dlssnr.dll`, `renodx-dlss5.addon64`, `ReShade.ini`, `ReShadePreset.ini`, plus `OptiScaler.ini` and `dlss5-bridge.cfg` depending on route. Anyone with a tuned OptiScaler config or their own renodx build would have lost it.

Every write now backs up what was there, and uninstall restores every backup it finds — including one an interrupted install left unrecorded. Verified across all four routes with eight pre-existing files: nothing lost, nothing left behind.

Switching routes also used to strand the previous one: feeder → optiscaler left 22 files, and the new manifest did not list them so uninstall could not clean them either. An install now removes the recorded route first.

## OptiScaler picks the name the game will actually load

OptiScaler is loaded by having it wear the name of a DLL the game already
loads. It went in as `dxgi.dll` every time. All seven names its own setup
supports are now offered — `dxgi`, `winmm`, `version`, `dbghelp`, `d3d12`,
`wininet`, `winhttp` — each with a note on when to reach for it, and on Auto
a name is chosen that is not already taken, so a game shipping its own
`dxgi.dll` (an ENB, a DXVK build) keeps it.

It also finds an OptiScaler **already installed under a different name**, the
way the official setup does — by reading the PE version resource, which still
says `OptiScaler.dll` whatever the file is called. Two copies loading at once
fight each other; the old one is moved aside and put back on uninstall.
Pre-0.9 leftovers (`nvapi64.dll`, `nvngx.dll`, `OptiScaler.asi`) are handled
the same way.

Two bugs turned up while doing this: the OptiScaler route was the only
component not using the cached API fetcher, so it alone broke outright when
GitHub rate-limited; and it looked the release up twice per install, spending
two requests out of an allowance of sixty an hour.

## It writes a log now, and finds more of your games

Someone reported it "doesn't load games for me and crashes a lot". Nothing was
recorded anywhere, so there was nothing to look at.

- **A log file** at `%LOCALAPPDATA%\dlss5-autopilot\autopilot.log` records the
  run and every exception — including ones on worker threads and inside
  tkinter callbacks, which a windowed build otherwise loses completely.
  **[ open log file ]** and **[ report a bug ]** are in the left rail; the bug
  link opens an issue with the version, card, game and route already filled in.
- **Every store failure was silently swallowed.** If Steam could not be read
  you got an empty list and no reason. Now it is logged and shown.
- **EA, Ubisoft, Battle.net and Xbox / Game Pass** are scanned as well as
  Steam, Epic and GOG. On the development machine that took 55 games to 61.
- An empty list says what to do instead of just "0 games".

## Three reasons it "sometimes" broke

All three were invisible, which is why they looked random.

- **A game whose architecture could not be read vanished.** The 32/64 filter
  compared against a value that is empty when the executable is locked at the
  moment of the scan — antivirus, a running updater, a OneDrive placeholder.
  The game silently disappeared from the list. Unknown architecture is now
  shown under every filter, because hiding it is the one outcome you cannot
  act on.
- **One bad folder emptied the whole list.** Inspecting each game reads its
  folder, and any single failure escaped and abandoned the list half-drawn.
  Each game is now inspected on its own and a broken one is listed as
  `unreadable`.
- **The interface could stop talking to its worker threads permanently.** An
  exception while handling a background result skipped the line that
  reschedules the pump, so progress, results and errors all stopped arriving
  and the window looked frozen. It now always reschedules, and says what went
  wrong.

Emulators were searched in too few places — an emulator unpacked straight to
`D:\PCSX2` was missed. Drive roots, `Documents`, `Emulation`, `Roms` and both
Steam library folders are searched now.

## Borderless, and the depth buffer

If the feeder route sets up correctly and then produces nothing, the usual
cause is that ReShade has no depth buffer selected. ReShade matches the depth
buffer against the back buffer, and borderless, Windows display scaling, or an
in-game render scale below 100% can make the two disagree. Diagnosis now says
so, and the README explains where to look and what to change. The native,
bridge and optiscaler routes do not use ReShade's depth buffer at all.

## Check whether your components are still current

A fresh install always fetches the newest of everything, but nothing told you
afterwards. **[ check versions ]** compares what a game has installed against
what the sources offer now. Versions are recorded in the manifest, and read
out of the notes for installs made by earlier releases. A different build
family — `-RTX40` against an `SF` build — is reported as different, not as
outdated, because that is a choice rather than a version behind.

## If your antivirus says something

It may, and the README now explains exactly why.

Windows Defender has **Block at First Sight** on by default. A file it has
never seen — unsigned, downloaded by a handful of people so far — is held
while it asks Microsoft's cloud, and for a brand new binary the cloud answers
with a generic machine-learning guess. Those are the verdicts ending in `!ml`.
Once enough machines have seen the file the verdict flips and it scans clean
from then on, without the file changing. That is why a release can be flagged
on day one and clean on day two, and why the SHA-256 matters more than any
scanner result.

The **components** are a separate and more likely case. `renodx-dlss5.addon64`,
`nvngx_dlssnr.dll` and OptiScaler are unsigned, freshly built, uncommon, and
hook graphics APIs. Defender has called a renodx build `Trojan:Win32/Ulthar.A!ml`
and OptiScaler `Trojan:Win32/Fonzi.A!ml`. That is about those files, and would
happen identically if you installed them by hand.

**This caused a real failure that looked like nothing.** Quarantine takes the
file *after* it is written, so the install reported success and the game then
did nothing. The installer now checks every file it wrote is still on disk, and
when one has gone it names it and says this is almost certainly antivirus, and
that restoring it and excluding the folder is the fix.

## You do not have to trust the .exe

Releases are now built by **GitHub Actions**, on GitHub's runners, from the
source in this repository. The build log is public. Every release carries
`SHA256SUMS.txt` and a signed provenance attestation tying the binary to the
exact commit that produced it:

```
gh attestation verify dlss5-autopilot.exe --repo Kizzuwatnaa/DLSS5-Autopilot
```

The README explains how to check the hash, and why PyInstaller builds trip
antivirus heuristics on VirusTotal — a handful of engines flag *any*
single-file Python build. Windows Defender reports no threat. If you would
rather not run the .exe at all, the tool is plain Python with no third-party
packages: clone it and run `python dlss5_autopilot.py`.

## Anti-cheat is detected

BattlEye, Easy Anti-Cheat, Vanguard, GameGuard, XIGNCODE, Denuvo AC, PunkBuster and others are spotted in the game folder and the game is marked `blocked`. **This is why Arma 3 and Arma Reforger do nothing when set up** — anti-cheat blocks ReShade add-ons. Not a tool bug, and there is no way around it. Do not install into an online game.

## Also

- **Uninstall from the game list** with an "installed only" filter — no walking the wizard
- **DirectX 9** can go through the bridge as well as the feeder; `CaptureMouse` is turned off in `dgVoodoo.conf`, which is what made GTA IV appear to freeze on Home (DLSS was delivering frames fine at 64.9 fps — the ReShade overlay just could not take mouse input), and an existing `D3D9.dll` (DXVK, say) is preserved
- **DirectX 10** detected and routed to the feeder, the only thing that can reach it
- **Pre-flight checks**: refuses to start if the folder is not writable or the game is running, instead of failing half way
- **Diagnosis is route-aware**: no longer reads a stale feeder log for a native install, and reads `host64/dlss5-feed-host.log` on the 32-bit path
- Installs recorded by v1.0–v1.2 are recognised and cleaned up properly

## Still honest about

DX11/DX12 is where this works. DirectX 9, DirectX 10, OpenGL and 32-bit games go through translation or a cross-process helper and often fail at `CreateFeature` — every game's outlook is labelled in the list rather than pretended away. Emulator support (DuckStation, PCSX2, Dolphin, PPSSPP, Xenia) is implemented but untested on real hardware. The Vulkan route is implemented and unit-tested but has not been run against a real Vulkan game.

Requirements: Windows, NVIDIA RTX 20 series or newer. First install pulls about 150 MB; later games install from cache.

This repository and archive contain no NVIDIA binaries and nothing proprietary — MIT, everything fetched at run time from the original sources. Not affiliated with NVIDIA, ReShade, RenoDX or OptiScaler.
