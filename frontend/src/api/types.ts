// Mirrors backend/lftpweb/models.py — extended as each phase adds endpoints.

export interface HealthResponse {
  status: string
  version: string
  db: boolean
  uptime_s: number
  repo_url: string
  // Phase 7, DESIGN.md §10.3. `null` = no host configured yet (distinct from `false`, "a
  // host exists but the pooled connection last failed").
  host_reachable: boolean | null
  scheduler_alive: boolean
  // 2026-08-20 (prompts/2026-08-20-queue-pause.md): whether admission is paused -- the header
  // bar and the Transfers -> Queue tab's own banner both read this.
  queue_paused: boolean
  // 2026-08-21 (prompts/2026-08-21-pause-for-duration.md): the absolute ISO-8601 UTC deadline
  // a timed pause resumes at, or `null` for an indefinite pause (or no pause at all).
  queue_paused_until: string | null
  // 2026-08-16 (docs/decisions.md): baked at image build time, `null` for every build that
  // never baked them (local dev, compose dev stack, a manual `docker build` with no
  // `--build-arg`) -- see `lib/versionBadge.ts` for how the nav's version readout uses them.
  build_sha: string | null
  build_channel: 'dev' | 'release' | null
}

export interface StatsResponse {
  current_speed_bps: number
  allocated_bps: number
  ceiling_bps: number
  queued_count: number
  queued_bytes: number
  transferred_24h_bytes: number
}

// --- Settings -> Connection (phase 2, DESIGN.md §3.1 `host`, §9.2) ---------------------

export type AuthMethod = 'key' | 'agent' | 'password'
export type KnownHostsPolicy = 'accept-and-pin' | 'strict' | 'insecure'

export interface HostOut {
  id: number
  name: string
  address: string
  port: number
  username: string
  auth_method: AuthMethod
  key_path: string | null
  has_password: boolean
  // migration 014 (DESIGN.md §8): whether a pasted key is currently stored, mirroring
  // `has_password` -- never the key itself.
  has_ssh_key: boolean
  // Which of `key_path` / a pasted key is actually in use for `auth_method === 'key'` -- the
  // pasted-wins-over-path rule is decided once, server-side (`api/settings.py`), so this is
  // read, never re-derived. `null` when `auth_method !== 'key'` or neither is set.
  active_key_source: 'pasted' | 'path' | null
  known_hosts_policy: KnownHostsPolicy
  credentials_need_reentry: boolean
  // Read-only (DESIGN.md §4.5/§9.3, docs/decisions.md 2026-08-12): whatever currently sits
  // in `net:connection-limit` inside the host's `connection_overrides` JSON blob, or null if
  // unset. There is no field on `HostIn` to set it -- Settings → Connection has no UI for
  // it -- so this is `null` on every install that hasn't hand-edited the database.
  net_connection_limit: number | null
}

// Mirrors HostIn — password/ssh_key are plaintext here only, and only ever sent, never
// received back (§9.2: neither must ever round-trip the stored secret to the browser).
export interface HostIn {
  name: string
  address: string
  port: number
  username: string
  auth_method: AuthMethod
  key_path: string | null
  password: string | null
  // migration 014: an *additional* way to satisfy `auth_method === 'key'`, alongside
  // `key_path` -- not a replacement. Wins over `key_path` when both are set.
  ssh_key: string | null
  known_hosts_policy: KnownHostsPolicy
}

export interface HostTestRequest {
  name?: string | null
  address?: string | null
  port?: number | null
  username?: string | null
  auth_method?: AuthMethod | null
  key_path?: string | null
  password?: string | null
  ssh_key?: string | null
  known_hosts_policy?: KnownHostsPolicy | null
}

export interface TestConnectionResponse {
  ok: boolean
  error_class: string | null
  message: string
}

// --- Settings -> Queues (phase 2, DESIGN.md §3.1 `path_queue`) -------------------------

export type SyncMode = 'copy' | 'move' | 'sync'

export interface PathQueueIn {
  name: string
  remote_path: string
  local_path: string
  staging_path: string | null
  enabled: boolean
  sync_mode: SyncMode
  // Phase 4 (DESIGN.md §4.7). Both default off/false -- enabling auto-queue is an explicit
  // user action; omitting these on create must not auto-enable anything.
  auto_queue_enabled: boolean
  auto_queue_patterns_only: boolean
  // Phase 5 (DESIGN.md §6); nullable-for-inherit as of 2026-08-13
  // (prompts/2026-08-13-postprocess-inherit-or-override.md). `null` means "inherit the
  // matching Settings -> Post-processing site-wide flag" -- the default, and what every
  // existing queue's row was set to by migration 015. `true`/`false` is an explicit per-queue
  // override, independent of the site-wide flag in either direction; the backend no longer
  // ANDs it with the site-wide flag. The backend forces auto_verify to an explicit `true`
  // whenever sync_mode is 'move' regardless of what's sent here -- the UI mirrors that by
  // disabling (not hiding) the checkbox rather than relying on the server alone.
  auto_verify: boolean | null
  auto_extract: boolean | null
  auto_move: boolean | null
  // Migration 012 (2026-08-13); nullable-for-inherit alongside the three above as of the task
  // cited above. Archive cleanup (Settings -> Post-processing -> Extract) shipped site-only
  // and was the odd one out; this is its per-queue half.
  auto_delete_archives: boolean | null
  // Migration 009 (prompts/done/2026-08-12-per-queue-scan-interval.md). `null` -- the default,
  // and what an existing queue already has -- means "use the site-wide default (30s)"; `0`
  // means on-demand only (no timer; "Rescan now" and auto-queue-driving passes still work when
  // something else forces a scan); any positive number is a literal per-queue interval in
  // seconds. The backend rejects a negative value with a 400.
  scan_interval_s: number | null
  // Migration 017 ("folder prefix during transfer", core/download_prefix.py). Both
  // nullable-for-inherit, resolved independently -- `null` means "inherit the matching
  // Settings -> Transfer field," an explicit value is this queue's own override. Directory
  // items only; see Settings -> Transfer's own section for why.
  download_prefix_enabled: boolean | null
  download_prefix: string | null
  // Sonarr/Radarr integration (migration 018, docs/arr-integration-spec.md "Data model" /
  // "API surface"). Binding is per-queue, one instance at most -- `null` (the default, and
  // every existing queue's value after the migration) means "no integration": no icons, no
  // matching, no *arr behavior at all for this queue. Full-replace fields, like the rest of
  // this interface (not the four post-processing toggles' merge-on-absence shape) -- Settings
  // -> Queues' edit form always submits the complete queue state.
  arr_instance_id: number | null
  // Default off, per-queue, and only meaningful when `arr_instance_id` is set -- the backend
  // rejects `true` with no bound instance (`api/settings_queues.py._validate_arr_binding`).
  arr_delete_completed: boolean
  // This queue's `local_path`, translated into the bound *arr's own namespace (spec "Path
  // namespaces") -- `null` means "same namespace, no translation," never an empty-string
  // sentinel.
  arr_visible_path: string | null
  // Migration 024 (docs/transfers-redesign-spec.md §3.6, phase 1 stage 3). `null` (the
  // default, and every existing queue's value) means "no short name set" -- every display
  // falls back to the full `name` (`lib/queueDisplayName.ts.queueDisplayName`). A per-queue
  // display hint for the compact per-row label stage 4 renders once Transfers drops its
  // per-queue grouping (`DC-Movies` -> `MOV`) -- not an identifier, so two queues may share
  // one. The backend trims and normalizes empty-after-trim to `null`, and rejects anything
  // over its own length cap.
  short_name: string | null
}

export interface PathQueueOut extends PathQueueIn {
  id: number
  host_id: number
}

// --- Settings -> Integrations (migration 018, docs/arr-integration-spec.md) -------------
//
// Mirrors `backend/lftpweb/models.py`'s `ArrInstanceIn`/`ArrInstanceOut`/`ArrTestResponse`.
// Sonarr and Radarr, v3 API, one client with a `kind` switch (spec "Scope"). Binding an
// instance to a queue happens on `PathQueueIn.arr_instance_id` above, not here -- this is
// only the instance CRUD + connectivity test.

export type ArrKind = 'sonarr' | 'radarr'

/** A create/update request body. `api_key` is plaintext here -- the only place it ever
 * appears -- and is encrypted at rest server-side before touching the database, the same
 * convention `HostIn.password` uses. Omitting it on an update keeps the stored key (the
 * identical "unchanged must not mean cleared" rule `settings_host.py.put_host` follows) --
 * `IntegrationsTab.tsx` never pre-fills this field with a real value, only a placeholder.
 */
export interface ArrInstanceIn {
  name: string
  kind: ArrKind
  base_url: string
  api_key?: string | null
  enabled: boolean
  notify_on_complete: boolean
}

export interface ArrInstanceOut {
  id: number
  name: string
  kind: ArrKind
  base_url: string
  // Never the key itself (DESIGN.md §9.2's "must never round-trip the stored secret back to
  // the browser") -- whether one is on file, mirroring `HostOut.has_password`.
  has_api_key: boolean
  enabled: boolean
  notify_on_complete: boolean
  created_at: string
  updated_at: string
}

/** `POST /api/settings/arr/{id}/test` -- the `GET /api/v3/system/status` round trip, the
 * Settings UI's Test button. Never a non-2xx for a reachable-but-erroring instance; the
 * failure is reported in `message`/`error_class`, the same "test tells you what's wrong,
 * doesn't throw" shape `TestConnectionResponse` already uses for the seedbox.
 */
export interface ArrTestResponse {
  ok: boolean
  error_class: string | null
  message: string
  version: string | null
}

/** `GET`/`PUT /api/settings/arr/poll-interval` (2026-08-21, issue #16) -- `core/arrsync.py.
 * ArrSettings.poll_interval_s` exposed here for the first time; before this it was DB-only,
 * never surfaced to a user. Server-side validated (`api/settings_arr.py.put_arr_poll_settings`)
 * against a 5s floor and a 3600s ceiling -- this page must not rely on its own `min`/`max` input
 * attributes alone.
 */
