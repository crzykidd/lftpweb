// Pure projection from a download-client instance's `CapabilitySetOut` (spec §4.1, §4.3) onto
// what `ClientsTab.tsx`'s capability readout renders -- kept out of the component per this
// repo's settled pattern (`lib/fileTree.ts`, `lib/pathBrowse.ts`, `QueuesTab.tsx`'s own pure
// predicates) so the honesty rules this task's own handoff prompt calls out are directly
// Vitest-able with no render harness:
//
// - A `derived` capability is labelled derived and carries its `note` (spec §4.3) -- the
//   canonical case is rTorrent's seed time, wall-clock since completion rather than true
//   accrued seed time, and a UI that renders `derived` and `native` identically is exactly the
//   failure the tri-state design exists to prevent.
// - A `none` capability carries a stated `disabledReason`, never a bare "unavailable" -- the
//   backend's own baseline `note` (e.g. "no ratio (spec §5)") is used verbatim when present, so
//   the framework's own reasoning surfaces to the user rather than being replaced by a generic
//   label; a connector that ever declares `none` with no `note` still gets an honest fallback
//   built from the field's own display label rather than rendering as an unexplained blank.

import type { CapabilityOut, CapabilitySetOut } from '../api/types'

/** Display labels for `Operation` members (`core/clients/base.py`) -- kept as a lookup table
 * here, not read from the client's own name, so a new connector's capabilities render with a
 * readable label from the moment its `client-types` entry exists, with no per-connector UI work.
 */
export const OPERATION_LABELS: Record<string, string> = {
  test_connection: 'Test connection',
  list_transfers: 'List active transfers',
  list_history: 'List history',
  get_transfer: 'Look up one transfer',
  list_trackers: 'List trackers',
  list_files: 'List a transfer’s files',
  list_base_paths: 'List its own base paths',
  free_space: 'Report free space',
  pause: 'Pause',
  resume: 'Resume',
  remove: 'Remove (unregister only — data stays on disk)',
  set_label: 'Set category / label',
  recheck: 'Recheck data against the torrent',
}

/** Display labels for `Field` members (`core/clients/base.py`). */
export const FIELD_LABELS: Record<string, string> = {
  content_path: 'On-disk path',
  size_bytes: 'Total size',
  bytes_done: 'Bytes done',
  eta_s: 'ETA',
  error_message: 'Error message',
  category: 'Category',
  added_at: 'Added at',
  completed_at: 'Completed at',
  ratio: 'Ratio',
  uploaded_bytes: 'Uploaded bytes',
  seed_time_s: 'Seed time',
  tracker_hosts: 'Tracker hosts',
}

export type CapabilityGroup = 'operations' | 'fields'

export interface CapabilityRow {
  key: string
  label: string
  group: CapabilityGroup
  support: CapabilityOut['support']
  note: string | null
  /** `true` exactly when `support === 'derived'` -- the row must render its `note`, never
   * treat this the same as `native`.
   */
  derived: boolean
  /** Set exactly when `support === 'none'` -- the stated reason a feature built on this key is
   * disabled. Never `null` for a `none` row: falls back to a label-derived sentence when the
   * connector's own declaration carries no `note`.
   */
  disabledReason: string | null
}

function toRow(key: string, label: string, group: CapabilityGroup, cap: CapabilityOut): CapabilityRow {
  const derived = cap.support === 'derived'
  const disabledReason =
    cap.support === 'none' ? (cap.note ?? `This client doesn't report ${label.toLowerCase()}.`) : null
  return { key, label, group, support: cap.support, note: cap.note, derived, disabledReason }
}

/** Every declared operation and field, projected to display rows -- `[]` for `null` (never
 * probed), so a caller can render "not yet tested" rather than a fabricated capability set.
 * Order is stable (operations first, in declaration order, then fields) so a snapshot or a
 * test asserting row order doesn't depend on `Object.entries`' iteration guarantees alone.
 */
export function capabilityRows(capabilities: CapabilitySetOut | null): CapabilityRow[] {
  if (capabilities == null) return []
  const rows: CapabilityRow[] = []
  for (const [key, cap] of Object.entries(capabilities.operations)) {
    rows.push(toRow(key, OPERATION_LABELS[key] ?? key, 'operations', cap))
  }
  for (const [key, cap] of Object.entries(capabilities.fields)) {
    rows.push(toRow(key, FIELD_LABELS[key] ?? key, 'fields', cap))
  }
  return rows
}
