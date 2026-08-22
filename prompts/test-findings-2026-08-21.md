# Browser test findings — 2026-08-21 `:dev` round

Live findings from the user testing the 11 commits pushed after v0.3.0 (`1791af8`…`60f174f`).
**Collecting first, fixing later** — the point is to batch these into as few handoff prompts as
possible rather than one prompt per nit.

Status key: **open** = recorded, not started · **prompted** = folded into a handoff prompt ·
**done** = fixed and committed.

## ✅ Confirmed working (user-verified in a browser, 2026-08-21)

- **Paused rows show `QUEUED n%` with their progress** (`32cf5fd`). This was the one item shipped
  with a visual nobody had ever seen — the `QUEUED` fill uses its own **indigo** pair rather than
  `PARTIAL`/`WAITING`'s confirmed amber, chosen from `StateChip.tsx`'s "never a different hue" rule
  but never rendered. **Now witnessed.** No change needed; do not "fix" the colour.
- **Queue-tab "Rescan now"** (`0a0c48b`) — works. Also exercises the shared `useRescan` hook the
  Files tab now runs through.
- **Dashboard long ranges / total downloaded** (`8ae1e53`) — works. Bucketing feedback is finding 5,
  not a defect.

---

## 1. Bandwidth slider must not exceed the Settings → Transfer maximum — **done**

> *"The max on this should never exceed the max set in transfer settings. That is the max."*

Today the Queue-tab slider's ceiling is `max(125 MB/s, current value)` — an invented bound, so the
slider can raise the site limit above whatever Settings → Transfer holds. The user's model is that
**Settings → Transfer defines the ceiling** and the Queue slider throttles *within* it.

**This is a data-model change, not a bounds tweak — resolve it before building.** There is
currently **one** value: `max_bandwidth_bps`, owned by `SchedulerSettings`/`TransferSettings` and
edited by both surfaces (that was the explicit design in
`prompts/done/2026-08-21-bandwidth-from-the-queue-page.md` — "one control, one setting").

Capping the slider at "the value the slider itself edits" is circular: lower it once and the
ceiling drops with it, so it can never be raised back from the Queue page. **A ratchet.** So the
fix needs two values:

- **Hard max** (Settings → Transfer) — the ceiling. Unchanged in meaning.
- **Effective/current limit** (Queue slider) — bounded to `[min_share_floor_bps, hard max]`, and
  what the scheduler actually allocates against.

**Built 2026-08-21** (`prompts/done/2026-08-21-bandwidth-ceiling-and-autocommit.md`), together
with finding 4 — they are the same control. `TransferSettings` now carries a
`throttle_bandwidth_bps` alongside the `max_bandwidth_bps` ceiling, clamped to it on every write;
`effective_bandwidth_bps()` is what `admit()`, the fast-lane reserve and Start-now fractions all
read. No migration (JSON settings row, absent key = `None` = follow the ceiling). `DESIGN.md`
§4.5's "The ceiling and the throttle" and `docs/decisions.md` carry the reasoning.

**Answered by the user, 2026-08-21:** the Queue throttle **persists across server restarts** —
*"if I drag to 10mb then it stays till I move back to max."* So it is a stored setting, not a
session-scoped or auto-reverting throttle, and nothing resets it on restart or on an empty queue.

That also confirms the two-value model is the right one: because the **ceiling stays fixed at the
Transfer max**, the slider can always be dragged back up to it. Under the current one-value design
it could not, which is the ratchet described above.

Remaining questions for that work:
- Lowering the hard max below the current throttle must **clamp** the throttle — assert it.
- Settings → Transfer then shows/edits only the max; the two surfaces stop being the same number,
  so the "both surfaces reflect each other" behavior needs restating rather than deleting.

## 2. "Pause after current" should not be offered when nothing is running — **open**

With zero running transfers, *Pause after current* and *Pause now* are the same action, so offering
both is noise at best and misleading at worst. Hide it (or disable it with a reason) when the
running count is zero.

Interacts with the duration dropdown from `1791af8` — the two live in the same control row.

**Superseded in shape by finding 3** — under that redesign the thing to hide/disable when nothing is
running is the **"Pause after active" checkbox**, not a menu entry. Same rule, new control. Fix the
two together.

## 3. Redesign the pause control as one dropdown + a checkbox — **open**

