## v1.3.1 - games that keep DLSS in a subfolder are recognised

Unreal Engine games keep `nvngx_dlss.dll` under
`Engine\Plugins\Runtime\Nvidia\DLSS\...`, Kingdom Come: Deliverance II under
`Bin\Win64Shared`, and the tool only looked next to the executable. Those
games came up as "no DLSS", which hid the native and OptiScaler routes and sent
them to the feeder. The tool now looks where engines actually keep it.

If you installed one of these games with v1.3.0, open it again: the route list
will now offer OptiScaler and native, and installing switches routes cleanly.