export interface ArrPollSettingsOut {
  poll_interval_s: number
}

export type ArrPollSettingsIn = ArrPollSettingsOut

// --- Settings -> Clients (migration 027, docs/download-client-framework-spec.md, stage 1b
// of #18) --------------------------------------------------------------------------------
//
// Mirrors `backend/lftpweb/models.py`'s `ClientConfigFieldOut`/`ClientTypeOut`/
// `DownloadClient*`. Unlike `ArrInstanceIn`'s fixed `base_url`/`api_key` pair, each connector
// declares its own connection-form schema (spec §8.1) -- `ClientConfigFieldOut.kind` is what
// lets `ClientsTab.tsx` render one generic form for every registered connector, with no
// `if client_type === "sabnzbd"` anywhere in this codebase (spec §4.4/§5.1).

export interface ClientConfigFieldOut {
  key: string
  label: string
  kind: 'str' | 'int' | 'bool' | 'secret'
  required: boolean
  default: unknown
  help_text: string | null
}

/** Display grouping only (spec §5.1) -- groups the type picker, and picks nothing else. Never
 * read anywhere as a behavioural branch.
 */
export type ClientFamily = 'usenet' | 'torrent'

export interface ClientTypeOut {
  client_type: string
  family: ClientFamily
  config_schema: ClientConfigFieldOut[]
}

/** The role a base path plays (spec §8.2 correction, migration 028, 2026-08-22) -- not
 * cosmetic, it decides what deleting there means: freeing a `content` root that is hardlinked
 * from a seeding torrent frees nothing; freeing a `working` root frees the space and kills the
 * seed. `unknown` is the honest default for a connector that can't say, or a manually-added
 * path no one has classified.
 */
export type BasePathKind = 'content' | 'working' | 'unknown'

/** One base path to save. Base paths are **detected from the client and SSH-verified, not
 * typed in** (spec §8.2 correction) -- `path` is the SSH-visible path lftpweb actually scans
 * and deletes within (the §10.2 containment boundary); `client_path` records the client's own
 * reported path only when it differs (mirrors `path_queue.arr_visible_path`, migration 018,
 * inverted), purely for display/diagnosis. `source` distinguishes a detected proposal the user
 * confirmed from a manually-typed escape-hatch row, so re-running detection can leave both
 * alone.
 */
export interface DownloadClientBasePathIn {
  path: string
  kind: BasePathKind
  client_path: string | null
  source: 'detected' | 'manual'
}

export interface DownloadClientBasePathOut extends DownloadClientBasePathIn {
  id: number
}

/** One entry in `DownloadClientTestResponse.detected_base_paths` -- what the connector's own
 * `list_base_paths` reported, and whether lftpweb can see it at the same path over SSH.
 * **Detection proposes; it never saves** -- turning one of these into a
 * `DownloadClientBasePathIn` (accepting it, or supplying the SSH-visible path for a
 * `not_found` one) is a separate, explicit action in `ClientsTab.tsx`.
 */
export interface DetectedBasePathOut {
  client_path: string
  kind: BasePathKind
  /** `verified` -- lftpweb sees it at the same path. `not_found` -- the seedbox clearly
   * reports it missing: the namespace mismatch, detected rather than asked about; supply the
   * SSH-visible equivalent. `unverified` -- the stat failed for any other reason (permission,
   * protocol, no SSH connection to try). **`not_found` and `unverified` are deliberately
   * distinct** -- never render an `unverified` path as if it were known to be wrong.
   */
  state: 'verified' | 'not_found' | 'unverified'
  /** The SSH-home expansion of a `~`/relative `client_path`, pre-filled as a suggestion in the
   * `not_found`/`unverified` box below -- never applied automatically (finding #1, 2026-08-23:
   * "give an option in the box with a note ... it appears your ~ path pwd is xxx"). `null` for
   * an already-absolute `client_path`, or when nothing honest could be offered.
   */
  resolved_candidate: string | null
}

export interface DownloadClientCategoryIn {
  category: string
  // `null` = not bound to a queue -- either undecided or explicitly excluded, see `excluded`
  // below for which (spec §8.3, three-state as of migration 031).
  queue_id: number | null
  // Migration 030 (round 4, 2026-08-23) -- mirrors `DownloadClientBasePathIn.source` exactly:
  // whether this row was produced by detection/path-arithmetic (`'client'`) or typed by hand via
  // the "Add category" escape hatch (`'manual'`) -- rTorrent's `list_categories` is DERIVED and
  // can only report labels currently in use, so a category that will exist later can never be
  // detected on its own.
  source: 'client' | 'manual'
  /** Migration 031 (finding #15, 2026-08-23,
   * prompts/2026-08-23-category-tristate-and-exclusion.md): "not used by this instance," saved
   * explicitly rather than inferred from `queue_id` being empty. A category is now three-state
   * (bound / excluded / undecided) rather than two -- before this, "deliberately not used" and
   * "never looked at" were the same on-disk state, so a user who dismissed a category got nagged
   * about it forever with no way to record the decision. **This is a safety boundary, not just a
   * UI preference** (finding #16): `core/disk_review.py` resolves an excluded category into an
   * excluded path wherever it can, and fails closed (suppresses debris for the client's entire
   * base path) wherever it can't. Mutually exclusive with `queue_id` -- the backend rejects a row
   * that sets both.
   */
  excluded: boolean
}

export interface DownloadClientCategoryOut extends DownloadClientCategoryIn {
  id: number
  /** Migration 032 (2026-08-23, prompts/2026-08-23-auto-add-categories-default-excluded.md):
   * when this category was first automatically recorded (a poll pass or a Test, never a manual
   * save) -- `null` for anything that predates this migration, was typed by hand, or survived a
   * Settings save (a pre-existing row's own value carries forward; a save's brand-new row gets
   * `null`, since the user just typed it themselves). `lib/clientCategoryInference.ts.
   * newCategoryCount` compares this against `DownloadClientOut.categories_acknowledged_at` for
   * the "N new since you last looked" signal.
   */
  first_seen_at: string | null
}

/** One path (or sub-path) never scanned, never proposed as debris, and never inside a future
 * delete's containment boundary, on this client's behalf (migration 031, finding #16). **The
 * enforceable primitive** -- an excluded *category* is only ever a convenience that resolves
 * into a path like this one at scan time; this is the direct, always-available expression of
 * "this tree belongs to the other lftpweb instance sharing this seedbox," and the only thing
 * that works when a client's category has no relationship to any path at all (rTorrent).
 */
export interface DownloadClientExcludedPathIn {
  path: string
}

export interface DownloadClientExcludedPathOut extends DownloadClientExcludedPathIn {
  id: number
}

/** A create/update request body. `config` carries every key the selected type's own
 * `config_schema` names, secret and non-secret alike -- the server splits it apart
 * (`api/settings_clients.py`). Omitting every secret key on an update keeps the stored
 * values, the same "unchanged must not mean cleared" rule `ArrInstanceIn.api_key` follows,
 * generalized to however many secret keys a connector's schema declares (all-or-nothing: a
 * request either resends every secret field it wants to keep, or none at all).
 */
export interface DownloadClientIn {
  name: string
  client_type: string
  config: Record<string, unknown>
  enabled: boolean
  base_paths: DownloadClientBasePathIn[]
  categories: DownloadClientCategoryIn[]
  // Migration 031, finding #16 -- see `DownloadClientExcludedPathIn`'s own docstring.
  excluded_paths: DownloadClientExcludedPathIn[]
}

/** Tri-state support level for one operation or field (spec §4.3) --
 * `docs/download-client-api-survey.md` §4.1's conclusion. `note` carries a caveat that matters
 * most for `derived`: the canonical case is rTorrent's seed time, wall-clock since completion
 * rather than true accrued seed time, and the UI must say so wherever this is shown -- never
 * render `native` and `derived` identically.
 */
export type Support = 'native' | 'derived' | 'none'

export interface CapabilityOut {
  support: Support
  note: string | null
}

export interface CapabilitySetOut {
  operations: Record<string, CapabilityOut>
  fields: Record<string, CapabilityOut>
}

export interface DownloadClientOut {
  id: number
  name: string
  client_type: string
  // Non-secret config only -- never the secret sub-values, in any form.
  config: Record<string, unknown>
  has_secret: boolean
  enabled: boolean
  // The probed capability layer (spec §4.1). `null` = never successfully probed.
  capabilities: CapabilitySetOut | null
  capabilities_probed_at: string | null
  version: string | null
  base_paths: DownloadClientBasePathOut[]
  categories: DownloadClientCategoryOut[]
  // Migration 031, finding #16 -- see `DownloadClientExcludedPathIn`'s own docstring.
  excluded_paths: DownloadClientExcludedPathOut[]
  created_at: string
  updated_at: string
  /** The poller's own last-pass status (migration 029, finding #2, 2026-08-23) -- distinct from
   * `has_secret`/`capabilities`/`version` above, which reflect the last manual **Test** click,
   * not what `core.clientsync.ClientSyncScheduler`'s own background pass most recently found.
   * All four `null` = never actually polled yet (a disabled instance, or one too new for the
   * poller's next pass) -- never a false "healthy" default. `last_poll_message` is the failure's
   * own wording ("rejected the configured credential", "unreachable", ...), `null` on success.
   * `last_success_at` is independent of the other three -- it's the positive "has this instance
   * ever worked" signal, so a currently-failing instance that worked yesterday still shows when.
   */
  last_poll_at: string | null
  last_poll_ok: boolean | null
  last_poll_message: string | null
  last_success_at: string | null
  /** Migration 030 (round 4, 2026-08-23) -- the last successful Test's own `detected_categories`,
   * persisted alongside the instance rather than living only in the settings page's own
   * in-memory `testResults[editingId]`, so re-opening a saved instance for edit in a fresh
   * session shows what was last detected (with its age) instead of an empty "never tested" hint.
   * Both `null` until the first successful Test; `[]` is a real, successful "reported none."
   */
  detected_categories: string[] | null
  detected_categories_at: string | null
  /** Migration 031 (Part 3, 2026-08-23) -- the poller's own OBSERVED attribution counts for this
   * instance's most recent informative pass: `attribution_sample_size` is how many transfers had
   * something to attribute at all, `attribution_matched_by_path` is how many of those needed no
   * category mapping because their own path already matched a queue. Both `null` until the
   * poller's first pass. `lib/clientAttribution.ts` turns these into the relevance copy shown
   * next to the category mapping control -- computed from these two numbers alone, never from
   * `client_type`.
   */
  attribution_sample_size: number | null
  attribution_matched_by_path: number | null
  /** Migration 032 (2026-08-23) -- the other half of the "new since you last looked" signal
   * (`DownloadClientCategoryOut.first_seen_at`'s own docstring). `null` = never acknowledged, so
   * every observed category with a `first_seen_at` counts as new. Stamped by
   * `acknowledgeClientCategories`, fired the moment `ClientsTab.tsx` opens this instance for
   * edit -- no separate button, no confirmation.
   */
  categories_acknowledged_at: string | null
}

