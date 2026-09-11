# DLSS 5 Autopilot

Puts DLSS 5 neural rendering into games that never shipped it. Scans your
library, reads each executable, picks the route that fits the game and your
card, downloads every part from its publisher, writes the configuration,
and can take all of it back out. One `.exe`, nothing to install, no admin.

**[Download the latest release](../../releases/latest)** · Windows 10/11 ·
NVIDIA RTX 20 or newer

> This repository holds installer logic only. No game files, no NVIDIA
> binaries, no third-party code is redistributed - everything is fetched at
> run time from the original publishers. [Credits and licensing](#credits-and-licensing).

## What DLSS 5 is

A network that runs over a finished frame and re-lights it: materials,
skin, tone. NVIDIA shipped it on 3 September 2026 in NBA 2K27, for RTX 50.
The community wired it into other games through ReShade add-ons and an
OptiScaler fork, and re-targeted the runtime so RTX 40, 30 and 20 run it
too. This tool automates that setup. Unofficial; the tool resolves the
current version of every part each time it runs.

## Using it

1. Run `dlss5-autopilot.exe` and press **scan games**. It reads Steam, Epic, GOG, EA, Ubisoft,
   Battle.net, Rockstar, Amazon, itch, Heroic, Xbox/Game Pass, `D:\Games\*`
   folders and 19 emulators. Anything else: **choose folder**. With **scan
   library at start** on - the default - later runs open straight on that
   library; **rescan** picks up games installed or removed since the last scan, and
   **full rescan** reads every launcher, game folder and emulator location
   again, as the first scan does.
2. Pick the game. The card shows what was read - executable, 32/64-bit,
   graphics API, whether it ships DLSS - and the route it will take.
3. Press **INSTALL**. The log says what went where. Then start the game and
   press the key the tool named; **did it work?** reads the game's logs
   afterwards and reports what happened.

Uninstall removes exactly what was written, restores anything it replaced,
and nothing else.

## Which route a game gets

<p align="center"><img src="docs/routes.svg" alt="Which route a game gets: an RTX Remix mod means remix; 32-bit means feeder; 64-bit DirectX 9 means renodx-dlss; Vulkan means bridge with DLSS and feeder without; OpenGL and DirectX 10 mean feeder; DirectX 11/12 with DLSS means optiscaler on D3D12 (native and neural-upstream offered too) or bridge on D3D11, and feeder without DLSS" width="900"></p>

The dropdown lists every route the game allows, marks the recommended one
and greys out what your card cannot run. The card under it says, per
route, what it does and what must not sit in the same folder.

The graphics API comes from the executable's import table, with one
exception: a d3d9.dll import is checked against the delay-load table, a
D3D12 Agility SDK in the folder and the executable's own names before the
game is called DirectX 9, because engines keep that import long after they
stop drawing with it. When the table names no graphics DLL at all (the
engine loads its renderer at run time), the tool reads the DLL names inside
the exe, the imports of the DLLs beside it and the names inside the largest
of them, and ranks them as the import table would: a Direct3D name anywhere
outranks OpenGL or Vulkan, because an engine that can drive several
backends names all of them and draws with Direct3D on Windows. A Unity game
(UnityPlayer.dll beside the exe) is Direct3D 11 unless started with
-force-d3d12, -force-vulkan or -force-glcore. An import table can also lie
- R.U.S.E. links D3D11 and renders with Direct3D 9 - so the install page
has a **graphics api** dropdown (auto / DirectX 9 / 10 / 11 / 12 / Vulkan /
OpenGL); the choice is remembered for that folder.

| Route | What it is | For | FPS dial |
|---|---|---|---|
| **native** | Krish's `renodx-dlss5` add-on hooks the DLSS calls the game already makes | 64-bit D3D12 games with DLSS; on an RTX card optiscaler is recommended first, native is one click away | the game's DLSS mode |
| **neural-upstream** | matiasLombo's add-on runs the network at render resolution, *before* the game's DLSS upscales | 64-bit D3D12 games with DLSS | cadence (every 1st/2nd/3rd frame) |
| **optiscaler** | Dagherbou's OptiScaler fork (or y4my4my4m's, or wilsjo2's - which is installed with its neural pass before the upscaler, the placement it exists for; neither of those two has been run here) replaces the upscaler and runs the model over its output; no ReShade | 64-bit D3D11/12 with DLSS, or with FSR 2/3 / XeSS redirected into DLSS | **model resolution 25-100 %** - cost falls with the square; optional **frame generation** (FSR 3.1, any card, D3D12) |
| **bridge** | NIGos' `dlss5-bridge` mirrors the game's DLSS contract onto a private D3D12 session | D3D11 and Vulkan games with DLSS | the game's DLSS mode |
| **feeder** | jlrouzies-fr's `DLSS5-Feeder` builds a DLAA contract from ReShade's depth buffer and shader motion vectors | games with **no** DLSS: D3D10/11/12, Vulkan, OpenGL, 32-bit (host64 helper, DirectX 9 through DXVK) | work area 50-100 % (64-bit D3D11) |
| **standalone-dlssnr** | kibblerz's add-on: own feed, DLAA or DLSS Super Resolution, frame generation, shown through its own window | 64-bit D3D11/12, with or without DLSS; experimental | run the game below native |
| **renodx-dlss** | ShortFuse's add-on hooks D3D9/11/12 in-process; no bridge, no shaders | 64-bit DirectX 9 (nothing else reaches it); reported failing in many other games | the game's DLSS mode |
| **remix** | the game has an **RTX Remix** mod; DLSS 5 runs inside the Remix runtime, after its upscaler. Nothing injected | any game with a `.trex` folder beside it | Remix's Neural Uplift sliders |

**Two things that override the diagram.** A Remix mod present means *remix*,
always - ReShade crashes a Remix game before it draws. And an online game
with anti-cheat (BattlEye, EAC, Vanguard, EA Javelin, HoYoverse, GameGuard,
XIGNCODE3, Denuvo Anti-Cheat, PunkBuster, FACEIT, Ricochet, ACE) is marked
in the list and asks for confirmation before INSTALL: ReShade add-ons and
anti-cheat do not coexist, and a ban is on the person who chooses to
install anyway.

### Frame generation

Two switches, both off by default:

- **Frame generation, any RTX card** (optiscaler route, D3D12). OptiScaler
  ships AMD's FSR 3.1 frame-generation libraries; the tool turns them on
  with the upscaler it already runs as the input. One generated frame per
  rendered one - 2x - on RTX 20 through 50. Turn the game's own frame
  generation off; expect added latency; the HUD is the part that varies
  by game.
- **Multi-frame generation on RTX 40** (ReShade routes, D3D12 and Vulkan).
  dashdogy's [RTX40MFG-Unlock](https://github.com/dashdogy/RTX40MFG-Unlock)
  raises the multiplier of a DLSS Frame Generation the game *already has*
  to 3x/4x, in memory, with the Ada temporal correction; the tool places it
  with [Ultimate ASI Loader](https://github.com/ThirteenAG/Ultimate-ASI-Loader)
  under a proxy name the executable imports. Offered only on an RTX 40 and
  only when `nvngx_dlssg.dll` or `sl.dlss_g.dll` is in the folder. Research
  software: higher multipliers and Vulkan can freeze or crash. The
  multiplier is chosen in ReShade's **DLSS MFG** tab.

The first works in any D3D12 game on the optiscaler route; the second only
raises a multiplier the game already has. NVIDIA's own multi-frame
generation stays an RTX 50 feature.

## Where the files come from

`nvngx_dlss.dll` (super resolution) and `nvngx_dlssg.dll` (frame generation)
are fetched from NVIDIA's own repository, read at its release tag.
`nvngx_dlssd.dll` (ray reconstruction) can be swapped the same way, and only
for a game that already ships one - the dropdown does not appear otherwise,
and not on the optiscaler or remix routes, whose install cannot act on it.
The game's own file is backed up and comes back on uninstall, and the
download is checked for being a 64-bit Windows DLL before anything is
overwritten.

A swap is worth knowing two things about: a launcher that verifies its files
puts its own copy back, and in an online game an anti-cheat can treat a
changed file as tampering. For anything you play online, leave these on
"keep the game's own".

Neural rendering itself is not in NVIDIA's SDK. `nvngx_dlssnr.dll` comes
from the community build that matches your card, which is what the next
section is about.

## Your graphics card

`nvngx_dlssnr.dll` is compiled per architecture. NVIDIA's build is FP8 for
RTX 50; the community re-targeted it for older cards. The tool detects the
card, picks the matching build, then opens the file and checks its CUDA
fatbin records against the card before installing it.

| Card | Build | Cost |
|---|---|---|
| RTX 50 | `310.8.0`, NVIDIA's own | full speed |
| RTX 40 | `310.8.0-RTX40`, community, sm_89 | moderate |
| RTX 20 / 30 | `310.8.SF` / `SF-v2`, community, FP16 | heavy - about half your fps at 100 % model resolution; use the dial |
| GTX, RTX 16 | - | does not run |
| AMD, Intel | - | not from here - see below |

Neural rendering runs inside NVIDIA's `nvngx_dlssnr.dll`, so a Radeon or an
Arc card has no runtime for it to use. Two projects run the network on
Radeon through HIP (RDNA 3 / RDNA 4 with HIP 7, Direct3D 12) and their users
report both working, at a heavy cost. Neither can be installed from here:
the open one keeps the runtime DLL and its weights in a Discord channel
rather than in its releases, and the other is closed source. The tool says
so when it finds an AMD card, and this becomes a route the day either of
them publishes the whole thing.

Swapping the DLL alone does nothing on any card: a game has to *ask* for
neural rendering, and outside NBA 2K27 none do. That request is what the
add-on or OptiScaler makes.

## In the game

The keys below are the defaults. A keyboard without an Insert or a Home key
has no way into the overlay at all, so the **overlay key** setting on the
install page rebinds both ReShade's and OptiScaler's, and every instruction
the tool prints then names the key that was chosen.

| Route | Keys |
|---|---|
| optiscaler | **Insert** opens the overlay; neural rendering is already on |
| native · bridge | **Home** → DLSS 5 tab → neural rendering on (F5 toggles it in add-on 4.6+); keep the game's DLSS on |
| neural-upstream | **Home** → NR Pre-Upscale tab; keep the game's DLSS on; with DLSS Frame Generation set the cadence to Quality |
| feeder | **Home** → tick `LUMENITE: Kernel 2.0` (or VORT) and `DLSS 5 Feed`, provider above the feed → DLSS 5 panel → on. 32-bit games: the panel is a separate helper window beside the game |
| renodx-dlss | **Home** → RenoDX DLSS tab; already on |
| remix | **Alt+X** → Developer Settings → Post-Processing → Enable Neural Uplift |
| standalone-dlssnr | **F10** compares |

Everywhere: MSAA/SSAA off. Set resolution and display mode **before**
turning neural rendering on - the feature is built for one back-buffer
size, and a rebuild mid-session is where crashes live. Prefer borderless
over exclusive fullscreen for the same reason. *"No .fx files found"* in
ReShade's overlay is normal on the add-on routes; the add-on tab is what
matters.

## Settings worth knowing

- **work area** (the slider; on the optiscaler route its ini and log call
  the same dial model resolution): 75 % is about half the cost of 100 %,
  50 % a quarter. The frame keeps full detail; only the model's
  contribution is computed small.
- **Feeder build**: stable, newest pre-release, or any exact release when
  the newest breaks a game. Builds before 0.8 pair with add-on 4.55 and the
  tool pins it. OpenGL games are pinned to 4.60 (4.70 stalls on GL).
- **Motion vectors** (feeder): LumeniteFX Kernel by default; **VORT
  Motion** (optical flow) on OpenGL, where LumeniteFX reads nothing - the
  tool installs whichever is chosen and puts it above the feed.
- **Profiles**: save the dials under a name; Quality / Balanced /
  Performance are built in.
- **scan library at start** (games page): off means no scan at all -
  *rescan* and *choose folder* still work. The scan never walks a whole
  disk (launcher registries, `XboxGames`, folders named Games and the
  like, emulator locations) and skips removable drives. What it finds is
  kept in `%LOCALAPPDATA%\dlss5-autopilot\library.json`, so later launches
  show the list at once; a game that has changed on disk since is read
  again. **rescan** asks the launchers what is installed and reads only the
  games it has not seen; **full rescan** walks everything, emulators
  included.
- **aim for _ fps** (optiscaler, and the feeder's 64-bit D3D11 path - the
  same places the work-area slider applies): put in the frame rate you want
  and the tool works out the work area to reach it, from what the last runs
  of that game actually measured. It says how confident it is and never
  applies anything on its own.
- **Overlay key**: ReShade opens its panel on Home and OptiScaler on
  Insert. A keyboard with neither can bind another key here, once, for
  every game.
- **Ray reconstruction**: a game that ships `nvngx_dlssd.dll` can have it
  replaced with a newer build. It is a swap - the game's own file is backed
  up and comes back on uninstall - and the tool says what that means for a
  launcher that verifies its files and for an online game's anti-cheat.
- **What will happen?** lists what INSTALL would write, back up and remove,
  without writing anything.
- **Before / after** puts the last two ReShade screenshots side by side.
- **Check versions**: games you set up earlier are checked against what
  their publishers offer now; **update (N newer)** appears in the list.
- The tool updates itself: a new release downloads in the background, its
  SHA-256 is checked against the `SHA256SUMS.txt` GitHub published, and the
  top bar offers a one-click restart. `"auto_update": false` in
  `%LOCALAPPDATA%\dlss5-autopilot\settings.json` keeps it manual.

## Video, YouTube, webcam

The feed does not care what draws the frame. The **video and youtube** page
(on the left) fetches a portable MPC-HC into a folder of your choice, sets its renderer
to D3D11 and installs DLSS 5 into it like a game. Open a file, paste a
YouTube link (played live via yt-dlp, or downloaded first), render a clip
through DLSS 5 offline with **process a file**, or point a webcam at it.
**F6** toggles the effect while playing. Neural rendering redraws the whole
window, menus included; expect text to look hand-drawn.

**Anything on your screen.** The **screen** row captures part of the
desktop (Desktop Duplication on the GPU, NVENC, 30 fps) and plays it
through DLSS 5 about half a second behind: a browser playing YouTube or
Twitch, an emulator, a video call, a game with anti-cheat. Two methods:
**screen** - with one monitor the left 62 % of the screen is captured and
the player is parked on the right, on top; with two monitors the whole
screen is captured and the player goes to the other one. **window** - one
window from the list is captured on its own, wherever it is and whatever
covers it, so the player can go fullscreen over it. Nothing is injected
into the source; the delay makes this for watching, not for playing.

## RTX Remix

A Remix mod rebuilds an old DirectX 8/9 game with path tracing. Once one is
installed its runtime sits in a `.trex` folder beside the game; press
**rescan**, the game appears with *remix* chosen, and INSTALL does three
things: the matching `nvngx_dlssnr.dll` into `.trex`, one line in
`rtx.conf`, and - only if you tick **swap the Remix runtime** - a
DLSS 5-capable community runtime in place of one that has no neural pass
(original backed up; experimental, it can undo a mod's own fixes).

The **rtx remix** page on the left names the games in your library that
have a mod. Its button opens a list of every project the tool knows about;
for the two that publish a complete install as a plain zip on their own
releases page - **GTA IV** and **NFS Underground 2** - the list offers
**download & install** when the game is in your library. Nothing is
mirrored; every
other project is a link.

<details>
<summary>Known Remix projects</summary>

| Game | Mod |
|---|---|
| Portal with RTX · Portal: Prelude RTX · Half-Life 2 RTX | official, already Remix |
| Grand Theft Auto IV | [xoxor4d/gta4-rtx](https://github.com/xoxor4d/gta4-rtx) - the one this tool was tested against |
| Need for Speed: Underground 2 | [Ekozmaster/NFSU2-RTX-Remix](https://github.com/Ekozmaster/NFSU2-RTX-Remix) |
| Garry's Mod | [Xenthio/garrys-mod-rtx-remixed](https://github.com/Xenthio/garrys-mod-rtx-remixed) |
| Deus Ex | [onnoj/DeusExEchelonRenderer](https://github.com/onnoj/DeusExEchelonRenderer) |
| Thief Gold | [Night1099/thief-gold-rtx-remix](https://github.com/Night1099/thief-gold-rtx-remix) |
| Morrowind | [BrunchyChineapple/Morrowind-RTX-Remix-source](https://github.com/BrunchyChineapple/Morrowind-RTX-Remix-source) |
| Vampire: Bloodlines | [CattoSalad/VTMB-RTX-Remix](https://github.com/CattoSalad/VTMB-RTX-Remix) |
| Prince of Persia: Sands of Time | [kaminoer/pop-sot-rtx](https://github.com/kaminoer/pop-sot-rtx) |
| Saints Row 2 · Saints Row: The Third | [BRAGme/sr2-rtx-remix-proxy](https://github.com/BRAGme/sr2-rtx-remix-proxy) · [PurrsianMilkman](https://github.com/PurrsianMilkman/Saints-Row-The-Third-RTX-REMIX-compatibility-mod) |
| Red Faction · Total Overdose · Assassin's Creed II | [BRAGme/RedFaction-RTX](https://github.com/BRAGme/RedFaction-RTX) · [Utkar5hM](https://github.com/Utkar5hM/TotalOverDoseRTXRemix) · [Kamzik123/ac2-rtx](https://github.com/Kamzik123/ac2-rtx) |
| Populous: The Beginning · Silent Storm · Dungeon Keeper 2 | [xmarre](https://github.com/xmarre/Populous-3-RTX-Remix) · [WormSlayer](https://github.com/WormSlayer/silent-storm-rtx) · [mencelot](https://github.com/mencelot/dk2-dxwrapper-with-path-tracing-support) |
| GTA: Vice City · Cry of Fear · Chess Titans | [GmanRO](https://github.com/GmanRO/GTA-VICE-CITY-RTX-REMIX-.ASI-compiled-within-linux-) · [michaelabilliot](https://github.com/michaelabilliot/CryofFear_RTX-REMIX) · [Kamilkampfwagen-II](https://github.com/Kamilkampfwagen-II/Chess-Titans-RTX) |

More on [ModDB](https://www.moddb.com/rtx). A mod existing is not the same
as it running well.
</details>

## When it does not work

**did it work?** reads `ReShade.log`, `dlss5-feed.log`, `OptiScaler.log`,
the DXVK and Remix logs, and - when the game left no log at all - Windows'
own Application Error record, and names the cause.

<details>
<summary>The game closes a second after starting, no message</summary>

Some games quit the moment ReShade hooks Direct3D (Metal Gear Solid V is
the known one). The tool runs those through **DXVK**: the game renders on
Vulkan and ReShade loads as a Vulkan layer outside it. Any D3D11 game can
take that path with the checkbox on the install page or `--dxvk`; DirectX 9
always does. Use borderless there - alt-tab in exclusive fullscreen
re-creates the swap chain, and the second feature creation crashes.
</details>

<details>
<summary>The desktop mirror changes, the VR headset does not</summary>

A VR game that runs on OpenXR draws the headset's image through it; the
desktop window is a mirror of it, and that is where a proxy DLL or the
Vulkan layer puts ReShade. The **VR headset (OpenXR)** checkbox on the
install page (or `--vr`) registers ReShade's OpenXR layer as well, which
hooks the image the headset shows. Games on OpenVR/SteamVR are not
reached by it. It is global for the user, like the Vulkan layer, and the
last VR uninstall removes it. This has not been tried with a headset
by the author. It is offered as an experiment; a report of what happens,
either way, is what it needs.
</details>

<details>
<summary>Driver 616.64 or newer: everything loads, nothing changes</summary>

NVIDIA's DLSS 5 launch drivers route the neural feature into the runtime
itself, and the `renodx-dlss5` 4.6/4.7 add-on faults on every evaluate
there (measured by the feeder's author: 4.7 passes 0/300, 4.55 passes
300/300). With the add-on dropdown on *auto* the tool installs 4.55 on
these drivers; a build picked from the list is used as picked. That is a
way round it and not a fix - some games fault on 4.55 too.

For a 64-bit D3D11 or D3D12 game, the **standalone** route is worth trying
before the driver: it runs its own feed and does not load `renodx-dlss5`.
It is experimental; the compatibility list is where the evidence for it
builds up. The bridge
route works around the fault in memory (dlss5-bridge 1.4.9). Rolling the
driver back to 616.56 is the surer test, and 616.56 works with every build.
On these drivers `Failed to find NVSDK_NGX_..._EvaluateFeature` in
ReShade.log does not mean the driver is too old - updating is not the
answer there.
</details>

<details>
<summary>Everything says it works and the picture never changes</summary>

A bordered window makes the swap chain the client area - 1920x1071 instead
of 1920x1080 - and the neural result never lands on screen while every log
reports success. Use borderless or true fullscreen at the display's own
resolution, then press F6. Found on Bayonetta.
</details>

<details>
<summary>"ReShade Vulkan layer is not registered"</summary>

DirectX 9 and Vulkan games reach ReShade as a Vulkan *layer* - a registry
entry, not a file. A 32-bit game needs the 32-bit layer; ReShade's own
installer registers only the 64-bit one. Install again: the tool adds the
missing one and says so in the log.
</details>

<details>
<summary>It worked, then stopped after a display change</summary>

The contract is built on ReShade's depth buffer, matched against the back
buffer. Borderless vs fullscreen, Windows scaling or a render scale below
100 % can leave none selected. ReShade → Add-ons → depth buffer list: one
entry must be selected; if none, turn off *Use aspect ratio heuristics*.
</details>

<details>
<summary>A Capcom RE Engine game crashes the instant ReShade loads</summary>

RE Engine rejects ReShade's add-on support on several titles, worst with
Denuvo (Resident Evil Requiem). The tool detects the engine and installs
**REFramework** first, which loads before the engine's own checks. Not
guaranteed on every title or update.
</details>

<details>
<summary>Antivirus quarantined a file</summary>

`renodx-dlss5.addon64`, `nvngx_dlssnr.dll` and OptiScaler are unsigned,
new, uncommon and hook graphics APIs - everything heuristics look for. The
tool notices the missing file and names it; restore it and exclude the
game folder. A download that dies with `SSL: DECRYPTION_FAILED` is an
antivirus or VPN inside the HTTPS connection; turn that off for the tool.
</details>

<details>
<summary>The tool does not list my game</summary>

Not every launcher is in the registry, and an executable locked at scan
time (antivirus, an updater, OneDrive placeholders) cannot be read.
**open log file** shows what each store returned; **choose folder** always
works. Xbox/Game Pass: some games protect only the executable and let files
beside it be written. For those, choose **architecture** in the game's
details and check **graphics api** there (both are remembered for that
folder). Where Windows refuses writes into the folder itself, only games
whose publisher allows modding have *Enable mods* (or *Manage > Files >
Browse*) in the Xbox app - turn it on and press **rescan**. Without it the
folder cannot be changed, and the Steam version of the game can be set up.
</details>

<details>
<summary>Reading the logs yourself</summary>

| Line | Meaning |
|---|---|
| `feature ready … DLAA` | the contract was established |
| `frame N delivered` | frames are being processed |
| `MV probe … N% non-zero` | should not be 0 % while moving |
| `CreateFeature raised exception 0xC0000005` | add-on / feeder version mismatch, or a runtime that does not match the card |
</details>

Bugs: **report a bug** in the tool asks two questions - did the game start,
and what happened - and then opens a GitHub issue already filled in with
version, card, driver, game, route, the last diagnosis, the log tails and,
when Windows recorded one, the faulting module of the crash. Nothing is sent
by itself - you see it in the browser and decide.

**share the result** does the same for the compatibility list: the game's
name and executable, the route and build, the graphics API, the card and
driver, this tool's version, whether it worked, and the one-line verdict
the diagnosis reached. No paths, no user name, nothing else. Those results
are added up into one file the tool reads before an install; once a game has
five results, the next person with it is told which route worked most often,
and whether the one they picked did worse. The issue is closed as soon as it
is read - it is a record, not a bug report - and still counts.

## Command line

```
dlss5-autopilot.exe "D:\Games\Game"                 install
dlss5-autopilot.exe "D:\Games\Game" --check         detect only, write nothing
dlss5-autopilot.exe "D:\Games\Game" --remove        uninstall
dlss5-autopilot.exe "D:\Games\Game" --route feeder  native, upstream, optiscaler, renodx, bridge, feeder, standalone, remix
dlss5-autopilot.exe "D:\Games\Game" --route remix --remix-swap   replace a Remix runtime that has no neural pass
dlss5-autopilot.exe "D:\Games\Game" --dxvk          run the game on Vulkan through DXVK (--no-dxvk turns the automatic choice off)
dlss5-autopilot.exe "D:\Games\Game" --vr            register ReShade's OpenXR layer as well (VR, OpenXR games; untried with a headset)
dlss5-autopilot.exe "D:\Games\Game" --route optiscaler --opti-build y4my4my4m   another OptiScaler build than Dagherbou's (not run here)
dlss5-autopilot.exe "D:\Games\Game" --route optiscaler --opti-build wilsjo2     ...the neural pass before the upscaler, 1-3 passes (not run here)
dlss5-autopilot.exe --video ["D:\DLSS5 Player"]     set up the video player
```

## Is it safe

- **Every release is built by GitHub, not uploaded by a person.** A version
  tag runs [`release.yml`](.github/workflows/release.yml) on GitHub's
  runner, which builds the exe from the commit you can read, writes
  `SHA256SUMS.txt` and attaches a signed provenance attestation.
  `certutil -hashfile dlss5-autopilot.exe SHA256` against the release page;
  `gh attestation verify dlss5-autopilot.exe --repo Kizzuwatnaa/DLSS5-Autopilot`.
- **Or skip the exe**: plain Python, standard library and tkinter only, no
  PyPI packages. `git clone` and `python dlss5_autopilot.py`.
- It writes only into the game folder you pick (plus, for Vulkan games, one
  per-user registry value it announces first and removes with the last
  Vulkan game), keeps its cache in `%LOCALAPPDATA%\dlss5-autopilot`, never
  asks for administrator rights, and sends nothing anywhere: no telemetry,
  no account.
- **Network access**, and nothing else: `reshade.me`,
  `raw.githubusercontent.com`, `api.github.com`, `github.com`,
  `objects.githubusercontent.com`, `codeload.github.com`. Every URL lives in
  [`core/sources.py`](core/sources.py).
- **Antivirus warnings.** Defender's cloud heuristics (`Wacatac.B!ml`,
  `Ulthar.A!ml` - the `!ml` is a confidence score, not a match) can delete
  a new release in its first hours, before enough PCs have run it;
  the same file is left alone a day later. Every release is submitted to
  Microsoft as a false positive when it is published; if it happens to
  you, Windows Security → Protection history → Restore → Allow, or run
  from source. SmartScreen's *Windows protected your PC* is the missing
  paid certificate, not the file: More info → Run anyway. Report false
  positives: <https://www.microsoft.com/en-us/wdsi/filesubmission>.

## Code signing policy

Free code signing provided by [SignPath.io](https://signpath.io), certificate
by [SignPath Foundation](https://signpath.org). Signing happens inside the
GitHub Actions release workflow, on GitHub-hosted runners; SignPath verifies
that a build came out of that workflow before it signs it, so a signed
`dlss5-autopilot.exe` is exactly what the tagged commit builds.

- **Team roles.** Author, reviewer and approver: [Kizzuwatnaa](https://github.com/Kizzuwatnaa)
  (project owner). Changes proposed by others are reviewed by the owner
  before they are merged; only the owner approves a signing request.
- **Privacy policy.** This program will not transfer any information to
  other networked systems unless specifically requested by the user. It
  downloads the components it installs from the publishers listed under
  [Network access](#is-it-safe) and sends nothing anywhere: no telemetry,
  no account. The components it installs are third-party software with
  their own terms, linked below.

## Credits and licensing

This tool is a downloader and configurator. It bundles nothing; each
component stays under its own licence, fetched from its own publisher.

| Component | Project | Licence |
|---|---|---|
| ReShade, shader headers | [crosire/reshade](https://github.com/crosire/reshade) · [reshade-shaders](https://github.com/crosire/reshade-shaders) | BSD-3-Clause · per file |
| DLSS5-Feeder | [jlrouzies-fr/DLSS5-Feeder](https://github.com/jlrouzies-fr/DLSS5-Feeder) | see repository |
| dlss5-bridge | [NIGos/dlss5-bridge](https://github.com/NIGos/dlss5-bridge) | MIT |
| neural-upstream | [matiasLombo/neural-upstream](https://github.com/matiasLombo/neural-upstream) | MIT |
| standalone-dlssnr | [kibblerz/DLSS5-Reshade-AIO](https://github.com/kibblerz/DLSS5-Reshade-AIO) | Apache-2.0 |
| OptiScaler DLSS-NR fork | [Dagherbou/OptiScaler_DLSSNR](https://github.com/Dagherbou/OptiScaler_DLSSNR) | GPL-3.0 |
| OptiScaler fork, multi-frame generation | [y4my4my4m/OptiScaler_DLSSNR_Multipass_MFG](https://github.com/y4my4my4m/OptiScaler_DLSSNR_Multipass_MFG) | GPL-3.0 |
| OptiScaler fork, neural pass before the upscaler | [wilsjo2/OptiScaler-DLSSNR-PreSR-Multipass](https://github.com/wilsjo2/OptiScaler-DLSSNR-PreSR-Multipass) | GPL-3.0 |
| LumeniteFX · VORT shaders | [umar-afzaal/LumeniteFX](https://github.com/umar-afzaal/LumeniteFX) · [vortigern11/vort_Shaders](https://github.com/vortigern11/vort_Shaders) | AGNYA · MIT |
| DXVK | [doitsujin/dxvk](https://github.com/doitsujin/dxvk) | zlib/libpng |
| REFramework (RE Engine games only) | [praydog/REFramework-nightly](https://github.com/praydog/REFramework-nightly) | MIT |
| Remix runtime with DLSS 5 (swap option only) | [lunks/dxvk-remix-plus-dlssnr](https://github.com/lunks/dxvk-remix-plus-dlssnr) | see repository |
| RTX40MFG-Unlock, Ultimate ASI Loader (multi-frame generation option only) | [dashdogy/RTX40MFG-Unlock](https://github.com/dashdogy/RTX40MFG-Unlock) · [ThirteenAG/Ultimate-ASI-Loader](https://github.com/ThirteenAG/Ultimate-ASI-Loader) | MIT · MIT |
| MPC-HC, yt-dlp, ffmpeg (video only) | their own releases | GPL / Unlicense / GPL |
| RenoDX DLSS 5 add-ons (Krish, ShortFuse) | community mirror [RankFTW/rhi-repo](https://github.com/RankFTW/rhi-repo) | **proprietary, no public licence** |
| NVIDIA DLSS runtimes (super resolution, ray reconstruction, frame generation) | [NVIDIA/DLSS](https://github.com/NVIDIA/DLSS) | NVIDIA DLSS SDK licence |
| NVIDIA NGX neural-rendering runtime | community mirror [RankFTW/rhi-repo](https://github.com/RankFTW/rhi-repo) | **proprietary, no public licence** |

The DLSS 5 add-ons and the neural-rendering runtime are closed-source with
no published licence. They are not in this repository, not in the release
archive, and not redistributed here; the tool downloads them from a public
community mirror exactly as a person would by hand. If you are not
comfortable with that, do not use this tool. The three NVIDIA runtimes are
taken from NVIDIA's own repository under NVIDIA's SDK licence; the mirror's
builds of the same three stay in the list behind them, so a build picked by
hand can still come from the mirror. Remix mods are never mirrored.
Nothing here is affiliated with or endorsed by NVIDIA, ReShade, RenoDX,
OptiScaler, RTX Remix or any project above. The installer's own code is
MIT - see [LICENSE](LICENSE). Rights holders: open an issue and it will be
addressed.

Thanks to [perseval-BLR/dlss5-classic-games](https://github.com/perseval-BLR/dlss5-classic-games)
for the OpenGL findings.

<details>
<summary>Building, tests, layout</summary>

```
build.bat                      needs Python 3.10+ and pyinstaller; the release build is GitHub's
python test_all.py             the whole suite
python test_install.py         end-to-end install + uninstall in temp folders
python test_clean_machine.py   empty cache, no local files
```

```
dlss5_autopilot.py    entry point (GUI + CLI)
core/games.py         library scanning     core/emulators.py   emulator profiles
core/library.py       the library, kept between launches
core/pe.py            PE parsing, API detection, exe ranking
core/gpu.py           card, driver, CUDA architecture, build tiers
core/dlss.py          which route fits the game and the card
core/sources.py       every download URL and version pin
core/net.py           download, cache, extract
core/installer.py     install engine, route switching, uninstall
core/optiscaler.py    the OptiScaler route      core/remix*.py    the Remix route
core/vulkan.py        ReShade as a Vulkan layer  core/dxvk.py      D3D9/D3D11 -> Vulkan
core/refw.py          REFramework               core/anticheat.py anti-cheat markers
core/reshade_ini.py   ReShade.ini and presets    core/feedcfg.py   feeder / bridge cfg
core/diagnose.py      logs -> verdict, bug-report body
core/wincrash.py      Windows' own Application Error record
core/autotune.py      the work area to reach a frame rate, from what it cost
core/community.py     what other people found in this game
core/reportui.py      the two questions a bug report needs answered
core/components.py    are the installed parts still current?
core/update.py / selfupdate.py    update check, verified swap-in
core/video.py         MPC-HC, YouTube, offline processing, webcam
core/gui.py           interface

_tools/upstream_watch.py     what moved upstream, and what they say they fixed
_tools/replay_report.py      a bug report's own logs, through the diagnosis
_tools/gui_scale_check.py    the window measured at other display scalings
_tools/walkthrough.py        the real window, driven through every route
_tools/detect_check.py       what every game in a library detects as
docs/releases/               the notes for every release, named after its tag
```
</details>
