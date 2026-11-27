---
name: 2026-11-26-d1d2-image-refresh-and-extractors
status: completed
created: 2026-11-26
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-11-26
result: base re-pinned (Alpine 3.24.4, Py 3.13.18) + apk upgrade, certifi 2026.11.20, 7-Zip 26.06 and UnRAR 7.23 built from source; image 0 CRITICAL, 5 HIGH (pip-vendored only)
---

# Task: refresh the base image, bump certifi, and build 7-Zip ≥26.02 and unrar ≥7.23 from source (audit batches D1 + D2)

The 2026-11-26 security audit (`private_data/security-audit-2026-11-26.md` — read §3 F3/F4/F5/F7/
F17, §4 the CVE appendix and "Fresh-base check", and §5 D1/D2) found:

- **Base:** both `python:3.13-alpine@sha256:540c7d91…` lines pin Alpine 3.24.1 / Python 3.13.15;
  the runtime stage never runs `apk upgrade`. The current tag resolves to Alpine 3.24.4 / Python
  3.13.18 (`sha256:9ef1a599f863…` at audit time — re-resolve, don't trust it blindly). Published
  images carry openssh-client `10.3_p1-r0` (CVE-2026-60002 CRITICAL, client-side, reachable from a
  hostile seedbox).
- **certifi 2026.7.22** in `uv.lock` trusts compromised emSign intermediates
  (GHSA-6q48-6c7w-9gw2) → ≥2026.10.18.
- **7-Zip 26.01** (Alpine 3.24 `7zip` package) has CVE-2026-14266, an XZ-decoder heap overflow,
  fixed in **26.02**; Alpine 3.24 has nothing newer, and trivy does not report it. lftpweb feeds it
  archives downloaded from the seedbox, i.e. attacker-influenceable input.
- **unrar** is built from `unrarsrc-7.2.3`, whose banner says **"UNRAR 7.20 beta 3"** — older than
  7.23's fixes (CVE-2026-14191 and the cross-extraction symlink hardening). The version-string ↔
  tarball mapping is not obvious; the audit saw `unrarsrc-7.3.2` as newest on rarlab.com.

## Before you start

- `CLAUDE.md`; `prompts/startnewsession.md` lines 1–650, especially the container traps (venv
  shebang paths across stages, `cap_drop: ALL` + entrypoint, numeric uid, read-only rootfs);
  `DESIGN.md` §6 (post-processing/extraction) and §11 (container); `NOTICE`; the whole of
  `docker/Dockerfile` and `docker/entrypoint.sh`; `.github/workflows/ci.yml` and `publish.yml`
  (the image smoke test).
- Find in `docs/decisions.md` / startnewsession the **2026-08-12 lesson that `7zz` once had no RAR
  codec** — the general rule is: assert in the built image what each binary can actually decode,
  never assume it from a package name.
- **Out of scope:** application code (another agent is changing `core/lftp.py`/`core/queue.py` in
  parallel; batch C2 will change `core/extract.py` after you — don't touch it, but do report
  anything about the new binaries' CLI that C2 needs to know, e.g. whether `-ol-` still exists and
  how `7zz l -slt` / `unrar lta` report symlinks and hardlinks). CI scanning/SBOM/cache changes are
  batch D3, later — don't add them.

## Working tree check

You are running in an isolated git worktree. First copy this prompt from the main checkout
(`/home/manderse/projects/lftpweb/prompts/2026-11-26-d1d2-image-refresh-and-extractors.md`) into
your worktree's `prompts/`. Run `git status --porcelain`; expect a clean tree apart from that copy.

## What to do

1. **D1 — base and deps.**
   - Re-resolve `python:3.13-alpine` to its current digest (`docker buildx imagetools inspect`),
     confirm the Alpine and Python versions inside it, and update **every** `FROM` that pins it.
   - Add `apk upgrade --no-cache` to the runtime stage (and any stage whose packages end up in the
     final image) in a way that doesn't defeat the existing layer order.
   - `uv lock --upgrade-package certifi`; confirm the lock diff touches only certifi (if uv drags
     others, explain each).
2. **D2 — extractors from upstream source.**
   - **7-Zip ≥ 26.02:** add a builder stage that downloads the official 7-Zip source tarball from
     7-zip.org (or its official GitHub release), verifies a **pinned sha256** you obtained and
     cross-checked from the upstream page, builds `7zz` (the `Alone2` bundle, gcc makefile) for
     musl, and copies only the binary into the runtime stage, replacing Alpine's `7zip` package.
     Pick the newest stable release; record why that one. Make sure the build includes the RAR
     codec if lftpweb relies on 7zz for RAR anywhere (check `core/extract.py` for which tool handles
     which format — read, don't edit).
   - **unrar ≥ 7.23 final (not beta):** bump the existing source build to the newest **final**
     release, new pinned sha256. **Verify the banner** of the built binary in the image — the
     current tarball turned out to be a beta, so a file name is not evidence.
   - Licensing: update `NOTICE` for both (7-Zip is LGPL with the unRAR restriction; unrar has its
     own licence — match how NOTICE already records it). If building 7-Zip from source changes the
     licensing obligations (e.g. source offer), say so explicitly in the report rather than
     guessing.
3. **Verify in the built image** (`docker build -f docker/Dockerfile --target runtime -t
   lftpweb:d-local .` — check the actual target name):
   - `7zz i` lists the handlers lftpweb needs (xz, 7z, zip, tar, gz, bz2, and rar if used);
     version ≥ 26.02. `unrar` banner ≥ 7.23 and not a beta.
   - **Real extraction by the new binaries inside the image** of a small fixture of every format
     lftpweb supports, including a **multi-volume rar** and a `.tar.xz`. Generate fixtures in the
     scratchpad or `private_data/` (rar creation needs `rar`, which you may not have — use an
     existing rar fixture in `tests/` if one exists, or say plainly that multi-volume rar was not
     exercised). Run lftpweb's own extraction code inside the container if feasible (mount the repo,
     run `tests/test_extract.py` with the image's Python and binaries — find out whether the image
     has what pytest needs; if not, describe exactly what you ran instead).
   - The existing image smoke test from `ci.yml`, run locally the same way.
   - **Re-scan with trivy** using the exact pinned scanner from the audit report §2
     (`ghcr.io/aquasecurity/trivy@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb`),
     feeding it a saved image tarball exactly as the audit did (no Docker socket mount). Report
     before/after CRITICAL/HIGH counts and every remaining CRITICAL/HIGH with its reachability.
4. **Docs (same commit):** `DESIGN.md` §11 (container: what's built from source and why);
   `docs/decisions.md` (newest at top: why build-from-source over Alpine edge packages — consider
   and record the edge-repo-pin alternative and why it was rejected or chosen; how versions/hashes
   are pinned and how to bump them); `CHANGELOG.md` `## [Unreleased]` → `### Security` entries
   (create the subsection if absent, per the skeleton comment at the file's top). README only if it
   states extractor versions or install requirements that are now wrong.

## Gates (each FOREGROUND, explicit generous timeout — image builds can take many minutes; read each exit code)

From the worktree root: `uv run ruff check`, `uv run ruff format --check`, `uv run pytest` (full,
from the root). **Do not start the fake seedbox** — another agent is using ports 2222/2223 right
now; report the resulting skip count as expected. Plus the image build, smoke test and trivy scan
above.

## When done

1. Update this prompt's frontmatter and move it to `prompts/done/` (or `failed/`) in your worktree.
2. **Do not commit, do not push, do not push any image.** Report back: chosen versions + sha256
   sources, the in-image verification output (versions, `7zz i` handlers, extraction results),
   trivy before/after, gate exit codes, notes for C2 about the new binaries' CLI, the exact file
   list, a proposed message (`fix:` or `chore:` prefix — security fixes are `fix:`; no
   `Co-authored-by:`), and your worktree path/branch.
