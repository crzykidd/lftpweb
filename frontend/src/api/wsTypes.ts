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

export type WsMessage =
  | SnapshotMessage
  | QueueDeltaMessage
  | ItemDeltaMessage
  | ScanErrorMessage
  | ProgressMessage
