---
name: 2026-08-22-client-framework-stage1b-frontend
status: completed          # pending | completed | failed
created: 2026-08-22
model: sonnet            # coding
completed: 2026-08-22
result: >
  Settings -> Clients tab shipped: generic connector form rendered from GET /api/settings/
  client-types' declared ConfigField schema (no if client_type === "..." anywhere in the
  frontend), base-path browse/add/remove reusing PathBrowseDialog, category -> queue mapping
  with a propose-only inference button, and an honest capability readout (derived rows show
  their note, none rows show a stated reason, a failed test never blanks a previously known
  set). README's recommended-seedbox-layout section written. 17 new frontend tests (697 total
  passing); all four gates green; no backend changes.
---

# Task: Stage 1b (frontend) — Settings → Clients, the generic connector form, and the README write-up

Make the stage 1b backend usable: a **Settings → Clients** tab that adds a download-client
instance, renders its connection form **from the connector's own declared schema**, browses and
validates its base paths, maps categories to queues, runs test-connection, and shows what the
client reports it can do.

**This is the half the user can actually click, and it is the gate on everything after it.** Until
an instance can be configured in the browser, no real SABnzbd capture exists, and
`docs/download-client-framework-spec.md` §13.4's twelve unverified guesses stay unverified.

## Before you start

Read, in this order:

1. **`docs/download-client-framework-spec.md`** — §4.3 and §4.4 (**tri-state capabilities, the
   `note` on a derived one, and "a missing capability disables a feature; it never fakes one"**),
   §5.1 (**`family` groups the picker and nothing else — never a behavioural branch**), §8.1
   (connector-declared config schema), §8.2 (**base paths: browsed, validated on save**), §8.3
   (category → queue, and the inference hint below).
2. **`backend/lftpweb/api/settings_clients.py`** — the API you are consuming. In particular
   `GET /api/settings/client-types`, which returns each connector's `client_type`, `family` and
   declared `ConfigField` list. **The form is rendered from that, not hand-written per client.**
3. **The existing Settings tabs** — find how `settings_arr.py`'s instances are presented in the
   frontend and mirror it closely. An *arr instance and a download-client instance are the same
   kind of object from a user's point of view: named, enabled, secret-bearing, testable.
4. **The existing path-browse dialog** — Settings → Queues uses it for `remote_path` via
   `GET /api/browse/remote`. **Reuse that component; do not build a second picker.**
5. **`README.md`** — for the write-up in step 4 below, and to match its voice.

## Working tree check

`git status --porcelain` first. Surface anything unexpected before editing. This prompt file is
exempt.

## What to do

### 1. Settings → Clients

Follow the *arr instances tab's structure. Per instance: name, type, enabled toggle, secret field
(write-only — the API never returns it, and an update that doesn't re-send it must preserve it),
test-connection button with its result, base paths, and category → queue mappings.

Add the tab to the settings nav the same way the existing tabs are registered. **Check `nav.ts`
against the page component** — this repo has already shipped a settings tab that renders
`PagePlaceholder` while its API sat complete and tested behind it (see `README.md`'s "Known gaps"),
so verify the route actually reaches your component.

### 2. The connection form is generic, rendered from the declaration

Render inputs from the selected type's `ConfigField` list: `kind` picks the widget (`"secret"` → a
password input), plus `label`, `required`, `default`, `help_text`.

**No `if client_type === "sabnzbd"` anywhere in the frontend.** That is spec §4.4/§5.1's rule, and
it is the whole reason the schema is declared server-side. `family` may group the type picker
visually; it may not select behaviour. A reviewer should be able to grep the frontend for every
connector name and find nothing.

### 3. Base paths and category mappings

- **Base paths use the existing remote-browse dialog**, and multiple are supported per instance.
- Save-time validation already happens server-side; **surface its real reason on failure**, never a
  generic "invalid path". A wrong base path is the boundary the delete containment check will
  authorise removal within (spec §10.2) — this is the one field where a silent typo is dangerous.
- **Category → queue: offer to infer it.** Spec §8.3 — on the live system the queue remote paths
  *are* the client's category folders (`/…/complete/ar-tv` ↔ queue `ar-tv`), so propose the
  obvious mapping and let the user confirm. Propose, never auto-apply.

### 4. The capability readout — the part that must be honest

After a successful test, show what the client reports it can do, driven **entirely** by the
returned capability set:

- **A `derived` capability is labelled as derived and shows its `note`.** Spec §4.3: rTorrent's
  seed time is wall-clock since completion, so a stopped torrent still accrues. A UI that renders
  derived and native identically is the failure this whole tri-state design exists to prevent.
- **A missing capability disables its feature with a stated reason**, never a greyed control with
  no explanation — "your client doesn't report seed time" is a good message.
- **A failed test must not blank a previously probed capability set.** The API already preserves
  it (spec §4.2, §9.2); the UI must render the last-known set rather than an empty one, and say
  the test failed separately.

### 5. `README.md` — the preferred setup

**The user's explicit ask (2026-08-22).** Write up the reference workflow, from
`docs/download-client-framework-spec.md` §1.1:

- SAB and rTorrent both drop into a shared completed directory tree, matching lftpweb's queue
  remote paths.
- For TV they share the same folder.
- SAB downloads then **extracts** into the completed structure — the completed folder holds the
  only copy.
- rTorrent downloads then **hardlinks** into the completed folder, and keeps seeding from its own
  data directory.

State it as the **recommended** setup, so other workflows are recognisable as departures from a
stated one rather than as undefined territory. Match README's existing voice. **Prose, and no
production anecdotes** — that is the scope call the user made on the last README section
(`c7e2d6e`, the safety-rails section) and it holds here.

Worth saying plainly, because it is genuinely useful and currently written down nowhere a user
would look: on this layout lftpweb's `move`-mode source delete removes the completed-folder copy,
which for an rTorrent release is **one of two links — so seeding continues and no space is
reclaimed until the torrent itself is removed**, while for a SAB release the space comes back
immediately. Same setting, two different real outcomes.

## Conventions to honor

- Match the existing frontend's structure, naming and test style — read a neighbouring settings tab
  and its tests before writing.
- Frontend tests for: the generic form rendering from a declared schema, the derived-capability
  label and note, the disabled-with-reason state, and the failed-test-preserves-capabilities case.
- Record non-obvious decisions in `docs/decisions.md`, newest at top.
- Doc updates ship in the same commit as the code (`CLAUDE.md`).

## Verification gates — read `CLAUDE.md`

**NEVER background a gate.** Foreground, with an EXPLICIT timeout of at least 600000 ms on the Bash
call. Two agents on this feature have already backgrounded `pytest`, received no completion
notification, and stalled indefinitely. **Run from the REPO ROOT, never `backend/`.**

Run each separately and read each exit code:

1. `uv run pytest`
2. `uv run ruff check .`
3. `uv run ruff format --check .`
4. The frontend lint / typecheck / test commands — find them in `package.json` and the CI workflow
   rather than guessing, and run each in the foreground too. Report the frontend test count.

## When done

1. Update frontmatter (`status`, `completed`, `result`); `git mv` to `prompts/done/`.
2. Record decisions in `docs/decisions.md`.
3. **Do not commit.** Report: files, every gate's exit code, backend **and** frontend test counts,
   a proposed one-line `feat:` message, anything in the spec found wrong or underspecified, and
   **the exact click-path a user follows to add their SABnzbd and run a test** — that is the next
   real-world step after this lands.

Never `git add -A`, never push. Branch is `dev`.
