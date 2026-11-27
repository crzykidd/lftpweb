---
name: 2026-11-26-c2-extraction-hardening
status: completed         # pending | completed | failed
created: 2026-11-26
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-11-26
result: pre-extraction listing refusal (7zz + unrar lta -v), per-archive work dirs, post-check hardening, unrar -ol-; 50 new tests; uncommitted
---

# Task: harden archive extraction against hostile archives (audit F8 + F4 / batch C2)

lftpweb extracts archives downloaded from a seedbox — attacker-influenceable input. The
2026-11-26 audit (`private_data/security-audit-2026-11-26.md`, §3 F4/F8, §5 C2 — read them) found
containment is checked only **after** extraction: `core/extract.py` `find_escaping_path`
(`:434-451`) runs after *all* archives in an item have been written into **one shared staging dir**
(`:540-556`). A write *through* a symlink created by an earlier archive happens before any check,
and hardlink members aren't considered at all (`resolve()` only follows symlinks). unrar is
invoked without `-ol-` (`:330`).

The extractors were just replaced (commit `2f717b7`): **7-Zip 26.06** and **UnRAR 7.23**, both
built from source. What the agent that built them measured about their CLI:

- `7zz` 26.06 has **no `-ol-`**. `7zz x` extracts symlinks as real symlinks (an absolute target
  like `/etc/passwd` is rewritten under the output dir and dangles) and hardlinks as real
  hardlinks (same inode).
- `7zz l -slt` reports links: `Symbolic Link = <target>`, `Hard Link = <path>`, and
  `Mode = lrwxrwxrwx` for symlinks (confirmed on a tar).
- `unrar` 7.23 supports `-ol[a,-]` (`-ol-` = skip symlinks), `-op`, `-ep`, `-ad`. **How `unrar lta`
  reports links was not tested** — you must measure it.

## Before you start

- `CLAUDE.md`; `prompts/startnewsession.md` lines 1–650 — especially the extraction traps
  (`_UNPACK_<name>` *sibling* staging, `_FAILED_<name>` evidence, never write a final-named file
  where an *arr can see it; the 2026-08-12 "7zz had no RAR codec" lesson; the upstream-extracted
  `SKIPPED` verify rule).
- `DESIGN.md` §6 (post-processing); **all** of `backend/lftpweb/core/extract.py`; how
  `core/postprocess.py` calls it and maps its result to item state; `tests/test_extract.py` and
  `tests/test_postprocess*.py`, and the existing rar fixtures they use.
