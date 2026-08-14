import { useMemo, useState } from 'react'
import { previewResetByPattern, resetByPattern, resetItem, resetQueue } from '../api/client'
import type { FileNode, ResetPatternPreviewItem, ResetSummaryResponse, SyncMode } from '../api/types'
import { describeResetTargets } from '../lib/resetComposition'
import { resetWarningLines } from '../lib/resetWarning'

/** `remote_size` is `null` only for a node never tracked remotely -- the identical reading
 * `FileTree.tsx.hasRemoteCopy` uses for the same question; duplicated here (one line) rather
 * than exported across files for a helper this small.
 */
function hasRemoteCopy(node: { remote_size: number | null }): boolean {
  return node.remote_size != null
}

/** Whether a node currently has an active job -- the identical reading `FileTree.tsx.
 * hasActiveJob` uses for the same question (there, to decide whether "Delete" makes sense to
 * offer); duplicated here for the same one-line reason `hasRemoteCopy` above is. Selected items
 * that are actively transferring are excluded from this scope's targets rather than offered and
 * bounced off a 409 -- `core/local_delete.py`'s own "refuse, don't race" guard.
 */
function hasActiveJob(node: { state: string }): boolean {
  return node.state === 'QUEUED' || node.state === 'DOWNLOADING'
}

const buttonClasses =
  'rounded-md border border-violet-300 px-2 py-1 text-xs font-medium text-violet-700 hover:bg-violet-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-violet-800 dark:text-violet-300 dark:hover:bg-violet-950'
const panelClasses =
  'flex flex-col gap-2 rounded-md border border-violet-300 bg-violet-50 px-3 py-2 text-sm dark:border-violet-800 dark:bg-violet-950/40'
const inputClasses =
  'rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'
const confirmButtonClasses =
  'rounded-md bg-violet-700 px-2 py-1 text-xs font-medium text-white hover:bg-violet-800 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-violet-800 dark:hover:bg-violet-700'

function WithheldList({ withheld }: { withheld: { rel_path: string; reason: string }[] }) {
  if (withheld.length === 0) return null
  return (
    <ul className="list-disc space-y-0.5 pl-5 text-xs">
      {withheld.map((w) => (
        <li key={w.rel_path}>
          <span className="font-mono">{w.rel_path}</span> — {w.reason}
        </li>
      ))}
    </ul>
  )
}

type Scope = 'all' | 'pattern' | 'selected'

function ScopeButton({
  label,
  active,
  onClick,
  disabled,
  disabledReason,
}: {
  label: string
  active: boolean
  onClick: () => void
  disabled?: boolean
  disabledReason?: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={disabled ? disabledReason : undefined}
      aria-pressed={active}
      className={`rounded-md border px-2 py-1 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-50 ${
        active
          ? 'border-violet-700 bg-violet-700 text-white dark:border-violet-600 dark:bg-violet-800'
          : 'border-violet-300 text-violet-700 hover:bg-violet-100 dark:border-violet-800 dark:text-violet-300 dark:hover:bg-violet-900'
      }`}
    >
      {label}
    </button>
  )
}

interface BulkResetFailure {
  rel_path: string
  error: string
}

/** The two shapes a completed reset can report -- `'summary'` for All/Pattern (the server
 * already resets every target in one call and reports `withheld`), `'bulk'` for Selected (this
 * component resolves it to one `resetItem` call per row, `Promise.allSettled`, the identical
 * shape `FileTree.tsx`'s own bulk Queue/Stop/Delete already use -- there is no bulk reset
 * endpoint, per `api/jobs.py.reset_item`'s own docstring). One `outcome` slot rather than three
 * separate ones (the pre-unification shape) since only one scope is ever active at a time.
 */
type ResetOutcome =
  | { kind: 'summary'; response: ResetSummaryResponse }
  | { kind: 'bulk'; total: number; succeeded: number; failures: BulkResetFailure[] }

