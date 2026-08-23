---
name: 2026-08-23-client-completion-delay
status: done
created: 2026-08-23
model: sonnet
completed: 2026-08-23
result: >
  Added `settle.CLIENT_COMPLETION_HOLD_S` (10s) and `settle.client_completion_ready`, checked in
  `AutoQueue.on_scan` after a matching `find_client_completion` verdict and before it's allowed
  to skip the settle gate. Measured from `Transfer.completed_at`; falls back to a new in-memory
  `AutoQueue._client_completion_first_seen` (first-observation) only when that field is absent
  or unparseable. `on_scan` gained an injectable `now` keyword param for testability. Shipped as
  a constant, not a setting -- argued in docs/decisions.md. Entirely behind the existing
  `client_skip_enabled` setting; every other path unchanged.
---

# Task: Hold a short delay after a client reports completion, before queuing

Finding **#9** in `prompts/test-findings-2026-08-23.md`, decided by the user 2026-08-23:

> *"Wait 5-10 seconds after complete before queuing."*

Today (stage 2b) a terminal `COMPLETED` verdict satisfies the settle gate **immediately**. That is
slightly too eager: a client can report "complete" a moment before the last bytes are flushed, a
final rename lands, or — for rTorrent specifically — before the hardlink into the completed folder
exists at all. Per spec §1.1 those are **two separate events**: rTorrent completes, and *then*
something links the files into the completed tree lftpweb actually scans.

## What to build

A short hold between "the client says it is done" and "the gate is satisfied".

- **Measure the delay from the client's own completion time, not from when lftpweb noticed it.**
  This is the part most likely to be got wrong. The poll cadence already introduces latency; if the
  delay were measured from first observation, a release that finished five minutes ago would still
  wait again for no reason, and the delay would compound with the cadence rather than overlap it.
  Use `Field.COMPLETED_AT` where the connector reports it; fall back to first-observation **only**
  when it does not, and say so in the code.
- **A completion already older than the delay satisfies the gate immediately** — the common case
  for anything the poller picks up on a later pass. There should be no added latency at all there.
- **Pick the conservative end of the user's range (10 s) as the default.** The cost of waiting is
  ten seconds; the cost of not waiting is transferring a directory that is not finished being
  written. Asymmetric, so default to the safe side.
- Make it a **named module-level constant** with the reasoning attached, in the style of
  `PREFLIGHT_HOLD_S` / `DROPPED_GONE_GRACE_S`. Whether it also becomes a user-facing setting is
  your call — argue it either way in `docs/decisions.md`, but **do not add a settings field
  speculatively**; this project's habit is to expose a constant only once someone needs it
  different.

## What must not change

- **This still lives entirely behind the existing off-by-default `client_skip_enabled` setting.**
  The delay is a refinement of the skip, not a new gate, and it must not affect any install that
  has not opted in.
- **Every uncertain path still falls back to today's full settle gate.** No verdict, unknown phase,
  unreachable client, unknown release — unchanged. This task only ever makes an *already-shortened*
  wait slightly longer; it must never make a normal wait shorter.
- Do not touch the withhold gate, the phase allowlist, or the merge/precedence logic.

## Tests

- A completion **younger** than the delay does **not** satisfy the gate; the same item satisfies it
  on a later pass once the delay has elapsed.
- A completion **older** than the delay satisfies immediately — assert there is no added wait.
- A connector that reports no `completed_at` falls back to first-observation, and that fallback is
  asserted explicitly rather than incidentally.
- With `client_skip_enabled` off, behaviour is byte-identical to today. Name the test.
- The full settle gate is unaffected for items with no client verdict.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate** — every gate Bash call MUST pass an explicit timeout of at least
600000 ms. **Run backend gates from the REPO ROOT**; if you `cd`, `cd` back (or use a subshell).

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`
4. Frontend only if touched.

## When done

Update frontmatter, `git mv` to `prompts/done/`, record the decision in `docs/decisions.md`, update
the spec's §4.3/§14 where the immediate-skip behaviour is described, and append the resolution
under finding #9.
**Do not commit or push.** Report: files, every exit code, test counts, a proposed one-line message,
whether you exposed the constant as a setting and why, and anything else found.
