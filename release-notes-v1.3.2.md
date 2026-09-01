## v1.3.2

- **Games that quit the moment ReShade loads** (Metal Gear Solid V: the game
  creates its D3D11 device and exits a second later, no crash, no message,
  add-ons or not) now run through **DXVK**: `dxgi.dll` + `d3d11.dll` become
  a D3D11-to-Vulkan layer, ReShade loads as a Vulkan layer outside the game,
  and the feeder's Vulkan transport builds the DLAA contract. Verified on
  MGS V. Known games get it automatically; any D3D11 game can take the path
  with the new checkbox on the install page or `--dxvk`. DX9 games can opt
  in too (DXVK translates D3D9 as well) as an alternative to dgVoodoo2 -
  experimental; a 32-bit game gets ReShade's 32-bit Vulkan layer registered
  alongside the 64-bit one.
- **Alt-tab warning on that path.** In exclusive fullscreen, leaving the game
  re-creates the swap chain and the DLSS feature with it, and that second
  creation crashes the game on the Vulkan transport. Set the game to
  borderless / windowed first, then enable neural rendering. The in-game
  checklist says so.
- **A second ReShade under another name** (`d3d11.dll` beside `dxgi.dll`)
  stopped games from starting - ReShade aborts the second copy, and MGS V
  never got as far as a swap chain. Installing now moves any stray ReShade
  copy out of the way, and an uninstall without a record finds ReShade under
  every name it can load as (a game's own `d3d11.dll` is left alone).
- **Your ReShade settings travel.** Key bindings, the overlay's tutorial
  state, fps counter and theme are carried over from the last game you set
  up, so a fresh install no longer starts from scratch. Nothing you set for
  a game is overruled.
- **Feeder 0.10.0 pre-releases** ship one zip instead of loose files; the
  pre-release option works again and the checkbox now says what changes
  (DLSS 5 add-on 4.7, alt-tab fixes on the D3D11 path, no settings tab -
  preset and work area are set on the install page).
- The work-area slider is impossible to miss now: amber handle at rest, a
  dark-amber track, bright value.
- Step counter no longer overshoots ("10/9") when DXVK is a step.
