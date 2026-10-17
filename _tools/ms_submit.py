r"""After a release: everything the owner needs to send the new exe to
Microsoft as a false positive, opened in front of him.

    python _tools\ms_submit.py              the version in core/update.py
    python _tools\ms_submit.py 2.0.8        that release
    python _tools\ms_submit.py --no-open    write the page, do not open Edge

It takes the release's own zip from GitHub, checks it against the release's
SHA256SUMS.txt, puts the exe in `microsoft-submit\`, scans it with Defender,
writes `MICROSOFT-SUBMIT-<version>.html` from the previous page with this
release's hashes, commit and run, and opens it in Edge beside Microsoft's
submission form.

This was a paragraph in the release-gate skill, done by hand each time, and
on 2.0.8 the page was written and never opened (the owner, 2026-10-03). A
step that depends on being remembered is a step that gets skipped.

LIMITS: the page is the previous page with five values replaced - a new
kind of sentence has to be written into it by hand. It exits 1 when any
value could not be found, rather than open a page that names the old build.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
from core import net  # noqa: E402

DESK = SRC.parent
REPO = "Kizzuwatnaa/DLSS5-Autopilot"
API = f"https://api.github.com/repos/{REPO}"
FORM = "https://www.microsoft.com/en-us/wdsi/filesubmission"
EXE = "dlss5-autopilot.exe"
MPCMD = Path(r"C:\Program Files\Windows Defender\MpCmdRun.exe")


def _ver(p: Path) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", p.stem.split("SUBMIT-")[1]))


def previous_page(version: str) -> Path:
    pages = [p for p in DESK.glob("MICROSOFT-SUBMIT-*.html") if p.stem.split("SUBMIT-")[1] != version]
    if not pages:
        raise SystemExit("no earlier MICROSOFT-SUBMIT-*.html to start from")
    return max(pages, key=_ver)


def release(version: str) -> dict:
    rel = net.json_get(f"{API}/releases/tags/v{version}")
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    zips = [n for n in assets if n.endswith(".zip")]
    if len(zips) != 1 or "SHA256SUMS.txt" not in assets:
        raise SystemExit(f"v{version}: expected one zip and SHA256SUMS.txt, the release has {sorted(assets)}")
    sums = {}
    for line in net.fetch_text(assets["SHA256SUMS.txt"]).decode("utf8", "replace").splitlines():
        m = re.match(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
        if m:
            sums[Path(m.group(2).strip()).name] = m.group(1).lower()
    return {"zip": zips[0], "url": assets[zips[0]], "sums": sums}


def fetch_exe(version: str, rel: dict) -> tuple[Path, str, str]:
    """The exe out of the release's own zip; both hashes, checked."""
    import urllib.request
    out = DESK / "microsoft-submit" / f"dlss5-autopilot-v{version}.exe"
    with tempfile.TemporaryDirectory() as td:
        z = Path(td) / rel["zip"]
        with urllib.request.urlopen(rel["url"], timeout=120, context=net.ssl_context()) as r:
            z.write_bytes(r.read())
        zip_sha = net.sha256(z)
        if rel["sums"].get(rel["zip"]) != zip_sha:
            raise SystemExit(f"the zip's SHA256 {zip_sha} is not the one in SHA256SUMS.txt")
        with zipfile.ZipFile(z) as zf:
            names = [n for n in zf.namelist() if n.replace("\\", "/").split("/")[-1] == EXE]
            if len(names) != 1:
                raise SystemExit(f"expected one {EXE} in the zip, found {names}")
            data = zf.read(names[0])
    if not out.is_file() or out.read_bytes() != data:
        out.parent.mkdir(exist_ok=True)
        out.write_bytes(data)
    exe_sha = net.sha256(out)
    listed = rel["sums"].get(EXE)
    if listed and listed != exe_sha:
        raise SystemExit(f"the exe's SHA256 {exe_sha} is not the one in SHA256SUMS.txt")
    return out, exe_sha, zip_sha