/** "Reset item tracking" -- one control, three scopes (2026-08-14,
 * `prompts/2026-08-14-reset-panel-counts-and-layout.md`; see `prompts/done/
 * 2026-08-13-reset-item-tracking.md` for the original warning-content reasoning, unchanged
 * here). Previously three near-identical UIs -- whole-queue and purge-by-pattern here, plus a
 * third "selected items" panel living entirely in `FileTree.tsx` -- that read as the same
 * feature with different ceremony, which is why a live user could not tell them apart, could
 * not dismiss the pattern panel without running a preview first (its Cancel/Close controls both
 * lived inside `preview &&` branches), saw a bare, uncomposed `{n} items` count that rendered as
 * nonsense at zero, and had to fight a `flex-col` label that rendered its confirmation sentence
 * across three separate lines. This component replaces all of that with: a scope selector
 * (**All / Pattern / Selected**), a **Cancel that is always present** once the box is open (at
 * every stage, including before any preview has run), and the identical
 * **choose scope → preview → confirm** flow for every scope, with `lib/resetWarning.ts` and
 * `lib/resetComposition.ts` as the two single sources of truth every scope reads its wording
 * from, so no two scopes can ever quietly disagree about counts or consequences.
 *
 * **The whole-queue scope's typed-name confirmation is deliberately still here, as one cleanly
 * removable stage** (`docs/decisions.md`, 2026-08-14) -- it used to be justified by whole-queue
 * reset having *no* preview at all (a blind "forget everything" with nothing on screen to
 * review); now that every scope previews first, the review already *is* the confirmation, the
 * same argument `api/jobs.py.reset_by_pattern`'s own docstring makes for the pattern scope. It
 * stays for now because the server (`QueueResetRequest.confirm_name`) still requires it -- see
 * that model's own docstring -- and weakening a server-side guard is not this task's call to
 * make. Deleting it later should be a small diff: the `confirmStage === 'typed-name'` branch
 * below, plus the `api/jobs.py`/`models.py` check it satisfies.
 */