> *"The pause for x minutes dialog is confusing. Currently I select it and then I have to hit the
> pause button, but really it should just do a pause on selection of an item. … Maybe just a simple
> pause drop down for it all. With a check box next to it that says Pause after active. Deselected
> by default. Then you hit pause and it shows a drop down list. Till I unpause, 1 min, 10 min, etc."*

**The problem:** today it is two controls and two steps — a duration `<select>`, then a separate
`PauseMenu` button that itself asks *after current* vs *now*. Picking a duration does nothing until
you click Pause, which is the confusing part.

**The target shape:**

- **One Pause control.** Clicking it opens a list: **"Till I unpause"** (first, the default), then
  **1 min / 10 min / 30 min / 60 min**.
- **Selecting an entry pauses immediately.** No second click, no confirm step. The selection *is*
  the action.
- **A checkbox beside it: "Pause after active", deselected by default.** That checkbox — not a
  second menu — is what chooses *pause after current* vs *pause now*. Default (unchecked) is pause
  now.

**Net effect:** one click to open, one click to act, with the mode as a persistent visible toggle
rather than a fork buried in a menu. Replaces both the duration `<select>` and the two-entry
`PauseMenu` from `1791af8`/`07e2471`.

Backend is unaffected — both entry modes and the `paused_until` deadline already exist and keep
their semantics. **This is a frontend control redesign**, plus finding 2's rule applied to the new
checkbox.

## 4. Bandwidth slider: too many clicks — checkbox + debounced auto-commit — **done**

> *"The bandwidth slider design is confusing. It is too many clicks. Again probably a check box
> apply to new items only… and when I drag it, after an x second wait to make sure it isn't changed
> more, we just execute the change."*

Same simplification as finding 3, applied to the other new control. Today: drag → two buttons plus
Cancel appear → click one → the in-progress path then shows an amber confirmation → confirm. Four
interactions to change a number.

**The target shape:**

- **A checkbox beside the slider: "Apply to new items only."** It replaces the two-button fork —
  checked means future admissions only, unchecked means also re-admit what is running.
- **Dragging commits on its own.** After ~x seconds of no further movement, the change executes.
  No Apply button, no Cancel.
- Pairs with finding 1's ceiling change (slider bounded by the Settings → Transfer max).

**The tension to resolve before building, flagged not resolved:** the unchecked path **interrupts
every running transfer**. Auto-commit plus an unchecked box means a stray drag restarts every
transfer with no confirmation step — the amber dialog exists today precisely because that is the
one visibly destructive control on the page.

Recommended resolution, for the user to accept or overrule: **default the checkbox to checked
("new items only")** so the interrupting path requires a deliberate uncheck, and treat that uncheck
as the act of consent rather than confirming on every drag. That keeps the click count at the
user's target for the safe path while not making transfer-wide interruption a thing that can happen
by accident.

**Accepted by the user, 2026-08-21** — checkbox defaults to checked, and the pre-action confirmation
is replaced by **post-action feedback**:

> *"Can we then show a banner that says bandwidth set to xx for all new items — and if it was
> checked, bandwidth set to xx and jobs restarted? Or something."*

So the amber confirm-before dialog goes away entirely and a result banner takes its place, worded
per path:

| Path | Banner |
|---|---|
| Checked (default) | "Bandwidth set to **10 MB/s** for all new transfers." |
| Unchecked | "Bandwidth set to **10 MB/s** — **N** running transfers restarted." |
| Unchecked, queue paused | Must say nothing was restarted, since the existing backend deliberately skips re-admission while paused and returns `skipped_because_paused` (`764aaa7`). Do **not** report a restart that did not happen. |

The count is already available — `POST /api/queue/bandwidth` returns it — so the banner should state
the real number rather than a generic "transfers restarted."

**Built 2026-08-21** (`prompts/done/2026-08-21-bandwidth-ceiling-and-autocommit.md`): checkbox
checked by default, no Apply/Cancel, no amber dialog, one banner that counts five seconds down in
place and then becomes the result line (with the server's real restart count, and the paused case
stated as "nothing was restarted"). The debounce/countdown/banner wording is pure and unit-tested
in `lib/bandwidth.ts`.

**Pending state — settled by the user, 2026-08-21.** *"Banner pops on change, with 'bandwidth update
applied in 5 seconds', then the update to applied."*

**One banner, two states, in place** — it does not appear twice or stack:

