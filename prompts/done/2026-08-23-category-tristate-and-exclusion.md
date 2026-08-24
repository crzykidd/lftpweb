---
name: 2026-08-23-category-tristate-and-exclusion
status: done
created: 2026-08-23
model: sonnet
completed: 2026-08-23
result: >
  Three-state categories (migration 031, `download_client_category.excluded`) persisted and
  round-tripped; the unattributed-clients banner counts only the undecided state
  (`core.clientsync._update_preflight` skips excluded categories before counting). Path exclusion
  shipped as the enforceable primitive (`download_client_excluded_path`) with category exclusion
  resolving into it (`core.disk_review.resolve_category_exclusion_paths`) or failing closed onto
  `unavailable_roots` when it can't (`_resolve_client_exclusions`). `core.disk_review.
  is_authorized_delete_target` seeds §10.2's future containment check, unit-tested now though
  unused until stage 5. Relevance copy (`lib/clientAttribution.ts`) derived from observed
  attribution counts, no client_type branch. All gates green: 2043 backend tests (24 new), 797
  frontend tests (16 new), ruff check/format clean, npm build/lint clean. Spec §8.3/§10.2/§11/§14
  updated; docs/decisions.md and this file's own findings #15/#16 resolution notes recorded.
  Stage 5 considered unblocked on findings #15/#16 specifically, but not yet built -- see the
  final session report for what else stands in the way (real-deployment verification, a real
  browser check on the new UI, stage 4's own outstanding real-box verification).
---

# Task: Three-state categories, exclusion as a safety boundary, and honest per-client copy

Fixes findings **#15** and **#16**. **Read both in full first** — #16 is a latent data-loss path,
not a UX issue, and **stage 5 of the whole feature is gated on this task** (see the spec's §14
staging table and `prompts/startnewsession.md`).

## The deployment shape that forces this

**The user runs two lftpweb instances against one seedbox.** One SABnzbd, one rTorrent, both
serving both instances; each lftpweb has its own *arr pair and its own subset of the download
locations. **Each instance permanently sees work that is not its business** — that is the steady
state, not a misconfiguration.

## Part 1 — categories become three-state (#15)

Today a category is either bound to a queue or not, and "not" is also what a never-configured
category looks like. Two genuinely different states are collapsed:

| State | Banner should warn? |
|---|---|
| **Bound** to a queue | no |
| **Explicitly "not used by this instance"** | **never again** |
| **Undecided** — never looked at | **yes** |

- Persist all three explicitly. "Not used" must be a **saved decision**, not the absence of one.
- **The banner counts only undecided categories.** A client whose every category is bound or
  explicitly excluded is fully configured and the banner must be **silent** — that is the entire
  point. A banner that cannot be resolved stops carrying information, which is the same failure as
  finding #2's silence, with the opposite sign.
- A category appearing *later* arrives **undecided** — correct, and exactly when the user does want
  telling.

## Part 2 — "not used" is a hard exclusion, not a preference (#16)

**This is the load-bearing half. A flag that only silences a banner leaves the delete path exactly
as dangerous while appearing to have addressed it.**

The disk review scan proposes `B − A − C`. The *other* instance's content is protected **only** by
set A — the clients still claim it. **That protection expires silently**: once the other instance
imports a release and SAB drops it from history, the content is claimed by nobody *this* instance
can see (set C knows only this lftpweb's items), and it becomes arithmetically indistinguishable
from debris. Stage 5 would then offer to delete another site's data with a correct-looking reclaim
figure.

So an excluded category must be:

1. **Never scanned** — excluded from `core/disk_review.py`'s walk,
2. **Never proposed** as debris, and
3. **Never inside §10.2's delete containment boundary.**

### The hard part, and the rule for it

**Exclusion is expressed per category, but the scan operates on paths.** For SAB the two relate
(a category maps to a folder under `complete_dir`). **For rTorrent they do not** — its
`content_path` is the seeding directory, unrelated to the completed tree (spec §1.1). So a category
exclusion cannot always be translated into a path exclusion.

> **Where an exclusion cannot be enforced precisely, FAIL CLOSED: do not propose debris in that
> path at all, and say why.**

Failing closed is this project's house style for anything that deletes, and the cost is a scan that
proposes less — never a scan that proposes someone else's data.

Also **support excluding a base path (or sub-path) directly**, since that is the more direct
expression of "this tree belongs to the other instance" and is the only thing that works when
category and path do not relate. Finding #16 leaves the choice open; **implement path exclusion as
the enforceable primitive and category exclusion as the convenience that resolves into it where it
can.**

## Part 3 — the control states its own relevance, from evidence

The user: *"the setting shows in SAB and the ui isn't clear that you don't need it in the current
configuration."*

Do **not** hardcode "usenet clients don't need this, torrent clients do" — that is precisely the
client-name branching §4.4/§5.1 forbids, and the generalisation this feature has now got wrong four
times. **Derive it from observation:** after a poll, how many of this client's transfers were
attributed by path versus needing a category?

- SAB: *"12 of 12 recent downloads matched by folder — no mapping needed unless a category lands
  outside a queue folder."*
- rTorrent: *"0 of 2 matched by folder — this client's downloads are matched by category, so a
  mapping is required."*

Same control, true copy for both, no client-type branch anywhere.

## Tests

- All three states persist and round-trip; "not used" survives a reload.
- **The banner counts only undecided** — a client with everything bound-or-excluded produces no
  banner at all. Assert directly.
- An excluded category's path is **not walked** and **cannot appear** in the debris pile.
- An excluded path is outside the containment boundary — assert a delete target under it is
  refused.
- **Fail-closed:** a category exclusion that cannot be resolved to a path suppresses debris
  proposals for that base path entirely, with a stated reason.
- The relevance copy is computed from observed attribution counts, not from `client_type`. Assert
  no client-name branch exists.
- A newly appearing category arrives undecided and does warn.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate** — explicit timeout of at least 600000 ms on every gate Bash call.
**Run backend gates from the REPO ROOT**; use a subshell `( cd frontend && … )`.

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`
4. `npm run build`, `npm run lint`, `npm test`

## When done

Update frontmatter, `git mv` to `prompts/done/`, record decisions in `docs/decisions.md`, update
spec §8.3/§10.2/§11 **and the §14 staging table's stage-5 gate** (state what now remains before
stage 5 may be built), and append resolutions under findings #15 and #16.
**Do not commit or push.** Report: files, every exit code, both test counts, a proposed one-line
message, **whether you consider stage 5 unblocked and what if anything still stands in its way**,
and what you could not verify without a real browser.
