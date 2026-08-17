---
name: 2026-08-17-support-bundle-polish
status: done
created: 2026-08-17
model: sonnet
completed: 2026-08-17
result: >
  All four flaws fixed plus README coverage. (1) extract_passwords redacted to a count in the
  bundle's settings assembly only. (2) ARR_LOG_BYTE_BUDGET is now a running total per instance
  (was per-file), files fetched newest-first, TRUNCATED.txt names what didn't fit. (3)
  FETCH-FAILED.txt is instance-level only; a per-file failure now writes
  <filename>.FETCH-ERROR.txt and the fetch continues. (4) bundle/settings.json gained the
  backup settings group. README gained a "Support bundle" section pointing at
  docs/concepts.md#support-bundle, which was updated for all four fixes. Verified: ruff check,
  ruff format --check, full pytest (1278 passed), npm test (438 passed), npm run lint, npm run
  build — all green. docs/decisions.md and CHANGELOG.md [Unreleased] updated same commit.
---

# Task: Support-bundle polish — four flaws from the first real bundle, plus README coverage

The user generated the first real support bundle from the test system (2026-08-17,
`lftpweb-support-0.2.1-20260817T161440Z.zip`) and the orchestrating session reviewed it.
Secrets handling verified clean. Four flaws found, plus a README documentation request.

## Fixes (all in `core/supportbundle.py` / `api/support_bundle.py` unless noted)

1. **Redact `extract_passwords`.** `bundle/settings.json` currently exports
   `postprocess.extract_passwords` verbatim — archive passwords are user secrets. Replace the
   list with a count (`"extract_passwords_count": N`, key removed) in the bundle's settings
   assembly only (the real API is untouched). Extend
   `tests/test_support_bundle_api.py`'s secrets-absence test: seed an extract password and
   assert the string appears nowhere in the bundle.
2. **The ~20 MB *arr log budget must be per-instance, not per-file.** Observed: one Sonarr
   with 53 debug files produced a 54 MB (uncompressed) folder. Track a running total across
   the instance's files; once the budget is exhausted, stop fetching and write a
   `TRUNCATED.txt` naming how many files were skipped and why. Keep a sane per-file cap too
   (a single file may not eat the whole budget — keep using the existing constant per file,
   but the instance-level budget is the binding one). Fetch files newest-first if the *arr's
   listing makes recency discernible (it does — prefer the non-rotated names first, e.g.
   `sonarr.txt`/`sonarr.debug.txt`, then rotations in ascending numeric order) so what's kept
   is the most recent when truncation bites.
3. **Per-file fetch failures must not read as instance-level failure.** Observed: one 404
   (`delete-sonarr-source.log`, a custom-script log the *arr lists but serves from a
   different endpoint) produced `FETCH-FAILED.txt` sitting beside 50+ successfully fetched
   logs. Split the marker: instance-level failure (unreachable, bad/undecryptable key, empty
   listing) keeps `FETCH-FAILED.txt`; an individual file's failure writes
   `<filename>.FETCH-ERROR.txt` beside the others with the error text. Update the fake-*arr
   fixture/tests for both shapes.
4. **Add the backup settings group** to `bundle/settings.json` (it's the one `*Settings`
   group missing; reuse the existing api-module conversion, same as the others).

## README (the user's explicit ask)

Add a short "Support bundle" subsection to `README.md` (near the ops/logs material — follow
the README's existing structure and tone) covering:

- What a bundle **captures**: lftpweb logs (already credential-redacted at write time), a
  build/environment snapshot, a sanitized settings dump, recent audit events and job history,
  and — only when selected — each enabled Sonarr/Radarr instance's own log files.
- What it **never captures**: the seedbox password, SSH keys, *arr API keys (a bundle carries
  only `has_*` booleans), archive extract passwords (count only), the SQLite database, the
  install secret, and host-key pins.
- The caveat that *arr logs are included **as the *arr wrote them**, unredacted — lftpweb
  doesn't rewrite another app's logs — so users should glance at them before sharing
  publicly; and the per-instance size cap.
- One sentence on where the button lives (Settings → Logs) and that generating a bundle is
  recorded in the audit trail.

Mirror the same content briefly wherever the docs section covered the bundle
(`docs/concepts.md` gained a section in the original task — keep them consistent rather than
duplicating; a pointer is fine).

## Working tree check

Run `git status --porcelain` before editing; cross-reference the files this plan touches. If
any have uncommitted changes, list them and ask. Gitignored `private_data/` content: leave
alone. This file is exempt.

## Verify — each gate separately, read each exit code

`uv run ruff check backend tests` · `uv run ruff format --check backend tests` ·
`uv run pytest` (full) · `npm test -- --run` · `npm run lint` · `npm run build`.
(Frontend is likely untouched; re-verify anyway.)

## Docs, same commit

`CHANGELOG.md` `[Unreleased]` (Fixed for 1–3, the README ride along); `docs/decisions.md`
entry for the budget/marker semantics; `prompts/startnewsession.md` next free build-run row.

## When done

1. Update this file's frontmatter (`status`, `completed`, `result`).
2. Move this file into `prompts/done/` (or `failed/`).
3. Hand off ONE commit. **You are a spawned agent: do not commit.** Prepare the tree, then
   report the file list + proposed `fix:` message back to the orchestrating session, which
   surfaces the `y/n`.
