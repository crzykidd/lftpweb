---
name: 2026-08-17-whats-new-popup-and-release-notes
status: done
created: 2026-08-17
model: sonnet
completed: 2026-08-17
result: >
  Built lib/releaseNotes.ts (parseChangelog/compareVersions/whatsNewSections/
  trimEmptySubsections, unit-tested incl. against the real CHANGELOG.md?raw), the
  WhatsNewDialog popup (mounted once in Layout.tsx), the Docs -> Release notes page
  (renders CHANGELOG.md verbatim via MarkdownDoc's newly-exported SectionBody, not
  parseDocSource, which the changelog's shape doesn't fit), and pointed
  lib/versionBadge.ts's non-dev href at the in-app route (VersionLink.tsx now
  branches Link-vs-<a> via lib/docLinks.classifyLink). Dockerfile copies CHANGELOG.md
  into the frontend-builder stage; no vite.config.ts change was needed. All gates
  green: ruff check/format, full pytest (1242 passed), npm test (410 passed), npm
  run lint, npm run build, and a real `docker build --target frontend-builder`.
  Unviewed -- no browser in this environment.
---

# Task: What's-new popup after upgrade, Release notes in Docs, version link points in-app

User request, design settled 2026-08-17 (this prompt is the record; build it, don't
re-litigate): after an upgrade, the first page load shows a popup with the new version's
release notes; the Docs section gains a Release notes page rendering `CHANGELOG.md`; and the
nav's bottom-left version readout links to that page instead of the GitHub release.

## Before you start

- Read `DESIGN.md` (required by `CLAUDE.md`) — especially §9 (frontend shape). This task is
  frontend + one Dockerfile line; no backend/API/schema changes.
- Conventions to match, look at these first:
  - `frontend/src/pages/docs/` — `QuickStartPage.tsx` etc. import repo markdown via Vite
    `?raw` (`../../../../docs/*.md?raw`) and render through `MarkdownDoc.tsx`. The new page
    follows this exactly, importing `../../../../CHANGELOG.md?raw`.
  - `frontend/src/nav.ts` + `App.tsx`'s `docs` routes — how a docs page is registered.
  - `frontend/src/lib/versionBadge.ts` (+ its test) — the pure-function pattern and the
    dev/release/no-channel distinctions this task must preserve.
  - `frontend/src/lib/storage.ts` — the localStorage wrapper; use it, don't hand-roll.
  - `frontend/src/lib/` pure-logic-with-Vitest convention (`fileTree.ts`, `transferPanel.ts`).
  - `docker/Dockerfile`'s frontend build stage — it copies repo-root `docs/` in (see the
    comment near it, 2026-08-14 precedent `d1fe8ca`); `CHANGELOG.md` needs the same motion or
    the image build fails on the new `?raw` import. Verify with the Image-build gate.
  - `frontend/src/components/PathBrowseDialog.tsx` / `FileTree.tsx`'s delete confirmation —
    modal styling to match.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files this plan
needs to modify. If any have uncommitted changes, list them and ask before touching them.
Surface unrelated dirty files once; don't block. This file is exempt.

## What to do

### 1. `lib/releaseNotes.ts` — the one changelog parser (pure, tested)

- `parseChangelog(raw: string)` → ordered `{version, date, body}[]` — split on the
  `## [X.Y.Z] — YYYY-MM-DD` headers, skip `[Unreleased]`, body is the section's raw markdown
  (empty `###` subheadings and all — trim fully-empty subsections from the *rendered* output
  if easy, but never mutate what the Docs page shows: it renders the file verbatim).
