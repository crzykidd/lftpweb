#!/usr/bin/env python3
"""Build a demo tree in the dev seedbox's hand-testing dropbox, for screenshots and manual UI work.

`private_data/seedbox-dropbox/` on the host is bind-mounted at `/data/dropbox` on both fake
seedboxes (deliberately *not* over `/data/pickup`, which would shadow the seeded fixture tree
several integration tests assert on). This writes a small, obviously-fake release tree there.

**Names are generic on purpose.** Screenshots end up in a public README; real release names in
them are somebody else's metadata and make the project look like it is for one specific thing.

Covers the four shapes worth photographing:

  1. a loose `.mkv` at the queue root            -- the `pget` path
  2. a directory holding one `.mkv` plus an nfo  -- the simplest `mirror`
  3. a directory of real rar volumes             -- extraction, genuinely extractable
  4. a multi-file "season pack"                  -- per-file speed/ETA inside one mirror

**The rar volumes are real.** They are the same hand-built RAR4 container bytes
`tests/test_postprocess.py` uses, imported rather than copied so they cannot drift. No RAR
*compressor* exists anywhere in this project's toolchain (`unrar` only extracts, and no Alpine
package ships one -- see README's "Known gaps"), so these cannot be regenerated at a realistic
size. They extract to a tiny text file, not video. That is a deliberate trade: an archive that
genuinely extracts photographs better than a fake one that fails, and a fake one is exactly the
mistake that let rar extraction stay broken for nine phases.

Media files are zero-filled, so they cost their nominal size on disk and transfer at full size.
Sizes are kept modest for that reason; to photograph a transfer actually in progress, lower the
bandwidth ceiling at Settings -> Transfer rather than making these bigger.

Idempotent: re-running replaces the tree. Usage, from the repo root:

    uv run python docker/test-seedbox/make_demo_tree.py
    uv run python docker/test-seedbox/make_demo_tree.py --dest /some/other/path
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEST = REPO_ROOT / "private_data" / "seedbox-dropbox"

# `tests/` is not an installed package; add it to the path so the real fixture bytes can be
# imported rather than duplicated here.
sys.path.insert(0, str(REPO_ROOT / "tests"))

MB = 1024 * 1024

NFO = (
    "Generic demo release\n"
    "====================\n\n"
    "Placeholder metadata for lftpweb screenshots. Not a real release.\n"
)


def write_media(path: Path, size_bytes: int) -> None:
    """A zero-filled stand-in. Written in chunks so a large file doesn't materialise in memory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    chunk = b"\0" * MB
    remaining = size_bytes
    with path.open("wb") as fh:
        while remaining > 0:
            n = min(remaining, MB)
            fh.write(chunk[:n])
            remaining -= n


def build(dest: Path) -> None:
    from test_postprocess import _RAR_MULTIVOL_VOL1, _RAR_MULTIVOL_VOL2  # noqa: PLC0415

    base = "Generic.Item"

    # 1. A loose file at the queue root -- the `pget` path, and the one shape the folder prefix
    #    deliberately does not apply to (a loose file is complete the instant it is renamed).
    one = f"{base}.1.S01E01.1080p.WEB-DL.x264-DEMO"
    write_media(dest / f"{one}.mkv", 120 * MB)

    # 2. A directory with a single media file plus a sidecar -- the simplest `mirror`, and a
    #    good shot for the R/L/V/E lifecycle icons once it has been through post-processing.
    two = f"{base}.2.S01E02.1080p.WEB-DL.x264-DEMO"
    write_media(dest / two / f"{two}.mkv", 180 * MB)
    (dest / two / f"{two}.nfo").write_text(NFO)

    # 3. Real rar volumes (old-style `<base>.rar` + `<base>.r00`, the convention
    #    `core/extract.py._rar_volume_number` handles). Genuinely extractable by the image's
    #    `unrar`; see this module's docstring for why they are tiny.
    three = f"{base}.3.S01E03.1080p.WEB-DL.x264-DEMO"
    (dest / three).mkdir(parents=True, exist_ok=True)
    (dest / three / f"{three}.rar").write_bytes(_RAR_MULTIVOL_VOL1)
    (dest / three / f"{three}.r00").write_bytes(_RAR_MULTIVOL_VOL2)
    (dest / three / f"{three}.nfo").write_text(NFO)

    # 4. A multi-file pack -- the shape that shows per-file speed and ETA inside one `mirror`
    #    job, and the one the folder prefix protects (an importer must not see it half-arrived).
    four = f"{base}.4.S01.1080p.WEB-DL.x264-DEMO"
    for episode in range(1, 5):
        name = f"{base}.4.S01E{episode:02d}.1080p.WEB-DL.x264-DEMO.mkv"
        write_media(dest / four / name, 60 * MB)
    (dest / four / f"{four}.nfo").write_text(NFO)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="add to whatever is already there instead of replacing the demo items",
    )
    args = parser.parse_args()

    dest: Path = args.dest
    if not args.keep_existing:
        for existing in sorted(dest.glob("Generic.Item.*")):
            if existing.is_dir():
                shutil.rmtree(existing)
            else:
                existing.unlink()
    dest.mkdir(parents=True, exist_ok=True)

    build(dest)

    total = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
    print(f"demo tree written to {dest} ({total / MB:.0f} MB total)")
    for entry in sorted(dest.iterdir()):
        print(f"  {entry.name}{'/' if entry.is_dir() else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
