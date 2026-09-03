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

## What is DLSS 5

A model that runs over a finished frame and re-lights it - materials, skin,
tone. NVIDIA ships it 3 September 2026 in NBA 2K27, RTX 50 only. The modding
community wired it into other games through ReShade add-ons and an
OptiScaler fork, and re-targeted the runtime so RTX 40/30/20 can run it too.
This tool automates that setup. **Unofficial, early, and the components
change daily.**

---

## Who does what: the eight routes

Every game gets one of these. The tool picks the best fit and says why; the
dropdown shows every route the game allows, marks the recommended one, and
marks the ones your card cannot use. You can always pick another.

| Route | What it is | Where it fits | Performance dial |
|---|---|---|---|
| **native** | Krish's `renodx-dlss5` ReShade add-on hooks the game's own DLSS calls on D3D12. | 64-bit D3D12 games that ship DLSS. The most proven route. | The game's own DLSS quality mode |
| **optiscaler** | Dagherbou's OptiScaler fork replaces the upscaler and runs the model over its output. No ReShade. | 64-bit D3D12 (D3D11 works with FSR underneath), game must already use DLSS. Author tested RTX 50; runs on RTX 20/30/40 with the per-card runtime the tool installs. | **Model resolution 25-100%** - the biggest fps lever there is |
| **renodx-dlss** | ShortFuse's `renodx-dlss` add-on (the "SF" build) hooks D3D9, D3D11 and D3D12 in-process. No bridge, no shaders. | 64-bit D3D9 / D3D11 / D3D12. Days old and **reported not working in many games**; offered last, never recommended except for 64-bit DX9 where nothing else exists. | The game's own DLSS mode where it has one |
| **neural-upstream** | matiasLombo's add-on runs the network at render resolution, *before* the game's own DLSS upscales. No renodx add-on beside it. | 64-bit D3D12 games with DLSS. Beta: days old, two games tested by its author. | Cadence (every frame, or one in two or three); the game's DLSS mode still applies |
| **bridge** | NIGos' `dlss5-bridge` reproduces the DLSS contract on a private D3D12 session. | Vulkan games with DLSS (mirror). D3D11 fallback. Maintained; every release is tested on D3D11 and Vulkan. | The game's own DLSS mode |
| **feeder** | jlrouzies-fr's `DLSS5-Feeder` builds a DLAA contract out of ReShade's depth buffer and shader motion vectors. | Games with **no** DLSS: D3D11, D3D12, Vulkan, OpenGL, and the only route for **32-bit** games (host64 helper) and DX9 (via DXVK). | `work_resolution` 50-100% (64-bit D3D11 only) |
| **standalone-dlssnr** | kibblerz's add-on brings its own feed, DLAA or DLSS Super Resolution, and frame generation, presented through its own window on top. | 64-bit D3D11/D3D12, with or without DLSS. Experimental; turn the game's DLSS, frame generation and anti-aliasing off. | Run the game below native resolution and it upscales |
| **remix** | The game already has an **RTX Remix** mod. DLSS 5 lives inside the Remix runtime, after its own upscaler. No ReShade, no feeder, no add-on. | Any game with a Remix mod installed (a `.trex` folder next to it). Chosen automatically when one is found. | The Remix menu's own Neural Uplift sliders |

**How the recommendation is made:**

- D3D12 with DLSS → **optiscaler** (native one click away). D3D11 with DLSS →
  **bridge** (optiscaler/feeder as alternatives).
- No DLSS → **feeder**; bridge as the alternative. FSR 2/3 or XeSS instead of
  DLSS → **optiscaler** redirects those calls into DLSS, then neural rendering.
- Vulkan → **bridge** (with DLSS) or **feeder** (without). OpenGL, 32-bit,
  32-bit DX9 → **feeder**. 64-bit DX9 → **renodx-dlss**, the only route that
  reaches it.
- **RTX Remix mod present → remix, always.** Every other route is refused:
  ReShade crashes a Remix game before it draws, and on DX9 it would overwrite
  the Remix runtime itself.
