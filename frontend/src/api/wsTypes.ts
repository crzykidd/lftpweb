// The one WebSocket's message shapes (DESIGN.md §2/§9), mirroring backend/lftpweb/api/ws.py
// and core/engine.py / core/queue.py's `events.publish(...)` calls. A full snapshot exactly
// once, on connect; every message after that is a delta proportional to what changed, never
// to the size of a queue's tree -- see docs/decisions.md's phase 3b entry ("the WebSocket
// delta fix") for why phase 2's per-queue full-snapshot shape couldn't survive phase 3a's
// ~1 Hz progress sampler.

import type { FileNode } from './types'

export interface QueueSnapshot {
  type: 'queue_snapshot'
  queue_id: number
  queue_name: string
  nodes: FileNode[]
  scanned_at: string | null
  warning: string | null
}

export interface SnapshotMessage {
  type: 'snapshot'
  queues: QueueSnapshot[]
}

/** Published by `core/engine.py.scan_queue` after every scan -- only the rows that changed
 * or were removed since the previous scan (`core/engine.py.diff_nodes`), not the whole tree.
 */
export interface QueueDeltaMessage {
  type: 'queue_delta'
  queue_id: number
  queue_name: string
  changed: FileNode[]
  removed: string[]
  scanned_at: string | null
  warning: string | null
}

/** Published by `core/queue.py` (`_publish_item_state`, `_sample_and_publish_progress`)
 * whenever a job's lifecycle changes an item's state, or once per ~1 Hz tick for the items
 * currently downloading -- bounded by the active set, never the tree.
 */
export interface ItemDeltaMessage {
  type: 'item_delta'
  queue_id: number
  nodes: FileNode[]
}

export interface ScanErrorMessage {
  type: 'scan_error'
  queue_id: number
  queue_name: string
  message: string
}

/** Published by `core/engine.py.scan_queue` at the end of *every* pass -- success or
 * failure, unlike `QueueDeltaMessage` which only fires on success. This is the one signal
 * a client can actually wait on for "this queue's scan attempt is over", rather than
 * guessing from a fixed timer (`pages/FilesPage.tsx`'s old 1s `setTimeout`) or inferring it
 * from a message shape meant for tree deltas. Fixed-size regardless of tree size: four
 * scalars, never a node list -- same delta-rule shape as every other message here.
 */
export interface ScanCompleteMessage {
  type: 'scan_complete'
  queue_id: number
  finished_at: string
  ok: boolean
  /** The partial-scan warning text carried by this pass, if any. Only ever set when `ok` is
   * true -- a pass that failed outright never got far enough to know. */
  warning: string | null
}

export interface ProgressJob {
  job_id: number
  item_id: number
  bytes_done: number
  bytes_total: number | null
  speed_bps: number
  eta_s: number | null
}

/** Published by `core/queue.py._sample_and_publish_progress` every ~1 Hz tick -- job-level
 * (Transfers page), bounded by how many jobs are currently running.
 */
export interface ProgressMessage {
  type: 'progress'
  jobs: ProgressJob[]
}

export interface ChildProgressItem {
  item_id: number
  speed_bps: number
}

/** Published by `core/queue.py._publish_child_progress` on the same throttled pass as
 * `item_delta` (2026-08-14, "per-file speed inside a mirror") -- a live, EMA-smoothed rate for
 * each changed file inside a mirroring directory. Deliberately a **third** message, not folded
 * into either existing one: `progress` is job-centric (a child has no `job_id` of its own, so a
 * pseudo-entry there would collide in `progressByJobId` and put a fictional row on the
 * Transfers page); `item_delta` carries `item_view()` projections of persisted `item` columns
 * only, and a live rate is a sample, never a persisted one (DESIGN.md §2/§9's invariant). Never
 * larger than `core/queue.py.MAX_CHILD_PROGRESS_UPDATES_PER_TICK` entries, and omitted
 * entirely on a tick with nothing to report -- same bound as `_publish_child_progress`'s other
 * work, never proportional to tree size. See `useLiveModel.ts`'s `childSpeedByItemId` and
 * docs/decisions.md for how the frontend gates display on this (freshness, not `state`, since
 * every actively-transferring child sits at `PARTIAL`, never `DOWNLOADING`).
 */
export interface ChildProgressMessage {
  type: 'child_progress'
  items: ChildProgressItem[]
}

export type WsMessage =
  | SnapshotMessage
  | QueueDeltaMessage
  | ItemDeltaMessage
  | ScanErrorMessage
  | ScanCompleteMessage
  | ProgressMessage
  | ChildProgressMessage
