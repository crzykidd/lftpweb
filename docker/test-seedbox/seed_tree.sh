#!/bin/sh
# Builds a known-size tree at $1 for the fake seedbox (DESIGN.md §14). Shared verbatim by
# both Dockerfile.gnu (Debian/GNU find) and Dockerfile.busybox (Alpine/busybox find), so the
# two containers are seeded identically — the phase 2 build's assertions about exact bytes
# hold against either scan path. POSIX sh only: busybox's ash doesn't have bash-isms.
#
# Sizes are deliberately hardcoded in bytes via `dd` (content doesn't matter, only size does)
# so the phase 2 verification report can state exact expected totals rather than "roughly."
set -eu

root="$1"
mkdir -p "$root"

# --- nested release directory: the main size/rollup assertion --------------------------
mkdir -p "$root/Some.Release.S01E01.720p.WEB/Subs"
dd if=/dev/zero of="$root/Some.Release.S01E01.720p.WEB/Some.Release.S01E01.720p.WEB.mkv" bs=1024 count=5120 status=none   # 5,242,880 bytes
dd if=/dev/zero of="$root/Some.Release.S01E01.720p.WEB/Some.Release.S01E01.720p.WEB.nfo" bs=1 count=1024 status=none     # 1,024 bytes
dd if=/dev/zero of="$root/Some.Release.S01E01.720p.WEB/Subs/eng.srt" bs=1 count=2048 status=none                         # 2,048 bytes
# directory total: 5,242,880 + 1,024 + 2,048 = 5,245,952 bytes

# --- a second, larger-ish release: exercises the big-file case -------------------------
mkdir -p "$root/Movie.Title.2024.2160p"
dd if=/dev/zero of="$root/Movie.Title.2024.2160p/Movie.Title.2024.2160p.mkv" bs=1024 count=20480 status=none  # 20,971,520 bytes (~20 MB, "large-ish")
: > "$root/Movie.Title.2024.2160p/empty.placeholder"                                                          # 0 bytes — the zero-byte-file case
# directory total: 20,971,520 bytes

# --- loose top-level file (an "item" in its own right per §4.7) ------------------------
dd if=/dev/zero of="$root/loose-notes.txt" bs=1 count=512 status=none  # 512 bytes

# --- filename edge cases: spaces, non-ASCII -------------------------------------------
dd if=/dev/zero of="$root/file with spaces.txt" bs=1 count=256 status=none  # 256 bytes
dd if=/dev/zero of="$root/日本語ファイル.txt" bs=1 count=128 status=none      # 128 bytes, non-ASCII name

# --- a deep path -------------------------------------------------------------------------
mkdir -p "$root/deep/a/b/c/d"
dd if=/dev/zero of="$root/deep/a/b/c/d/deepest-file.bin" bs=1 count=4096 status=none  # 4,096 bytes

# Grand total across the whole tree:
#   5,245,952 + 20,971,520 + 512 + 256 + 128 + 4,096 = 26,222,464 bytes
