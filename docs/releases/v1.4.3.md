## The feeder's own crash record is read

When the feed goes down after it has already delivered frames, it writes a
`### CRASH RECORDED ###` line with the exception and the step it was in,
plus a dump. "did it work?" used to see the delivered frames and say
"Working". It now reports the crash, points at the dump, and suggests the
two things worth trying first: another feeder build from the list (the
stable 0.7.0 for 32-bit games) and a lower work resolution. Seen on
Bayonetta (32-bit, DX9 through dgVoodoo2), issue #2.
