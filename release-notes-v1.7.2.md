## Unity games are not OpenGL games, the window reads at 4K, and the library scan no longer freezes

### Graphics API detection

1.7.1 started reading the DLL names inside an executable when its import
table names no renderer, and took the first name it found. Engines that
can drive several backends name all of them - Unity's player names
d3d11, d3d12, vulkan-1 and opengl32 - so House Party, Cities: Skylines
II and every other Unity game landed on the OpenGL route, where the game
never loads opengl32.dll and ReShade never appears (#46, #47, #48).

The evidence is gathered from the exe's strings, the imports of the
DLLs beside it and the names inside the largest of those DLLs, and
ranked the way the import table is: a Direct3D name anywhere outranks
OpenGL or Vulkan. A Unity game (UnityPlayer.dll beside the exe) is
Direct3D 11 outright. Call of Juarez: Gunslinger, which only names
d3d9.dll, still gets DirectX 9 and DXVK (#31).

Two things for the games this already happened to:

- The diagnosis for an opengl32.dll install that never produced a
  ReShade.log says the game most likely does not draw with OpenGL, and
  points at the **graphics api** dropdown.
- The bug-report body carries the reason behind the detected API
  ("Unity player beside the exe", "named in the exe", ...), so a wrong
  label can be read off the report.

### The window at 4K

Fonts followed the display scale, the pixel sizes did not: rows in the
game list stayed 26 px tall while the text doubled, and the list showed
the upper half of every line (#40). Row heights, the side rail, wrap
widths, column widths and button padding scale with the display.

### Library scan

Scanning stopped at "95/97" and stayed there. Two causes, both from a
contributor's pull request (#32): Epic registers engine plugins as
installed items, and Quixel Bridge's install location is the entire
Unreal Engine tree, which the compatibility check then walked; and the
compatibility checks themselves ran on the window's thread as the last
scan messages arrived. Non-application Epic entries are skipped, the
folder walk has a size and time budget, the checks run on the scan
worker, and the progress line names the game being inspected.

### A second OptiScaler build

The install page has an **optiscaler build** dropdown on the optiscaler
route: Dagherbou's DLSS-NR build, or y4my4my4m's fork of it, which adds
multi-frame generation on RTX 40 (#21). The fork publishes development
builds as .7z archives; Windows' own tar.exe unpacks them, so nothing
extra ships in the exe. The choice is recorded in the folder's manifest
and shown in the install log.

### VR, as an experiment

A VR game's desktop window is a mirror; on OpenXR the headset draws
through the OpenXR swapchain, where neither a proxy DLL nor the Vulkan
layer reaches (#33). Games on OpenVR/SteamVR are not reached by this
either. The install
page has a **VR headset (OpenXR)** checkbox on the ReShade routes (and
`--vr` on the command line) that registers ReShade's own OpenXR layer for
the user, so ReShade and the add-ons load on the headset's swapchain. The
layer is global like the Vulkan one and the last VR uninstall removes it.
Nothing here has been tried with a headset: the checkbox says so, the
install log says so, and a report of what happens is asked for.

### Downloads on a fresh Windows

The first download on a newly installed Windows 11 ended with
"CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate"
(#54, two machines). Windows fills its root-certificate store on demand,
and the exe's own TLS never asks it to; the exe carries the certifi root
bundle and trusts it next to the Windows store. When verification still
fails, the message says what to do instead of showing the ssl.c line.

### Reports

- DLSS5-Reshade-AIO 2.1.0 ships one 64-bit archive laid out like a game
  folder instead of three loose files; the standalone route takes the
  archive and extracts the files it needs (loose-file releases still work).
- reshade.me answers "500 Internal Server Error" to about every other
  request on some days, with the page as the body, and the install ended
  there. The installer link is read from that body when it is there; a
  bare error is retried, then the version comes from the crosire/reshade
  tag on GitHub, then from the newest setup already in the cache.

- Picking a different executable in the list (Conan Exiles: the Shipping
  exe instead of the launcher) did not move the install: the scan's
  record of an earlier install put the launcher back, and the files went
  beside it, where the game never loads them (#56). A picked executable
  keeps its pick and the files go beside it.
- Files the tool's own routes place beside a game (Streamline, FidelityFX,
  XeSS, the NGX wrappers, and whatever the folder's manifest lists) are no
  longer read as evidence of the game's renderer.

- neural-upstream normalises the frame against the game's own exposure
  buffer; in games that do not expose one the picture only darkens (#22,
  #34). The install notes and the diagnosis say what to do: take the
  native route, which runs after the game's own tone mapping.
- The scan took the feeder's own 32-bit helper
  (host64\dlss5-feed-host64.exe) for the game's executable in a folder
  where only our own files were left (a Bayonetta folder after the game
  was gone), so it showed as 64-bit / DirectX 12. Our helper is never a
  candidate.
- DLSS5-Feeder 0.14.0-beta.5 is the release the feeder marks latest, and
  "stable - newest release" picks it; reports of the feed crashing after
  start on beta.4 (#35, #37) should be retried on it.