1. **On slider change (immediate):** "Bandwidth update applied in **5** seconds…", counting down.
2. **On commit:** the same banner becomes the result line from the table above.

Notes for whoever builds it:

- **The debounce is 5 s and visible**, which is why it can be that long — a silent 5 s wait would
  feel broken, a counted-down one reads as deliberate.
- **Moving the slider again restarts the countdown**, which gives a cancel affordance for free: keep
  dragging, or drag back to where you started, and nothing commits. No Cancel button needed — that
  would add back the click this finding exists to remove.
- The countdown is also the last moment to change the checkbox; decide whether toggling it mid-count
  resets the timer (probably yes — it changes what is about to happen).

## 5. Dashboard chart: per-range default bucketing, plus a "group by" dropdown — **open**

> *"On dashboard this works, but maybe we do a dropdown or change the groups. 24h = hourly. 7 day =
> daily. 30 day maybe daily, but 90 day and yearly is weekly I think… might be good to default to
> those but have a dropdown for group by hour/day/week/month."*

Not a defect — the ranges work. This is about bucket width being fixed per range today.

**What the buckets actually are now** (`api/metrics.py`, `_RANGES` / `_DAILY_RANGES`):

| Range | Today | User's preference |
|---|---|---|
| `1h` | 60 x 1-minute | — |
| `12h` | 48 x 15-minute | — |
| `24h` | 24 x 1-hour | hourly ✅ already |
| `7d` | **28 x 6-hour** | **daily** — a change |
| `30d` | 30 x 1-day | daily ✅ already |
| `90d` | 90 x 1-day | **weekly** |
| `1y` | 365 x 1-day | **weekly** (or monthly) |

So: `24h` and `30d` already match; `7d` needs to move from 6-hour to daily; `90d`/`1y` need weekly
defaults. Then a **group-by dropdown (hour / day / week / month)** over the top, with those as the
per-range defaults rather than the only option.

**The constraint that must shape the dropdown: not every grouping is available at every range.**
Raw tables are capped at 30 days retention, and `metric_daily` is one-day granularity by
construction. So **hourly grouping is impossible for `90d`/`1y`** — there is no sub-day data that
far back, and no setting can produce it. The dropdown must **disable unavailable groupings with a
reason** rather than offering them and silently returning something else. (Same discipline as the
connector capability rule in `docs/download-client-api-survey.md` §4 — a missing capability
disables a control, it never fakes one.)

Cheap half: **week and month are derived by summing daily rows on read**, which is exactly what the
rollup design anticipated ("weekly is derivable by summing daily; keeping both risks the two
disagreeing" — `prompts/done/2026-08-21-daily-metric-rollups.md`). No new table, no new migration.

Structural note: `_RANGES` currently couples range to bucket width as one tuple. A group-by control
means decoupling those two — range says *how far back*, grouping says *how wide a bar*. Worth doing
deliberately rather than adding more entries to the dict.

## 6. The poll-cadence setting is unfindable and unexplained — **open**

> *"I don't know what the poll cadence setting is or where to find it."*

It is at **Settings → Integrations**, top card, headed **"Poll cadence"**, field *"Poll interval
(seconds)"*, with helper text naming the 10s default and 5s floor (`IntegrationsTab.tsx`). The
author of that card knew what it meant; the user reading the page did not — which is the whole
problem.

Two separable defects:

1. **The name says nothing about what is polled.** "Poll cadence" is internal vocabulary. Nothing in
   the heading or the field label mentions Sonarr/Radarr, so on a page full of *arr configuration it
   still reads as unrelated plumbing. Something like **"How often to check Sonarr/Radarr"** names the
   thing being done and the thing it is done to.
2. **It does not say what changing it affects.** The two user-visible symptoms this setting governs
   are exactly the ones in issue #16: how smoothly Preflight progress ticks, and how quickly a
   finished item leaves "Awaiting import." The help text talks about the floor and the default —
   mechanism, not consequence. It should say what gets faster, and note the cost (more requests to
   the *arr).

Worth checking placement too: "top card" was the implementer's choice, and a site-wide cadence knob
sitting above the instance list may read as belonging to the first instance rather than to all of
them.

**Related and worth folding in:** `FieldHelp.tsx` exists for exactly this kind of explanation and is
already used elsewhere in Settings — reuse it rather than growing the paragraph under the field.
