# Reporting a security problem

Mail **ismeturkk@gmail.com** with "DLSS5-Autopilot" in the subject, or open
a [private advisory](../../security/advisories/new). Please do not put a
working exploit in a public issue.

What is worth reporting, in the order it matters here:

- **Anything that makes the tool write outside a game's own folder**, or
  restore a backup to somewhere other than where it came from. Every install
  is recorded in `dlss5-autopilot.json` beside the game and uninstall reads
  only that.
- **Anything that gets a file past the download checks.** Each component is
  fetched over HTTPS from its publisher's own release; archives are checked
  for being what their extension says before they are opened, and the
  tool's own updates are verified against the `SHA256SUMS.txt` published
  beside the release.
- **A release asset that does not match its published hash.** The hashes in
  every release are produced by the GitHub Actions run that built the
  `.exe`, from the same job, and the run is public: if a download does not
  match, it did not come from here.
- **Anything in the release archive that is not the `.exe`, `README.md`,
  `LICENSE` and `NOTICE`.**

What is not a security problem, and belongs in a normal issue: a game that
crashes with the neural pass on, an antivirus flagging the unsigned `.exe`
(see "Is it safe" in the README), or a component's own bug - those are the
publisher's, and the diagnosis names which one.

Expect a reply within a few days. There is no bounty; credit in the release
notes if you want it.
