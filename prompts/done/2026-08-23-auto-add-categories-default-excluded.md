---
name: 2026-08-23-auto-add-categories-default-excluded
status: done
created: 2026-08-23
model: sonnet
completed: 2026-08-23
result: >
  Every observed category (poll pass or Test) now persists via
  core.clientsync.persist_observed_categories, defaulting to excluded=1. The "undecided" state was
  kept, not removed (verified reachable via a bound category's queue being deleted, ON DELETE SET
  NULL). Chosen "new since you last looked" signal: a plain count on the Clients row
  (categories_acknowledged_at + first_seen_at, migration 032), cleared on opening the instance for
  edit, no button/confirmation. Two extra defects folded in mid-task per the coordinator: the
  unattributed-clients banner is now recomputed at request time against a live exclusion read
  (mirrors core/arrsync.py's own 2026-08-21 eviction-latency fix), and disk_review's fail-closed
  suppression is narrowed from a whole base path to only the genuinely unclaimed remainder (a
  claimed file resolves off its own transfer's category, ClientClaim.category, universal, no path
  arithmetic). All four backend/frontend gates green (2056 backend tests, 802 frontend tests).
  Not verified against the user's real two-instance deployment or in a real browser.
---

# Task: Every observed category is recorded automatically, defaulting to "not used here"

Two defects reported from live use, 2026-08-23. **Read findings #15 and #16 first** — this changes a
decision made in the task that resolved them.

## Defect 1 — a category the poller sees never reaches Settings

> *"The transfer page says it sees dc-tv, but when I go to category it doesn't show it."*

Only **Test-detected** categories are persisted (`_persist_detected_categories`, written on a
successful Test). The **poller** observes categories on every pass and discards that observation.

So a category that appears after setup — which is the normal case, since rTorrent's
`list_categories` is `DERIVED` and can only report labels *currently in use* — shows up on the
Transfers page and is **invisible in Settings**, where the user would act on it. Exactly backwards.

**Fix:** any category observed by *either* route (a Test, or a normal poll pass) is recorded against
the instance. The settings form lists every category lftpweb has ever seen from that client, not
only those seen during a manual Test in the current session.

## Defect 2 — the default state should be "not used here", not "undecided"

> *"By default a category should be not used here until the user overrides the setting."*

**This reverses part of finding #15's design, and the user is right — it is the safer default, not
merely the quieter one.**

Finding #16: the user runs **two lftpweb instances against one seedbox**. The other instance's
categories will keep appearing here forever. Under the current design they arrive **undecided**,
which is a state that *permits scanning and proposing* their content. Under the new default they
arrive **excluded** — never walked, never proposed, never inside the delete containment boundary
(§10.2) — until the user deliberately opts them in.

**Fail-closed for a newly-appeared category is correct**, and it matches this project's house style
for anything that leads to deletion.

### What changes

- A newly recorded category defaults to **`excluded` ("not used here")**.
- The **undecided** state is therefore no longer reachable for new categories. Decide deliberately
  whether it remains meaningful at all — if it does not, remove it rather than leaving a state that
  can never occur; a vestigial state is a trap for the next reader. Record the reasoning.
- **The unattributed banner should now be quiet by default.** Consequence to handle, not ignore: a
  category of the *user's own* that appears later will silently do nothing until they notice it.
  **Do not solve this with a warning banner** — that is what the user is removing. A non-nagging
  signal is right: e.g. the Clients row showing "3 new categories since you last looked", or new
  categories sorted to the top of the list and visually marked. Choose one, keep it calm, and
  record why.

## Non-negotiable

- An excluded category stays a **hard exclusion** — never scanned, never proposed as debris, never
  inside §10.2's containment boundary. That is finding #16's safety property and this task must not
  weaken it while changing the default.
- **Fail-closed behaviour is unchanged**: where a category exclusion cannot be resolved to a
  concrete path, debris proposals for that base path stay suppressed.
- No `client_type` branching anywhere.

## Tests

- A category seen only by a **poll pass** (never by a Test) appears in the settings list.
- A newly recorded category defaults to **excluded**, and is therefore **not walked and not
  proposable** as debris — assert the scan behaviour, not just the stored flag.
- Opting a category in (binding it to a queue) makes its content attributable and scannable again.
- The banner is silent for a client whose categories are all newly-recorded defaults.
- Whatever "new since you last looked" signal you choose, assert it appears and can be cleared.
- Existing three-state round-trip and exclusion tests still pass — if the undecided state is
  removed, update rather than delete their coverage.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate** — explicit timeout of at least 600000 ms on every gate Bash call.
**Run backend gates from the REPO ROOT**; use a subshell `( cd frontend && … )`.

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`
4. `npm run build`, `npm run lint`, `npm test`

## When done

Update frontmatter, `git mv` to `prompts/done/`, record decisions in `docs/decisions.md` (**this
reverses part of the #15 design — record it as a reversal with its cause**, the way §8.2 and §11.1c
record theirs), update spec §8.3 and the §14 stage-5 row if its blockers changed, and append a note
under findings #15/#16.
**Do not commit or push.** Report: files, every exit code, both test counts, a proposed one-line
message, whether you kept or removed the undecided state and why, what "new since you last looked"
signal you chose, and what you could not verify without a real browser.