/** `POST /api/settings/clients/{id}/test` -- `capabilities` always reflects whatever is now on
 * file: unchanged on `ClientUnreachable`/`ClientError` (a transport failure changes no
 * capability, spec §4.2), narrowed by exactly one key on `CapabilityUnavailable`, reset to the
 * connector's static declaration on a fresh success. **Never blanked by a failed test** -- a
 * previously probed set stays exactly as it was; only `ok`/`error_class`/`message` report the
 * failure.
 */
export interface DownloadClientTestResponse {
  ok: boolean
  error_class: string | null
  message: string
  version: string | null
  capabilities: CapabilitySetOut | null
  // Only ever populated on a fresh success (spec §8.2 correction) -- `[]` on any failed test
  // (detection never runs against a connector that couldn't be reached) and `[]` for a
  // connector that doesn't declare `list_base_paths`, which is not an error either.
  detected_base_paths: DetectedBasePathOut[]
  // The client's own reported category names (spec §8.3, joined 2026-08-23) -- same "only on a
  // fresh success" rule as `detected_base_paths`. Unlike a base path, a category has nothing to
  // SSH-verify, so this is a bare name list rather than a per-entry state.
  detected_categories: string[]
}

// --- The disk review scan (docs/download-client-framework-spec.md §11, stage 4 of #18) -----
//
// `POST /api/disk-review/scan` (`api/disk_review.py`, `core/disk_review.py`) -- review-only,
// manual trigger, deletes nothing. `debris` is the only selectable pile; `seeding_estate` is
// shown for visibility only (spec §11.1d). `link_paths` is every on-disk path sharing a
// candidate's inode (including itself) when `nlink > 1` -- the frontend's own `freedBytes`
// (`lib/diskReview.ts`) mirrors `core/disk_review.py.freed_bytes` exactly, so a partial
// selection of a hardlinked pair never reports bytes that a delete wouldn't actually reclaim.
//
// 2026-08-24 (prompts/done/2026-08-24-disk-review-visibility-backend.md, spec §11.1e/§17.6) --
// "exclusion is a delete-safety boundary, not a visibility boundary." `excluded_content` is a
// new fourth pile, and `torrents`/`clients` are new response-level shapes the per-client table
// (this task, prompts/done/2026-08-24-disk-review-table-frontend.md) sections and columns by --
// `broken_seeds`/`DiskReviewBrokenSeedOut` are retired entirely, superseded by `torrents` rows
// with `missing_on_disk=true`.

export interface DiskReviewDebrisOut {
  root: string
  rel_path: string
  abs_path: string
  size: number
  mtime: number
  inode: number | null
  nlink: number | null
  link_paths: string[]
}

export interface DiskReviewSeedingEstateOut {
  root: string
  rel_path: string
  abs_path: string
  size: number
  claimed_by_client_id: number
  claimed_by_client_name: string
  // The claim's own torrent identity (finding #7, 2026-08-23) -- two files sharing a torrent's
  // inode (the seeding-directory copy and its completed-folder hardlink) carry the identical
  // triple, which is what `lib/diskReview.ts.groupSeedingEstateByTorrent` groups by. Display-only
  // -- `core/disk_review.py.reconcile()` itself is unchanged, still per-file.
  claimed_transfer_id: string
  claimed_transfer_name: string
  claimed_content_path: string
  // 2026-08-24 -- migration 031's three-state category, copied onto the claim purely for display
  // (`'bound' | 'excluded' | 'undecided'`, kept as `string` since this mirrors the backend's own
  // `Literal` alias rather than re-declaring it as a union here). A row tagged `'excluded'`
  // appearing in this array (rather than being hidden) is the point of this task's backend half,
  // not a bug -- see `DiskReviewScanResponse`'s own comment.
  attribution: string
  // `${claimed_by_client_id}:${claimed_transfer_id}` -- joins a file row to its own summary row
  // in `DiskReviewScanResponse.torrents` without a second fetch. Matches `DiskReviewTorrentOut.
  // claim_key` exactly.
  claim_key: string
}

/** The fourth pile, 2026-08-24 (spec §11.1e/§17.6) -- one on-disk file under an excluded path
 * with no claim currently covering it right now (the other lftpweb instance's client dropped its
 * history entry, or the torrent was removed, while the bytes are still sitting there). Never
 * selectable, never debris, never counted toward a reclaim total -- `excluded_path` names which
 * excluded root matched, so its absence from every other pile is explained, not merely felt.
 */
export interface DiskReviewExcludedContentOut {
  root: string
  rel_path: string
  abs_path: string
  size: number
  excluded_path: string
  link_paths: string[]
}

export interface DiskReviewSkippedBasePathOut {
  root: string
  reason: string
}

// The third pile (finding #17, 2026-08-23) -- a genuinely unclaimed file under a root where some
// client's excluded category could not be resolved to a path. Shown, not counted -- replaces the
// earlier bare-count `DiskReviewSuppressedDebrisOut`. Same shape as DiskReviewDebrisOut plus
// `reason`, so it groups and reclaim-totals the same way (lib/diskReview.ts), but it is never
// selectable through the ordinary debris flow. See core.disk_review.UnclaimedItem's own
// docstring for why a file claimed by an excluded category never appears here (or anywhere).
export interface DiskReviewUnclaimedOut extends DiskReviewDebrisOut {
  reason: string
}

/** One row per claim, 2026-08-24 (spec §11.1e/§17.6) -- supersedes the retired
 * `DiskReviewBrokenSeedOut`/`broken_seeds` entirely (a broken seed is exactly
 * `missing_on_disk=true` here). `size_bytes`/`uploaded_bytes`/`ratio`/`seed_time_s` are `null`
 * rather than `0` whenever the reporting client doesn't declare the equivalent field capability
 * (every SABnzbd row's `ratio`/`uploaded_bytes`/`seed_time_s`, per `USENET_BASELINE`) -- see
 * `lib/diskReviewSort.ts.visibleTorrentColumns`, the one place that turns a client's declared
 * capabilities into which of those three columns render at all, never a `client_type` check.
 * `file_count`/`size_on_disk` are `null` (not `0`) whenever the claim's own root was never
 * walked, or the claim reported no `content_path` at all -- absent information is not a verdict.
 * `missing_on_disk=true, file_count=0` is the one case that *is* a real, walked, empty zero.
 */
export interface DiskReviewTorrentOut {
  client_id: number
  transfer_id: string
  transfer_name: string
  content_path: string | null
  category: string | null
  attribution: string
  size_bytes: number | null
  uploaded_bytes: number | null
  ratio: number | null
  seed_time_s: number | null
  added_at: string | null
  raw_status: string | null
  phase: string | null
  file_count: number | null
  size_on_disk: number | null
  missing_on_disk: boolean
  claim_key: string
}

export interface DiskReviewClientFailureOut {
  client_id: number
  client_name: string
  reason: string
}

/** One row per **enabled** `download_client` instance, 2026-08-24 (spec §11.1e/§17.6) -- the
 * roster `DiskReviewPage.tsx` sections the torrent table by. `capabilities` is a flat
 * `Field name -> support level` mapping (`'native' | 'derived' | 'none'`), the same flattened
 * shape `core.disk_review.ClientSummary`'s own docstring explains -- deliberately not the typed
 * `CapabilitySetOut` used elsewhere on the wire, because the only decision this page makes from
 * it is "does this client declare `ratio`/`uploaded_bytes`/`seed_time_s`" (spec §17.2's "the UI
 * is driven by the declaration, never by the client's name" -- `client_type` below is display
 * metadata only, read by nothing that decides what renders). Empty for a client never
 * successfully probed.
 */
export interface DiskReviewClientOut {
  client_id: number
  name: string
  client_type: string
  reachable: boolean
  failure_reason: string | null
  capabilities: Record<string, string>
}

/** The whole scan result. `debris` is the only selectable pile; `seeding_estate` is shown for
 * visibility only, `unclaimed` (finding #17) is shown but gated off the ordinary
 * select-and-remove flow, and `excluded_content` (2026-08-24) is shown but never selectable
 * either -- see each pile's own `*Out` docstring.
 *
 * **2026-08-24 -- the governing principle this whole shape follows: exclusion is a delete-safety
 * boundary, not a visibility boundary.** A claim whose category is marked "not used by this
 * instance" is no longer dropped before it can appear anywhere; its files show up in
 * `seeding_estate` tagged `attribution: 'excluded'`, and its own row in `torrents` can be
 * reported `missing_on_disk` like any other claim. None of this touches what may ever be
 * deleted.
 */
