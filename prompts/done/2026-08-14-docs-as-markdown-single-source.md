---
name: 2026-08-14-docs-as-markdown-single-source
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: >
  Moved the Docs prose from JSX to `docs/quick-start.md`/`docs/concepts.md`, the only copy;
  the app renders those same files via Vite `?raw` imports. Chose `react-markdown` +
  `remark-gfm` over a hand-rolled parser (docs/decisions.md) -- unverifiable-by-eye correctness
  risk favoured a well-tested library over from-scratch parsing in a no-browser environment.
  Structural parsing (title/lede/Jump-nav/section boundaries/anchor ids) is a pure-function
  layer in `lib/docMarkdown.ts`, tested directly; a small custom remark plugin
  (`lib/remarkCallouts.ts`) maps a `> **Warning:**`/`> **Note:**` blockquote onto the existing
  `Warn`/`Note` styling. `prose.tsx`'s component vocabulary survives as the renderer's mapping
  target, minus the old aggregate `Table`. Verified content against the code while migrating:
  added a Quick-start bullet for "Folder prefix during transfer" and a paragraph on the
  ~5s active-queue local-only scan, both undocumented; everything else was already current
  (Reset item tracking's one-control redesign, the three-retryable-error-classes fix). Added
  `docs/README.md` and linked it from `README.md`. `server.fs.allow` added to
  `vite.config.ts`/`vitest.config.ts` since `docs/` sits outside Vite's default root.
  Frontend: 105 -> 189 tests, lint/build clean (bundle grew to 635 kB / 183 kB gzip -- noted,
  not treated as blocking). Backend untouched: 967 tests, ruff clean. All three compose files
  validate. Not verified: nobody has seen any of it rendered in a browser.
---

# Task: Move the in-app user docs to Markdown in `docs/`, rendered by the app from that one source

The user-facing documentation exists only as ~640 lines of TSX in `frontend/src/pages/docs/`,
with the prose embedded as JSX. Anyone reading the GitHub repo cannot read the docs without
running the app or reading React components — `docs/` currently holds only `decisions.md` and
`repo-setup.md`, both internal engineering records. Move the prose into Markdown files under
`docs/`, and have the app render those same files.

**The single-source requirement is the whole point of this task.** Do not end up with Markdown
in `docs/` and prose still living in TSX. This repo's own scar is documentation outliving the
behaviour it describes — `docker/Dockerfile`'s comment claimed RAR support for nine phases while
the image shipped no RAR decoder (`prompts/open-issues.md`). Two copies of the same prose drift,
and the copy nobody runs drifts first.

## Before you start

- Read `CLAUDE.md`, `DESIGN.md` §9.1/§9.2, and `prompts/done/2026-08-13-docs-section.md` (the
  task that built the current pages — its reasoning about *what* the docs say is still correct
  and must be preserved; only the storage format changes).
- Read the three existing files end to end before changing anything:
  `frontend/src/pages/docs/QuickStartPage.tsx`, `ConceptsPage.tsx`, `prose.tsx`.
- **This is a format migration, not a rewrite.** Preserve the existing wording. The current text
  was written by reading the code rather than from memory, and several of its statements are
  load-bearing corrections of things this project got wrong before (the `/downloads` vs
  `/staging` explanation in Quick start step 1 is the clearest example — `staging_path` is the
  post-processing *destination*, not a landing zone). Do not "improve" prose you have not
  verified against the code.

## Working tree check

Run `git status --porcelain` first and cross-reference. If any file this plan touches has
uncommitted changes, list it and ask before proceeding. Surface unrelated dirty files once;
don't block. This prompt file is exempt.

## What to do

### 1. Choose and justify the rendering approach

Two viable shapes; pick one and record the reasoning and the rejected alternative in
`docs/decisions.md`:

- **A markdown renderer dependency** (e.g. `react-markdown` + `remark-gfm` for tables), with the
  Markdown imported at build time via Vite's `?raw` suffix. Standard, handles GFM tables, but
  adds runtime dependencies.
- **A build-time Markdown→component transform** (e.g. a Vite plugin), which keeps the runtime
  dependency-free at the cost of build complexity.

Precedent for taking a dependency exists (`@tanstack/react-virtual`, phase 3b) but was flagged
as a deviation at the time — so justify it either way. **Whichever you pick, the Markdown files
under `docs/` must be the only place the prose lives.**

### 2. Convert the prose to Markdown

Create `docs/quick-start.md` and `docs/concepts.md` (match the existing route slugs in
`frontend/src/nav.ts`: `/docs/quick-start`, `/docs/concepts`).

`prose.tsx` exports a specific component vocabulary that must survive the move in some form —
`DocsPage`, `Section`, `Step`, `P`, `UL`, `Warn`, `Note`, `Code`, `Where`, `Jump`, `Table`.
Map each to a Markdown construct:

- `DocsPage` title/lede → the page's `#` heading and opening paragraph, or frontmatter. Say which.
- `Section` (has an `id`, used as an anchor target by `Jump`) → `##` heading with a stable slug.
- `Jump` → the in-page nav. Either generate it from the headings or hand-author a list; anchors
  must keep working.
- `Step n=` → numbered headings or an ordered list; the numbering is meaningful in Quick start.
- `Warn` / `Note` → a blockquote convention (e.g. `> **Warning**`) the renderer styles. These
  carry real safety content — the `move`-mode warnings especially — so they must remain visually
  distinct, not collapse into ordinary paragraphs.
- `Where to="/settings/queues"` → an ordinary Markdown link. **These must navigate via the
  router, not do a full page load** — map relative links to the SPA router in the renderer.
- `Table` → a GFM table (this is why the renderer needs GFM support).

### 3. Wire the app to render them

Keep the routes and nav exactly as they are (`/docs`, `/docs/quick-start`, `/docs/concepts`,
`DOCS_TABS` in `nav.ts`, and `App.tsx`'s `<Route path="docs">` block with its index redirect).
Only the page components' internals change. Keep the existing visual styling — this is a
storage-format change, not a redesign.

Delete `QuickStartPage.tsx`/`ConceptsPage.tsx`'s prose. Keep `prose.tsx` only for whatever
styling primitives the renderer still maps onto; delete what becomes unused rather than leaving
dead exports.

### 4. Make the docs discoverable from the repo

Add a short `docs/README.md` indexing the user docs and distinguishing them from the engineering
records (`decisions.md`, `repo-setup.md`) that share the directory. Link the user docs from the
main `README.md`.

**Note for whoever runs this:** `README.md` is being edited by a concurrent task at the time this
prompt was written (a Known-gaps trim and a pre-release→beta banner change). Re-read it before
editing and do not revert those changes.

### 5. Verify the content still matches the code

While converting, check each factual claim against the code rather than copying it blindly. Any
claim you cannot confirm: **flag it in your report rather than silently keeping or dropping it.**
The docs describe behaviour that changed repeatedly during 2026-08-12/13, and at least one
related defect is already known — `QueuesTab.tsx`/`PostProcessingTab.tsx` label extraction as
*"7zz — zip/7z/rar/rar5/…"*, which is false (`prompts/2026-08-13-field-help-sweep.md` carries
that fix). Do not fix that here; it belongs to the sweep. Just don't reproduce the same error in
the docs.

## Conventions to honor

- Doc updates ship in the same commit as the code they describe.
- Non-obvious decisions go in `docs/decisions.md`, newest at top, with rejected alternatives.
- If you add a dependency, `CHANGELOG.md` gets an entry and the reasoning goes in
  `docs/decisions.md`.
- Frontend tests go in the Vitest suite (`129cfcf`). At minimum, test the link-mapping logic
  (relative links route internally, external ones don't) as a pure function.
- **You cannot see the UI** — no browser exists in this environment. Every rendering claim means
  "builds, type-checks, and lints cleanly", never "renders correctly". Say so plainly; the docs
  pages will need a human to click through afterward.

## Verification

`npm run lint`, `npm test`, `npm run build`, `uv run pytest` (nothing here should touch the
backend — if a backend test fails, stop and report rather than adapting it), `ruff check` and
`ruff format --check`, and `docker compose config --quiet` on all three compose files.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` it to `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`.
4. Prepare ONE commit; **do not commit**. Report the file list and a proposed one-line message
   back to the orchestrating session, which surfaces the `y/n`. Never `git add -A`, never push.
