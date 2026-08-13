---
name: 2026-08-13-postprocess-inherit-or-override
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: >
  Migration 015 makes the four path_queue post-processing columns nullable (NULL = inherit);
  core/postprocess.py._effective replaces the AND. Mid-task the user dropped the
  behaviour-preserving migration requirement (pre-release, one install, test environment) --
  migration 015 just sets every existing row to NULL. Found and fixed a latent cascade-delete
  bug in db.py.migrate()'s table-rebuild handling (PRAGMA foreign_keys now off for the whole
  pending-migrations batch) that migration 008 also depended on and had wrongly documented as
  safe. update_queue now merges (model_fields_set) only the four toggle fields; every other
  field stays a full replace. Frontend: InheritableToggle component in QueuesTab.tsx replaces
  PostprocessStepReadout. 798 tests passing (+4 net), both ruff gates clean, npm lint/build
  clean. Not committed/pushed -- prepared for the orchestrating session to review and commit.
---

# Task: Per-queue post-processing toggles become "inherit global, or explicitly override"

The four post-processing toggles exist at two levels and are **ANDed**
(`core/postprocess.py.process_item`):

| Site-wide | Per-queue |
|---|---|
| `verify_enabled` | `auto_verify` |
| `extract_enabled` | `auto_extract` |
| `move_enabled` | `auto_move` |
| `delete_archives_after_extract` | `auto_delete_archives` |

The user hit the consequence twice on 2026-08-13: a per-queue toggle reads **on** while the
feature is globally off and nothing happens. `0781352` added a "System setting: off" readout to
explain it; this task removes the need for the explanation.

## The decision, made by the user

> if we have a global setting, on each setting for a queue we need to have an "override global"
> and set it locally option. so by default the queue UI shows the global setting unchangeable,
> and if you click override then we store the local setting changes. This way it is obvious
> what is global and what isn't. Global is a convenience setting and most of the time it would
> be the same for all queues, but you might have a specific workflow that you need to tweak for
> 1 queue.

**What is missing today is the ability to say "inherit."** `auto_verify` and friends are
`INTEGER NOT NULL DEFAULT 0` — they can only say on or off, never "whatever the site says". The
AND was standing in for inheritance and doing it badly.

**New rule:** `effective = queue_value if queue_value is not None else site_value`. No AND.

## Migration 015 — and it preserves behaviour exactly

Verify nothing has claimed 015 (014 is `host.ssh_key_enc`).

Make the four `path_queue` columns **nullable**, where `NULL` means inherit. Then set each
existing row from its current `(site, queue)` pair:

| site | queue | effective today | set to | why |
|---|---|---|---|---|
| 1 | 0 | 0 | **explicit `0`** | the only case where the queue genuinely says "not me" |
| 1 | 1 | 1 | `NULL` | inherit gives 1 |
| 0 | 1 | 0 | `NULL` | inherit gives 0 — and matches the intent if the global is later turned on |
| 0 | 0 | 0 | `NULL` | inherit gives 0 |

**This is exactly behaviour-preserving, now and under any future change to the global.** Check
`site=0, queue=1`: today `0 AND 1 = 0`; inherit gives 0. Turn the global on later — old model
`1 AND 1 = 1`, new model inherits 1. Identical. And `site=1, queue=0` stays off under both
models however the global moves afterwards.

The migration must read the **site settings JSON out of the `setting` table** to compute this
per row. Handle the case where no postprocess settings row exists yet (every field takes its
dataclass default — all four are `False`). Test the migration against each of the four
combinations.

## The exception that stays

`sync_mode == 'move'` **forces verification on regardless of either level**, because it is the
sole gate on an irreversible remote delete (`verify_effective` ORs it in). That does not change.
Make sure the UI does not claim verification is off for a `move` queue — the readout added in
`0781352` already gets this right; do not regress it.

## UI

Settings → Queues, per toggle:

- **Default state: shows the global value, and is not editable.** Label it as inherited, and
  make the source obvious ("Global: on").
- **An explicit "Override global" control** unlocks the field and stores a local value.
- **Clearing the override returns to inherit**, and should show what it will revert to before
  you commit — reverting to an invisible value is the same discoverability problem in reverse.
- The existing "System setting: off — this queue's toggle has no effect" readout becomes
  obsolete. Remove it rather than leaving a message describing a rule that no longer exists.

## The API subtlety worth getting right

`NULL` and "field not sent" are **different** now: the first means inherit, the second means
leave unchanged. Pydantic can tell them apart via `model_fields_set` — the same mechanism
`0781352` used to fix the silent-reset bug on the postprocess endpoint. Get this right or
saving a queue form will quietly clear overrides, which is precisely the class of bug that has
already bitten this project twice today.

Check whether the queue create/update endpoint replaces or merges, and make it deliberate.

## Before you start

- `core/postprocess.py.process_item` — `verify_effective`/`extract_effective`/`move_effective`
  and the archive-cleanup gate.
- `backend/lftpweb/migrations/` — `001` (`auto_verify`/`auto_extract`), `003` (`auto_move`),
  `012` (`auto_delete_archives`).
- `api/settings.py` queue create/update and the postprocess settings endpoints.
- `frontend/src/pages/settings/QueuesTab.tsx` — the existing toggles and the site readout it
  fetches.
- `prompts/open-issues.md` § "Post-processing settings" for the options considered and why this
  one was chosen.

## Working tree check

`git status --porcelain`. If files you need are dirty, list them and ask.

## Tests

- **The migration, per combination** — all four `(site, queue)` pairs land where the table above
  says, and the *effective* result is unchanged before and after.
- Effective resolution: inherit follows the global as it changes; an explicit override does not.
- `move`-mode verification still forced on regardless of both levels.
- Saving a queue **without** sending a toggle field does not clear an existing override.
- Sending an explicit `null` **does** clear it back to inherit.
- All four toggles, since they are separate columns and it is easy to wire three correctly and
  miss one.

## Conventions to honor

- `docs/decisions.md`, newest at top — record that the AND was standing in for inheritance,
  the behaviour-preserving migration table, and the options rejected (status quo + readout;
  site-as-creation-default; per-queue only).
- `CHANGELOG.md` under `### Changed` — this changes how existing settings behave, even though
  no install's *effective* behaviour changes on upgrade. Say both things.
- `DESIGN.md` §6/§7.3 (standing approval to edit directly).
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`; `uv run pytest` with the fake seedbox up (794 pass today).
- **You cannot see the UI.** No browser here — you cannot judge whether the override control
  reads clearly. Say so plainly.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line message, how you handled
   `null`-vs-absent in the API, what the override control looks like in markup terms, test
   count, lint results, and anything not fixed. Never `git add -A`, never push.
