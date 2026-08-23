// Base-path detection (docs/download-client-framework-spec.md §8.2 correction, migration 028,
// 2026-08-22) -- kept out of `ClientsTab.tsx` per this repo's settled pattern for pure,
// Vitest-able logic (lib/clientCategoryInference.ts, lib/pathBrowse.ts).
//
// The backend's `POST /api/settings/clients/{id}/test` detects a connector's own base paths
// and SSH-verifies each one (`DetectedBasePathOut`), but **detection proposes; it never
// saves** -- the settings page decides whether and how a proposal becomes a saved
// `DownloadClientBasePathIn`. This module is that decision, expressed as pure functions so it's
// testable without a render harness:
//
// - `isDetectedRowAccepted` / `acceptedPathFor` -- has this detected row already been turned
//   into a base-path row in the current draft? Re-running detection (clicking Test again) must
//   not prompt again for one already accepted, and must never duplicate it.
// - `buildAcceptedBasePath` -- turn one detected row into a `DownloadClientBasePathIn`, given
//   the SSH-visible path the user confirms (identical to `client_path` for `verified`/
//   `unverified`; a distinct, user-supplied path for `not_found`).
//
// **Manual rows are untouched by any of this.** A row's own `source` is the only thing that
// matters -- these functions only ever look at `source === 'detected'` rows when deciding
// what's "already handled," so a manually-added row (`source === 'manual'`) can never be
// mistaken for an unconfirmed detection, or vice versa.

import type { BasePathKind, DetectedBasePathOut, DownloadClientBasePathIn } from '../api/types'

export interface BasePathDraft extends DownloadClientBasePathIn {}

/** Whether `row` (identified by its `client_path`) already has a matching accepted row in
 * `basePaths` -- matched against each existing row's own `client_path` when it recorded a
 * translation, or its `path` when it didn't (spec: "`client_path` is `null` when no
 * translation was needed", so the accepted path *is* the client path in that case). Only
 * `source === 'detected'` rows are ever considered -- a manually-typed row that happens to
 * equal the same path is not "the same proposal," and must not suppress showing it.
 */
export function isDetectedRowAccepted(
  row: Pick<DetectedBasePathOut, 'client_path'>,
  basePaths: readonly BasePathDraft[],
): boolean {
  return basePaths.some(
    (bp) => bp.source === 'detected' && (bp.client_path ?? bp.path) === row.client_path,
  )
}

/** The SSH-visible path already accepted for `row`, or `null` if it hasn't been -- lets the UI
 * show "already added as `/data/pickup`" instead of re-prompting blank.
 */
export function acceptedPathFor(
  row: Pick<DetectedBasePathOut, 'client_path'>,
  basePaths: readonly BasePathDraft[],
): string | null {
  const match = basePaths.find(
    (bp) => bp.source === 'detected' && (bp.client_path ?? bp.path) === row.client_path,
  )
  return match ? match.path : null
}

/** Turn one detected row into a saved-shape base path draft. `sshPath` is the path lftpweb
 * should actually use -- for `verified`/`unverified` this is ordinarily `row.client_path`
 * itself (accepted as-is); for `not_found` it's the SSH-visible equivalent the user supplied.
 * `client_path` on the built draft records the client's own view **only when it differs** from
 * `sshPath` -- identical to `path_queue.arr_visible_path`'s own "NULL = no translation needed"
 * rule (migration 018), so accepting a `verified` path unmodified never fabricates a
 * translation that isn't real.
 */
export function buildAcceptedBasePath(
  row: { client_path: string; kind: BasePathKind },
  sshPath: string,
): BasePathDraft {
  const trimmed = sshPath.trim()
  return {
    path: trimmed,
    kind: row.kind,
    client_path: trimmed === row.client_path ? null : row.client_path,
    source: 'detected',
  }
}

/** Whether a detected row's own `client_path` is already SSH-visible as written -- `false` for
 * `~/downloads/rtorrent` or a bare relative path, `true` for `/home/crzykidd/downloads/rtorrent`.
 * 2026-08-23 (finding #1): **the one guard standing between "Accept anyway" and a `~` literal
 * ending up in the saved `path` column** -- a `~`/relative path can never be verified by
 * `core.browse.remote_directory_error`'s literal `stat` (no SFTP server expands `~`), so it can
 * only ever reach `ClientsTab.tsx` in the `not_found` or `unverified` state; `not_found` already
 * asks for an SSH-visible equivalent instead of a direct Accept, and this is what makes
 * `unverified` do the same rather than letting its own "Accept anyway" button hand the literal
 * `client_path` straight to `buildAcceptedBasePath` as `sshPath`.
 */
export function isAbsoluteClientPath(clientPath: string): boolean {
  return clientPath.startsWith('/')
}