- `compareVersions(a, b)` — integer-triple semver compare (no pre-release/build handling;
  this project's versions are always bare `MAJOR.MINOR.PATCH`).
- `whatsNewSections(currentVersion, lastSeenVersion, sections)` → the sections to show in the
  popup, per the settled rules:
  - `lastSeenVersion == null` → `[]` (first-ever visit / fresh browser — not an upgrade;
    caller stores current and shows nothing).
  - `lastSeenVersion == currentVersion` → `[]`.
  - otherwise → every section with `lastSeen < version <= current`, newest first — an
    upgrade that skipped a release shows all of them. If that filter yields nothing (a
    downgrade, or versions archived out of the changelog) → `[]`, caller stores silently.
- Vitest coverage for all three, including: multi-version accumulation, downgrade, unknown
  stored version, `[Unreleased]` skipped, and parsing the *actual* `CHANGELOG.md?raw` content
  (import it in the test — a format drift in a future release should fail a test, not
  silently break the popup).

### 2. Docs → Release notes page

- `pages/docs/ReleaseNotesPage.tsx`: import `../../../../CHANGELOG.md?raw`, render via
  `MarkdownDoc` (verbatim — the changelog is the single source of truth, per the
  `release-prep-and-cut` standard). Add a small "View on GitHub" link at the top (the repo's
  releases page — note: `repo_url` comes from `/api/health` at runtime; if the page has no
  health access, link the static repo path used elsewhere or thread the existing health
  reading in, whichever is less invasive — record the choice).
- Register in `nav.ts` + `App.tsx` (`/docs/release-notes`), after the existing docs entries.
- `docker/Dockerfile`: copy `CHANGELOG.md` into the frontend build stage alongside the
  existing `docs/` copy, with a one-line comment mirroring the existing one.

### 3. The what's-new popup

- New `components/WhatsNewDialog.tsx` + wiring in the app shell (wherever health is already
  fetched once on mount — follow `VersionLink`/`StatsHeader`'s existing health source rather
  than adding a new poll).
- Logic (all decisions via `lib/releaseNotes.ts`, component stays thin):
  - When health arrives with a `version`: read `whatsnew.lastSeenVersion` from storage.
  - `whatsNewSections(...)` non-empty → show the modal: one block per section
    (`vX.Y.Z — date` heading + rendered markdown body), "View full release notes" linking to
    `/docs/release-notes`, a single dismiss action. Dismiss (or navigating via the link)
    writes the current version to storage.
  - Sections empty → write the current version to storage silently (covers first visit,
    same-version, downgrade, archived-out).
  - Dev builds need no special casing — the version only changes when actually bumped;
    say so in a comment.
- Per-browser semantics (localStorage) is the settled, named limitation — record it.

### 4. Version link points in-app

- `lib/versionBadge.ts`: a **release** build's (and the no-channel fallback's) `href` becomes
  the in-app `/docs/release-notes` route; a **dev** build keeps its commit link unchanged.
  Update the existing unit tests, and check `VersionLink.tsx` renders an internal route
  correctly (react-router `Link` vs `<a>` — an internal route must not full-page-navigate).
- The GitHub release URL isn't lost — it lives on the Release notes page (step 2).

### 5. Not in scope (name it, don't build it)

Archived per-minor changelogs (`docs/CHANGELOG-0.x.md`, none exist yet) are not rendered
in-app; a popup spanning an archive boundary shows only what's still in `CHANGELOG.md`.
No server-side per-user seen-state. Say both in the `docs/decisions.md` entry.

### 6. Docs, same commit

- `CHANGELOG.md` `[Unreleased]` → Added: one entry covering all three pieces, user-facing
  language.
- `docs/decisions.md`: newest-at-top entry — first-visit-silent rule, multi-version
  accumulation, per-browser limitation, archive-boundary gap, the versionBadge link change
  (GitHub URL relegated to the page), and anything non-obvious you hit.
- `prompts/startnewsession.md`: add row **U** to the build-run table (after row T), same
  style, same commit. Note the popup and page are **unviewed** (no browser here).
- `DESIGN.md`: only if §9's page list enumerates docs pages — update minimally; if it
  doesn't, leave it alone and say so.

### 7. Verify — each gate separately, read each exit code

Backend untouched but re-verify anyway: `uv run ruff check backend tests`,
`uv run ruff format --check backend tests`, `uv run pytest` (full). Frontend:
`npm test -- --run`, `npm run lint`, `npm run build`. The `?raw` import of a repo-root file
is the risky bit — `npm run build` failing on path resolution means the Vite `fs.allow`/
served-root note in `frontend/vite.config.ts` (see its comment) needs the same treatment for
the repo root as `docs/` already has; fix it there, don't move the file.

## Conventions to honor

- Comment style: constraints the code can't show; match the density of the files touched.
- Conventional-commit prefix (`feat:`), no `Co-authored-by:` trailers.
- Never `git add -A`, never auto-commit, never push.

## When done

1. Update this file's frontmatter (`status`, `completed`, `result`).
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record the non-obvious decisions in `docs/decisions.md` (see §6).
4. Hand off ONE commit covering the prompt file, the changes, and the prompt move. **You are
   a spawned agent: do not commit.** Prepare the working tree, then report the file list +
   proposed `feat:` message back to the orchestrating session, which surfaces the `y/n`.
