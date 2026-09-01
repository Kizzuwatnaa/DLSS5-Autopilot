## v1.3.1

- **Games that keep DLSS in a subfolder** (Unreal's `Engine\Plugins`,
  CryEngine's `Bin\Win64Shared`) are recognised, so the native and OptiScaler
  routes are offered for them.
- **Launcher stubs**: when a store names the `.exe` in the root but the game
  runs from `...\Binaries\Win64`, the files go beside the real one. Start
  the game from the store as usual.
- **"No .fx files found"**: ReShade says this on the native, renodx-dlss and
  bridge routes because they use no shaders. The tool now says so up front
  instead of leaving you to wonder.
- **Two executables in one folder** (Medieval II + Kingdoms, game + launcher)
  share one install; the tool tells you, and that uninstalling one removes
  the files for both.
- **renodx-dlss demoted.** ShortFuse's new add-on is reported not working in
  many games, so it is no longer recommended anywhere but 64-bit DX9, and it
  sits last in the list. D3D11 games with DLSS go to the bridge, D3D12 games
  with DLSS to OptiScaler, games without DLSS to the feeder.
- The work-area slider is readable when it is disabled, with the reason
  beside it.
- Plain notice when no DLSS files were found, and where to report it.
