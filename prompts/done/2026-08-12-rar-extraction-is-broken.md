---
name: 2026-08-12-rar-extraction-is-broken
status: done
created: 2026-08-12
model: sonnet
completed: 2026-08-12
result: >
  Rar extraction was completely non-functional since phase 5 -- Alpine's 7zz has no RAR codec
  at all. Replaced with unrar built from RARLAB source in a new Dockerfile builder stage
  (statically linked against libstdc++/libgcc), wired into core/extract.py alongside 7zz.
  Verified inside both the built runtime and dev images, not just unit tests. Two hand-built
  real RAR4 fixtures (single-volume and a genuine two-volume old-style split set) close the
  test gap that let this ship undetected through nine phases of green CI; both cross-validated
  against a real desktop 7-Zip's RAR reader before being committed. Licence position (UnRAR
  freeware, redistribution permitted, decompression-only) recorded in NOTICE and
  docs/decisions.md, not treated as a blocker. Not independently verified: encrypted-rar
  password retry (no compressor exists to build a real encrypted fixture) and new-style
  .partNN.rar real-archive extraction (old-style only was fixture-tested, per the prompt's
  "at least one").
---

# Task: rar extraction has never worked — the image has no RAR decoder

Found 2026-08-12 by diagnosing a real production failure. **This is the highest-priority
open item**: rar is the dominant format for the releases this app exists to fetch, and
extraction of it is completely non-functional and always has been.

## The finding, already verified — do not re-litigate it, build on it

The user's production log:

```
event[extract] item=33404: 1 of 1 archive(s) failed:
all.american.s08e06.1080p.web.h264-ggwp.rar: ERROR: ... Cannot open the file as archive
```

- **`unrar t` on the same file, on the host, passes.** The archive is fine.
- **`7zz i` inside the container lists no `Rar` handler at all.** It lists `zip`, `7z`,
  `tar`, `gzip`, `bzip2`, `Lzh`, `Cab`, `Iso`, `SquashFS` and others — no Rar, no Rar5.
  Alpine's `7zip` package (26.01) is built without the RAR codec; distros strip it because
  7-Zip's RAR decoder derives from unRAR source, whose licence they won't ship in main.

So `docker/Dockerfile:91-92`'s comment — *"7zip: 7zz, the sole archive tool
(rar/rar5/zip/7z/tar/gz/bz2/xz)"* — is **false**, and `DESIGN.md` §6's rar and multi-part
rar requirements are unimplementable with the current image. `core/extract.py`'s
`_is_first_rar_volume` / `_RAR_PART_RE` machinery is dead code in practice.

**Why nine phases of green CI missed it:** no test has ever built a real rar. Every rar
fixture in `tests/test_postprocess.py` is fake bytes — `b"volume 1"`,
`b"not real rar bytes, just non-empty"`. They exercise naming and precondition logic and
never hand a genuine rar to the extractor. Today's extraction-gating work extended that
same pattern.

**Package availability, already checked against the live Alpine 3.24 indexes:**

| package | main | community |
|---|---|---|
| `unrar` | no | no |
| `unar` | no | no |
| `p7zip` | no | no |
| `unrar-free` | no | no |
| `libarchive` / `libarchive-tools` | **yes** | — |

## Before you start

- Read `DESIGN.md` §6, and `core/extract.py` in full.
- Read `docker/Dockerfile` — note it has a **builder stage with `build-base`** already, and
  **two** stages that install runtime tools (the `dev` stage around line 64 and the runtime
  stage around line 94). Both need whatever you add.
- Read `NOTICE` — this repo is AGPL-3.0 and already records bundled third-party programs
  (lftp, OpenSSH, 7-Zip, su-exec, tini) as *aggregated, not linked*.
- Read `prompts/open-issues.md`.

## Working tree check

`git status --porcelain`. Two untracked prompt files may exist that are not yours. If
anything you need is dirty, list it and ask.

## What to do