export function QueueResetControls({
  queueId,
  queueName,
  syncMode,
  autoQueueEnabled,
  scanIntervalS,
  nodes,
  selected,
  onSelectionChange,
}: {
  queueId: number
  queueName: string
  syncMode: SyncMode
  autoQueueEnabled: boolean
  scanIntervalS: number | null
  nodes: FileNode[]
  /** The Files-page selection (2026-08-14) -- lifted to `FilesPage.tsx`, the one component that
   * already renders both this control and `FileTree.tsx`, and passed down to both rather than
   * tracked twice. `FileTree.tsx` still owns the *mechanics* of selecting (click, shift-range)
   * but no longer owns the *state* -- see `docs/decisions.md` for why a second, independent
   * selection store here was rejected outright rather than worked around.
   */
  selected: Set<string>
  onSelectionChange: (next: Set<string>) => void
}) {
  const ctx = { syncMode, autoQueueEnabled, scanIntervalS }

  // Only top-level entries are "items" in the sense the All/Pattern scopes actually target
  // (DESIGN.md §4.7; the same filter `api/settings.py.pattern_preview` applies to its own
  // remote-tree read) -- a nested file's presence/absence isn't what either warning is about.
  // Selected is different on purpose: a user can check a nested file directly, and each checked
  // row resets individually regardless of depth (`api/jobs.py.reset_item`'s own docstring), so
  // `eligibleSelected` below is deliberately *not* filtered to top-level.
  const topLevel = useMemo(() => nodes.filter((n) => !n.rel_path.includes('/')), [nodes])

  const selectedEntries = useMemo(
    () => nodes.filter((n) => n.id != null && selected.has(n.rel_path)),
    [nodes, selected],
  )
  const eligibleSelected = useMemo(
    () => selectedEntries.filter((n) => !hasActiveJob(n)),
    [selectedEntries],
  )
  const activeSelectedCount = selectedEntries.length - eligibleSelected.length

  const [open, setOpen] = useState(false)
  const [scope, setScope] = useState<Scope | null>(null)

  const [pattern, setPattern] = useState('')
  const [patternPreview, setPatternPreview] = useState<ResetPatternPreviewItem[] | null>(null)
  const [patternPreviewBusy, setPatternPreviewBusy] = useState(false)
  const [patternPreviewError, setPatternPreviewError] = useState<string | null>(null)

  // The All scope's own removable stage (module docstring above) -- 'preview' is every scope's
  // normal resting state; 'typed-name' only ever applies when `scope === 'all'`.
  const [confirmStage, setConfirmStage] = useState<'preview' | 'typed-name'>('preview')
  const [confirmName, setConfirmName] = useState('')

  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [outcome, setOutcome] = useState<ResetOutcome | null>(null)

  const resetScopeState = () => {
    setPattern('')
    setPatternPreview(null)
    setPatternPreviewError(null)
    setConfirmStage('preview')
    setConfirmName('')
    setActionError(null)
  }

  const openPanel = () => {
    setOpen(true)
    setScope(null)
    resetScopeState()
  }

  // The always-present Cancel (the defect this task fixes: the old panels' dismiss controls
  // only rendered inside `preview &&` branches, so a panel opened by mistake could not be
  // closed without running a preview first). Closes the whole box back to the trigger button,
  // regardless of scope or stage -- it never touches `outcome`, which is a separate, dismissible
  // banner that should survive the box closing, same as before this task.
  const cancel = () => {
    setOpen(false)
    setScope(null)
    resetScopeState()
  }

  const selectScope = (next: Scope) => {
    setScope(next)
    resetScopeState()
  }

  const runPatternPreview = async () => {
    const trimmed = pattern.trim()
    if (!trimmed) return
    setPatternPreviewBusy(true)
    setPatternPreviewError(null)
    setPatternPreview(null)
    try {
      const result = await previewResetByPattern(queueId, { pattern: trimmed })
      setPatternPreview(result.items)
    } catch (err) {
      setPatternPreviewError(err instanceof Error ? err.message : String(err))
    } finally {
      setPatternPreviewBusy(false)
    }
  }

  // What the currently-open scope's preview is showing -- `null` means "no preview available
  // yet" (Pattern before its first successful Preview click), as opposed to a preview that ran
  // and matched nothing (`[]`), which is what drives `lib/resetWarning.ts`'s own zero branch.
  const targets: { rel_path: string; is_dir: boolean; remote_size: number | null }[] | null =
    scope === 'all' ? topLevel : scope === 'selected' ? eligibleSelected : patternPreview

  const remoteCount = (targets ?? []).filter(hasRemoteCopy).length

  const confirmAllReset = async () => {
    if (confirmName !== queueName) return
    setBusy(true)
    setActionError(null)
    try {
      const response = await resetQueue(queueId, { confirm_name: confirmName })
      setOutcome({ kind: 'summary', response })
      cancel()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const confirmPatternReset = async () => {
    const trimmed = pattern.trim()
    if (!trimmed || patternPreview == null) return
    setBusy(true)
    setActionError(null)
    try {
      const response = await resetByPattern(queueId, { pattern: trimmed })
      setOutcome({ kind: 'summary', response })
      cancel()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const confirmSelectedReset = async () => {
    if (eligibleSelected.length === 0) return
    setBusy(true)
    setActionError(null)
    try {
      const results = await Promise.allSettled(eligibleSelected.map((e) => resetItem(e.id as number)))
      const failures: BulkResetFailure[] = []
      const succeededPaths = new Set<string>()
      results.forEach((result, i) => {
        const entry = eligibleSelected[i]
        if (result.status === 'fulfilled') succeededPaths.add(entry.rel_path)
        else {
          failures.push({
            rel_path: entry.rel_path,
            error: result.reason instanceof Error ? result.reason.message : String(result.reason),
          })
        }
      })
      // Only the rows that actually succeeded drop out of the selection -- a failure stays
      // checked so a retry is one click away, the same convention `FileTree.tsx`'s own bulk
      // Queue/Stop/Delete already use.
      const nextSelection = new Set(selected)
      for (const path of succeededPaths) nextSelection.delete(path)
      onSelectionChange(nextSelection)
      setOutcome({ kind: 'bulk', total: eligibleSelected.length, succeeded: succeededPaths.size, failures })
      cancel()
    } finally {
      setBusy(false)
    }
  }

  const confirmAction =
    scope === 'all'
      ? () => setConfirmStage('typed-name')
      : scope === 'pattern'
        ? confirmPatternReset
        : confirmSelectedReset

  const confirmLabel = (() => {
    if (busy) return 'Resetting…'
    if (targets == null) return 'Continue'
    if (targets.length === 0) return 'Nothing to reset'
    if (scope === 'all') return 'Continue'
    return `Reset these ${targets.length}`
  })()

  return (
    <div className="flex flex-col gap-2">
      {!open && (
        <div>
          <button type="button" onClick={openPanel} className={buttonClasses}>
            Reset item tracking…
          </button>
        </div>
      )}

      {open && (
        <div className={panelClasses}>
          {/* The scope selector plus the always-present Cancel -- "all/pattern/selected and
              cancel on the main box," per the user's own request. Cancel sits here, outside
              every scope-specific branch below, so it exists at every stage including before
              any scope has even been chosen. */}
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[10px] font-medium tracking-wide text-violet-700 uppercase dark:text-violet-300">
              Scope
            </span>
            <ScopeButton label="All" active={scope === 'all'} onClick={() => selectScope('all')} />
            <ScopeButton label="Pattern" active={scope === 'pattern'} onClick={() => selectScope('pattern')} />
            <ScopeButton
              label="Selected"
              active={scope === 'selected'}
              onClick={() => selectScope('selected')}
              disabled={selected.size === 0}
              disabledReason="Select rows in the Files table first"
            />
            <button type="button" onClick={cancel} className="ml-auto rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900">
              Cancel
            </button>
          </div>

          {scope == null && (
            <p className="text-violet-900 dark:text-violet-200">
              Choose a scope above to preview what will be reset for <strong>{queueName}</strong>.
            </p>
          )}

          {scope === 'pattern' && (
            <div className="flex flex-col gap-2">
              <p className="text-violet-900 dark:text-violet-200">
                Reset tracking for every top-level item in <strong>{queueName}</strong> whose name
                matches a pattern (case-insensitive; glob with <span className="font-mono">*?[</span>,
                plain substring otherwise -- the identical matching auto-queue's own select/skip
                patterns use).
              </p>
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={pattern}
                  onChange={(e) => {
                    setPattern(e.target.value)
                    setPatternPreview(null)
                    setPatternPreviewError(null)
                  }}
                  placeholder="e.g. Show.S01* or a substring"
                  className={inputClasses}
                  aria-label="Pattern to purge"
                />
                <button
                  type="button"
                  onClick={runPatternPreview}
                  disabled={!pattern.trim() || patternPreviewBusy}
                  className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
                >
                  {patternPreviewBusy ? 'Previewing…' : 'Preview'}
                </button>
              </div>
              {patternPreviewError && <p className="text-red-700 dark:text-red-300">{patternPreviewError}</p>}
            </div>
          )}

          {scope === 'selected' && activeSelectedCount > 0 && (
            <p className="text-violet-900 dark:text-violet-200">
              {activeSelectedCount} of {selectedEntries.length} selected{' '}
              {activeSelectedCount === 1 ? 'row is' : 'rows are'} transferring right now and{' '}
              {activeSelectedCount === 1 ? 'is' : 'are'} excluded from this reset -- stop{' '}
              {activeSelectedCount === 1 ? 'it' : 'them'} first if you want{' '}
              {activeSelectedCount === 1 ? 'it' : 'them'} included.
            </p>
          )}

          {/* Every scope from here down reads the identical two functions -- neither knows or
              cares which scope is asking. */}
          {targets != null && (
            <>
              {scope === 'pattern' && targets.length > 0 && (
                <ul className="max-h-40 list-disc space-y-0.5 overflow-y-auto pl-5 text-xs">
                  {targets.map((item) => (
                    <li key={item.rel_path} className="font-mono">
                      {item.rel_path}
                    </li>
                  ))}
                </ul>
              )}
              <p className="font-medium text-violet-900 dark:text-violet-200">{describeResetTargets(targets)}</p>
              {resetWarningLines(targets.length, remoteCount, ctx).map((line) => (
                <p key={line} className="text-violet-900 dark:text-violet-200">
                  {line}
                </p>
              ))}

              {confirmStage === 'preview' && (
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={confirmAction}
                    disabled={busy || targets.length === 0}
                    title={targets.length === 0 ? 'Nothing matches this scope -- there is nothing to reset' : undefined}
                    className={confirmButtonClasses}
                  >
                    {confirmLabel}
                  </button>
                </div>
              )}

              {/* The whole-queue scope's typed-name stage (module docstring above) -- reached
                  only via the "Continue" click above, only for `scope === 'all'`. */}
              {scope === 'all' && confirmStage === 'typed-name' && (
                <>
                  <label className="flex flex-col gap-1 text-violet-900 dark:text-violet-200">
                    <span>
                      Type the queue name (<span className="font-mono">{queueName}</span>) to confirm:
                    </span>
                    <input
                      type="text"
                      value={confirmName}
                      onChange={(e) => setConfirmName(e.target.value)}
                      className={inputClasses}
                      placeholder={queueName}
                      aria-label="Type the queue name to confirm"
                    />
                  </label>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={confirmAllReset}
                      disabled={confirmName !== queueName || busy}
                      className={confirmButtonClasses}
                    >
                      {busy ? 'Resetting…' : 'Reset this queue'}
                    </button>
                  </div>
                </>
              )}
            </>
          )}

          {actionError && <p className="text-red-700 dark:text-red-300">{actionError}</p>}
        </div>
      )}

      {outcome && (
        <div className="rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-xs text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200">
          <div className="flex items-center justify-between gap-3">
            <span>
              {outcome.kind === 'summary' ? (
                <>
                  Reset {outcome.response.reset_top_level} item(s) ({outcome.response.affected_count} row(s)
                  forgotten across item/item_settle/deleted_archive)
                  {outcome.response.withheld.length > 0 && `, ${outcome.response.withheld.length} withheld`}.
                </>
              ) : (
                <>
                  Reset {outcome.succeeded} of {outcome.total} item(s)
                  {outcome.failures.length > 0 && `, ${outcome.failures.length} failed`}.
                </>
              )}
            </span>
            <button
              type="button"
              onClick={() => setOutcome(null)}
              className="shrink-0 text-xs underline decoration-dotted"
            >
              Dismiss
            </button>
          </div>
          {outcome.kind === 'summary' && <WithheldList withheld={outcome.response.withheld} />}
          {outcome.kind === 'bulk' && outcome.failures.length > 0 && (
            <ul className="list-disc space-y-0.5 pl-5 text-xs">
              {outcome.failures.map((f) => (
                <li key={f.rel_path}>
                  <span className="font-mono">{f.rel_path}</span> — {f.error}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
