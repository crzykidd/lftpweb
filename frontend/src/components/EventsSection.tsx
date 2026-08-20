import { useVirtualizer } from '@tanstack/react-virtual'
import { useEffect, useMemo, useRef, useState } from 'react'
import { clearHistoryEvent, clearHistoryEvents, getHistoryEvents } from '../api/client'
import type { HistoryEventOut, HistoryEventsFilter, PathQueueOut } from '../api/types'

const inputClasses =
  'rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'

// The kinds `core/audit.py`'s callers actually emit today (core/postprocess.py) -- offered as
// quick-pick options, not an exhaustive enum enforced by the backend (which accepts any
// string). The three delete kinds are what DESIGN.md §7.3 calls "the delete audit trail";
// grouped visually apart from the rest via LEVEL_STYLES below.
const KNOWN_KINDS = [
  'verify',
  'extract',
  'move',
  'move_failed',
  'remote_delete',
  'remote_delete_withheld',
  'remote_delete_failed',
]

const DELETE_KINDS = new Set(['remote_delete', 'remote_delete_withheld', 'remote_delete_failed'])

const LEVEL_STYLES: Record<string, string> = {
  debug: 'text-zinc-400 dark:text-zinc-600',
  info: 'text-zinc-600 dark:text-zinc-300',
  warning: 'text-amber-700 dark:text-amber-400',
  error: 'text-red-700 dark:text-red-400',
}

function formatTs(ts: string): string {
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString()
}

type VirtualRow =
  | { kind: 'header'; queueId: number | null; queueName: string }
  | { kind: 'event'; event: HistoryEventOut }

const UNASSOCIATED_LABEL = '(no queue -- item or queue since removed)'

function groupByQueue(events: HistoryEventOut[]): VirtualRow[] {
  const rows: VirtualRow[] = []
  let currentQueueId: number | null | undefined
  for (const event of events) {
    if (event.queue_id !== currentQueueId) {
      rows.push({
        kind: 'header',
        queueId: event.queue_id,
        queueName: event.queue_name ?? UNASSOCIATED_LABEL,
      })
      currentQueueId = event.queue_id
    }
    rows.push({ kind: 'event', event })
  }
  return rows
}