### 1. Pick a decoder and justify it

Leading candidate: **build `unrar` from RARLAB source in the existing builder stage** and
copy the binary forward. It is what most comparable containers do, it is a small
self-contained C++ makefile build, and the builder stage already has `build-base`.

Evaluate `libarchive-tools` (`bsdtar`) as the licence-clean alternative and say why you
rejected it if you do — its RAR support is read-only and historically weak on the
multi-volume sets scene releases actually use, which is precisely this project's case.

**You have network access.** Verify claims by building, not by reasoning.

### 2. The licence question is real and must be surfaced, not buried

UnRAR's licence permits redistributing the binary with attribution but forbids using its
source to create a RAR *compressor*. That is compatible with aggregating it in an image —
and `NOTICE` exists for exactly this — but it is the user's decision to ship it.

- Add a `NOTICE` entry in the same style as the existing ones.
- State the licence position plainly in your report and in `docs/decisions.md`, including
  the alternative (libarchive, weaker but BSD).
- Do **not** treat this as a blocker; implement it, and let the user reverse it if they
  object.

### 3. Wire it into `core/extract.py`

Keep `7zz` for everything it genuinely handles (zip/7z/tar/gz/bz2/xz — verified present).
Route rar to the new decoder. Preserve everything that already works:

- The `_UNPACK_` / `_FAILED_` sibling staging (**do not weaken it** — it is what stops a
  half-extracted release appearing under its final name where an `*arr` imports it).
- First-volume-only handling for multi-part sets, both `.rar` + `.r00`/`.r01` and
  `.partNN.rar`.
- The precondition checks added earlier today (zero-length head, gaps in the volume
  sequence).
- Password support (`extract_passwords`).

### 4. The regression guard is as important as the fix

Two layers, both required:

1. **A runtime capability assertion** — a test that fails if the shipped image cannot
   decode rar. Grep the decoder's own format/capability output; do not assert that a
   package name appears in the Dockerfile, which proves nothing about the built binary.
2. **A real rar fixture, actually extracted.** Nothing in this repo can *create* a rar
   (unrar decompresses only; no compressor exists in any Alpine package). So **hand-craft a
   minimal valid RAR archive as bytes in the test fixture** — a stored, uncompressed
   single small file. The RAR4 container format is simple enough to construct
   deterministically, and a committed ~100-byte fixture with a comment explaining its
   construction is worth far more than another `b"volume 1"`. If you genuinely cannot
   construct one, say so plainly and ship layer 1 alone rather than faking layer 2.

Also: **upgrade at least one existing multi-volume test to use real archives.** The
fake-bytes fixtures are why this went unnoticed for nine phases.

### 5. Correct the documentation that is now known false

- `docker/Dockerfile`'s comment listing rar among 7zz's formats.
- `README.md` wherever extraction formats are claimed.
- `DESIGN.md` §6 — **draft the wording, do not edit the file.** Six proposed wordings are
  already awaiting the user's approval; add yours to `docs/decisions.md` with them.

### 6. Prove it end to end

Build the image and verify **inside the built container** that the decoder is present and
extracts a real rar. A green unit test against a host binary proves nothing about what
ships — that is exactly the gap that produced this bug. Check the **dev stage too**: the
dev image has historically been missing tools the runtime stage had (that cost a whole
debugging session on 2026-08-12; see `prompts/startnewsession.md`).

## Conventions to honor

- `docs/decisions.md`, newest at top, with the licence reasoning and rejected alternatives.
- `CHANGELOG.md` under `### Fixed` — say plainly that rar extraction never worked before.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `uv run pytest` with the fake seedbox up. All three compose files must still validate.
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. Record decisions in `docs/decisions.md`.
4. **Do not commit.** Report back: file list, proposed one-line `fix:` message, test count,
   lint results, which decoder you chose and the licence position, whether you managed a
   real rar fixture, proof you verified inside the built image, and anything not fixed.
   Never `git add -A`, never push.
