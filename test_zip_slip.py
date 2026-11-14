"""An archive we downloaded must not be able to write outside the cache folder.

Run: python test_zip_slip.py
"""
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core import optiscaler


def _make_zip(path: Path, members: dict[str, bytes], link: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as arc:
        for name, data in members.items():
            arc.writestr(name, data)
        if link:
            info = zipfile.ZipInfo(link)
            info.external_attr = (0o120777 << 16)
            arc.writestr(info, "/etc/passwd")
    return path


def _unpack(tmp: Path, members: dict[str, bytes], link: str = "") -> Path:
    archive = _make_zip(tmp / "OptiScaler.zip", members, link)
    return optiscaler._unpacked(archive)


def run() -> None:
    real_cache = optiscaler.net.cache_dir

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        optiscaler.net.cache_dir = lambda: tmp

        # A normal release archive still unpacks.
        out = _unpack(tmp / "good", {"OptiScaler.dll": b"dll", "readme.txt": b"hi"})
        assert (out / "OptiScaler.dll").is_file(), "normal archive did not unpack"

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        optiscaler.net.cache_dir = lambda: tmp

        for label, members, link in (
            ("parent traversal", {"../escaped.txt": b"x"}, ""),
            ("nested traversal", {"a/../../escaped.txt": b"x"}, ""),
            ("absolute path", {"/tmp/escaped.txt": b"x"}, ""),
            ("symlink member", {"OptiScaler.dll": b"dll"}, "link"),
        ):
            try:
                _unpack(tmp / label.replace(" ", "_"), members, link)
            except RuntimeError as exc:
                assert "refusing" in str(exc), exc
            else:
                raise AssertionError(f"{label} was extracted instead of refused")
            print(f"  refused: {label}")

    optiscaler.net.cache_dir = real_cache
    print("zip slip tests PASSED")


if __name__ == "__main__":
    run()
