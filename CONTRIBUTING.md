# Contributing

## The most useful thing you can send

A **result**. In the tool, after you have played: **did it work?**, then
**share the result**. It carries the game, the route, the card, the driver
and whether it worked - no paths, no user name. Once a game has five, the
next person with it is told which route worked most often before they
install. That list is the only real evidence this project has.

A **bug report** from **report a bug** in the tool is the second: it fills
in the version, the card, the driver, the route, the diagnosis and the log
tails, which is what makes a report answerable in one round instead of
four.

## Running it from source

```
python dlss5_autopilot.py        needs Python 3.10+ (3.12 is what ships)
python test_all.py               the whole suite, a few minutes
python test_install.py           install and uninstall in temp folders
build.bat                        a local .exe; the release build is GitHub's
```

## If you send a pull request

- **Small and about one thing.** The first outside PR (#157) was one
  distinction - a protected executable is not an unwritable folder - and it
  went in.
- **A check with it.** The suite is `test_all.py`; add the case your change
  is about, built from a real log or a real folder rather than from a clean
  file made up for it. A rule that has never seen a real input is a guess.
- **Run the checks that guard what you touched.** `_tools/verdict_check.py`
  after anything in `core/diagnose.py` - it replays every report anybody has
  sent and prints the ones whose answer moved. `_tools/detect_check.py`
  after anything in `core/pe.py` or `core/games.py`.
- **The checks can run themselves.** `_tools/hooks/` holds four small
  scripts - refuse a shell heredoc that the tool's escaping would mangle,
  run the check that guards a file after editing it, check the tree at the
  end of a turn, and print where the project stands at the start of a
  session. They are wired through a `.claude/settings.json` that lives
  outside this repository (it carries absolute paths), so they are opt-in:
  point your own at them if you want them.
- **Match the file you are editing.** Comments here say why a line exists,
  usually with the issue number that caused it; that is what stops the same
  thing being undone six months later.

## What this project will not take

- A component bundled into the repository or the release archive. Everything
  is fetched at run time from its own publisher - see [NOTICE](NOTICE).
- Anything that writes outside the game's folder without recording it in
  that install's manifest, or that uninstall cannot take back out.
- A rule that decides something about a game from one weak signal. The API
  detection is full of scars from exactly that.
