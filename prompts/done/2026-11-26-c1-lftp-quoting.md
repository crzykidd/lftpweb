---
name: 2026-11-26-c1-lftp-quoting
status: completed        # pending | completed | failed
created: 2026-11-26
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-11-26
result: _lftp_quote fixed to lftp's measured grammar; CR/LF/NUL refused as UNSAFE_PATH; open URL/connect-program/address hardened; real-lftp + seedbox tests added
---

# Task: fix lftp script quoting so a seedbox filename can never run a command (audit F1 / batch C1)

The 2026-11-26 security audit (`private_data/security-audit-2026-11-26.md`, §3 F1 and §5 C1 —
read both first) found that `_lftp_quote` (`backend/lftpweb/core/lftp.py:191`) quotes values the
POSIX-shell way (`'` → `'\''`), but **lftp's own tokenizer is not a POSIX shell**: in lftp 4.9.3 a
`\` inside single quotes is an escape, and a newline ends the command even inside quotes. Reproduced
by the audit against the shipped lftp binary:

- name `Show.S01E01\'; !echo PWNED #` → `PWNED` printed (shell command executed)
- name `Show.S01E01<newline>!echo PWNED #` → `PWNED` printed
- name ending in `\` silently swallows the following ` -o <local>` argument

Remote names flow from the seedbox scan into `pget`/`mirror` arguments (`core/queue.py:3005`), and
auto-queue needs no click. **This is a remote-code-execution bug and the top blocker for 0.4.0.**

## Before you start

- `CLAUDE.md`; `prompts/startnewsession.md` lines 1–650; `DESIGN.md` §4 (the transfer engine —
  especially how each job's rc/script is built) and the phase-3 traps in startnewsession about
  lftp (leading blank line corrupting quote-stripping, `open -u user,password`, mirror's parent
  target, `pget -o` not creating the parent).
- Read **all** of `backend/lftpweb/core/lftp.py` and every caller of `_lftp_quote` and of
  `build_transfer_command` / `build_rc_text`. Then `core/queue.py` around the spawn path.
- **Out of scope, leave alone:** `extra_lftp_settings` validation and Host/Origin checks (batch C3,
  later); extraction (C2); the Dockerfile (another agent is changing it right now in parallel).

## Working tree check

You are running in an isolated git worktree. First copy this prompt from the main checkout
(`/home/manderse/projects/lftpweb/prompts/2026-11-26-c1-lftp-quoting.md`) into your worktree's
`prompts/` — it is untracked so the worktree doesn't have it. Run `git status --porcelain` and
confirm a clean tree apart from that copy.

## What to do

1. **Establish lftp's real quoting grammar empirically before writing the fix.** Use the real
   binary (`/usr/bin/lftp` on this host — record `lftp --version`; and the one in the image,
   `docker run --rm --entrypoint lftp lftpweb:audit-local --version`, if that tag exists, since
   the shipped one is what matters). Use `lftp -c` / a sourced script with `echo` and harmless
   `!`-commands writing into a scratch dir to determine exactly how `'…'`, `"…"`, and `\` behave,
   and what happens with `#`, `;`, `&`, `|`, `!`, `$`, backtick, glob chars `* ? [ ]`, tab,
   CR, LF, NUL, leading `-`, and non-ASCII. Write the findings into the function's docstring —
   the current docstring's claim ("follows shell-like quoting conventions") is the false belief that
   caused this, so replace it with what you measured.
2. **Fix `_lftp_quote`** so every byte sequence that can legitimately appear in a filename round-
   trips to exactly itself as one argument. Also audit **every other value interpolated into an lftp
   script or rc file** (the `open` line's address/user/port — the audit flags
   `open … sftp://{address}` as unquoted at `core/lftp.py:457`; the connect-program string and the
   paths inside it; temp/status file paths) and quote or validate each one. Don't stop at the call
   sites the audit listed — grep for every f-string that ends up in lftp input.
3. **Globbing:** determine whether `pget`/`mirror` glob-expand their arguments in lftp. If a name
   with `*`/`?`/`[` could match *other* remote files, that is a correctness bug in the same class;
   fix it (lftp has options/escaping for literal names — find the right one) or, if it cannot be
   expressed, refuse those names the same way as step 4.
4. **Refuse names that cannot be passed safely** — at minimum any path containing `\n`, `\r` or
   NUL — **before** anything reaches lftp, failing the job with a clear, named error class that the
   Transfers row already knows how to display (find the existing failure-code mechanism; don't
   invent a parallel one). A refused item must not be retried forever by auto-queue — check how
   other permanent failures avoid that and follow the same rule.
5. **Tests — the existing test is the failure mode.** `tests/test_lftp.py:351` asserts only the
   string form, which is why this shipped. Add:
   - a test that feeds adversarial names through the **real `lftp` binary** and asserts lftp parsed
     exactly one argument equal to the original name, and that no `!` command ran (e.g. a marker
     file is absent). Cover at least: `it's`, `a\b`, `end\`, `x\'; !touch MARKER #`,
     `x'; !touch MARKER #`, `a"b`, `$HOME`, `` `id` ``, `a;b`, `a#b`, `*`, `[ab]`, tab, unicode,
     leading `-`. Find out whether CI has an `lftp` binary for unit tests (read
     `.github/workflows/ci.yml`); **a security regression test that silently skips in CI is not
     acceptable** — if CI lacks lftp, add it to the test job's setup, and state what you did.
   - a test that `\n`/`\r`/NUL names are refused before spawn with the named error.
   - Write the tests first and **watch them fail against the current code** (record that in the
     report), then fix.
6. **Integration:** bring up the fake seedbox (`docker compose -f docker-compose.test.yml up
   --build -d` — read the file first) and run a real `pget` and a real `mirror` of files/dirs named
   `it's`, `a\b`, `end\`, `x\'; !touch /tmp/pwned #` (adapt the marker path to the container), and
   a glob-char name; assert the bytes land at the right local path and the marker does not exist.
   Use the existing integration-test helpers if there are any; add these as permanent tests if it
   fits the suite's shape. Tear the seedbox down afterwards.
7. **Docs (same commit):** `DESIGN.md` §4 wherever it describes building the lftp command/rc;
   `docs/decisions.md` entry (newest at top) — the measured grammar, why refuse-not-escape for
   CR/LF/NUL, what CI now does; `CHANGELOG.md` `## [Unreleased]` → a `### Security` entry
   (create the subsection if absent, following the skeleton comment at the top of the file).
   **Do not describe the exploit step by step in public docs** — say what class of name was
   mishandled and that it could execute commands; keep the payloads to tests.

## Gates (each FOREGROUND, explicit generous timeout, read each exit code)

From the worktree root: `uv run ruff check`, `uv run ruff format --check`, `uv run pytest`
(full suite, ~3.5 min, **with the fake seedbox up** so integration tests actually run — report the
skip count; it should be 0 or explain each). Frontend gates only if you touched frontend files
(you probably need to for the error label — then `npm ci` in `frontend/` first, `npm run lint`,
`npx tsc -b`, `npm test`).

## When done

1. Update this prompt's frontmatter (`status`, `completed`, `result`) and move it to
   `prompts/done/` (or `failed/`) inside your worktree.
2. **Do not commit.** Report back: what lftp's grammar actually is, the fix, proof the new tests
   failed before and pass after, gate results with exit codes and skip counts, the exact file list,
   and a proposed message (`fix:` prefix, no `Co-authored-by:`), plus your worktree path/branch.