- Host tools for tests: this dev host has Debian's `7z` (tests pass `binary="7z"` /
  `LFTPWEB_7Z_BIN`) and `unrar` at `~/.local/bin/unrar` (record both versions — they are NOT the
  image's). The image with the real binaries is `lftpweb:dev-local` (built from current `dev`);
  rebuild it from your tree if you need to: `docker build -f docker/Dockerfile --target runtime
  -t lftpweb:c2-local .` (foreground, generous timeout).
- **Out of scope:** the lftp/transfer path, auth, the Dockerfile (unless a binary truly needs a
  flag at build time — then stop and report rather than change it).

## Working tree check

You are working **directly in the main checkout** `/home/manderse/projects/lftpweb` on `dev` (not a
worktree — worktrees here start from `main`, which is far behind). Run `git status --porcelain`;
expect only this prompt and the untracked `.claude/worktrees/` (ignore it — do not touch, delete or
run tools against it; pass explicit paths where a tool would otherwise recurse into it). Nothing
else is running in this tree.

## What to do

1. **Measure first.** Build small hostile fixtures (in `tests/fixtures` if that's the suite's
   convention for small generated fixtures, otherwise generate them at test time in `tmp_path`;
   rar needs the `rar` tool, which you probably don't have — check for existing rar fixtures and
   for whether you can make a link-bearing rar any legitimate way; if you can't, say so). For each
   of: zip with `../` member, zip with absolute member, tar with a symlink to `/` (or `..`) plus a
   later member written through it, the same split across **two archives in one item**, tar with a
   hardlink, a tar.xz variant — record what `7zz l -slt` (image) and `7z l -slt` (host) print, and
   for rar what `unrar lta` prints for a symlink/hardlink member if you can produce one.
2. **Pre-extraction listing check.** Before extracting an archive, list it and **refuse** it if any
   member is absolute, contains a `..` component (after normalising separators — check `\` too for
   zip), is a symlink, or is a hardlink. Refuse = the item ends `EXTRACT_FAILED` via the existing
   failure path, with a detail string that names the reason and the offending member (sanitise it
   for logs — it's attacker-controlled text), and nothing is written. Fail **closed**: if the
   listing can't be parsed, or a field you rely on is missing, refuse rather than extract. Record in
   `docs/decisions.md` why refusing link-bearing archives outright (vs. skipping links) is the
   chosen policy — legitimate releases essentially never carry links; say so and say what a user
   hitting it should do.
3. **One fresh directory per archive**, then merge into the item's staging dir only after that
   archive's own post-check passes — so no archive can see another's output during extraction.
   Keep the existing `_UNPACK_` sibling / `_FAILED_` evidence / final-merge-on-full-success
   semantics exactly; multi-volume rar sets are **one** archive (one head volume), not one per
   volume.
4. **Extractor flags as defence in depth:** `-ol-` for unrar (verify against 7.23 that it really
   skips symlinks). For 7zz, find whether any switch prevents link creation on extraction in 26.06
   (`-snl`/`-snh` are the *store* side — check the extract side in its help); use it if one exists,
   otherwise record that the pre-check is the only guard for 7zz.
5. **Post-check hardening:** keep `find_escaping_path`, and also fail on any symlink at all and any
   regular file with `st_nlink > 1` in the per-archive output dir.
6. **Tests first, watch them fail against the current code (record it), then fix.** Every hostile
   fixture above ends `EXTRACT_FAILED` with nothing written outside the per-archive dir, and
   nothing at all merged into staging/final. And the non-hostile matrix keeps working: zip, 7z,
   tar, tar.gz, tar.bz2, tar.xz, single gz/xz/bz2, single rar, multi-volume old-style rar (the
   existing fixtures), passworded archives if the suite covers them.
7. **Run the extraction and postprocess tests inside the image** with its real 7zz 26.06 / unrar
   7.23, the way the D agent did (its account: install pytest into a temp dir, run
   `tests/test_postprocess.py` / `test_postprocess_e2e.py` / `test_extract.py` with the image's
   Python and binaries, repo mounted read-only). Report exactly what ran and the counts.
8. **Docs (same commit):** `DESIGN.md` §6; `docs/decisions.md` (newest at top);
   `CHANGELOG.md` — add to the **real** `## [Unreleased]` section's `### Security` (there is a
   commented-out skeleton at the top of the file — **do not** put entries in that comment; an
   earlier patch landed there by mistake). Append after the existing Security bullets. README only
   if it describes extraction behaviour that changed (a release that used to extract and now
   refuses is user-visible — check README's post-processing section).

## Gates (each FOREGROUND, explicit generous timeout, read each exit code)

From the repo root: `uv run ruff check backend tests`, `uv run ruff format --check backend tests`,
`uv run pytest` (full, ~5 min). The fake seedbox is currently up (`docker-compose.test.yml`); check
with `docker ps` and if it's down bring it up so the run has **0 skips** — report the skip count.
Plus the in-image run from step 7. Frontend gates only if you touched frontend (you shouldn't need
to).

## When done

1. Update this prompt's frontmatter, `git mv` is not possible (untracked) — plain `mv` it to
   `prompts/done/` (or `failed/`).
2. **Do not commit, do not push.** Report back: measured listing output per fixture, the policy,
   proof tests failed before and pass after, host and in-image results with counts, gate exit
   codes + skip count, the exact file list, and a proposed message (`fix:` prefix, no
   `Co-authored-by:`).
