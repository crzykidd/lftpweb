import { useEffect, useState } from 'react'
import type { FileNode, QueueFiles } from '../api/types'
import type { ProgressJob, WsMessage } from '../api/wsTypes'

export type SocketState = 'connecting' | 'open' | 'reconnecting'

const RECONNECT_DELAY_MS = 3000

/** One `child_progress` sample, kept with the moment it arrived so a consumer can decide for
 * itself whether it's still fresh (2026-08-14, "per-file speed inside a mirror") -- see
 * `childSpeedByItemId` below for why freshness, not `state`, is what gates a child's display.
 */
export interface ChildSpeedSample {
  speedBps: number
  receivedAt: number
}

interface QueueState {
  queue_id: number
  queue_name: string
  scanned_at: string | null
  error: string | null
  warning: string | null
  nodesByPath: Record<string, FileNode>
}

function mergeNodes(existing: Record<string, FileNode>, incoming: FileNode[]): Record<string, FileNode> {
  if (incoming.length === 0) return existing
  const next = { ...existing }
  for (const node of incoming) next[node.rel_path] = node
  return next
}

function toQueueFiles(q: QueueState): QueueFiles {
  return {
    queue_id: q.queue_id,
    queue_name: q.queue_name,
    scanned_at: q.scanned_at,
    error: q.error,
    warning: q.warning,
    nodes: Object.values(q.nodesByPath),
  }
}

/**
 * DESIGN.md §2/§9: one WebSocket, a full snapshot on connect, deltas thereafter. This is the
 * single place that WebSocket is opened; both the Files page (the reconciled tree) and the
 * Transfers page (per-file drawer contents, plus live job progress) read from it, since the
 * two routes are never mounted at once.
 *
 * **The WS delta fix (phase 3b, docs/decisions.md):** phase 2's `queue_snapshot` resent an
 * entire queue's tree on every scan. `core/engine.py.diff_nodes` now sends only `changed` /
 * `removed` rel_paths, and `core/queue.py` pushes single-item deltas (`item_delta`) on
 * lifecycle changes and ~1 Hz progress ticks — all merged into a `rel_path`-keyed map here so
 * a node that hasn't changed keeps showing its last-known value, exactly like phase 2's
 * queue-keyed merge did for whole queues.
 */
