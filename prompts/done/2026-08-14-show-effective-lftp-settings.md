---
name: 2026-08-14-show-effective-lftp-settings
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: |
  Added GET /api/settings/transfer/effective-lftp (api/jobs.py), backed by a new
  core/lftp.py.effective_tuning_settings() split out of build_rc_text so the credential-bearing
  rc lines never pass through the code path this endpoint calls -- structural separation, not
  string-filtering. Rendered in Settings -> Transfer as a collapsed-by-default <details> section
  directly above "Extra lftp settings" (EffectiveLftpSettingsSection in TransferTab.tsx).
  Collision detection is a pure frontend function (lib/effectiveLftpSettings.ts), tested in
  effectiveLftpSettings.test.ts. Last-write-wins for a colliding key was verified against a real
  lftp binary (new test in tests/test_lftp_settings_accepted.py) before the UI was allowed to say
  the user's line wins. Credential-absence proven by byte-search tests with a positive control
  (tests/test_effective_lftp_settings.py), same shape as core/backup.py's encryption-secret test.
  Full verification run clean: uv run pytest (1009 passed), ruff check + format --check, npm
  run lint/test/build, docker compose config --quiet on all three files. Not click-tested --
  no browser exists in this environment; density/placement need a human look.
---

# Task: Show the lftp settings lftpweb already applies, next to the "Extra lftp settings" box

Settings → Transfer has a free-text **Extra lftp settings** field, and no indication of what
lftpweb already sets. A user typing into it is guessing: they cannot tell whether they are adding
a setting, duplicating one, or fighting one. Show the effective settings, read-only, beside it.

## The one hard safety requirement

**The generated rc file contains credentials. The response must not.**

`core/lftp.py.build_rc_text` produces a single list that interleaves tuning with two
credential-bearing lines:

- `open -u <user>,<password>` — the seedbox password, in clear text.
- `set sftp:connect-program "…"` — the full ssh invocation, including the key path.

Both must be excluded from anything this endpoint returns. Do **not** filter by string matching on
the rendered output and hope; separate them at the point of construction so a future setting
added to the credential half cannot silently start being published. `core/logsetup.py` already
has a credential redactor — reuse it as a second layer if it fits, but not as the primary
mechanism.

Add a test that asserts a known password and key path do **not** appear anywhere in the response,
using the same shape as the existing "the encryption secret is absent from a backup byte-for-byte"
test (`core/backup.py`'s, phase 7) — proven absent, not assumed absent.

## Generate it, never hand-maintain it

This project's defining failure mode is UI text outliving the behaviour it describes — a
Dockerfile comment claimed rar support for nine phases while extraction was completely broken, and
the Settings page claimed `7zz` handled rar until 2026-08-14. **A hand-written list of "settings
we set" would be that same bug, pre-installed.**

The response must be derived from the same code that builds the real rc and the real argv, so it
cannot drift:

- **rc settings** — from `build_rc_text` (or a refactor of it that yields the tuning half
  separately), with the current `TransferSettings` applied, so the numbers shown are the numbers
  in force.
- **argv flags** — from `build_transfer_command`. These are not rc settings but are equally "what
  we already set", and they are the ones a user is most likely to be surprised by: `pget -c -n N`
  and `mirror -c --parallel=N --use-pget-n=N`, plus `--exclude-glob` when file-exclude patterns
  apply. `-c` (continue/resume) in particular is load-bearing for restart survivability and worth
  showing explicitly.

Where a value comes from a setting the user can change, that should be evident — a reader should
be able to tell "this says 8 because I set pget connections to 8" from "this is always set".

## Before you start

- Read `CLAUDE.md`, `DESIGN.md` §4.5 and §9.3.
- Read `core/lftp.py` end to end — `build_rc_text`, `build_transfer_command`, and the module
  docstring. Several settings there carry hard-won comments explaining *why* they exist (the
  `net:reconnect-interval-base` bare-number quirk, `pget:min-chunk-size` existing because
  `pget -n 4` fanned a 16-byte file across four connections, `pget:save-status` at 1s for the
  progress sampler). Surfacing a short version of that "why" in the UI is worth more than the
  bare setting name — but only where you can state it accurately from the code.
- Read `api/settings.py`'s existing transfer endpoints and `frontend/src/pages/settings/TransferTab.tsx`.

## Working tree check

Run `git status --porcelain` first. `core/queue.py` and the frontend may have uncommitted work in
progress — if a file this plan needs is dirty, list it and ask before editing. This prompt file is
exempt.

## What to do

1. **A read-only endpoint** returning the effective settings, credential-free. Shape is yours to
   choose, but it must be structured data (name/value/why), not a pre-rendered blob, so the
   frontend controls presentation.
2. **Render it in Settings → Transfer**, adjacent to the Extra lftp settings field, visually
   subordinate to it (this is reference, not a second control). Collapsed by default is fine if
   the tab is already dense — you cannot see the page, so prefer the option that cannot make an
   already-crowded tab worse.
3. **Flag collisions if it is cheap.** If a user's extra setting names a key lftpweb already sets,
   saying so is the single most useful thing this feature can do — that is precisely the confusion
   it exists to remove. Whether lftp's last-write-wins actually lets the user override is a
   behavioural claim: **verify it against a real lftp binary** (`tests/test_lftp_settings_accepted.py`
   already feeds generated settings to a real lftp and is the right place) before saying anything
   about it in the UI. If you cannot verify it, show the collision and say nothing about which wins.

## Testing

- The credential-absence test described above — non-negotiable.
- A test that the endpoint's values track `TransferSettings` (change a setting, see it reflected),
  which is what proves it is generated rather than hardcoded.
- Frontend tests for whatever collision detection you add, as pure functions in `lib/`.
- Run `uv run pytest` (fake seedbox likely already running — if so, leave it), `ruff check` **and**
  `ruff format --check`, `npm run lint`, `npm test`, `npm run build`, and
  `docker compose config --quiet` on all three compose files.

## Conventions to honor

- Non-obvious decisions in `docs/decisions.md`, newest at top, with rejected alternatives.
- `CHANGELOG.md` entry.
- Update `docs/concepts.md` or `docs/quick-start.md` only if they describe this field — they are
  the single source the in-app Docs render from.
- **You cannot see the UI** — no browser exists here. Density and placement are exactly what you
  cannot judge; say plainly that this needs a human to look at.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` it to `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`.
4. Prepare ONE commit; **do not commit**. Report the file list and a proposed one-line message
   back to the orchestrating session, which surfaces the `y/n`. Never `git add -A`, never push.
