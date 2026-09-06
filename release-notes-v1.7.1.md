## Screen capture without the mirror, a graphics-API override, and the first reports

### Screen capture

The player was part of the screen it was showing, so a screen capture
showed the player showing the capture, all the way down - and every frame
of that went through the encoder again. Windows does not let one program
hide another's window from capture, so the two are kept apart: with one
monitor the left 62 % of the screen is captured and the player is parked
on the right, on top; with two monitors the whole screen is captured and
the player goes to the other one. A window picked from the list is
captured on its own through the window's composited surface, wherever it
is and whatever covers it - so the player can go fullscreen over it (GDI
capture shows hardware-accelerated windows such as browsers and Discord as
black, which is why the window's own surface is read instead). Capture
runs at 30 fps; a window that changes size restarts the encoder at the
new size, and closing the player stops the capture.

### Graphics API override

The executable's import table decides which route a game gets, and it can
lie: R.U.S.E. links D3D11 and renders with Direct3D 9 (#24). The install
page has a **graphics api** dropdown (auto / DirectX 9 / 10 / 11 / 12 /
Vulkan / OpenGL); the choice is remembered per folder and the diagnosis
points at it when ReShade reports a D3D9 device under a DXGI install.

### Reports

- The DLSS 5 add-on dropdown opened on the newest build and handed it to
  the installer as an explicit choice, so the 1.7.0 pin to 4.55 on driver
  616.64+ (and the OpenGL and feeder pins) never applied from the window -
  only from the command line. The dropdown now opens on **auto**; the pins
  apply again (#30).
- An executable with no graphics import at all - the engine loads its
  renderer at run time - was labelled Unknown and sent down the DXGI path.
  Call of Juarez: Gunslinger names d3d9.dll inside the exe; the tool now
  reads the names an exe carries, then the imports of the engine DLLs
  beside it, in the same order the import table is read (DXGI first, a
  lone d3d9.dll last), so the game is 32-bit / DirectX 9 and gets DXVK
  (#31).
- HoYoverse games (Zenless Zone Zero, Genshin Impact, Honkai: Star Rail)
  and EA Javelin games (EA SPORTS FC) are recognised as anti-cheat games.
  Anti-cheat games are no longer shown as blocked: the list marks them,
  the card says what can happen, and INSTALL asks for confirmation
  (#29, #26).
- A timed-out read of a release page or reshade.me is retried instead of
  ending the install on the first attempt (#26).
- The Xbox/Game Pass note no longer claims every Store game has an
  "Enable mods" switch; only games whose publisher allows modding show
  it, and a locked folder cannot be modified by anything (#25).
