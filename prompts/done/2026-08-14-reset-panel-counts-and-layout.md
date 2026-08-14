---
name: 2026-08-14-reset-panel-counts-and-layout
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: >
  Replaced the three near-identical "Reset item tracking" panels (whole-queue and
  purge-by-pattern in QueueResetControls.tsx, plus a third selected-items panel that lived
  entirely inside FileTree.tsx's own multi-select toolbar) with one control: a scope selector
  (All/Pattern/Selected), a Cancel now always present once the box is open (fixes the old
  defect where both dismiss controls lived inside `preview &&` branches), and an identical
  choose scope -> preview -> confirm flow for every scope. Selection state lifted from
  FileTree.tsx to FilesPage.tsx (one Record<queueId, Set<rel_path>>, passed to both
  components) so the two can never disagree about what's checked. New lib/resetComposition.ts
  (describeResetTargets, "3 directories and 12 files -- 15 items", its own zero case) and a
  total===0 branch added to lib/resetWarning.ts's resetWarningLines (single plain line, no
  always-true lines, confirm button disabled with reason). Fixed the flex-col label rendering
  the queue-name confirmation across three lines (wrapped the sentence in one span) and the
  literal "--" in ALWAYS_TRUE_RESET_LINES (now a real em dash). Whole-queue's typed-name
  confirmation kept (server still requires QueueResetRequest.confirm_name) but moved to a
  single removable confirmStage 'typed-name' stage reached only after the preview, per the
  user's own decision recorded 2026-08-14 in the prompt and mirrored in docs/decisions.md. No
  backend change made or needed. 11 new frontend tests (resetComposition.test.ts,
  resetWarning.test.ts's new total-zero cases); 145 frontend tests and 903 backend tests
  passing; both ruff gates, npm run lint/build, and docker compose config --quiet on all three
  compose files all clean. DESIGN.md, CHANGELOG.md, ConceptsPage.tsx, docs/decisions.md
  updated. Could not be visually confirmed -- no browser exists in this environment.
---

# Task: Unify the three reset-tracking scopes into one control with a uniform preview → confirm flow

"Reset item tracking" currently exists as three separate UIs that do the **same operation** with
different ceremony, which is why the user could not tell them apart. Replace them with one
control: a scope selector (**All / Pattern / Selected**), an always-available Cancel, and the
same **preview → confirm** flow for every scope. Fix the counting, zero-state, and layout defects
found live at the same time.

The user specified this shape directly: *"all/pattern/selected and cancel on the main box, and
when I select all I get a preview and confirm, or pattern gets a preview and a confirm."*

## What is broken today (all reported from the running app, 2026-08-13, queue `ar-tv`)

1. **Three near-identical panels.** Whole-queue and purge-by-pattern read almost the same because
   they *are* the same — both forget `item`/`item_settle`/`deleted_archive` rows via
   `core/local_delete.py`, cascading `job` rows away and unlinking `event` rows, leaving local
   files alone. They differ only in selection and confirmation. The third scope (selected items)
   lives somewhere else entirely, in `FileTree.tsx`.
2. **The pattern panel cannot be dismissed.** Both its dismiss controls sit inside `preview &&`
   branches — a "Close" rendering only when `preview.length === 0` (~line 286) and a "Cancel"
   inside the `preview.length > 0` block. On open `preview` is `null`, so neither exists: you must
   run a preview just to make the panel go away.
3. **Counts are bare and the zero case is nonsense.** The panel says only `{topLevel.length} items`,
   and with an empty queue it renders *"— 0 items"* and *"None of these 0 items still exist on the
   seedbox, so nothing will be re-downloaded."* `lib/resetWarning.ts`'s `reDownloadLine` handles
   `remoteCount === 0` but has no `total === 0` branch.
4. **A flex-container defect breaks the confirmation line.** It renders as
   `Type the queue name (` / `ar-tv` / `) to confirm:` on three lines.
5. **Literal `--` in user-facing copy.** `ALWAYS_TRUE_RESET_LINES` contains *"Local files are not
   deleted -- this only resets tracking"* while the surrounding panel uses real em dashes.

## Before you start

- Read `CLAUDE.md`. Read `prompts/done/2026-08-13-reset-item-tracking.md` — the task that built
  these panels. Its reasoning about *what* the warning must say (real numbers, never a hedge; the
  local-files fact and the job-cascade fact stated plainly) is correct and **must be preserved**.
  This task changes structure, scope selection, and layout — not the warning's philosophy.
- `lib/resetWarning.ts` is shared by all scopes deliberately, so they can never disagree about
  consequences. **Keep that single-source property.** It gets easier under this design, not harder.
- Read `api/jobs.py`'s `reset_item`, `reset_queue_all`, `reset_preview`, `reset_by_pattern`, and
  `core/local_delete.py`'s corresponding functions, before designing the frontend.

## Working tree check

Run `git status --porcelain` first. Other work may be in flight on `frontend/src/pages/` and
`frontend/src/components/`. If any file this plan touches is dirty, list it and ask before
editing. This prompt file is exempt.

## What to do

### 1. One control, three scopes, one flow

Replace `components/QueueResetControls.tsx`'s two panels (and fold in `FileTree.tsx`'s
bulk-selected reset panel) with a single control:

- A scope selector: **All** / **Pattern** / **Selected**.
- **Cancel is always present on the main box**, at every stage, including before any preview has
  run. This is the defect in item 2 above and it must not survive in any scope.
- Every scope follows the identical flow: **choose scope → preview → confirm**. The preview lists
  exactly what will be forgotten, then `resetWarningLines` states the consequences, then a confirm
  button acts.
