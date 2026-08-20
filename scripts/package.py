#!/usr/bin/env python3
"""Create the deterministic runtime ZIP and checksums."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
FILES = tuple(
    line.strip()
    for line in (ROOT / "RELEASE_MANIFEST.txt").read_text(encoding="utf-8").splitlines()
    if line.strip()
)
OUTPUT = ROOT / "artifacts" / f"cec-volume-osd-{VERSION}.zip"
TIMESTAMP = (2026, 8, 20, 0, 0, 0)
PLUGIN_ROOT = "CEC Volume OSD"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def build(output: Path) -> None:
    missing = [name for name in FILES if not (ROOT / name).is_file()]
    if missing:
        raise SystemExit(f"release manifest files missing: {missing}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in FILES:
            source = ROOT / name
            info = zipfile.ZipInfo(f"{PLUGIN_ROOT}/{name}", TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def main() -> None:
    first = OUTPUT.with_suffix(".first.zip")
    second = OUTPUT.with_suffix(".second.zip")
    for path in (first, second, OUTPUT):
        path.unlink(missing_ok=True)
    build(first)
    build(second)
    if first.read_bytes() != second.read_bytes():
        raise SystemExit("deterministic package check failed")
    shutil.move(first, OUTPUT)
    second.unlink()
    checksum = f"{digest(OUTPUT)}  {OUTPUT.name}\n"
    (OUTPUT.parent / "SHA256SUMS").write_text(checksum, encoding="utf-8")
    provenance = {
        "artifact": OUTPUT.name,
        "sha256": digest(OUTPUT),
        "version": VERSION,
        "members": list(FILES),
        "source_date": "2026-08-20",
    }
    (OUTPUT.parent / "provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"{provenance['sha256']}  {OUTPUT}")


if __name__ == "__main__":
    main()
