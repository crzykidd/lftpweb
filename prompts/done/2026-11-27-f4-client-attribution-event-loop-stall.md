---
name: 2026-11-27-f4-client-attribution-event-loop-stall
status: completed
created: 2026-11-27
model: sonnet
completed: 2026-11-27
result: indexed _write_client_attribution; 3000x1000 0.457s -> 0.007s; 2454 passed
---

# Task: F4, stop `_write_client_attribution` from blocking the event loop for about 5 s every poll

**Read F4 in `prompts/test-findings-2026-11-27.md` first.** It has the measurements. The test
system's server freezes for about 5 s roughly every 11 s. asyncio debug blames
`ClientSyncScheduler._loop`. A cProfile of `run_once` in the container puts almost all of that
time in `core/clientsync.py._write_client_attribution`, and specifically in its nested loop:
every transfer this pass reported (1,005 from rTorrent) against every candidate `item` row
(about 3,000). That comes to about 3 M `settle._client_content_path_matches` calls per pass,
in pure Python, on the event loop.

**The user's hard rule is that nothing breaks for an existing install.** The attribution
results must be identical: the same items get the same `download_client_id`, in the same
cases. It just has to be fast.

## Before you start

- Read `CLAUDE.md`.
- Read `_write_client_attribution` and its whole docstring, including the write-once rule, the
  "path only" rule, and the "every transfer, not just Preflight phases" rule. Read its caller
  in `_update_preflight` as well.
- Read `core/settle.py._client_content_path_matches` (the component-boundary rule).
- Read `git show 7c26bcf` and `git show 9f6cb6a`, which made the most recent changes to this
  file and to `engine.py`.
- Work in **this checkout on `dev`**. Do not create a worktree. Ignore `.claude/worktrees/`.

## Working tree check

Run `git status --porcelain`. Only `.claude/worktrees/`, this prompt and
`prompts/test-findings-2026-11-27.md` should be dirty. If anything else is, stop and report it.

## What to do

1. **Index, don't scan.** Once per call, build a dict that maps each candidate's normalised
   remote path (`queue remote_path.rstrip("/") + "/" + rel_path`, trailing slash stripped) to
   that candidate. Keep the candidate's position in the query order alongside it.
2. **For each transfer with a `content_path`**, normalise it the way `_client_content_path_matches`
   does. Then look it up, followed by each ancestor obtained by stripping one `/` component at a
   time, stopping above the shortest queue root. This reproduces "equal, or `content_path` sits
   at a component boundary under the item path" exactly.
3. **Pick the same winner as today.** Today the loop takes the **first candidate in query order**
   that matches and hasn't already matched this pass, then `break`s. When several ancestors are
   in the index (for example a top-level item and a nested row), choose the one with the lowest
   query-order position that isn't already in `matched_this_pass`. Preserve the `UPDATE`,
   commit, write-once and overwrite-a-different-instance behaviour exactly.
4. **Equivalence test.** Write a test that drives the old algorithm, kept as a test-only
   reference function or reconstructed inline, and the new one over randomised and adversarial
   inputs. Assert they produce the same set of `(item_id → instance)` writes. The inputs must
   include:
   - sibling-prefix names (`Show.S01` vs `Show.S01.EXTRA`)
   - trailing slashes
   - nested rows
   - a file-level `content_path`
   - two queues with nested remote roots
   - items already attributed to this instance or another one
   - transfers with no `content_path`
5. **Performance test.** With 3,000 candidates and 1,000 transfers, the call must finish well
   under 0.2 s. Give it a generous bound so the test isn't flaky in CI.
6. **Look for the same O(n×m) shape elsewhere** in this pass: `_attribution_sample`,
   `_update_preflight`'s queue-level path matching, and `finished_transfers` lookups. Fix any
   that would cost more than about 50 ms at this scale with the same index approach, or leave
   it and report the number. Don't fix things that are fine.
7. **Docs, in the same commit:**
   - `CHANGELOG.md` `[Unreleased]` Fixed, in user words: "Pages no longer pause for several
     seconds every few seconds on installs with many torrents: matching downloads to items is
     now indexed."
   - The function's docstring.
   - A `docs/decisions.md` entry.
   - The findings file: mark F4 **done**.

## Conventions to honor

- Run every verification gate in the **foreground** with an explicit timeout (pytest: 600000 ms).
  You will never receive a background-task notification, so read each exit code yourself.
- From the **repo root**, run each of these and read its exit code separately:
  - `uv run ruff check backend tests`
  - `uv run ruff format --check backend tests`
  - `uv run pytest`: the fake seedbox containers are up, and the target is **0 skipped**. The
    baseline is 2452 passed.
- Report every gate's exit code and the counts, plus the before and after timing from your
  performance test.

## When done

1. Set this file's frontmatter (`status`, `completed`, `result`) and move it into
   `prompts/done/` (it is untracked, so a plain `mv` is fine).
2. **Do not commit.** Report back:
   - what changed
   - the before and after timing
   - anything that could behave differently for an existing install
   - the gate results
   - the explicit list of paths for a single commit, including this prompt in `prompts/done/`
     and the findings file
   - a proposed one-line `fix:` message with no `Co-authored-by` trailer
