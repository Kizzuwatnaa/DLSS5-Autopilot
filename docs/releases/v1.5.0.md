## Two new ways in, and FSR/XeSS games

- **neural-upstream** (beta): for D3D12 games with DLSS. The network runs
  at render resolution, before the game's own DLSS upscales - the same
  picture for a fraction of the cost, and your DLSS quality mode still
  applies. No renodx add-on beside it. With frame generation set its
  cadence to Quality in the overlay.
- **standalone-dlssnr** (experimental): brings its own feed, DLAA at native
  resolution or DLSS Super Resolution below it, and frame generation.
  Presents through its own window on top. Turn the game's DLSS, frame
  generation and anti-aliasing off first.
- **FSR 2/3 and XeSS games** with no DLSS now get the OptiScaler route:
  their upscaler calls are redirected into DLSS, then neural rendering.

Every route now lists, under its description, what it will not tolerate
in the same folder and what to switch off - before you press INSTALL.

## Only the shader the feed reads

The feeder install used to drop the whole LumeniteFX pack in: eight
effects ReShade compiled at every start for nothing. On a 32-bit game
behind dgVoodoo2 that compile stall was long enough for the game to crash
in its own code (Bayonetta, issue #2). Now only the selected motion-vector
shader and its includes go in; a reinstall removes the rest.

## Video

- **Process a file**: render a clip on disk through DLSS 5 offline, at
  native size, 2x, or 4K (DLSS Super Resolution does the upscale), with a
  style choice, into the player's `processed` folder. Uses video2dlssnr
  and ffmpeg, fetched on first use.
- **Webcam**: pick a camera and it plays live through DLSS 5 in the
  player (ffmpeg reads it, about half a second behind); F6 to compare.
- **Open a video file** and **downloads folder** buttons; the download path
  is shown before a download starts.
- The player's Home key used to jump to the start of the video whenever
  ReShade's overlay was opened; that binding is removed.

## Window

Dark title bar, own icon, opens maximised, crisp text on high-DPI
displays, readable greys, a taller log. The bridge is no longer labelled as
abandoned - it is maintained and tested on every release.
