---
name: 2026-08-23-category-binding-redesign
status: done
created: 2026-08-23
model: sonnet
completed: 2026-08-23
result: >
  Added `Operation.LIST_CATEGORIES` to the closed operation vocabulary (spec §2.1, §5) and
  implemented it on both connectors (SABnzbd via `mode=get_config&section=categories`, rTorrent
  via in-use `d.custom1` values, both doc-derived/UNVERIFIED, §13.4/§13.6 new rows). Replaced the
  free-text category control in `ClientsTab.tsx` with one row per client-reported category bound
  to a queue dropdown (suggested bindings pre-selected, never placeholder text); path-arithmetic
  inference is retained only as a labelled fallback. Two decisions recorded in
  `docs/decisions.md`: uncategorised items get no bindable pseudo-row; newly-appeared categories
  are surfaced by re-testing while editing, not a background poll. All gates green: backend 1983
  passed, `ruff check`/`ruff format --check` clean; frontend build clean, 724 tests passed, lint
  clean (pre-existing warnings only).
---

# Task: Category binding — show the client's real categories, bind each to a queue

Fixes findings **#10, #11a, #11b, #11c** in `prompts/test-findings-2026-08-23.md`. **Read all four
before starting** — they contain the live evidence and the user's own design.

## Why the current control is wrong

- **It proposes nothing** for a real setup. Inference matches queue `remote_path`s against
  *configured base paths* and proposes the trailing segment. SAB had no base paths → zero
  proposals. rTorrent's base path is its seeding dir, unrelated to any queue path → zero proposals.
- **It cannot ever work for rTorrent.** Categories there are labels in `d.custom1`, with no
  relationship to any directory. Path arithmetic cannot recover them at any configuration.
- **It silently discards what the user typed.** The category `<input>` has `placeholder="ar-tv"` —
  greyed text that reads as a filled-in recommendation but is not a value. The save then filters
  `c.category.trim() !== ''`, so the row is dropped and the save "succeeds" storing nothing. The
  user hit exactly this.
- **It is unexplained.** The user could not tell what the field was for. That is the most important
  control on the page — every other symptom in the findings file flows from it being empty.

## The design (the user's own, 2026-08-23)

> *"First, we know the categories. So we should show them all on setup and suggest the bindings,
> and if we aren't using that category the person leaves it unbound."*

One row per category the client actually reports. Each row is a **queue dropdown**, defaulting to
**unbound**. No free-text category field at all.

**This kills the defect class rather than the defect**: with no free-text input there is no blank
row, so there is nothing to silently drop. #11b becomes structurally impossible.

## What to build

### 1. `list_categories` joins the operation vocabulary (spec §2.1)

A **vocabulary change** — treat it with the care §2.1 asks for, and update the spec's operation
table and both baseline profiles in §5. Categories are currently only observable as a *field on
transfers*, so a client with an empty queue and empty history reports none — precisely the
fresh-setup case this design exists for.

- **SABnzbd** — from `mode=get_config`. **Doc-derived and UNVERIFIED**: mark it, and add it to
  §13.4's correction list as a new numbered row.
- **rTorrent** — the labels already returned by the `d.multicall2` the poller issues (`d.custom1`),
  deduplicated. Note this only ever yields labels *in use*; add it to §13.6's list.
- A connector that cannot answer declares `Support.NONE` and the UI falls back (see 3 below).

The conformance suite will require both connectors to declare the new key — that is the point.

### 2. Surface them

Return the client's categories from the probe/test path so the settings page can render them
without a second round trip. Follow the existing detected-base-paths shape — this is the same
"detect, propose, confirm" flow, and it should look like it.

### 3. The UI

- One row per reported category: the category name (**text, not an input**) and a queue dropdown
  defaulting to **"— not used —"**.
- **Suggest a binding** where a queue's name or the trailing segment of its `remote_path` matches
  the category. Suggestions are *pre-selected dropdown values*, never placeholder text.
- **Explain the control in one line** where it lives (#11a). Something close to: *"SABnzbd sorts
  downloads into categories. Tell lftpweb which queue each category belongs to — leave the ones
  you don't use unbound."*
- **Fallback when the client reports no categories** (fresh SAB, unlabelled rTorrent): keep the
  existing path-arithmetic proposal, but **say which mechanism produced the suggestion**. Do not
  blur "the client told us" with "we guessed from your paths."
- Remove the old free-text add-a-category row and the "Infer mappings from base paths + queues"
  button it served — superseded, not merely supplemented.

### 4. Two decisions to make explicitly, not by default

- **Uncategorised items.** rTorrent torrents frequently carry no label. Either an explicit
  "(no category)" pseudo-row that can bind to a queue, or such items are simply never attributable.
  The second matches §8.3's silent-omission rule and is more honest; the first is more useful.
  **Decide, implement one, and record why in `docs/decisions.md`.**
- **Categories appearing later.** A category added in SAB next month will not be in the stored
  mapping. The page should show newly-seen-but-unmapped categories rather than expecting the user
  to notice — same visibility theme as #2. Decide how, and whether it needs anything beyond
  re-probing on page load.

## Tests

- A client reporting categories renders one row each; **no free-text input exists**.
- Saving with a row left unbound stores that category with `queue_id = NULL`, and it **survives a
  re-edit** (this is #11b's regression test — assert the round trip, not just the request).
- A suggested binding is a selected value, and saving without touching it **persists**.
- A client reporting no categories falls back to path arithmetic, **labelled as such**.
- The new operation is declared by both connectors (conformance suite).
- Whichever uncategorised behaviour you chose, asserted directly.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate** — every gate Bash call MUST pass an explicit timeout of at least
600000 ms. **Run backend gates from the REPO ROOT**; if you `cd` into `frontend/`, `cd` back.

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`
4. `npm run build`, `npm run lint`, `npm test` from `frontend/`

## When done

Update frontmatter, `git mv` to `prompts/done/`, record decisions in `docs/decisions.md`, update
the spec (§2.1, §5, §8.3, §13.4, §13.6), and append resolutions under findings #10 and #11 in
`prompts/test-findings-2026-08-23.md` (leave the original text).
**Do not commit or push.** Report: files, every exit code, both test counts, a proposed one-line
message, your two explicit decisions with reasoning, and anything else found.