export interface DiskReviewScanResponse {
  debris: DiskReviewDebrisOut[]
  seeding_estate: DiskReviewSeedingEstateOut[]
  skipped_base_paths: DiskReviewSkippedBasePathOut[]
  // 2026-08-23, finding #17 -- shown as its own pile, not selectable through the ordinary debris
  // flow. See DiskReviewUnclaimedOut's own comment for how this differs from skipped_base_paths.
  unclaimed: DiskReviewUnclaimedOut[]
  // The fourth pile, 2026-08-24 -- see DiskReviewExcludedContentOut's own comment.
  excluded_content: DiskReviewExcludedContentOut[]
  // One row per claim, 2026-08-24 -- see DiskReviewTorrentOut's own comment. Supersedes the
  // retired `broken_seeds` field entirely.
  torrents: DiskReviewTorrentOut[]
  // The per-client roster, 2026-08-24 -- see DiskReviewClientOut's own comment. This is what
  // DiskReviewPage.tsx sections the torrent table by -- there is exactly one seedbox host in
  // this product today (core/engine.py.load_host_config is "single, v1"), so the client is the
  // only grouping axis worth building.
  clients: DiskReviewClientOut[]
  client_failures: DiskReviewClientFailureOut[]
  scanned_at: string
}

// --- Settings -> Post-processing (phase 5, DESIGN.md §6) -------------------------------

export interface PostprocessSettingsOut {
  verify_enabled: boolean
  verify_hash_on_disk: boolean
  extract_enabled: boolean
  extract_target_dir: string | null
  extract_passwords: string[]
  // Off by default -- deletes an item's spent archive volumes once they've extracted
  // successfully (2026-08-13). See core/local_delete.py.delete_extracted_archives.
  delete_archives_after_extract: boolean
  move_enabled: boolean
  concurrency: number
}

export type PostprocessSettingsIn = PostprocessSettingsOut

// --- Settings -> the settle gate (prompts/open-issues.md #2, `core/settle.py`) ---------

export interface SettleSettingsOut {
  enabled: boolean
  // Stage 2b of #18 (prompts/2026-08-23-settle-gate-skip.md), reworked 2026-08-29
  // (prompts/done/2026-08-29-settle-verify-under-existing-toggle.md) -- see
  // core/settle.py.SettleSettings.client_skip_enabled's own docstring for why this now defaults
  // true independently of `enabled`.
  client_skip_enabled: boolean
  // Read-only -- core/settle.py.REQUIRED_SETTLE_SCANS / SETTLE_MIN_AGE_S. Not settable from
  // this API; surfaced only so the Settings page can explain what the gate requires without
  // hardcoding numbers that could drift from the backend's own constants.
  required_scans: number
  min_age_s: number
}

// `enabled`/`client_skip_enabled` are writable; `required_scans`/`min_age_s` are informational.
// The backend PUT merges over the stored value for whichever of these two a request omits
// (`api/settings_postprocess.py.put_settle_settings`), so a caller sending only one field never
// resets the other.
export interface SettleSettingsIn {
  enabled?: boolean
  client_skip_enabled?: boolean
}

// --- Settings -> the removal grace period (core/mount_sentinel.py, DESIGN.md §7.3) -----
//
// GET-only -- no `...In` counterpart. `DEFAULT_GRACE_S` isn't a per-install setting this
// phase (core/mount_sentinel.py's own comment); surfaced only so the Files page's removal-
// grace countdown (2026-08-14, prompts/2026-08-14-removal-grace-countdown.md) reads the real
// window instead of a second, hand-maintained 600 that could drift from the backend's own
// constant -- the same reasoning as SettleSettingsOut's required_scans/min_age_s above.

export interface RemovalGraceSettingsOut {
  grace_s: number
  /** The states the grace clock can actually run for -- `core/mount_sentinel.py.COMPLETE_STATES`,
   * shipped rather than duplicated here so a new post-processing state added on the Python side
   * can't silently stop being eligible in the UI. `lib/format.ts.REMOVAL_GRACE_ELIGIBLE_STATES`
   * is a bootstrap default for the render before this fetch resolves, not a second source of
   * truth; `tests/test_settings_api.py` pins the equality against the live set.
   */
  eligible_states: string[]
}

// --- Settings -> "folder prefix during transfer" (core/download_prefix.py) -------------
//
// Site-wide default; a queue's own `download_prefix_enabled`/`download_prefix` (PathQueueOut,
// above) can independently override either half. Off by default -- unlike the settle gate,
// this one was not given the "ships on" exception (see core/download_prefix.py's docstring):
// it changes where in-flight bytes physically live, which an install with a transfer already
// running when it upgrades would notice immediately.

export interface DownloadPrefixSettingsOut {
  enabled: boolean
  prefix: string
}

export type DownloadPrefixSettingsIn = DownloadPrefixSettingsOut

// --- Settings -> auto-queue (`core/autoqueue.py.AutoQueueSettings`) ---------------------
//
// Site-level, default false. Governs only whether an item something *outside* lftpweb removed
// (an `*arr` importer, a human, a script) is eligible to be re-fetched by auto-queue --
// lftpweb's own deletions (Files-page delete, retention) are never re-fetched regardless of
// this setting. Only matters for `copy`-mode queues; `move` deletes the remote copy on
// completion, so there is nothing left to re-fetch either way.

export interface AutoQueueSettingsOut {
  re_download_externally_removed: boolean
}

export type AutoQueueSettingsIn = AutoQueueSettingsOut

// --- Settings -> Queues -> Patterns (phase 4, DESIGN.md §3.1 `pattern`, §4.7) -----------

export type PatternKind = 'select' | 'skip' | 'file_exclude'

export interface PatternIn {
  queue_id: number | null // null = global, applies to every queue
  kind: PatternKind
  expr: string
  enabled: boolean
}

export interface PatternOut extends PatternIn {
  id: number
}

export interface PatternPreviewRequest {
  patterns: PatternIn[]
  patterns_only: boolean
}

export interface PatternPreviewItem {
  rel_path: string
  is_dir: boolean
  matched: boolean
}

export interface PatternPreviewFile {
  rel_path: string
  excluded: boolean
}

export interface PatternPreviewResponse {
  items: PatternPreviewItem[]
  sample_item: string | null
  sample_files: PatternPreviewFile[]
}

export interface QueueAutoQueueStatus {
  mount_ok: boolean
  gated_reason: string | null
}

// --- Settings -> Queues path-browse dialog (GitHub issue #4,
// prompts/done/2026-08-16-path-browse-dialog.md) -- api/browse.py. One shared response shape
// for both the local (container filesystem) and remote (seedbox over SFTP) endpoints.

export interface BrowseEntry {
  name: string
}

export interface BrowseResponse {
  path: string
  parent: string | null
  entries: BrowseEntry[]
  truncated: boolean
  // Set only when the endpoint had to walk up from what was actually requested -- see
  // core/browse.py's own docstring. `null` means `path` is exactly what was asked for.
  fallback_from: string | null
}

// --- Files (phase 2, DESIGN.md §9.2) ----------------------------------------------------

// Lifecycle facets (2026-08-13, prompts/2026-08-13-lifecycle-icons.md,
// core/itemview.py._lifecycle_facets) -- R(emote)/L(ocal)/V(erified)/E(xtracted), derived
// server-side from the same persisted row `state` comes from, so there is exactly one place
// that decides what a fact means. `level` drives color; `reason` plus this row's own raw
// size/timestamp fields (below) is what `FileTree.tsx` builds a tooltip sentence from --
// deliberately not a pre-formatted string, the same split `stateAgeLabel` already uses for
// `state`/`state_changed_at`.
export type FacetLevel = 'green' | 'amber' | 'red' | 'dim'

export interface LifecycleFacet {
  level: FacetLevel
  reason: string
}

export interface LifecycleFacets {
  remote: LifecycleFacet
  local: LifecycleFacet
  verified: LifecycleFacet
  extracted: LifecycleFacet
}

