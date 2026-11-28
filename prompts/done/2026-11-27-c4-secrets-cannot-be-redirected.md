---
name: 2026-11-27-c4-secrets-cannot-be-redirected
status: completed
created: 2026-11-27
model: sonnet
completed: 2026-11-27
result: "Stored secrets are refused (422, no outbound call) on endpoint change for download-client update, *arr update, seedbox save and host test; trivial edits normalised; forms hint; 2415 pytest, 865 vitest"
---

# Task: C4, stop a stored secret being sent to a new endpoint (non-breaking)

**Audit finding F6** in `private_data/security-audit-2026-11-26.md` (batch **C4**; read both
sections). lftpweb's write-only secrets can be recovered by someone who can only write settings.
They change an endpoint's URL or address, leave the secret field blank, and lftpweb sends the
**stored** secret to the **new** endpoint.

That happens in three places:
- **Download-client test and update** (`api/settings_clients.py`, `_resolve_secret_for_test`
  around line 337, and the update path).
- **\*arr instance update** (`api/settings_arr.py` around line 153: `api_key_enc` is kept while
  `base_url` changes). Check whether an \*arr test endpoint has the same flaw.
- **Seedbox host test and save** (`api/settings_host.py` around line 204: an override address
  combined with the stored password). Only `auth_method == "password"` sends a secret. Key
  auth sends no secret, so leave it alone.

**The fix:** when the endpoint's identity changes, a stored secret is not reused. The user must
type the secret again.

**Hard rule from the user: non-breaking.** An existing install that upgrades and changes
nothing must keep working exactly as before:
- Stored configs keep working.
- Saves that don't change the endpoint keep working with the secret left blank.
- Trivial edits don't count as an endpoint change: a trailing slash, the case of the
  hostname, an explicit default port, or surrounding whitespace.

## Before you start

- Read `CLAUDE.md` and the relevant `DESIGN.md` sections (§17 for download clients, the \*arr
  section, and the host section).
- Work in **this checkout on `dev`**. Do not create a worktree. The tree should be clean apart
  from `.claude/worktrees/`, which you should ignore.
- Read `git show b251d35 --stat` and its `docs/decisions.md` entry. That's how C3 was made
  non-breaking, so match its tone.

## Working tree check

Run `git status --porcelain`. If anything other than `.claude/worktrees/` and this prompt is
dirty, stop and report it.

## What to do

1. **Define endpoint identity** in one shared helper, with tests.
   - For a URL (download clients and \*arr): the scheme, the lowercased host, and the port
     with the default port filled in (http 80, https 443). Include the path, normalised with no
     trailing slash, because a different path on the same host can be a different service.
     Credentials embedded in the URL also count as a change.
   - For an SSH host: the lowercased address, the port and the username.
2. **Enforce it on every path where a stored secret would otherwise go out:** create (not
   applicable), update, test-on-save and standalone test.
   - If the identity changed and the request supplies no new secret, return a **422** with a
     clear, field-specific message. For example: "The URL changed, so re-enter the API key —
     the saved key is only sent to the address it was saved for." Make the error shape match
     each endpoint's existing error conventions.
   - Make sure **no network request reaches the new endpoint** in the refused case. Assert this
     with a mock transport or fake that records calls.
   - If the identity is unchanged, keep today's behaviour exactly: blank means keep the
     stored secret.
3. **Frontend forms** (the download-client, \*arr and host settings forms). When the user edits
   an endpoint field away from its saved value, mark the secret field as required. Add an
   inline hint saying why, and show the server's 422 message if it comes back anyway.
   - Don't add a confirm dialog. The user prefers fewer clicks.
   - Use the same normalisation as the backend so a trailing slash doesn't trigger the hint, or
     at least don't block on the client side and let the server decide.
4. **Tests:**
   - Backend: for each of the three areas, cover:
     - an identity change without a secret: 422, and no outbound call
     - an identity change with a secret: works, and the new secret is stored
     - an unchanged identity with a blank secret: works, and the stored secret is used
     - a trivial edit, such as a trailing slash or uppercase host, with a blank secret: works
   - Frontend: the field becomes required after an endpoint edit.
5. **Docs, in the same commit:**
   - `CHANGELOG.md` `[Unreleased]` (Security section): say no action is needed on upgrade, and
     that changing an endpoint now asks for the secret again.
   - `DESIGN.md`, in the relevant sections.
   - `README.md`, if it describes secret handling.
   - `prompts/startnewsession.md`: mark C4 done in the security-pass block.
   - A new top entry in `docs/decisions.md`: what counts as identity, why the path is included,
     why trivial edits are normalised, and why the client-side hint doesn't block.

## Conventions to honor

- Every verification gate runs in the **foreground** with an explicit timeout; for pytest use
  600000 ms. You will never receive a background-task notification, so read each exit code
  yourself.
- From the **repo root**, run each of these and read its exit code separately:
  - `uv run ruff check backend tests`
  - `uv run ruff format --check backend tests`
  - `uv run pytest`: the fake seedbox containers are up, and the target is **0 skipped**. The
    baseline is 2371 passed.
- From `frontend/`, run:
  - `npm run lint`
  - `npx tsc -b`
  - `npm test` (baseline 858)
- Report every gate's exit code and the test counts.

## When done

1. Set this file's frontmatter (`status`, `completed`, `result`). Then move it into
   `prompts/done/`; it is untracked, so a plain `mv` is fine.
2. **Do not commit.** Report back:
   - what changed in each area
   - any place you found the flaw beyond the three listed
   - the gate results
   - the full explicit list of paths for a single commit, including this prompt in
     `prompts/done/`
   - a proposed one-line `fix:` message with no `Co-authored-by` trailer
