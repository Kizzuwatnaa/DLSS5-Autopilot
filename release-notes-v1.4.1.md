## A drive letter that is not ready no longer kills the scan

A card reader with no card, a locked BitLocker volume or a drive that went
away makes Windows answer "wrong parameter" instead of "not a folder", and
one such letter took the whole Xbox, plain-folder and emulator scan down with
it (issue #2). Those scans now skip it.

Everything from 1.4.0 (video and YouTube, profiles, install preview,
before/after, emulator backends, the honest diagnosis) is unchanged.