def commit_and_run(version: str) -> tuple[str, str]:
    sha = subprocess.run(["git", "rev-list", "-n", "1", f"v{version}"], capture_output=True,
                         text=True, cwd=SRC).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SystemExit(f"no tag v{version} in this repository (git fetch --tags?)")
    runs = net.json_get(f"{API}/actions/runs?head_sha={sha}&per_page=50").get("workflow_runs", [])
    rel = [r for r in runs if r.get("name") == "release" and r.get("conclusion") == "success"]
    if not rel:
        raise SystemExit(f"no green 'release' run for {sha[:7]}: {[(r.get('name'), r.get('conclusion')) for r in runs]}")
    return sha, rel[0]["html_url"]


def scan(exe: Path) -> str:
    """'clean', or what Defender said."""
    if not MPCMD.is_file():
        return "not scanned (MpCmdRun.exe is not on this PC)"
    p = subprocess.run([str(MPCMD), "-Scan", "-ScanType", "3", "-File", str(exe), "-DisableRemediation"],
                       capture_output=True, text=True)
    if p.returncode == 0 and "no threats" in p.stdout.lower():
        return "clean"
    found = re.findall(r"Threat\s*:\s*(\S+)", p.stdout)
    return "DETECTED: " + (", ".join(found) or p.stdout.strip()[-200:] or f"exit {p.returncode}")


def page(version: str, exe_sha: str, zip_sha: str, sha: str, run: str, scanned: str) -> Path:
    old = previous_page(version)
    text = old.read_text(encoding="utf8")
    old_ver = ".".join(map(str, _ver(old)))
    was = {"exe": re.search(r"SHA256 \(exe\): ([0-9a-f]{64})", text),
           "zip": re.search(r"SHA256 \(release zip\): ([0-9a-f]{64})", text),
           "commit": re.search(r"commit ([0-9a-f]{40})", text),
           "run": re.search(r"https://github\.com/[^\s<\"]+/actions/runs/\d+", text)}
    missing = [k for k, m in was.items() if not m]
    if missing or old_ver not in text:
        raise SystemExit(f"{old.name} has no {missing or 'version'} to replace - write the new page by hand")
    for m, new in ((was["exe"].group(1), exe_sha), (was["zip"].group(1), zip_sha),
                   (was["commit"].group(1), sha), (was["run"].group(0), run), (old_ver, version)):
        text = text.replace(m, new)
    if scanned != "clean":
        # the page's own sentence says this PC scans it clean; do not let it stand
        text = text.replace("<h1>", f"<p style=\"background:#a00;color:#fff;padding:8px\">Bu PC'de tarama: "
                                    f"{scanned} - sayfadaki \"temiz cikiyor\" cumlelerini duzelt.</p>\n<h1>", 1)
    out = DESK / f"MICROSOFT-SUBMIT-{version}.html"
    out.write_text(text, encoding="utf8")
    left = [v for v in (was["exe"].group(1), was["zip"].group(1), was["commit"].group(1)) if v in text]
    if left or len(re.findall(r"Additional|SHA256 \(exe\)", text)) < 2:
        raise SystemExit(f"{out.name} still carries the previous build's values: {left}")
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        version = args[0].lstrip("v")
    else:
        from core import update
        version = update.VERSION
    rel = release(version)
    exe, exe_sha, zip_sha = fetch_exe(version, rel)
    sha, run = commit_and_run(version)
    said = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=120).stdout.strip()
    if version not in said:
        raise SystemExit(f"the released exe says {said!r}, not {version}")
    scanned = scan(exe)
    out = page(version, exe_sha, zip_sha, sha, run, scanned)
    print(f"v{version}  exe {exe_sha}\n          zip {zip_sha}\n          commit {sha[:7]}  {run}")
    print(f"          Defender here: {scanned}")
    print(f"          {exe}\n          {out}")
    if "--no-open" not in sys.argv:
        # Edge, not Chrome (the owner, 2026-09-30); the form first, the page beside it
        subprocess.run(["cmd", "/c", "start", "", "msedge", FORM, out.as_uri()])
        print("          opened in Edge: the form and the page")
    return 0


if __name__ == "__main__":
    sys.exit(main())