export interface FileNode {
  id: number | null
  rel_path: string
  is_dir: boolean
  state: string
  // The settle gate (prompts/open-issues.md #2): 'settling' for a top-level REMOTE_ONLY item
  // whose remote fingerprint hasn't held still for 2 consecutive scans yet. 'removing'
  // (2026-08-13, prompts/2026-08-13-delete-state-truthfulness.md) for the whole subtree of an
  // in-progress delete (core/local_delete.py.delete_local). null otherwise.
  substate: string | null
  // item.suppressed_reason (2026-08-13, prompts/2026-08-13-delete-state-truthfulness.md), null
  // unless auto_queue_suppressed is set server-side. Drives the "Re-Download" action label
  // below -- a self-delete ('deleted_local') reads differently from every other suppression
  // reason and from an unsuppressed REMOVED_LOCAL/REMOVED_BOTH row.
  suppressed_reason: string | null
  remote_size: number | null
  local_size: number | null
  remote_mtime: number | null
  // The local-side counterpart to `remote_mtime` (migration 011, 2026-08-13,
  // prompts/2026-08-13-files-detail-inspector.md) -- the item drawer's "modified date, both
  // sides" reading. Files only, null for a directory, mirroring `remote_mtime`'s own convention
  // (core/reconcile.py -- see that module for why the local side deliberately stays consistent
  // rather than inventing a directory rule of its own).
  local_mtime: number | null
  // When `state` last actually changed value (migration 006), stamped by that migration's own
  // triggers. null only for a row the migration's backfill genuinely couldn't date -- render
  // gracefully rather than assuming a value. Not the same question as "when did it complete"
  // (downloaded_at, the planned local-retention feature's key) -- a DOWNLOADED item that dips
  // to PARTIAL and back moves this without earning a fresh retention lease.
  state_changed_at: string | null
  // When this row was first ever seen (migration 001) -- the first entry in the item drawer's
  // lifecycle chronology (2026-08-13). Existed server-side since phase 2; new to the wire only.
  first_seen_at: string | null
  // The settle gate's countdown (2026-08-13, prompts/2026-08-13-files-ux-pass.md item 3):
  // item_settle.matched_scans/updated_at (core/settle.py.SettleRecord), joined in only for
  // top-level rows and only while substate === 'settling' -- null the rest of the time,
  // including for a non-top-level row (item_settle has no row for one at all) or before this
  // item's first scan. See core/itemview.py.item_view's own docstring for why this is gated
  // on substate rather than passed through whenever the join happens to have a row (an
  // ungated read would make this climb forever on a row nothing else about is changing,
  // which would defeat the WebSocket delta's "only publish what changed" property).
  settle_matched_scans: number | null
  settle_first_matched_at: string | null
  // The settle gate's *other* display state (2026-08-13,
  // prompts/2026-08-13-settle-progress-visibility.md, migration 013): a top-level item that
  // hasn't been confirmed unchanged even once yet (settle_matched_scans === 1 -- a first-ever
  // sighting, or the fingerprint changed on the most recent scan and reset the count; see
  // lib/format.ts.isStillArriving) has nothing useful to say via the countdown above.
  // settle_total_bytes (item_settle.total_bytes, already computed as part of the fingerprint)
  // is what a "still arriving" reading watches climb; settle_first_observed_at/
  // settle_last_changed_at answer "how long have we watched this" / "when did it last move."
  // Gated on substate === 'settling' exactly like the two fields above, and null for the same
  // reasons those two are, plus one more: a pre-migration item_settle row that hasn't changed
  // since carries null for these two timestamps specifically (core/settle.py.SettleRecord) --
  // render that as "unknown," never a fabricated time.
  settle_total_bytes: number | null
  settle_first_observed_at: string | null
  settle_last_changed_at: string | null
  // Milestone/audit timestamps (2026-08-13) -- raw material for a lifecycle icon's tooltip.
  // `downloaded_at` already existed server-side (§7.3's retention key); the other four are new
  // to the wire only.
  downloaded_at: string | null
  verified_at: string | null
  extracted_at: string | null
  first_missing_at: string | null
  remote_deleted_at: string | null
  // "Folder prefix during transfer" (core/download_prefix.py): the exact prefix string this
  // item's local root is *currently* written under, null when nothing is in flight under a
  // prefixed name. Never part of rel_path -- purely the item drawer's "where does this
  // actually live right now" answer.
  pending_download_prefix: string | null
  // `deleted_archive.deleted_at` (2026-08-14, prompts/2026-08-14-extracted-archives-rest-as-
  // extracted.md), joined the same optional way as the settle fields above. null unless this
  // rel_path is a spent archive volume `core/local_delete.py.delete_extracted_archives` removed
  // after a successful extraction -- the row's own `state` already reads `EXCLUDED` for this
  // reason (never through the removal-grace clock). `lib/format.ts.isDeletedArchiveVolume` is
  // the one place that turns this into the chip substitution.
  deleted_archive_at: string | null
  // Sonarr/Radarr integration (migration 018, docs/arr-integration-spec.md): the Files page's
  // *arr icon reads `arr_status` directly. A facet, not a lifecycle state -- passed through
  // verbatim from `item.arr_status`/`item.arr_status_at` (`core/itemview.py.item_view`), null
  // for every item on a queue with no bound instance (or one the poller hasn't matched yet).
  // `null | 'detected' | 'notified' | 'imported' | 'cleaned' | 'gone'`, kept as `string | null`
  // here (not a literal union) since this type mirrors the wire shape and the backend's own
  // `item.arr_status` column is a plain `TEXT`, not a `CHECK`-constrained enum -- see
  // `lib/fileTree.ts.arrIconVariant` for the one place that switches on the known values and
  // degrades an unrecognized one to the neutral icon rather than rendering nothing.
  arr_status: string | null
  arr_status_at: string | null
  // Which download client fetched this item (2026-08-30, prompts/2026-08-30-client-chip-on-
  // files-tree.md, migration 033): `download_client.name`/`client_type`, joined via `item.
  // download_client_id` the same way `arr_status` above joins `arr_instance` -- the identical
  // `client_instance_name`/`client_instance_kind` field names `JobOut`/`HistoryJobOut` already
  // carry for the Transfers/History row chip (`components/LifecycleIcons.tsx.ClientBrandMark`).
  // Unlike `arr_status`, this arrives per-item straight off this node -- there is no queue-level
  // resolution step the way the *arr *kind* needs (`FileTree.tsx`'s own `ArrRowChip` call site
  // comment explains why that one is different). null for both whenever this item has no
  // recorded client: every item downloaded before migration 033 shipped, or one the poller
  // hasn't matched a transfer's own path to yet.
  client_instance_name: string | null
  client_instance_kind: string | null
  facets: LifecycleFacets
}

// `POST /api/items/{item_id}/delete`'s optional body (2026-08-16, the delete dialog's
// independent Local/Source scopes, prompts/2026-08-16-manual-delete-local-and-remote.md).
// Omitted entirely means exactly the pre-existing behavior (`local=True, source=False`);
// `client.ts.deleteItem` always sends both explicitly instead, so every call site says what it
// means rather than relying on the backend's own default.
export interface DeleteItemRequest {
  local: boolean
  source: boolean
}

// `POST /api/items/{item_id}/delete` (prompts/open-issues.md "7 + 8" -- the first delete
// endpoint in this API). A request that accomplishes *nothing at all* is a non-2xx response
// (client.ts's `sendJson` throws) -- this shape describes every request that succeeded at
// least partially. `deleted`/`reason`/`bytes_freed` describe the **local** scope, unchanged
// from before the Source scope existed; `source_deleted`/`source_reason` are `null` when
// `source` was not requested, and otherwise describe that independent outcome -- see
// `api/jobs.py.DeleteItemResponse`'s own docstring for why a combined request can report
// `deleted: true` alongside `source_deleted: false` rather than throwing.
export interface DeleteItemResponse {
  deleted: boolean
  reason: string
  bytes_freed: number | null
  source_deleted: boolean | null
  source_reason: string | null
}

// --- Reset item tracking (2026-08-13, prompts/2026-08-13-reset-item-tracking.md) -----------
//
// Distinct from Delete (above, removes bytes) and from Clear History (a few pixels away on
// the History page, which removes job/event rows and never touches an item at all) -- this
// forgets an item's tracking outright, so a suppressed or failed path can be reused.

export interface ResetItemResponse {
  reset: boolean
  reason: string
  affected_rel_paths: string[]
}

export interface QueueResetRequest {
  /** Must equal the queue's own `queue_name` exactly -- the whole-queue scope's typed
   * confirmation, checked again server-side as defense in depth. */
  confirm_name: string
}

export interface ResetSummaryResponse {
  reset_top_level: number
  withheld: { rel_path: string; reason: string }[]
  affected_count: number
}

export interface ResetPatternPreviewRequest {
  pattern: string
}

export interface ResetPatternPreviewItem {
  rel_path: string
  is_dir: boolean
  remote_size: number | null
  local_size: number | null
}

export interface ResetPatternPreviewResponse {
  items: ResetPatternPreviewItem[]
}

export interface QueueFiles {
  queue_id: number
  queue_name: string
  scanned_at: string | null
  error: string | null
  // A *soft* note (DESIGN.md §5) -- set when the last scan skipped one or more unreadable
  // remote subtrees (core/remote.py's scan-abort fix, phase 3b) rather than failing
  // outright. Distinct from `error`, which means the whole scan failed and the tree shown
  // is stale.
  warning: string | null
  // DESIGN.md §7.3's mount sentinel, required starting phase 4. `null`/absent before this
  // queue has ever scanned or on the WebSocket's own queue shape (which doesn't carry this
  // field -- see hooks/useLiveModel.ts); `false` means auto-queue is currently gated off for
  // it regardless of its own toggle. Optional so the REST (`GET /api/files`) and WS-derived
  // shapes can share this one interface without the WS side fabricating a value.
  mount_ok?: boolean | null
  nodes: FileNode[]
}

export interface FilesResponse {
  queues: QueueFiles[]
}

// --- Jobs / transfer engine (phase 3a API, phase 3b UI -- DESIGN.md §4, §9.2 Transfers) ---

export type JobKind = 'mirror' | 'pget'
export type JobState = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
export type Lane = 'main' | 'small'