- **Pattern** keeps its existing matching semantics — case-insensitive, glob when the pattern
  contains `*?[`, plain substring otherwise, the same evaluator auto-queue's select/skip patterns
  use. Do not change the matching rules.
- **Selected** is disabled with a stated reason when nothing is selected, rather than hidden.

### 2. The architectural question: where selection state lives

Today `FileTree.tsx` owns multi-select and renders its own reset panel; `QueueResetControls`
renders below the tree in `FilesPage.tsx` and never sees the selection. A unified control needs
both.

Lift the selection state to `FilesPage.tsx` (it already renders both components) and pass it down,
rather than duplicating selection tracking or reaching across components. **If you find a reason
that won't work, stop and report it rather than working around it with a second source of truth
for selection** — two components disagreeing about what is selected, on a destructive action, is
strictly worse than the duplication this task is removing.

### 3. Decide what happens to the typed confirmation — and surface it

`api/jobs.py.reset_queue_all` requires `body.confirm_name` to equal the queue's name, re-checked
server-side as defence in depth. The user finds typing it "a little intense" and has asked for
preview → confirm on every scope.

There is a real gap underneath this: **a pattern of `*` already matches everything**, and
`reset_by_pattern` requires no typed confirmation at all (its docstring says reviewing the preview
*is* the confirmation). So today's typed guard is bypassable in two clicks, which makes it theatre
on one scope or under-guarding on the other.

**Recommended:** keep `confirm_name` required by the API (do not weaken a server-side guard as a
side effect of a UI task), and in the unified UI have the **All** scope's final confirm step ask
for the typed name *after* the preview — so the flow is uniform, the preview does the explaining,
and the most destructive scope still costs one deliberate keystroke sequence. Pattern and Selected
confirm from the preview alone, as they do today.

**Report this decision explicitly in your final message.** If the user wants the typed
confirmation gone entirely, that is an API contract change (`ResetQueueRequest.confirm_name`) and
belongs in its own task — do not make the frontend auto-fill the queue name to satisfy the check,
which would leave a guard that looks real and is not.

**Decided 2026-08-14: keep the typed confirmation for now, but it is living on borrowed time.**

The user's reasoning, which is sound and should not have to be re-derived later: the typed name
was justified *because the whole-queue reset had no preview* — it was a blind "forget everything"
with nothing on screen to review, so the keystrokes were the only thing standing between an
accidental click and the most destructive action in the app. **This task adds a preview to that
scope.** Once the user is shown the full list and has to click confirm against it, typing the
queue name adds friction without adding information — the review *is* the confirmation, which is
exactly the argument `api/jobs.py.reset_by_pattern`'s own docstring already makes for the pattern
scope.

So: **build it as one cleanly removable step** — a discrete stage in the All scope's flow, not a
condition threaded through shared state, shared validation, or the confirm button's enablement
logic in a way that would have to be unpicked from three places. Deleting it later (frontend step
plus the server-side `confirm_name` check) should be a small, obvious diff.

### 4. Report the real composition of what will be reset

Replace bare `{topLevel.length} items` with a breakdown: *"3 directories and 12 files — 15 items"*.
`topLevel` is the nodes with no `/` in `rel_path` (DESIGN.md §4.7 — a reset targets top-level
items); split by `FileNode.is_dir`. Handle singulars ("1 directory and 1 file — 2 items"). Apply
it to every scope's preview, not just All.

Nested children are reset along with their parent but are not what these numbers count — say so
in the copy if it isn't obvious from context.

### 5. Give the zero case its own branch

Add an explicit `total === 0` branch to `lib/resetWarning.ts`:

- Return a single plain line — this scope matches nothing, so there is nothing to reset. Do **not**
  emit the always-true lines about local files and job history; there are no items for them to be
  true of.
- The confirm button is disabled in that state, with the reason visible.

`FileTree.tsx`'s selected scope cannot reach `total === 0`, but the function is shared — make sure
output for any `total >= 1` is unchanged.

### 6. Fix the flex defect and the literal `--`

The three-line rendering is a CSS defect, not text wrapping: the confirmation `<label>` is
`flex flex-col`, so **every child becomes its own row** — the bare text runs and the inline
`<span>` alike. Wrap the sentence in a single element so the label has two flex children. Check
the whole file and `FileTree.tsx` for the same `flex flex-col` + inline-children shape; this is a
pattern bug, not a one-off.

Use a real em dash in user-facing strings in `ALWAYS_TRUE_RESET_LINES`. **Only** in strings the
user reads — `--` stays in code comments, which is this repo's house style.

## Testing

`lib/resetWarning.test.ts` covers every branch of that function — extend it, don't replace it. Add
cases for `total === 0` and the singular/plural boundaries of the new breakdown. Put the
breakdown's string-building in `lib/` as a pure function so it is unit-testable without mounting
anything; that is the existing convention and why these tests exist at all.

Run `npm run lint`, `npm test`, `npm run build`. No backend change is expected — if you conclude
one is needed, stop and report rather than making it. If a backend test fails, stop and report
rather than adapting it.

## Conventions to honor

- Non-obvious decisions go in `docs/decisions.md`, newest at top, with rejected alternatives.
- Doc updates ship in the same commit as the code. If the in-app docs describe the old three-panel
  shape, update them (`frontend/src/pages/docs/ConceptsPage.tsx` mentions reset tracking).
- **You cannot see the UI** — no browser exists in this environment. The flex fix is reasoned from
  the CSS, not observed. Every rendering claim means "builds, type-checks, and lints cleanly."
  Say plainly that this redesign needs a human to click through.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` it to `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`.
4. Prepare ONE commit; **do not commit**. Report the file list and a proposed one-line message
   back to the orchestrating session, which surfaces the `y/n`. Never `git add -A`, never push.
