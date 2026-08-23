---
name: 2026-08-22-sab-auth-error-handling
status: done
created: 2026-08-22
model: sonnet
completed: 2026-08-22
result: >
  Fixed tests/fake_sabnzbd.py first and confirmed the suite went red (2 tests:
  test_connection_bad_api_key_raises_client_error_not_unreachable and
  test_test_connection_failure_leaves_a_previously_persisted_set_intact) before touching the
  connector. Added ClientAuthenticationFailed(ClientError) to core/clients/errors.py.
  sabnzbd.py's _get now recognises the measured 403/text-html/"API Key Incorrect" body and
  raises it on every authenticated call; test_connection now makes a second, authenticated
  mode=queue call so a bad key actually fails (GitHub #23 regression test added), while
  mode=version still supplies reachability/version/capture. Spec §13.4 #9/#10 and
  docs/decisions.md updated. All three gates green: pytest 1815 passed, ruff check clean, ruff
  format clean (after reformatting tests/fake_sabnzbd.py).
---

# Task: SABnzbd auth failures are a 403 with a plain-text body — handle them as such

Correct `core/clients/sabnzbd.py`'s error handling against **measured** behaviour of a live
SABnzbd 5.1.1, and correct the fixture that encoded the wrong assumption.

## The measured facts (not doc-derived — probed directly, 2026-08-22)

| Call | Bogus key | HTTP | Content-Type | Body |
|---|---|---|---|---|
| `mode=version` | **accepted** | 200 | `application/json` | `{"version":"5.1.1"}` |
| `mode=version` | **no key at all — accepted** | 200 | `application/json` | `{"version":"5.1.1"}` |
| `mode=queue` | rejected | **403** | `text/html` | `API Key Incorrect` |
| `mode=history` | rejected | **403** | `text/html` | `API Key Incorrect` |
| `mode=get_config` | rejected | **403** | `text/html` | `API Key Incorrect` |

Two guesses in `docs/download-client-framework-spec.md` §13.4 were falsified by this:

- **#10** — `mode=version` is unauthenticated. `test_connection` therefore validates reachability
  only; any key passes. GitHub [#23](https://github.com/crzykidd/lftpweb/issues/23).
- **#9** — an auth failure is **not** `{"status": false, "error": ...}` JSON on a 200. It is a
  **403 with a `text/html` plain-text body**. `response.json()` raises on it, so every
  authenticated call currently surfaces "non-JSON body" or "HTTP 403" rather than "wrong API key".

## Scope — read this carefully

**In scope:** the connector's error handling, and `test_connection` using an authenticated call so
a bad key actually fails.

**Out of scope, explicitly:** the save-on-test *flow* in `api/settings_clients.py`. The user asked
for that process to be left as-is pending a Settings rework. Do not change when a test runs, what
blocks a save, or the endpoint's shape — only what the connector reports when a call fails.

## Order of work — this matters and is not negotiable

**Fix `tests/fake_sabnzbd.py` FIRST, and watch the existing tests fail before touching
`sabnzbd.py`.** The fixture currently returns an auth error for `mode=version` (real SAB accepts
it) and returns `{"status": false, "error": ...}` on a 200 (real SAB returns a 403 with plain
text). It encodes the same wrong assumption the connector does, which is exactly why 1811 green
tests did not catch this.

This repo has now had this failure **twice** — `IMPORT_EVENT_TYPES = {3}` was the first, and it
reached production. A fixture edited only to match new connector code repeats the mistake a third
time. Correct the fixture to match the measured table above, confirm the suite goes red, then fix
the connector and watch it go green.

## What to do

1. **`tests/fake_sabnzbd.py`** — model reality: `mode=version` accepts any key (and no key);
   every other mode with a bad key returns **HTTP 403, `text/html`, body `API Key Incorrect`**.
   Keep `echo_key_in_version_body` working. Update the docstrings: these shapes are now
   **measured against SAB 5.1.1**, not doc-derived — say so, and say when.

2. **A distinct authentication error.** Add one to `core/clients/errors.py`. "Wrong credential"
   and "host unreachable" want different messages and are different facts. Follow the existing
   taxonomy's reasoning (§4.2): decide deliberately whether it subclasses `ClientError` or
   `ClientUnreachable` — it is **not** `CapabilityUnavailable`, because a bad key says nothing
   about what the client supports and **must not degrade a capability**. State the choice in the
   docstring.

3. **`core/clients/sabnzbd.py`** — treat a 403 whose body is a plain-text SAB error as an
   authentication failure, on **every** call, not only `test_connection`. Do not assume every 403
   is an auth failure: key off the body where it is recognisable and fall back to a plain
   `ClientError` where it is not. Keep the existing `{"status": false, "error": ...}` JSON
   handling — it remains the right shape for *non-auth* action failures, which are still
   unverified.

4. **`test_connection` uses an authenticated call** so a bad key fails. `mode=queue` is the
   obvious choice (already implemented, already needed by the poller). If reachability-without-
   credentials is still worth reporting separately, that is a judgement call — make it, and write
   the reasoning down rather than leaving it implicit.

5. **Spec + docs.** Update §13.4 rows #9 and #10 to reflect that the connector now matches
   measured behaviour. Record the decision in `docs/decisions.md`, newest at top.

## Tests

- A bad key on `mode=queue`/`history`/`get_config` raises the new auth error, not `ClientError`
  and not a JSON-parse failure.
- A bad key now **fails** `test_connection` (this is the regression test for #23).
- `mode=version` with no key still parses — the endpoint is genuinely public and that must not
  become an error.
- A 403 with an *unrecognised* body is still a plain `ClientError`, not misreported as an auth
  failure.
- An auth failure **does not degrade any capability** (assert against `degrade_from_error`).

## Verification gates — read `CLAUDE.md`

**NEVER background a gate** — pass an explicit timeout of at least 600000 ms on every gate Bash
call. Three agents on this feature have stalled ~25 minutes each by omitting it. **Run backend
gates from the REPO ROOT, never `backend/`.** If you `cd` anywhere, `cd` back first — the working
directory persists between calls.

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`

## When done

Update frontmatter, `git mv` to `prompts/done/`, record decisions. **Do not commit or push.**
Report: files, exit codes, backend test count, a proposed one-line `fix:` message, and — explicitly
— **confirmation that you saw the suite go red after the fixture change and green after the
connector change.** If it never went red, say so; that means the fixture correction missed
something.