export interface JobOut {
  id: number
  item_id: number
  queue_id: number
  queue_name: string
  // The queue's short display name (migration 024, `path_queue.short_name`) -- `null` when
  // unset. Added 2026-08-19 (docs/transfers-redesign-spec.md §3.6, phase 1 stage 4a) for the
  // ungrouped Transfers row's queue badge (`lib/queueDisplayName.ts`).
  queue_short_name: string | null
  rel_path: string
  is_dir: boolean
  kind: JobKind
  state: JobState
  lane: Lane
  rank: number
  attempt: number
  queued_at: string
  started_at: string | null
  finished_at: string | null
  pid: number | null
  // The allocation this job was admitted with (DESIGN.md §4.5/§9.1) -- fixed for its
  // lifetime, distinct from `speed_bps` (what it's *actually* pulling right now). Under
  // admission control a job can hold its full allocation while pulling far less of it.
  rate_limit_bps: number | null
  // "Start now" as a menu, not a single button (2026-08-19,
  // prompts/done/2026-08-19-start-now-bandwidth-fractions.md) -- widened from a plain
  // `forced_full_rate: boolean`. `null` means never force-started; `1.0` means Max
  // (byte-identical to the old `true`); `0.1`/`0.25`/`0.5`/`0.75` are the new menu options.
  forced_rate_fraction: number | null
  bytes_start: number
  bytes_done: number
  bytes_total: number | null
  speed_bps: number | null
  eta_s: number | null
  exit_code: number | null
  error_class: string | null
  // `null` on a row from `getCompleteJobs` (2026-08-19, docs/transfers-redesign-spec.md §3.2,
  // phase 1 stage 4b) -- that endpoint is paginated but unbounded in total row count, so it
  // never inlines this ~4KB blob (the identical trap `HistoryJobOut`'s own comment names for
  // History's list endpoint). `getJobs` (the Active/pending box) stays bounded by construction
  // and keeps inlining it unchanged. `has_output_tail` below is the one signal a row's expand
  // panel needs to decide whether to fetch it on demand, regardless of which endpoint it came
  // from.
  output_tail: string | null
  // Mirrors `HistoryJobOut.has_output_tail` -- always populated. `TransfersPage.tsx.
  // RowDetailPanel` fetches on demand via the existing `getHistoryJobOutput` (same `job` table,
  // same id) exactly when this is `true` and `output_tail` came back `null`.
  has_output_tail: boolean
  // 2026-08-15 (prompts/2026-08-15-transfers-single-line-rows-with-detail.md): the item-level
  // facts the Transfers row's expand panel needs -- see `api/jobs.py._job_out`/`core/queue.py.
  // list_jobs`'s own comments for the join these ride on. Mirrors `FileNode.verified_at`/
  // `extracted_at`/`remote_deleted_at`/`arr_status`/`arr_status_at` (`lib/fileTree.ts`) exactly,
  // plus `arr_instance_name` -- resolved server-side here (unlike the Files page, which resolves
  // it client-side from `GET /api/settings/arr` -- see `lib/fileTree.ts`'s own comment on why
  // that page does it differently) since `JobOut`'s row set is already bounded.
  verified_at: string | null
  extracted_at: string | null
  remote_deleted_at: string | null
  arr_status: string | null
  arr_status_at: string | null
  // `null` whenever this job's queue has no bound *arr instance -- the signal
  // `lib/transferPanel.ts.hasArrGroup` gates the panel's *arr group on.
  arr_instance_name: string | null
  // The bound instance's `kind` ('sonarr' | 'radarr', migration 018's CHECK constraint) --
  // added 2026-08-16 (prompts/2026-08-16-arr-chip-on-row-lines.md) for the row chip's
  // brand-logo choice (`components/LifecycleIcons.tsx.ArrRowChip`). `arr_instance_name` is
  // free text the user can rename to anything, so it can't drive which logo to draw; `kind` is
  // the one field that reliably says which. `null` under the same condition
  // `arr_instance_name` is null. Kept as `string | null` (not a literal union), same reasoning
  // `FileNode.arr_status`'s own comment gives -- an unrecognized value degrades to a text chip
  // rather than being rejected at the type level.
  arr_instance_kind: string | null
  // 2026-08-30 (prompts/2026-08-30-downloader-icon-on-rows.md, migration 033): the download-
  // client instance that fetched this item, resolved server-side via `item.download_client_id ->
  // download_client.name`/`client_type` -- the identical shape `arr_instance_name`/
  // `arr_instance_kind` just above already have. `null` whenever this item has no recorded
  // client: every item downloaded before migration 033 shipped (forward-only, no backfill --
  // docs/decisions.md), or one the poller hasn't matched a transfer's own reported path to yet.
  // `client_instance_kind` is the connector registry key ('sabnzbd' | 'rtorrent' today) --
  // `components/LifecycleIcons.tsx.ClientBrandMark`'s own display switch, kept as `string | null`
  // rather than a literal union for the identical "an unrecognized value degrades gracefully"
  // reason `arr_instance_kind` above documents.
  client_instance_name: string | null
  client_instance_kind: string | null
  // **Which box this row belongs in** (2026-08-20, docs/transfers-redesign-spec.md §3.2's
  // pipeline-completion rule) -- `true` = Active/pending, `false` = Complete. Computed
  // server-side by `core/pipeline_flight.py`, the same expression `GET /api/jobs/complete`
  // filters its listing *and its `total`* on. Never re-derived here: the Active box is
  // client-side and the Complete box is server-paginated, so a second encoding of the rule would
  // drift and put a row in both boxes or neither. Optional so a response from an older server
  // still type-checks and degrades to "complete" -- the same fail-safe direction the predicate
  // itself takes for anything unknown.
  pipeline_in_flight?: boolean
  // What the row is waiting on ('verifying' | 'extracting' | 'processing' | 'awaiting_import' |
  // 'deleting_source'), or `null`. From the *same* `CASE` as `pipeline_in_flight`, so the label
  // and the box can never disagree. `null` for a queued/running row -- the state chip already
  // says DOWNLOADING/QUEUED. `string | null`, not a literal union, for the same
  // unrecognized-value-degrades-gracefully reason `arr_instance_kind` above documents.
  pipeline_waiting_reason?: string | null
  // The manual escape hatch (migration 025) -- 'complete' | 'failed' once a human resolved this
  // item out of the Active box. **A classification only**; the row shows it so a manual
  // resolution never silently reads as a normal completion.
  manual_outcome?: string | null
  manual_outcome_at?: string | null
}

export interface JobsResponse {
  jobs: JobOut[]
}

/** `GET /api/queue/preflight` (docs/transfers-redesign-spec.md §4, prefigured; this task's own
 * handoff prompt, prompts/done/2026-08-20-preflight-box.md, plus its follow-up
 * prompts/2026-08-20-preflight-waiting-sources.md) -- one row for something lftpweb already
 * knows about but has no work to do on yet. **Source-agnostic by construction**: the *arr
 * poller and the settle gate's own eligibility check are the two sources wired up --
 * `source`/`source_label`/`source_kind` are how a row names *which* upstream it came from, never
 * a field of their own assuming it's always the *arr. `components/PreflightBox.tsx`'s
 * `SourceChip` -- gated on `row.source === 'arr'` -- is where *arr-specific rendering lives, not
 * here.
 *
 * Deliberately thin, matching the backend's own `PreflightRowOut` (`backend/lftpweb/models.py`)
 * field for field -- no `id`, no `queue_position`, no `bytes_done`: there is no `item` and no
 * `job` behind a row here, and the handoff prompt's own "the rows are inert, and the box is what
 * makes that structural" is exactly why nothing here invites a per-row control that would need
 * one.
 */
/** One upstream's own pre-merge view of a row (finding #3, 2026-08-23) -- mirrors the backend's
 * `PreflightContributorOut` field for field. Read-only provenance, never itself branched on
 * beyond picking a badge/detail block to render (`lib/preflight.ts`).
 */
export interface PreflightContributorOut {
  source: string
  source_label: string
  source_kind: string | null
  status_label: string | null
  size_bytes: number | null
  size_remaining_bytes: number | null
}

export interface PreflightRowOut {
  source: string
  queue_id: number
  // The bound queue's own display identity (2026-08-21, "we moved the columns around" fix) --
  // mirrors `JobOut.queue_name`/`queue_short_name` so `lib/queueDisplayName.ts.
  // queueDisplayName` renders this row's tag identically to every Transfers row's own tag.
  queue_name: string
  queue_short_name: string | null
  title: string
  status_label: string | null
  source_label: string
  source_kind: string | null
  size_bytes: number | null
  size_remaining_bytes: number | null
  // How many seconds until this row's own source expects its wait to clear (2026-08-21, "we
  // missed the remaining time") -- an *arr row's own `timeleft`, rendered through the same
  // `formatEta`/`transferLineValue` shape the Transfers row already uses for its own ETA. `null`
  // when the source has no meaningful estimate this pass -- never a fabricated or zero figure. A
  // settle-gated row always carries `null` here -- its own remaining figure is `size_bytes`
  // above ("remote -- 22 GB"), not a time.
  remaining_s: number | null
  // The download client actually fetching this release, from the *arr's own point of view --
  // an *arr row's own `downloadClient`, read server-side straight from its queue record's `raw`.
  // `null` for a settle row (no separate download client in that source's own model) or an *arr
  // row whose response didn't happen to carry one. Display-only provenance for the chip tooltip
  // (`lib/preflight.ts.preflightChipTooltip`), never branched on.
  download_client: string | null
  // A generic "how far along has this row's own wait gotten" detail for the chip's own hover
  // tooltip (2026-08-21, "the settling chip should have a mouseover that shows time details")
  // -- `backend/lftpweb/core/preflight.py.PreflightRow.wait_scans`/`wait_since`'s own docstring
  // has the full reasoning. `wait_since` is already an ISO-8601 string on the wire
  // (`core/settle.py.SettleProgress.first_matched_at`). `null` for an *arr row (its own wait
  // isn't bound by scan count) or a settle row with no `item_settle` history yet -- both
  // fields together, never one alone. Fed straight into `lib/format.ts.settleWaitLabel` by
  // `lib/preflight.ts.preflightChipTooltip`, the same helper the Files tree and the lifecycle
  // R-icon tooltip already share, rather than a third copy of that wording.
  wait_scans: number | null
  wait_since: string | null
  // Both contributors' own pre-merge view for a row deduped across the *arr and a download
  // client (finding #3, 2026-08-23) -- `[]` for a row from a single source (exactly one badge,
  // no empty second slot); exactly two entries, *arr then client, for a merged one. Every field
  // above this line already reflects the §9.2-precedence winner; this is the losing side's own
  // reading, kept for the row's badges and its expand.
  contributors: PreflightContributorOut[]
}

/** One line of the Preflight box's mount-gate banner (2026-08-20,
 * prompts/2026-08-20-preflight-waiting-sources.md, decided with the user) -- **a banner, not
 * rows**: `core/autoqueue.py.AutoQueue.gated` blocks a queue's whole auto-queue pass at once, so
 * this names the queue and the reason once, never one row per affected item. `reason` is
 * `AutoQueue.gated`'s own string, verbatim.
 */
export interface PreflightGatedQueueOut {
  queue_name: string
  reason: string
}