function EventRow({
  event,
  onClearRequest,
}: {
  event: HistoryEventOut
  onClearRequest: (event: HistoryEventOut) => void
}) {
  const isDelete = DELETE_KINDS.has(event.kind)
  return (
    <div
      className={`flex flex-col gap-0.5 border-b border-zinc-100 px-3 py-2 text-sm dark:border-zinc-900 ${
        isDelete ? 'bg-amber-50/60 dark:bg-amber-950/10' : ''
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className={`shrink-0 text-xs font-semibold uppercase ${LEVEL_STYLES[event.level] ?? ''}`}>
          {event.level}
        </span>
        <span className="shrink-0 rounded bg-zinc-100 px-1.5 py-0.5 text-xs font-medium text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
          {event.kind}
        </span>
        {event.rel_path && (
          <span className="min-w-0 truncate text-xs text-zinc-500 dark:text-zinc-400" title={event.rel_path}>
            {event.rel_path}
          </span>
        )}
        <span className="ml-auto shrink-0 text-xs text-zinc-400 dark:text-zinc-500">{formatTs(event.ts)}</span>
        {/* Clear (2026-08-13, prompts/2026-08-13-clear-history.md) -- deletes this row
         * outright. No category is protected, including the delete-audit kinds highlighted
         * above (docs/decisions.md) -- the confirm panel this opens is the safeguard, not a
         * missing button. */}
        <button
          type="button"
          onClick={() => onClearRequest(event)}
          title="Clear this record -- cannot be undone"
          className="shrink-0 rounded-md px-1.5 py-0.5 text-xs font-medium text-zinc-400 hover:bg-red-50 hover:text-red-700 dark:text-zinc-600 dark:hover:bg-red-950 dark:hover:text-red-300"
        >
          Clear
        </button>
      </div>
      {/* The message string already carries queue/mode/gating-condition detail
       * (core/postprocess.py's audit.record_event calls) -- what was deleted, from which
       * queue, under which mode, and what gated it, including the failing precondition for a
       * withheld delete (DESIGN.md §7.3). Rendered verbatim; nothing here re-parses it. */}
      <p className="text-xs text-zinc-700 dark:text-zinc-300">{event.message}</p>
    </div>
  )
}

export interface EventsItemFilter {
  itemId: number
  itemLabel: string | null
}

interface EventsSectionProps {
  queues: PathQueueOut[]
  // The per-item deep link (2026-08-20, docs/transfers-redesign-spec.md §2, phase 1 stage 7) --
  // `null` is the normal, unfiltered log; a non-null value came from the URL (`EventsPage.tsx`'s
  // own `parseItemEventsFilter`), not component state, so a reload or a bookmark reproduces the
  // exact same filtered view. `onClearItemFilter` is how the banner below gets back to the
  // unfiltered log without losing any of the *other* filters the user may have also set.
  itemFilter: EventsItemFilter | null
  onClearItemFilter: () => void
}

const ROW_ESTIMATE_PX = 56
const HEADER_ESTIMATE_PX = 32

/** Events -- the audit-event log (2026-08-20, docs/transfers-redesign-spec.md §2, phase 1 stage
 * 7). Formerly one half of the History page (alongside a `job` list, `HistoryJobsSection.tsx`,
 * now removed -- the Queue tab's Complete box, stage 4b, covers "what finished, in what order,"
 * which made the jobs half a second, overlapping answer to the same question). This is what
 * nothing else has: every verify/extract/move outcome and, critically, every remote delete and
 * every delete *withheld*, with the failing precondition. A remote delete is irreversible; this
 * is the reconstruction trail. Grouped by queue, filterable by kind/level/date range, and now
 * also by a single item via the URL (`itemFilter` above); virtualized and server-capped.
 */
export function EventsSection({ queues, itemFilter, onClearItemFilter }: EventsSectionProps) {
  const [queueId, setQueueId] = useState<string>('')
  const [kind, setKind] = useState<string>('')
  const [level, setLevel] = useState<string>('')
  const [since, setSince] = useState<string>('')
  const [until, setUntil] = useState<string>('')

  const [events, setEvents] = useState<HistoryEventOut[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  const filter: HistoryEventsFilter = useMemo(
    () => ({
      item_id: itemFilter?.itemId,
      queue_id: queueId ? Number(queueId) : undefined,
      kind: kind || undefined,
      level: (level || undefined) as HistoryEventsFilter['level'],
      since: since ? `${since}T00:00:00.000000Z` : undefined,
      until: until ? `${until}T23:59:59.999999Z` : undefined,
    }),
    [itemFilter, queueId, kind, level, since, until],
  )

  const load = async (offset: number, replace: boolean) => {
    setLoading(true)
    try {
      const res = await getHistoryEvents({ ...filter, offset, limit: 200 })
      setTotal(res.total)
      setEvents((prev) => (replace ? res.events : [...prev, ...res.events]))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load(0, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter])

  // --- Clearing (2026-08-13, prompts/2026-08-13-clear-history.md) -- server-side bulk delete
  // against the filter (including a URL-driven item filter, if one is set), local removal for a
  // single row.
  const hasActiveFilter = Boolean(itemFilter || queueId || kind || level || since || until)
  const [pendingClear, setPendingClear] = useState<
    { kind: 'row'; event: HistoryEventOut } | { kind: 'bulk' } | null
  >(null)
  const [clearing, setClearing] = useState(false)
  const [clearError, setClearError] = useState<string | null>(null)

  const confirmClear = async () => {
    const pending = pendingClear
    if (!pending) return
    setClearing(true)
    setClearError(null)
    try {
      if (pending.kind === 'row') {
        await clearHistoryEvent(pending.event.id)
        setEvents((prev) => prev.filter((e) => e.id !== pending.event.id))
        setTotal((t) => Math.max(0, t - 1))
      } else {
        await clearHistoryEvents(filter)
        await load(0, true)
      }
      setPendingClear(null)
    } catch (err) {
      setClearError(err instanceof Error ? err.message : 'Clear failed')
    } finally {
      setClearing(false)
    }
  }

  const rows = useMemo(() => groupByQueue(events), [events])

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: (index) => (rows[index]?.kind === 'header' ? HEADER_ESTIMATE_PX : ROW_ESTIMATE_PX),
    overscan: 10,
  })

  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">Events</h2>
        <span className="text-xs text-zinc-500 dark:text-zinc-400">
          {events.length} of {total} shown
        </span>
      </div>

      {/* The per-item deep link's own "you are not looking at everything" banner (2026-08-20,
       * docs/transfers-redesign-spec.md §2) -- a filtered view that looks like the whole log is
       * the same class of mistake the Queue tab's Complete box already had to avoid (stage 4b's
       * page-scoped filter, docs/decisions.md). Placed above every other control so it can never
       * be scrolled past unnoticed. */}
      {itemFilter && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-sky-300 bg-sky-50 px-3 py-2 text-sm text-sky-900 dark:border-sky-800 dark:bg-sky-950/40 dark:text-sky-200">
          <span>
            Showing events for <strong>{itemFilter.itemLabel ?? `item ${itemFilter.itemId}`}</strong> only.
          </span>
          <button
            type="button"
            onClick={onClearItemFilter}
            className="ml-auto shrink-0 rounded-md border border-sky-400 px-2 py-1 text-xs font-medium text-sky-800 hover:bg-sky-100 dark:border-sky-700 dark:text-sky-200 dark:hover:bg-sky-900"
          >
            Show full log
          </button>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <select className={inputClasses} value={queueId} onChange={(e) => setQueueId(e.target.value)}>
          <option value="">All queues</option>
          {queues.map((q) => (
            <option key={q.id} value={q.id}>
              {q.name}
            </option>
          ))}
        </select>
        <input
          className={inputClasses}
          list="history-event-kinds"
          placeholder="Kind (e.g. remote_delete)"
          value={kind}
          onChange={(e) => setKind(e.target.value)}
        />
        <datalist id="history-event-kinds">
          {KNOWN_KINDS.map((k) => (
            <option key={k} value={k} />
          ))}
        </datalist>
        <select className={inputClasses} value={level} onChange={(e) => setLevel(e.target.value)}>
          <option value="">Any level</option>
          <option value="debug">debug</option>
          <option value="info">info</option>
          <option value="warning">warning</option>
          <option value="error">error</option>
        </select>
        <label className="flex items-center gap-1 text-xs text-zinc-500 dark:text-zinc-400">
          Since
          <input type="date" className={inputClasses} value={since} onChange={(e) => setSince(e.target.value)} />
        </label>
        <label className="flex items-center gap-1 text-xs text-zinc-500 dark:text-zinc-400">
          Until
          <input type="date" className={inputClasses} value={until} onChange={(e) => setUntil(e.target.value)} />
        </label>
        <button
          type="button"
          onClick={() => load(0, true)}
          disabled={loading}
          className="rounded-md border border-zinc-300 px-2.5 py-1.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
        <button
          type="button"
          onClick={() => setKind(kind === 'remote_delete' ? '' : 'remote_delete')}
          className="rounded-md border border-amber-300 px-2.5 py-1.5 text-sm font-medium text-amber-800 hover:bg-amber-50 dark:border-amber-800 dark:text-amber-300 dark:hover:bg-amber-950"
          title="Quick filter: only actual remote deletes (not withheld/failed)"
        >
          Deletes only
        </button>
        <button
          type="button"
          disabled={total === 0}
          onClick={() => setPendingClear({ kind: 'bulk' })}
          className="ml-auto rounded-md border border-red-300 px-2.5 py-1.5 text-sm font-medium text-red-700 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950"
        >
          {hasActiveFilter ? `Clear filtered (${total})` : `Clear all (${total})`}
        </button>
      </div>

      {/* Clearing is irreversible -- one confirm panel shared by the per-row "Clear" button and
       * the toolbar's bulk clear above, distinguished only by its message and count. */}
      {pendingClear && (
        <div className="flex flex-col gap-2 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm dark:border-red-900 dark:bg-red-950/40">
          <p className="text-red-900 dark:text-red-200">
            {pendingClear.kind === 'row' ? (
              <>
                Clear this <strong>{pendingClear.event.kind}</strong> event record? This cannot be undone.
              </>
            ) : (
              <>
                Clear <strong>{total}</strong> event {total === 1 ? 'record' : 'records'}
                {hasActiveFilter ? ' matching the current filters' : ''}, including any delete-audit rows
                among them? This cannot be undone.
              </>
            )}
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={confirmClear}
              disabled={clearing}
              className="rounded-md bg-red-700 px-2 py-1 text-xs font-medium text-white hover:bg-red-800 disabled:opacity-50 dark:bg-red-800 dark:hover:bg-red-700"
            >
              {clearing ? 'Clearing…' : 'Clear'}
            </button>
            <button
              type="button"
              onClick={() => setPendingClear(null)}
              disabled={clearing}
              className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {clearError && (
        <div className="flex items-center justify-between gap-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          <span>{clearError}</span>
          <button type="button" onClick={() => setClearError(null)} className="shrink-0 text-xs underline decoration-dotted">
            Dismiss
          </button>
        </div>
      )}

      {events.length === 0 && !loading && (
        <div className="flex h-32 items-center justify-center rounded-lg border border-dashed border-zinc-300 text-zinc-400 dark:border-zinc-700 dark:text-zinc-600">
          No events match these filters.
        </div>
      )}

      {events.length > 0 && (
        <div
          ref={scrollRef}
          className="max-h-[28rem] overflow-auto rounded-md border border-zinc-200 dark:border-zinc-800"
        >
          <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
            {virtualizer.getVirtualItems().map((virtualRow) => {
              const row = rows[virtualRow.index]
              return (
                <div
                  key={virtualRow.key}
                  data-index={virtualRow.index}
                  ref={virtualizer.measureElement}
                  style={{
                    position: 'absolute',
                    top: 0,
                    left: 0,
                    width: '100%',
                    transform: `translateY(${virtualRow.start}px)`,
                  }}
                >
                  {row.kind === 'header' ? (
                    <div className="border-b border-zinc-200 bg-zinc-50 px-3 py-1.5 text-xs font-semibold text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900/60 dark:text-zinc-300">
                      {row.queueName}
                    </div>
                  ) : (
                    <EventRow event={row.event} onClearRequest={(event) => setPendingClear({ kind: 'row', event })} />
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {events.length < total && (
        <button
          type="button"
          onClick={() => load(events.length, false)}
          disabled={loading}
          className="self-start rounded-md border border-zinc-300 px-2.5 py-1.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
        >
          Load more ({total - events.length} remaining)
        </button>
      )}
    </section>
  )
}