export function useLiveModel(): {
  queues: QueueFiles[]
  progressByJobId: Record<number, ProgressJob>
  /** The same `progress` WS message's `speed_bps`, re-keyed by `item_id` rather than `job_id`
   * (2026-08-14, prompts/2026-08-14-files-page-speed-column.md) -- the Files page's rows are
   * items, not jobs, and `FileTree.tsx`'s Speed column needs to look a rate up by the id its own
   * rows already carry (`FileNode.id`). Built from the exact same message as `progressByJobId`
   * above, not a second subscription or poll -- see that map's own shape for why one `progress`
   * tick already carries both ids per running job. Like `progressByJobId`, entries are never
   * pruned on job completion; a stale value here is harmless because `lib/format.ts`'s
   * `transferSpeedLabel`/`transferSpeedSortValue` gate display on the row's own `state ===
   * 'DOWNLOADING'`, not on whether a value is present.
   */
  speedByItemId: Record<number, number>
  /** The same `progress` WS message's `eta_s`, re-keyed by `item_id` exactly like
   * `speedByItemId` above (2026-08-14, "ETA on Files rows") -- the parent's ETA is already fully
   * computed server-side (`core/progress.py.JobProgress.eta_s`); this is only a second read of
   * the same `progress` message's already-arrived `ProgressJob.eta_s`, not a second
   * subscription or a client-side computation of its own. Same lifetime contract as
   * `speedByItemId`: never pruned client-side, so a stale value only stops mattering because
   * `lib/format.ts`'s `transferEtaLabel` gates display on the row's own `state ===
   * 'DOWNLOADING'`, not on presence.
   */
  etaByItemId: Record<number, number | null>
  /** `child_progress` WS messages (2026-08-14, "per-file speed inside a mirror"), keyed by
   * `item_id` like `speedByItemId` above -- but unlike that map, each entry also carries when
   * it arrived (`Date.now()` at receipt), because the gating rule for a child is different from
   * the parent's. `speedByItemId` is safe to leave unpruned forever because its consumer
   * (`FileTree.tsx`) gates purely on `state === 'DOWNLOADING'`, which stops being true the
   * instant a job's own item leaves that state. A mirroring directory's *children* never reach
   * `DOWNLOADING` at all -- `core/reconcile.py`'s leaf rule puts an actively-transferring child
   * at `PARTIAL` -- so there is no state transition to gate on, and a value that's simply never
   * pruned would linger on a finished/stalled child forever. `_publish_child_progress` already
   * stops emitting an entry for a child the instant it stops changing (a natural consequence of
   * only diffing *changed* children), so a consumer that gates on "was a sample received
   * recently" closes the staleness gap by construction rather than needing an explicit prune
   * message. See docs/decisions.md for the two options considered and why this one was picked.
   */
  childSpeedByItemId: Record<number, ChildSpeedSample>
  state: SocketState
  /** Bumped by one on every `scan_complete` message, for any queue. Purely a change signal --
   * a caller (`FilesPage.tsx`'s "Rescan now") that captures this value before triggering a
   * rescan and then watches for it to move knows a real scan pass finished, without either
   * side needing to correlate a request id it was never given. See docs/decisions.md for why
   * a WS message rather than a blocking rescan endpoint. */
  scanCompleteSeq: number
} {
  const [queuesById, setQueuesById] = useState<Record<number, QueueState>>({})
  const [progressByJobId, setProgressByJobId] = useState<Record<number, ProgressJob>>({})
  const [speedByItemId, setSpeedByItemId] = useState<Record<number, number>>({})
  const [etaByItemId, setEtaByItemId] = useState<Record<number, number | null>>({})
  const [childSpeedByItemId, setChildSpeedByItemId] = useState<Record<number, ChildSpeedSample>>({})
  const [state, setState] = useState<SocketState>('connecting')
  const [scanCompleteSeq, setScanCompleteSeq] = useState(0)

  useEffect(() => {
    let cancelled = false
    let ws: WebSocket | null = null
    let retryTimer: ReturnType<typeof setTimeout> | undefined

    const connect = () => {
      if (cancelled) return
      setState((prev) => (prev === 'open' ? 'reconnecting' : 'connecting'))

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      ws = new WebSocket(`${protocol}//${window.location.host}/api/ws`)

      ws.onopen = () => {
        if (cancelled) return
        setState('open')
      }

      ws.onmessage = (event) => {
        if (cancelled) return
        let msg: WsMessage
        try {
          msg = JSON.parse(event.data as string) as WsMessage
        } catch {
          return
        }

        if (msg.type === 'snapshot') {
          const next: Record<number, QueueState> = {}
          for (const q of msg.queues) {
            const nodesByPath: Record<string, FileNode> = {}
            for (const node of q.nodes) nodesByPath[node.rel_path] = node
            next[q.queue_id] = {
              queue_id: q.queue_id,
              queue_name: q.queue_name,
              scanned_at: q.scanned_at,
              error: null,
              warning: q.warning,
              nodesByPath,
            }
          }
          setQueuesById(next)
        } else if (msg.type === 'queue_delta') {
          setQueuesById((prev) => {
            const existing = prev[msg.queue_id]
            const nodesByPath = mergeNodes(existing?.nodesByPath ?? {}, msg.changed)
            for (const removedPath of msg.removed) delete nodesByPath[removedPath]
            return {
              ...prev,
              [msg.queue_id]: {
                queue_id: msg.queue_id,
                queue_name: msg.queue_name,
                // `scan_complete` (below) is the one source for these two now -- carried
                // forward rather than duplicated from this message's own copies (which are
                // always identical on a successful pass; `core/engine.py.scan_queue`
                // publishes both from the same `self.last_scan_at[q.id]`), so "last scanned"
                // has exactly one place it comes from, including the failed-pass case
                // `queue_delta` never fires for at all.
                scanned_at: existing?.scanned_at ?? null,
                error: null,
                warning: existing?.warning ?? null,
                nodesByPath,
              },
            }
          })
        } else if (msg.type === 'scan_complete') {
          setScanCompleteSeq((n) => n + 1)
          // Only a successful pass updates the displayed "last scanned" reading -- a failed
          // attempt has nothing new to report (`scan_error`, handled separately, already
          // carries the failure message) and must not overwrite the last time this queue
          // actually finished scanning with "just now".
          if (msg.ok) {
            setQueuesById((prev) => {
              const existing = prev[msg.queue_id]
              if (!existing) return prev
              return {
                ...prev,
                [msg.queue_id]: { ...existing, scanned_at: msg.finished_at, warning: msg.warning },
              }
            })
          }
        } else if (msg.type === 'item_delta') {
          setQueuesById((prev) => {
            const existing = prev[msg.queue_id]
            if (!existing) return prev // a delta for a queue we haven't snapshotted yet
            return { ...prev, [msg.queue_id]: { ...existing, nodesByPath: mergeNodes(existing.nodesByPath, msg.nodes) } }
          })
        } else if (msg.type === 'scan_error') {
          setQueuesById((prev) => {
            const existing = prev[msg.queue_id]
            return {
              ...prev,
              [msg.queue_id]: existing
                ? { ...existing, error: msg.message }
                : {
                    queue_id: msg.queue_id,
                    queue_name: msg.queue_name,
                    scanned_at: null,
                    error: msg.message,
                    warning: null,
                    nodesByPath: {},
                  },
            }
          })
        } else if (msg.type === 'progress') {
          setProgressByJobId((prev) => {
            const next = { ...prev }
            for (const job of msg.jobs) next[job.job_id] = job
            return next
          })
          setSpeedByItemId((prev) => {
            const next = { ...prev }
            for (const job of msg.jobs) next[job.item_id] = job.speed_bps
            return next
          })
          setEtaByItemId((prev) => {
            const next = { ...prev }
            for (const job of msg.jobs) next[job.item_id] = job.eta_s
            return next
          })
        } else if (msg.type === 'child_progress') {
          const receivedAt = Date.now()
          setChildSpeedByItemId((prev) => {
            const next = { ...prev }
            for (const it of msg.items) next[it.item_id] = { speedBps: it.speed_bps, receivedAt }
            return next
          })
        }
      }

      ws.onclose = () => {
        if (cancelled) return
        setState('reconnecting')
        retryTimer = setTimeout(connect, RECONNECT_DELAY_MS)
      }

      ws.onerror = () => {
        ws?.close()
      }
    }

    connect()

    return () => {
      cancelled = true
      if (retryTimer) clearTimeout(retryTimer)
      ws?.close()
    }
  }, [])

  const queues = Object.values(queuesById)
    .map(toQueueFiles)
    .sort((a, b) => a.queue_id - b.queue_id)

  return { queues, progressByJobId, speedByItemId, etaByItemId, childSpeedByItemId, state, scanCompleteSeq }
}