/** One line of the Preflight box's "this client reports items, none attributable" banner
 * (finding #2, 2026-08-23, prompts/2026-08-23-tilde-and-visibility.md) -- the mount-gate
 * banner's own shape, applied to a different silent drop: a configured, authenticating, enabled
 * download-client instance whose category -> queue mapping doesn't cover what it's currently
 * reporting. `count` is never `0` -- a quiet, fully-attributed client has nothing to say here,
 * so it simply isn't in the list. `client_id` (finding #13, 2026-08-23) lets the banner deep-link
 * straight to this specific instance in Settings -> Clients (`lib/clientEditLink.ts`) rather than
 * naming a settings path for the user to navigate by hand.
 */
export interface PreflightUnattributedClientOut {
  client_id: number
  client_name: string
  count: number
  /** Widened round 4 (2026-08-23, live evidence): `count` alone told a user *that* a client had
   * unattributable items, never *which* categories to go map -- a client that already had
   * `ar-tv` mapped left the user guessing what else needed one. Distinct, sorted category names
   * seen among this pass's unattributable items, **excluding** "no category at all" -- that's
   * `no_category_count`'s own job below, a different problem with a different fix.
   */
  categories: string[]
  /** Count of unattributable items that carried no category at all -- "this client isn't
   * labelling its downloads" is a different problem than "map this category," and conflating the
   * two would send a user chasing a mapping that was never the issue.
   */
  no_category_count: number
}

/** `source_configured=false` (with `rows` always empty in that case) means "no row source is
 * configured at all" -- `components/PreflightBox.tsx` hides the row list for that case rather
 * than showing an empty "Nothing in preflight" that would be meaningless for a user with nothing
 * configured. `gated_queues`/`unattributed_clients` are both independent of `source_configured`
 * -- the mount gate, and an unattributable client, can each have something to say whether or not
 * either row source is configured, so the box itself renders whenever *any* of the three does.
 */
export interface PreflightResponse {
  source_configured: boolean
  rows: PreflightRowOut[]
  gated_queues: PreflightGatedQueueOut[]
  unattributed_clients: PreflightUnattributedClientOut[]
}

/** `GET /api/jobs/complete` (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase 1 stage
 * 4b) -- the Queue tab's **Complete** box, server-side paginated. Same `total`/`limit`/`offset`
 * shape `HistoryJobsResponse` already established below -- reused rather than a second
 * pagination idiom; `lib/pagination.ts` is the pure page-arithmetic this response feeds.
 */
export interface CompleteJobsResponse {
  jobs: JobOut[]
  total: number
  limit: number
  offset: number
}

/** `POST /api/jobs/dismiss-all` (2026-08-15) -- the bulk counterpart to `dismissJob`. */
export interface DismissAllResponse {
  dismissed: number
}

/** `GET /api/items/{id}/events` (2026-08-15) -- the Transfers panel's on-demand "processing
 * story" fetch, one `event` row per entry, newest first, server-capped.
 */
export interface ItemEventOut {
  id: number
  ts: string
  level: string
  kind: string
  message: string
  job_id: number | null
}

export interface ItemEventsResponse {
  events: ItemEventOut[]
}

/** `GET /api/items/{id}/children` (2026-08-20, docs/transfers-redesign-spec.md §3.3, phase 1
 * stage 5) -- the Transfers row's on-demand per-file expansion. `children` is `FileNode`, the
 * same `core/itemview.py.item_view` projection every other consumer of the `item` table reads
 * through -- never a second shape invented for this one panel. `total`/`limit`/`offset` are the
 * same paging trio `HistoryJobsResponse`/`CompleteJobsResponse` already use: `total` is the true
 * descendant-file count regardless of the server-side cap (`api/jobs.py.
 * ITEM_CHILDREN_MAX_LIMIT`), so a capped response can still say "showing N of total" honestly.
 */
export interface ItemChildrenResponse {
  children: FileNode[]
  total: number
  limit: number
  offset: number
}

export interface QueueItemRequest {
  item_id: number
  start_now: boolean
}

// --- Settings -> Transfer (phase 3a API, DESIGN.md §4.5/§9.2/§9.3) -----------------------
//
// Mirrors `core/queue.py.TransferSettings` -- twelve editable fields, one site-wide set
// (DESIGN.md §4.5: "a queue governs what and where, never how fast"), plus one read-only
// derived number (below). Bandwidth/size fields are `_bps`/`_bytes` on the wire; TransferTab.tsx
// converts to/from MB(/s) at the edge, this type stays in the backend's native units so a
// round-trip through the API never drifts.
export interface TransferSettingsIn {
  /** The **ceiling** -- Settings → Transfer's own field, and since 2026-08-21
   * (`prompts/done/2026-08-21-bandwidth-ceiling-and-autocommit.md`) no longer the number the
   * Queue tab's slider writes: that slider owns a *throttle* within this ceiling, and the limit
   * actually in force is `effective_bandwidth_bps` on `TransferSettingsOut` below.
   */
  max_bandwidth_bps: number
  max_concurrent_transfers: number
  small_item_threshold_bytes: number
  small_lane_concurrency: number
  // null = derived (10% of the ceiling, min 1 MB/s, capped at half the ceiling -- see
  // `effective_small_lane_reserve_bps()`'s docstring in core/queue.py). TransferTab.tsx must
  // compute and show that effective value itself; the server doesn't send it separately.
  small_lane_reserve_bps: number | null
  min_share_floor_bps: number
  mirror_parallel_transfer_count: number
  mirror_use_pget_n: number
  pget_default_n: number
  max_attempts: number
  retry_backoff_base_s: number
  extra_lftp_settings: string
}

/** ...plus the one read-only number the two-value bandwidth model adds. **The limit actually in
 * force** -- the Queue-tab throttle when one is set, `max_bandwidth_bps` otherwise -- and what
 * the scheduler allocates against, so it is what the slider displays and what a "Start now"
 * fraction is a fraction of. Never sent back on a PUT: the throttle is
 * `POST /api/queue/bandwidth`'s to write, and including it here would let a stale Settings form
 * silently undo a throttle set on another page. "Is a throttle in force?" reads as
 * `effective_bandwidth_bps < max_bandwidth_bps`.
 */
export interface TransferSettingsOut extends TransferSettingsIn {
  effective_bandwidth_bps: number
}

/** `POST /api/queue/bandwidth`'s response (2026-08-21,
 * `prompts/done/2026-08-21-bandwidth-from-the-queue-page.md`) -- what the Queue tab's bandwidth
 * slider actually did. `effective_bandwidth_bps` is the throttle **as applied**, which is not
 * always what was asked for: a value above the ceiling is clamped to it rather than refused, so
 * the banner and the optimistic echo both read this rather than the requested number.
 * `interrupted` is how many running transfers were stopped and re-queued so the scheduler could
 * re-admit them against the new limit (`0` for a new-items-only change, and for an
 * apply-to-in-progress with nothing running); `skipped_because_paused` marks the case where the
 * number was written but the queue's pause -- including a timed pause's deadline -- was
 * deliberately left untouched, and the banner must then say nothing was restarted.
 */
export interface QueueBandwidthResponse {
  effective_bandwidth_bps: number
  interrupted: number
  skipped_because_paused: boolean
}

// --- Settings -> Transfer's "effective lftp settings" readout (2026-08-14,
// prompts/2026-08-14-show-effective-lftp-settings.md) -------------------------------------
//
// Read-only and credential-free by construction -- see `core/lftp.py.effective_tuning_settings`
// and `api/jobs.py.get_effective_lftp_settings`'s own module comment for why. Re-exported from
// `lib/effectiveLftpSettings.ts` rather than duplicated -- that module's collision-detection
// pure functions are typed against these same shapes.
export type { EffectiveLftpJobKind, EffectiveLftpSetting } from '../lib/effectiveLftpSettings'

export interface EffectiveLftpSettingsOut {
  kinds: import('../lib/effectiveLftpSettings').EffectiveLftpJobKind[]
  bandwidth_note: string
}

// --- History (phase 6, DESIGN.md §9.2 History page) ---------------------------------------

/** Deliberately not `JobOut` -- that shape carries `output_tail` inline because the
 * Transfers page's row set is bounded by construction. History has no such bound (a busy
 * install accumulates thousands of terminal jobs), so `output_tail` (~4KB/row) is fetched
 * on demand via `getHistoryJobOutput` instead of shipped in every list row -- see
 * `has_output_tail`.
 */
export interface HistoryJobOut {
  id: number
  item_id: number
  queue_id: number
  queue_name: string
  rel_path: string
  is_dir: boolean
  kind: JobKind
  state: 'succeeded' | 'failed' | 'cancelled'
  attempt: number
  queued_at: string
  started_at: string | null
  finished_at: string | null
  bytes_total: number | null
  bytes_done: number
  exit_code: number | null
  error_class: string | null
  has_output_tail: boolean
  // Migration 016 (2026-08-13) -- when this job was dismissed from the Transfers page, or
  // `null` if it never was. History shows every terminal job either way (dismissal only ever
  // hides a Transfers row); this just answers "did I dismiss this."
  dismissed_at: string | null
  // The same *arr facts `JobOut` carries (2026-08-16, prompts/2026-08-16-arr-chip-on-row-lines.md)
  // -- `item.arr_status`/`item.arr_status_at` plus the bound instance's `name`/`kind`, joined by
  // `api/history.py.list_history_jobs` the identical way `core/queue.py.list_jobs()` does, so
  // `HistoryJobsSection.tsx` can render the same `ArrRowChip`. `null` whenever this job's queue
  // has no bound *arr instance.
  arr_status: string | null
  arr_status_at: string | null
  arr_instance_name: string | null
  arr_instance_kind: string | null
  // 2026-08-30 (prompts/2026-08-30-downloader-icon-on-rows.md, migration 033) -- the same
  // `item.download_client_id -> download_client.name`/`client_type` join `JobOut` carries, joined
  // here the identical way `arr_instance_name`/`arr_instance_kind` above are. `null` whenever this
  // item has no recorded client (forward-only migration, no backfill -- see `JobOut`'s own
  // comment).
  client_instance_name: string | null
  client_instance_kind: string | null
}