- **DirectX 10 is not supported.** The tool says so instead of installing.
- Each route's card on the install page names what must not sit in the same
  folder - two things hooking the same NGX calls is flicker or nothing.

### Support matrix

| Path | Status | How |
|---|---|---|
| 64-bit D3D12 with DLSS | reliable | native / optiscaler / neural-upstream / renodx-dlss |
| 64-bit D3D11 / D3D12 with FSR 2/3 or XeSS, no DLSS | beta | optiscaler (upscaler calls redirected into DLSS) |
| 64-bit D3D11 with DLSS | beta | bridge / optiscaler |
| 64-bit D3D11 / D3D12 without DLSS | reliable | feeder (ReShade + shaders) |
| Vulkan (64-bit) | beta | ReShade as a Vulkan layer + bridge or feeder |
| 64-bit D3D11 that quits when ReShade loads (MGS V) | beta | DXVK (D3D11 → Vulkan) + the Vulkan path above |
| OpenGL | often fails | feeder, ReShade as `opengl32.dll` |
| 32-bit D3D11 / D3D12 | often fails | feeder + `host64\` helper process |
| DirectX 9 (32-bit) | experimental | DXVK `d3d9.dll` → Vulkan → feeder, plus the 32-bit Vulkan layer and the `host64\` helper |
| DirectX 9 (64-bit) | beta | renodx-dlss |
| DirectX 10 | not supported | nothing hooks D3D10 |
| 64-bit D3D11 / D3D12, own feed with upscaling and frame generation | experimental | standalone-dlssnr |
| A game with an RTX Remix mod | beta | remix: DLSS 5 inside the Remix runtime |
| Emulators | reliable* | D3D11/12 backend, set by the install; Vulkan is the beta path |

\* set the emulator's renderer to Direct3D 11/12. Vulkan works through the
layer registration; OpenGL is the least reliable.

---

## Your graphics card, honestly

The runtime (`nvngx_dlssnr.dll`) is compiled per GPU architecture - NVIDIA's
own build is FP8 for RTX 50 only, the community re-targeted it for older
cards. The tool detects your card, picks the right build, then **opens the
downloaded file and checks the CUDA fatbin records** to confirm it really
matches your architecture.

| Card | Build the tool installs | What to expect |
|---|---|---|
| **RTX 50** | `310.8.0` - NVIDIA's original, FP8 | Full speed. The 3 September Game Ready driver ships this same runtime. |
| **RTX 40** | `310.8.0-RTX40` - community, re-targeted to sm_89 | Works. Moderate frame-time cost. |
| **RTX 20 / 30** | `310.8.SF` / `SF-v2` - community, FP16 path | Works. **Heavy**: roughly half your fps at full model resolution. Use the resolution dial. |
| GTX / RTX below 20 | - | Does not run. |

**"I have an RTX 50, can I just swap the DLSS DLL?"** No - a game has to
*ask* for neural rendering, and outside NBA 2K27 none do. That's what the
add-on or OptiScaler is for, on every card; RTX 50 just skips the patched
runtime and its frame-time cost.

The table above isn't hard-coded - it's read from the files, so new builds
work too. The install log names the tier you're on.

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

On the native, renodx-dlss and bridge routes ReShade's overlay says
*"no .fx files found in the effect search paths"*. That is normal: those
routes use no shaders, only add-ons. The add-on tab is what matters.

Two executables in one folder (Medieval II and its Kingdoms expansion, a
game and its launcher) share one install: the tool says so when you pick
either, and uninstalling one removes the files for both.

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
- **Feeder build** (list) - stable, the newest pre-release, or any exact
  release when the newest one breaks a game. Builds before 0.8 pair with
  DLSS 5 add-on 4.55 (the tool pins it); 0.10 and later use 4.7.
- **Profiles** - save the settings you liked under a name and pick it on any
  other game; Quality / Balanced / Performance are built in.
- **What will happen?** - lists what INSTALL would write, back up and clean
  up, and whether anything leaves the game folder, without writing a thing.
- **Before / after** - shows the last two ReShade screenshots side by side
  (toggle with F6, shoot, toggle, shoot) and exports a combined PNG.
- **Did it work?** - reads the game's own logs and says what happened, in
  plain words: not started yet, ReShade's DLL gone, the feed crashed, another
  DLSS hook in the folder, and so on.
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

### Video and YouTube

The feed doesn't care what draws the frame. The **video and youtube** card on
the first page fetches a portable **MPC-HC** into a folder of your choice
(default `Videos\DLSS5 Player`), sets its renderer to D3D11, and installs
DLSS 5 into it like any game - no depth buffer needed for video.

- **File > Open File**, or paste a YouTube link into **link** and press
  **play** (live via yt-dlp, nothing downloaded; a clipboard link is picked
  up automatically).
- **download, then play** saves it under `downloads` first (up to 1440p, 4K
  when ticked) - first run fetches ffmpeg (170 MB) to join YouTube's separate
  video/audio streams.
- **process a file** renders a clip through DLSS 5 offline (native/2x/4K,
  style choice) into `processed`, then opens it.
- **webcam**: pick a camera, press start, plays live through DLSS 5 (~0.5s
  behind). Stop ends it.
- **F6** or the **neural rendering on/off** button toggles it while playing.

Neural rendering redraws the whole window, menus included - use fullscreen,
and expect text to look hand-drawn (the model, not a bug). A Chromium build
works technically but smears the whole browser through the model, so it
isn't offered.

### RTX Remix: path tracing and DLSS 5 together

An **RTX Remix** mod rebuilds an old game with path tracing - whole remasters
made by other people, gigabytes of replaced assets, each with its own
installer. **This tool does not install them and never will**; that's the
mod author's project and their download.

What it does is the last mile. Once the mod is in, its runtime sits in a
`.trex` folder beside the game. Press **rescan**, the game shows up with
**remix** already chosen, and INSTALL does at most three things: puts the
matching `nvngx_dlssnr.dll` into `.trex`; if you tick **swap the Remix
runtime**, replaces it with a DLSS 5 capable build (original backed up -
experimental, can undo a mod's own fixes); writes one line into `rtx.conf`.
Uninstall reverses exactly that, nothing else - the mod's assets and runtime
are never touched.

In game: **Alt+X → Developer Settings Menu → Post-Processing → Enable Neural
Uplift (DLSS-NR)**, with sliders for style, intensity and structure.
**did it work?** reads Remix's own log to confirm the feature was created.

**Which games?** Every project this tool knows about (checked to exist on
2026-09-03) - the same list the **rtx remix** card on the first page shows,
with the ones in your library marked there. Remix only reaches roughly-2000
to 2005 fixed-function DirectX 8/9 games, there's no universal mod, and "a
mod exists" isn't "it runs well". If a page moves, the link is the
authority, not this table.

| Game | Mod | |
|---|---|---|
| **Portal with RTX** | already Remix, official, free for owners of Portal | [page](https://store.steampowered.com/app/2012840/) |
| **Portal: Prelude RTX** | already Remix, official, free | [page](https://store.steampowered.com/app/2410180/) |
| **Half-Life 2 RTX** | already Remix, official demo, free | [page](https://store.steampowered.com/app/2477290/) |
| **Grand Theft Auto IV** | GTAIV RTX Remix Compatibility Mod (xoxor4d) - the one this tool was tested against; its runtime already carries DLSS 5, so only the runtime file is needed | [page](https://github.com/xoxor4d/gta4-rtx) |
| **Need for Speed: Underground 2** | NFSU2-RTX-Remix (Ekozmaster) | [page](https://github.com/Ekozmaster/NFSU2-RTX-Remix) |
| **Garry's Mod** | Garry's Mod RTX Remixed (Xenthio) - needs the game in a fixed-function mode; read its own guide | [page](https://github.com/Xenthio/garrys-mod-rtx-remixed) |
| **Deus Ex** | Deus Ex Echelon Renderer (onnoj) - a renderer that gives the game a fixed-function pipeline first | [page](https://github.com/onnoj/DeusExEchelonRenderer) |
| **Thief Gold** | thief-gold-rtx-remix (Night1099) - NewDark 1.27 | [page](https://github.com/Night1099/thief-gold-rtx-remix) |
| **The Elder Scrolls III: Morrowind** | Morrowind RTX Remix (BrunchyChineapple) - there is a separate set of loose files for OpenMW | [page](https://github.com/BrunchyChineapple/Morrowind-RTX-Remix-source) |
| **Vampire: The Masquerade - Bloodlines** | VTMB RTX Remix (CattoSalad) - a knowledge base rather than a one-click mod | [page](https://github.com/CattoSalad/VTMB-RTX-Remix) |
| **Prince of Persia: The Sands of Time** | pop-sot-rtx (kaminoer) | [page](https://github.com/kaminoer/pop-sot-rtx) |
| **Saints Row 2** | sr2-rtx-remix-proxy (BRAGme) | [page](https://github.com/BRAGme/sr2-rtx-remix-proxy) |
| **Saints Row: The Third** | Saints Row The Third RTX Remix shim (PurrsianMilkman) - the 2011 DirectX 9 release only | [page](https://github.com/PurrsianMilkman/Saints-Row-The-Third-RTX-REMIX-compatibility-mod) |
| **Red Faction** | RedFaction-RTX (BRAGme) - version 1.20 NA | [page](https://github.com/BRAGme/RedFaction-RTX) |
| **Total Overdose** | TotalOverDoseRTXRemix (Utkar5hM) | [page](https://github.com/Utkar5hM/TotalOverDoseRTXRemix) |
| **Assassin's Creed II** | ac2-rtx (Kamzik123) - later than the era Remix is built for; expect rough edges | [page](https://github.com/Kamzik123/ac2-rtx) |
| **Populous: The Beginning** | Populous-3-RTX-Remix (xmarre) - an experiment, in its author's words | [page](https://github.com/xmarre/Populous-3-RTX-Remix) |
| **Silent Storm** | silent-storm-rtx (WormSlayer) | [page](https://github.com/WormSlayer/silent-storm-rtx) |
| **Dungeon Keeper 2** | dk2-dxwrapper with path tracing (mencelot) | [page](https://github.com/mencelot/dk2-dxwrapper-with-path-tracing-support) |
| **Grand Theft Auto: Vice City** | GTA Vice City RTX Remix ASI (GmanRO) | [page](https://github.com/GmanRO/GTA-VICE-CITY-RTX-REMIX-.ASI-compiled-within-linux-) |
| **Cry of Fear** | CryofFear_RTX-REMIX (michaelabilliot) | [page](https://github.com/michaelabilliot/CryofFear_RTX-REMIX) |
| **Chess Titans** | Chess-Titans-RTX (Kamilkampfwagen-II) | [page](https://github.com/Kamilkampfwagen-II/Chess-Titans-RTX) |

Many more live on [ModDB's Remix section](https://www.moddb.com/rtx) and in
the Remix Showcase Discord, including projects with no public repository -
not listed here for that reason, not because they do not exist.

**Two things it will not do**, on purpose: it does not download or mirror
anybody's mod, and it does not put a ReShade DLL in a Remix folder.

### Something crashed, or it does nothing?

Nothing is sent anywhere by itself - no telemetry. Instead:

- **report a bug** (left rail), or the **report it** button after an
  internal error, opens a GitHub issue already filled in: version, card,
  driver, game, route, last diagnosis, last error, log tail. You see it in
  your browser and decide whether to post it - edit it first if you like.
- **suggest a feature** (left rail) opens an issue labelled *enhancement*.

That's where fixes and features come from; every copy offers to restart into
a new release the next time it's opened.

### Command line

```
dlss5-autopilot.exe "D:\Games\Game"            install
dlss5-autopilot.exe "D:\Games\Game" --check    detect only, write nothing
dlss5-autopilot.exe "D:\Games\Game" --remove   uninstall
dlss5-autopilot.exe "D:\Games\Game" --dxvk     run the game on Vulkan through DXVK (see below); --no-dxvk turns the automatic choice off
dlss5-autopilot.exe "D:\Games\Game" --route feeder   pick a route: native, upstream, optiscaler, renodx, bridge, feeder, standalone
dlss5-autopilot.exe --video ["D:\DLSS5 Player"]  set up the video player and feed it
```

---

## What makes it more than a copy script

- **Finds your games.** Steam, Epic, GOG, EA app, Ubisoft Connect,
  Battle.net, Rockstar, Amazon Games, itch, Heroic, Xbox/Game Pass, plain
  `D:\Games\*` folders, and 18 emulators (DuckStation, PCSX2, Dolphin,
  PPSSPP, Xenia, Cemu, RPCS3, Ryujinx, yuzu/suyu/Eden, shadPS4, Azahar/Citra,
  melonDS, Flycast, xemu, Vita3K, RetroArch, mGBA, Snes9x, Play!). Anything
  else: **Choose folder…**.
- **Finds the right executable.** Files go next to the exe that actually
  runs - a subfolder in Unreal/CryEngine games, not the launcher in the
  root - even when the store's manifest names the launcher; you keep
  launching from Steam/Epic as usual.
- **Nothing is overwritten without a backup**, restored on uninstall.
- **Uninstall removes exactly what was installed** - recorded in
  `dlss5-autopilot.json`; a locked file is retried, reported, and kept in
  the record so the next uninstall finishes the job.
- **Switching routes is clean** - the previous route is removed first, so no
  two routes' add-ons ever fight over the same NGX calls.
- **Versions that go together are pinned** - e.g. the feeder's stable
  release needs DLSS 5 add-on ≤4.55 or `CreateFeature` dies; the tool holds
  that pairing and says so in the log.
- **"Did it work?"** reads the components' logs back in plain words after
  you've played - shader loaded, motion vectors alive, DLSS feature created,
  frames actually delivered. The usual failure is silent: nothing changes.
- **Survives GitHub's rate limit** - every API answer is cached on disk and
  reused when a live call fails.

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

### The game closes a second after starting, no crash, no message

Some games watch their own process and quit the moment ReShade hooks
Direct3D. **Metal Gear Solid V** is the known case: with ReShade as
`dxgi.dll` or `d3d11.dll` it creates its D3D11 device and exits cleanly
before the first frame - with or without any add-on. The tool recognises
these games and runs them through **DXVK**: `dxgi.dll` + `d3d11.dll` become
a Vulkan translation layer, ReShade loads as a Vulkan layer outside the
game, and the feeder's Vulkan transport does the rest. Verified on MGS V.
Any D3D11 game can be sent down this path with the checkbox on the install
page or `--dxvk`. **DirectX 9 always takes it**, ticked or not: the feed
needs a D3D11/D3D12 device to build its contract on and ReShade on a raw
D3D9 device cannot give it one. For a 32-bit game the tool registers
ReShade's 32-bit Vulkan layer next to the 64-bit one.

Two things to know on this path:

- **Alt-tab and display-mode changes.** In exclusive fullscreen, leaving the
  game re-creates the swap chain, and with it the DLSS feature - and that
  second creation crashes the game on the Vulkan transport (feeder 0.7.0
  and 0.10.0-beta.2 alike). Set the game to **borderless / windowed**
  before enabling neural rendering, and do not switch modes mid-session.
- DXVK writes `<game>_dxgi.log` and `<game>_d3d11.log` beside the game;
  uninstall removes them.

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

### A game crashes the instant ReShade loads, no window, no message

Capcom's **RE Engine** (the Resident Evil 2/3/4 remakes, RE7, RE8/Village,
Resident Evil Requiem) is documented to reject ReShade's add-on support
outright on several of its titles - worst on the ones that also carry
Denuvo, Requiem in particular. This is the engine's own tamper protection,
not a setup mistake, and it is unrelated to RTX Remix above. The tool
detects it (`re_chunk_000.pak` in the folder) and, on any route, installs
**REFramework** first - a separate, actively maintained mod that loads
before the game's own checks and patches around them, the same fix players
report using on Requiem itself. It fetches the current build from
[praydog/REFramework-nightly](https://github.com/praydog/REFramework-nightly),
which detects the running game itself, so it is not tied to a fixed game
list. Not guaranteed on every title or every game update.

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

Fair question for any .exe from a Discord link. Don't take anyone's word for
it - here's what you can check.

**Every release is built by GitHub, not uploaded by a person.** Pushing a
version tag runs [`release.yml`](.github/workflows/release.yml) on GitHub's
own runner: it builds the .exe from the commit you can read, writes
`SHA256SUMS.txt`, and attaches a signed provenance attestation tying the
file to that exact commit and build log - both on every release page.

```
certutil -hashfile dlss5-autopilot.exe SHA256
gh attestation verify dlss5-autopilot.exe --repo Kizzuwatnaa/DLSS5-Autopilot
```

If the hash doesn't match the release page, the file didn't come from here.
Or skip the .exe and run the source directly (plain Python, standard library
and tkinter only - nothing from PyPI in the build):

```
git clone https://github.com/Kizzuwatnaa/DLSS5-Autopilot
cd DLSS5-Autopilot
python dlss5_autopilot.py
```

**About antivirus warnings.** PyInstaller's stock bootloader is a wrapper
real malware also uses, so heuristics flag *any* PyInstaller build -
`Trojan.Generic`, `Wacatac.C!ml`. Since v1.3.0 the release workflow compiles
a fresh bootloader on the runner instead, which clears most of these.
SmartScreen's *"Windows protected your PC"* is separate - it's about the
missing paid publisher certificate, not the file: **More info → Run anyway**,
once. Defender's **Block at First Sight** can also flag a brand-new release
for a day, unrelated to the file itself; report a false positive at
<https://www.microsoft.com/en-us/wdsi/filesubmission>.

**What it actually does to your machine:**

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
| DXVK | [doitsujin/dxvk](https://github.com/doitsujin/dxvk) | zlib/libpng |
| REFramework, on RE Engine games only | [praydog/REFramework-nightly](https://github.com/praydog/REFramework-nightly) | MIT |
| RTX Remix runtime with DLSS 5, only when you tick the swap option | [lunks/dxvk-remix-plus-dlssnr](https://github.com/lunks/dxvk-remix-plus-dlssnr) | see repository |
| RenoDX DLSS 5 add-ons (Krish, ShortFuse), NVIDIA NGX runtimes | community-distributed | **proprietary, no public licence** |

The DLSS 5 add-ons and the NVIDIA NGX runtimes are closed-source software
with no published licence. **They are not in this repository, not in the
release archive, and not redistributed by this project.** The tool downloads
them from a public community mirror, exactly as a person would by hand. If
you are not comfortable with that, do not use this tool.

**RTX Remix mods are not downloaded, mirrored or installed by this tool.**
Each one is its author's own project with its own terms. The tool links to
the page, and once you have installed it yourself, puts DLSS 5 into the
runtime that is already there.

Nothing here is affiliated with or endorsed by NVIDIA, ReShade, RenoDX,
OptiScaler, RTX Remix or any of the projects above. Use at your own risk.

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
core/dxvk.py          D3D11/D3D9 to Vulkan via DXVK
core/anticheat.py     BattlEye / EAC / Vanguard detection
core/installer.py     install engine, route switching, uninstall
core/components.py    are the installed parts still current?
core/update.py        update check
core/selfupdate.py    download, verify and swap in a new build
core/diagnose.py      reading the logs back into an answer
core/gui.py           interface
```