/** One queue's honest aggregate over the whole filtered set, not just the loaded page
 * (2026-08-16, prompts/2026-08-16-history-jobs-group-collapse.md) -- `HistoryJobsResponse.jobs`
 * is one `LIMIT`/`OFFSET` page, so a client-side sum over it would be wrong whenever more rows
 * match the filter than are loaded. `backend/lftpweb/api/history.py._queue_summaries` computes
 * this with a bounded `GROUP BY` against the exact same filter as the `jobs` list beside it.
 * History's job domain is terminal-only, so unlike the Transfers page's `QueueGroupCounts`
 * (`lib/transferPanel.ts`) there is no `active`/`queued` bucket here.
 */
export interface HistoryQueueSummaryOut {
  queue_id: number
  queue_name: string
  succeeded: number
  failed: number
  cancelled: number
  total_bytes_done: number
}

export interface HistoryJobsResponse {
  jobs: HistoryJobOut[]
  total: number
  limit: number
  offset: number
  queue_summaries: HistoryQueueSummaryOut[]
}

export interface HistoryJobOutputOut {
  job_id: number
  error_class: string | null
  output_tail: string | null
}

export interface HistoryEventOut {
  id: number
  ts: string
  level: 'debug' | 'info' | 'warning' | 'error'
  kind: string
  message: string
  item_id: number | null
  job_id: number | null
  queue_id: number | null
  queue_name: string | null
  rel_path: string | null
}

export interface HistoryEventsResponse {
  events: HistoryEventOut[]
  total: number
  limit: number
  offset: number
}

export interface HistoryJobsFilter {
  // One item's own transfer history (2026-08-13, prompts/2026-08-13-files-detail-inspector.md)
  // -- the item drawer's bounded "load on open" fetch. Mirrors `HistoryEventsFilter.item_id`.
  item_id?: number
  queue_id?: number
  state?: 'succeeded' | 'failed' | 'cancelled'
  error_class?: string
  since?: string
  until?: string
  limit?: number
  offset?: number
}

export interface HistoryEventsFilter {
  kind?: string
  level?: 'debug' | 'info' | 'warning' | 'error'
  item_id?: number
  queue_id?: number
  since?: string
  until?: string
  limit?: number
  offset?: number
}

/** The response shape for every `DELETE` under `/api/history/*` (2026-08-13,
 * prompts/2026-08-13-clear-history.md) -- one row, a filtered batch, or everything. `deleted`
 * is the actual row count the server removed, not the pre-delete `total` the confirmation
 * prompt showed -- the two can differ if something else changed the rows in between.
 */
export interface HistoryClearResponse {
  deleted: number
}

// --- Settings -> Backup (phase 7, DESIGN.md §10.2) --------------------------------------

export interface BackupSettingsOut {
  interval_days: number
  keep_count: number
}

export type BackupSettingsIn = BackupSettingsOut

export interface BackupInfoOut {
  filename: string
  size_bytes: number
  created_at: string
}

export interface BackupListResponse {
  backups: BackupInfoOut[]
}

// --- Metrics / Dashboard (this task -- DESIGN.md new section proposed, docs/decisions.md) --

export interface MetricsSettingsOut {
  retention_days: number
}

export type MetricsSettingsIn = MetricsSettingsOut

// 2026-08-17 (prompts/done/2026-08-17-bytes-chart-7d-30d-ranges-and-total.md): the two Dashboard
// charts' range selectors are independent and no longer offer the same option list -- the
// speed chart (Chart 2) keeps its original 1h/12h/24h window (fine-grained speed over a month
// would average away exactly the spikes it exists to show); the bytes chart (Chart 1, see
// api/metrics.py's `_RANGES` comment) gained 7d/30d instead of 12h, since a speed-chart-style
// short window says little about total bytes moved. `MetricsRange` stays the
// union both narrower types feed into `MetricsThroughputResponse.range`/`getThroughput`, which
// don't otherwise care which selector a given range came from.
export type SpeedRange = '1h' | '12h' | '24h'
// 2026-08-21 (daily rollups, prompts/done/2026-08-21-daily-metric-rollups.md): 90d/1y read the
// new `metric_daily` table instead of the raw ones (api/metrics.py's `_DAILY_RANGES`) -- the
// only thing that changes for the frontend is that `retentionNoteForRange` (lib/bytesChart.ts)
// doesn't apply the raw-retention note to them.
export type BytesRange = '24h' | '7d' | '30d' | '90d' | '1y'
export type MetricsRange = SpeedRange | BytesRange

// 2026-08-21 (chart grouping, prompts/done/2026-08-21-chart-grouping.md): the bytes chart's
// group-by control -- independent of `BytesRange` (which only says how far back). Not every
// value is available at every range (`lib/bytesChart.ts.groupOptionsForRange` mirrors
// `api/metrics.py._AVAILABLE_GROUPS`; the server independently rejects the same combination,
// never trust the client alone).
export type MetricsGroup = 'hour' | 'day' | 'week' | 'month'

export interface MetricsBucketOut {
  ts: string
  // `false` = no heartbeat fell in this bucket at all -- lftpweb wasn't running. Render as a
  // gap, never a zero (docs/decisions.md's idle-vs-down decision).
  up: boolean
  total_bytes: number | null
  // JSON object keys are always strings on the wire -- queue_id -> bytes moved this bucket.
  by_queue: Record<string, number>
  // 2026-08-21: fraction (0.0-1.0) of a full day's expected heartbeats actually observed --
  // only set on a daily-granularity bucket (the 90d/1y ranges at group=day, sourced from
  // `metric_daily`); `null` for every raw-table-sourced bucket (group=hour or group=day at
  // 1h/12h/24h/7d/30d), where `up` alone is already exact at that bucket's own width. Lets the
  // UI tell a genuinely quiet day (`up: true`, `coverage` near 1.0) apart from one lftpweb was
  // mostly down for (`coverage` well under 1.0).
  //
  // 2026-08-21 (chart grouping): for a `week`/`month` bucket (any range), this is instead the
  // fraction of *days* in the bucket that were `up` -- see `api/metrics.py._aggregate_day_points`
  // for why that's a different (and deliberately simpler) computation than the per-day case.
  coverage?: number | null
}

export interface MetricsThroughputResponse {
  range: MetricsRange
  // 2026-08-21 (chart grouping): the bucket width actually used -- `null` for the speed chart's
  // own untouched fixed-width ranges (1h/12h), which don't have a group-by control.
  group: MetricsGroup | null
  bucket_seconds: number
  buckets: MetricsBucketOut[]
}

// 2026-08-21 (daily rollups): the Dashboard's "total downloaded" readout --
// `GET /api/metrics/total`, `core/metrics.py.total_bytes`.
export interface MetricsTotalOut {
  total_bytes: number
  // Earliest UTC calendar day (`'YYYY-MM-DD'`) this total actually covers, or `null` when
  // there's no rolled-up history yet (a fresh install) -- say "since <date>", never imply an
  // unbounded history.
  since_day: string | null
}

// --- Settings -> Logs (phase 7, DESIGN.md §10.1) -----------------------------------------

export interface LogFileOut {
  name: string
  size_bytes: number
  modified_at: string
  is_current: boolean
}

export interface LogFilesResponse {
  files: LogFileOut[]
}

export interface LogTailResponse {
  lines: string[]
  // True when the bounded read hit its byte cap before satisfying `lines` -- a level filter
  // may be under-showing what's actually in the file (core/logtail.py never re-scans further
  // back just to satisfy a filter). See core/logtail.py's module docstring.
  truncated: boolean
}

export type LogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL'

// --- Support bundle (Settings -> Logs, 2026-08-17) --------------------------------------
//
// `POST /api/support-bundle` -- mirrors `backend/lftpweb/models.py`'s `SupportBundleRequest`.
// lftpweb's own logs are always included server-side regardless of what's sent here (the
// dialog shows that checkbox checked and disabled), so there is no field for them.

export interface SupportBundleRequest {
  include_environment: boolean
  include_settings: boolean
  include_events: boolean
  include_jobs: boolean
  arr_instance_ids: number[]
}

// --- Auth (phase 8, DESIGN.md §8) -------------------------------------------------------

export type AuthMode = 'none' | 'password' | 'proxy'

export interface AuthSettingsOut {
  mode: AuthMode
  proxy_header: string
  proxy_trusted_cidrs: string[]
  has_user: boolean
  username: string | null
}

// Mirrors AuthSettingsIn — `username`/`new_password` are only consulted when `mode ===
// 'password'`, and are required together the first time a user is created (the backend
// refuses to store `mode: 'password'` with nobody able to log in — DESIGN.md §8).
export interface AuthSettingsIn {
  mode: AuthMode
  proxy_header: string
  proxy_trusted_cidrs: string[]
  username?: string | null
  new_password?: string | null
}

export interface ChangePasswordIn {
  current_password: string
  new_password: string
}

export interface LoginIn {
  username: string
  password: string
}

/** GET /api/auth/session — "whoami," always reachable unauthenticated so the SPA can decide
 * whether to render the login form at all (see `hooks/useAuth.tsx`).
 */
export interface AuthSessionOut {
  mode: AuthMode
  authenticated: boolean
  username: string | null
  // Present only when authenticated via a password-mode session — attached as
  // `X-CSRF-Token` on every mutating request afterwards.
  csrf_token: string | null
}

export interface ApiKeyOut {
  id: number
  name: string
  created_at: string
  last_used_at: string | null
}

export interface ApiKeyIn {
  name: string
}

// Plaintext `key` — present only in the create response, shown once, never again.
export interface ApiKeyCreatedOut extends ApiKeyOut {
  key: string
}
