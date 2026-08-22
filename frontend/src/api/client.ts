import type {
  ApiKeyCreatedOut,
  ApiKeyIn,
  ApiKeyOut,
  ArrInstanceIn,
  ArrInstanceOut,
  ArrPollSettingsIn,
  ArrPollSettingsOut,
  ArrTestResponse,
  AuthSessionOut,
  AuthSettingsIn,
  AuthSettingsOut,
  AutoQueueSettingsIn,
  AutoQueueSettingsOut,
  BrowseResponse,
  ChangePasswordIn,
  BackupInfoOut,
  BackupListResponse,
  BackupSettingsIn,
  BackupSettingsOut,
  ClientTypeOut,
  CompleteJobsResponse,
  DeleteItemResponse,
  DismissAllResponse,
  DownloadClientIn,
  DownloadClientOut,
  DownloadClientTestResponse,
  DownloadPrefixSettingsIn,
  DownloadPrefixSettingsOut,
  EffectiveLftpSettingsOut,
  FilesResponse,
  HealthResponse,
  HistoryClearResponse,
  HistoryEventsFilter,
  HistoryEventsResponse,
  HistoryJobOutputOut,
  HistoryJobsFilter,
  HistoryJobsResponse,
  HostIn,
  HostOut,
  HostTestRequest,
  ItemChildrenResponse,
  ItemEventsResponse,
  JobOut,
  JobsResponse,
  LoginIn,
  LogFilesResponse,
  LogTailResponse,
  MetricsGroup,
  MetricsRange,
  MetricsSettingsIn,
  MetricsSettingsOut,
  MetricsThroughputResponse,
  MetricsTotalOut,
  PathQueueIn,
  PathQueueOut,
  PatternIn,
  PatternOut,
  PatternPreviewRequest,
  PatternPreviewResponse,
  PostprocessSettingsIn,
  PostprocessSettingsOut,
  PreflightResponse,
  QueueAutoQueueStatus,
  QueueBandwidthResponse,
  QueueResetRequest,
  RemovalGraceSettingsOut,
  ResetItemResponse,
  ResetPatternPreviewRequest,
  ResetPatternPreviewResponse,
  ResetSummaryResponse,
  SettleSettingsIn,
  SettleSettingsOut,
  StatsResponse,
  SupportBundleRequest,
  TestConnectionResponse,
  TransferSettingsIn,
  TransferSettingsOut,
} from './types'

// The CSRF token issued at login (DESIGN.md §8) — held in memory only, never localStorage
// (nothing durable needs to survive a page reload; `hooks/useAuth.tsx` re-fetches it from
// `GET /api/auth/session` on mount instead). `setCsrfToken` is called by that hook whenever
// a login/session response carries one.
let csrfToken: string | null = null

export function setCsrfToken(token: string | null): void {
  csrfToken = token
}

const MUTATING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) {
    throw new Error(`${path} responded ${res.status}`)
  }
  return (await res.json()) as T
}

async function sendJson<T>(path: string, method: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  // Attached whenever we have one, regardless of mode — a no-op in `none`/`proxy` mode (the
  // backend only checks it for a password-mode session, middleware.py) and required for
  // every mutating call once a password-mode session exists.
  if (csrfToken && MUTATING_METHODS.has(method)) headers['X-CSRF-Token'] = csrfToken

  const res = await fetch(path, {
    method,
    headers: Object.keys(headers).length ? headers : undefined,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`${method} ${path} responded ${res.status}${detail ? `: ${detail}` : ''}`)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export function getHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>('/api/health')
}

export function getStats(): Promise<StatsResponse> {
  return getJson<StatsResponse>('/api/stats')
}

// --- Settings -> Connection -------------------------------------------------------------

export function getHost(): Promise<HostOut | null> {
  return getJson<HostOut | null>('/api/settings/host')
}

export function putHost(body: HostIn): Promise<HostOut> {
  return sendJson<HostOut>('/api/settings/host', 'PUT', body)
}

export function testHost(body?: HostTestRequest): Promise<TestConnectionResponse> {
  return sendJson<TestConnectionResponse>('/api/settings/host/test', 'POST', body ?? {})
}

// --- Settings -> Queues ------------------------------------------------------------------

export function listQueues(): Promise<PathQueueOut[]> {
  return getJson<PathQueueOut[]>('/api/settings/queues')
}

export function createQueue(body: PathQueueIn): Promise<PathQueueOut> {
  return sendJson<PathQueueOut>('/api/settings/queues', 'POST', body)
}

export function updateQueue(id: number, body: PathQueueIn): Promise<PathQueueOut> {
  return sendJson<PathQueueOut>(`/api/settings/queues/${id}`, 'PUT', body)
}

export function deleteQueue(id: number): Promise<void> {
  return sendJson<void>(`/api/settings/queues/${id}`, 'DELETE')
}

export function getAutoQueueStatus(queueId: number): Promise<QueueAutoQueueStatus> {
  return getJson<QueueAutoQueueStatus>(`/api/settings/queues/${queueId}/autoqueue-status`)
}

// --- Settings -> Queues path-browse dialog (GitHub issue #4,
// prompts/done/2026-08-16-path-browse-dialog.md) ------------------------------------------

/** Omit `path` (or pass `''`) to open at the container filesystem root. */
export function browseLocal(path?: string): Promise<BrowseResponse> {
  return getJson<BrowseResponse>(`/api/browse/local${path ? `?path=${encodeURIComponent(path)}` : ''}`)
}

/** Omit `path` (or pass `''`) to open at the SSH user's home directory. Rejects (throws) with
 * a 409 whose message names why -- no host configured, or credentials need re-entry -- shown
 * verbatim by `PathBrowseDialog.tsx`, the same "err.message carries the server's own detail"
 * convention every other settings error in this app already follows.
 */
export function browseRemote(path?: string): Promise<BrowseResponse> {
  return getJson<BrowseResponse>(`/api/browse/remote${path ? `?path=${encodeURIComponent(path)}` : ''}`)
}

// --- Settings -> Queues -> Patterns (phase 4, DESIGN.md §3.1 `pattern`, §4.7) -----------

export function listPatterns(queueId?: number): Promise<PatternOut[]> {
  const qs = queueId != null ? `?queue_id=${queueId}` : ''
  return getJson<PatternOut[]>(`/api/settings/patterns${qs}`)
}

export function createPattern(body: PatternIn): Promise<PatternOut> {
  return sendJson<PatternOut>('/api/settings/patterns', 'POST', body)
}

export function updatePattern(id: number, body: PatternIn): Promise<PatternOut> {
  return sendJson<PatternOut>(`/api/settings/patterns/${id}`, 'PUT', body)
}

export function deletePattern(id: number): Promise<void> {
  return sendJson<void>(`/api/settings/patterns/${id}`, 'DELETE')
}

/** The live "what would this match" preview (DESIGN.md §4.7, §9.2) -- evaluates an *unsaved*
 * pattern set against the queue's current remote tree.
 */
export function previewPatterns(
  queueId: number,
  body: PatternPreviewRequest,
): Promise<PatternPreviewResponse> {
  return sendJson<PatternPreviewResponse>(
    `/api/settings/queues/${queueId}/pattern-preview`,
    'POST',
    body,
  )
}

// --- Settings -> Integrations (migration 018, docs/arr-integration-spec.md) -------------

export function listArrInstances(): Promise<ArrInstanceOut[]> {
  return getJson<ArrInstanceOut[]>('/api/settings/arr')
}

export function createArrInstance(body: ArrInstanceIn): Promise<ArrInstanceOut> {
  return sendJson<ArrInstanceOut>('/api/settings/arr', 'POST', body)
}

/** `api_key` omitted (or `null`/`undefined`) keeps the previously stored key -- the browser
 * never has the plaintext to send back. See `ArrInstanceIn`'s own docstring.
 */
export function updateArrInstance(id: number, body: ArrInstanceIn): Promise<ArrInstanceOut> {
  return sendJson<ArrInstanceOut>(`/api/settings/arr/${id}`, 'PUT', body)
}

export function deleteArrInstance(id: number): Promise<void> {
  return sendJson<void>(`/api/settings/arr/${id}`, 'DELETE')
}

/** The Settings UI's Test button -- `GET /api/v3/system/status` round trip. Never rejects for
 * a reachable-but-erroring instance (see `ArrTestResponse`'s own docstring); only a genuine
 * HTTP/network failure against lftpweb's own API throws here.
 */
export function testArrInstance(id: number): Promise<ArrTestResponse> {
  return sendJson<ArrTestResponse>(`/api/settings/arr/${id}/test`, 'POST')
}

// --- Settings -> Clients (migration 027, docs/download-client-framework-spec.md, stage 1b of
// #18) -------------------------------------------------------------------------------------

/** The registry's declared connector types (spec §6, §8.1) -- each with its own connection-
 * form schema, so `ClientsTab.tsx` renders one generic form per connector instead of one
 * hand-written form per client.
 */
export function listClientTypes(): Promise<ClientTypeOut[]> {
  return getJson<ClientTypeOut[]>('/api/settings/client-types')
}

export function listClientInstances(): Promise<DownloadClientOut[]> {
  return getJson<DownloadClientOut[]>('/api/settings/clients')
}

export function createClientInstance(body: DownloadClientIn): Promise<DownloadClientOut> {
  return sendJson<DownloadClientOut>('/api/settings/clients', 'POST', body)
}

/** Every secret key omitted from `body.config` keeps the stored secret unchanged -- the
 * browser never has the plaintext to send back. See `DownloadClientIn`'s own docstring.
 */
export function updateClientInstance(id: number, body: DownloadClientIn): Promise<DownloadClientOut> {
  return sendJson<DownloadClientOut>(`/api/settings/clients/${id}`, 'PUT', body)
}

export function deleteClientInstance(id: number): Promise<void> {
  return sendJson<void>(`/api/settings/clients/${id}`, 'DELETE')
}

/** The Settings UI's Test button -- never rejects for a reachable-but-erroring instance, or
 * for an unreachable one; the failure is reported in `message`/`error_class`, the same
 * "test tells you what's wrong, doesn't throw" shape `ArrTestResponse` already uses. Only a
 * genuine HTTP/network failure against lftpweb's own API throws here.
 */
export function testClientInstance(id: number): Promise<DownloadClientTestResponse> {
  return sendJson<DownloadClientTestResponse>(`/api/settings/clients/${id}/test`, 'POST')
}

/** *arr poll cadence (2026-08-21, issue #16) -- `core/arrsync.py.ArrSettings.poll_interval_s`
 * exposed here for the first time. Server-side validated on `PUT`; see `ArrPollSettingsOut`'s
 * own docstring.
 */
export function getArrPollSettings(): Promise<ArrPollSettingsOut> {
  return getJson<ArrPollSettingsOut>('/api/settings/arr/poll-interval')
}

export function putArrPollSettings(body: ArrPollSettingsIn): Promise<ArrPollSettingsOut> {
  return sendJson<ArrPollSettingsOut>('/api/settings/arr/poll-interval', 'PUT', body)
}

// --- Settings -> Post-processing (phase 5, DESIGN.md §6) --------------------------------

export function getPostprocessSettings(): Promise<PostprocessSettingsOut> {
  return getJson<PostprocessSettingsOut>('/api/settings/postprocess')
}

export function putPostprocessSettings(
  body: PostprocessSettingsIn,
): Promise<PostprocessSettingsOut> {
  return sendJson<PostprocessSettingsOut>('/api/settings/postprocess', 'PUT', body)
}

// --- Settings -> the settle gate (prompts/open-issues.md #2, `core/settle.py`) ---------

export function getSettleSettings(): Promise<SettleSettingsOut> {
  return getJson<SettleSettingsOut>('/api/settings/settle')
}

export function putSettleSettings(body: SettleSettingsIn): Promise<SettleSettingsOut> {
  return sendJson<SettleSettingsOut>('/api/settings/settle', 'PUT', body)
}

// --- Settings -> the removal grace period (core/mount_sentinel.py, DESIGN.md §7.3) -----

export function getRemovalGraceSettings(): Promise<RemovalGraceSettingsOut> {
  return getJson<RemovalGraceSettingsOut>('/api/settings/removal-grace')
}

// --- Settings -> "folder prefix during transfer" (core/download_prefix.py) -------------

export function getDownloadPrefixSettings(): Promise<DownloadPrefixSettingsOut> {
  return getJson<DownloadPrefixSettingsOut>('/api/settings/download-prefix')
}

export function putDownloadPrefixSettings(
  body: DownloadPrefixSettingsIn,
): Promise<DownloadPrefixSettingsOut> {
  return sendJson<DownloadPrefixSettingsOut>('/api/settings/download-prefix', 'PUT', body)
}

// --- Settings -> auto-queue (`core/autoqueue.py.AutoQueueSettings`) ---------------------

export function getAutoQueueSettings(): Promise<AutoQueueSettingsOut> {
  return getJson<AutoQueueSettingsOut>('/api/settings/autoqueue')
}

export function putAutoQueueSettings(body: AutoQueueSettingsIn): Promise<AutoQueueSettingsOut> {
  return sendJson<AutoQueueSettingsOut>('/api/settings/autoqueue', 'PUT', body)
}

// --- Files ---------------------------------------------------------------------------------

export function getFiles(): Promise<FilesResponse> {
  return getJson<FilesResponse>('/api/files')
}

export function rescanFiles(): Promise<{ triggered: boolean }> {
  return sendJson<{ triggered: boolean }>('/api/files/rescan', 'POST')
}

// --- Jobs / transfer engine (DESIGN.md §4, §9.2 Transfers) -------------------------------

export function getJobs(): Promise<JobsResponse> {
  return getJson<JobsResponse>('/api/jobs')
}

/** The Queue tab's Complete box (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase 1
 * stage 4b) -- `getJobs` above stays the Active/pending box's own bounded fetch, unchanged;
 * terminal jobs live here now, server-side paginated and (optionally) filtered. Same
 * `queryString` helper `getHistoryJobs` already uses below, for the same reason: an
 * `undefined` field is simply omitted from the query string rather than sent as the literal
 * string `"undefined"`.
 */
export function getCompleteJobs(params: {
  nameFilter?: string
  limit?: number
  offset?: number
}): Promise<CompleteJobsResponse> {
  return getJson<CompleteJobsResponse>(
    `/api/jobs/complete${queryString({
      name_filter: params.nameFilter,
      limit: params.limit,
      offset: params.offset,
    })}`,
  )
}

/** Manual queue (§4.7): always wins over auto-queue suppression. `startNow` requests the
 * "start now at max bandwidth" admission path (§4.5) at the moment of queueing.
 */
export function queueItem(itemId: number, startNow = false): Promise<JobOut> {
  return sendJson<JobOut>('/api/jobs', 'POST', { item_id: itemId, start_now: startNow })
}

export function stopJob(jobId: number): Promise<void> {
  return sendJson<void>(`/api/jobs/${jobId}/stop`, 'POST')
}

export function moveJobToTop(jobId: number): Promise<void> {
  return sendJson<void>(`/api/jobs/${jobId}/move-to-top`, 'POST')
}

/** The chevron reorder controls (2026-08-19, docs/transfers-redesign-spec.md §3.4 stage 2,
 * prompts/2026-08-19-queue-reorder-chevrons.md) -- ▲ up one / ▼ down one / ▲▲ to top, one
 * endpoint for all three (`api/jobs.py.move_job`) rather than three near-identical calls.
 * `sendJson` throws on a non-2xx response -- a 404 (unknown job) or 409 (the job is no longer
 * `queued`, e.g. it started running between the page render and the click) both surface as a
 * thrown `Error`, the same shape every other job action on this page already reports through
 * `withBusy`. Already-at-the-edge and a single-job queue are silent 204 no-ops server-side, not
 * errors -- the frontend's own `canMoveUp`/`canMoveDown` (`lib/transferPanel.ts`) additionally
 * disable the buttons for those cases so the request is rarely even sent.
 */
export type MoveDirection = 'up' | 'down' | 'top'

export function moveJob(jobId: number, direction: MoveDirection): Promise<void> {
  return sendJson<void>(`/api/jobs/${jobId}/move`, 'POST', { direction })
}

/** Dismiss a terminal (`failed`/`cancelled`) job from the Transfers page (2026-08-13,
 * prompts/done/2026-08-13-dismiss-terminal-jobs.md) -- the row's own record stays in History;
 * this only stops it showing here. Rejects (non-2xx, `sendJson` throws) for a `queued`/
 * `running` job -- see `core/queue.py.dismiss_job`.
 */
export function dismissJob(jobId: number): Promise<void> {
  return sendJson<void>(`/api/jobs/${jobId}/dismiss`, 'POST')
}

/** "Dismiss all" at the top of the Transfers page (2026-08-15, user addition to
 * prompts/2026-08-15-transfers-single-line-rows-with-detail.md) -- one server-side bulk call
 * (`core/queue.py.dismiss_all_terminal`), not a client-side loop over `dismissJob` for every
 * dismissable row.
 *
 * `queueId` (2026-08-17, the Transfers group header's own "Dismiss Queue" control,
 * prompts/2026-08-17-transfers-dismiss-per-queue.md) scopes the same bulk call to one queue's
 * own terminal jobs. Omitted (the pre-existing call every caller before this task still makes)
 * sends no body at all -- byte-for-byte the original request -- matching
 * `api/jobs.py.dismiss_all_jobs`'s own "omitted body means every queue" contract. No caller in
 * this app passes it any more (the per-queue "Dismiss Queue" control it served was removed
 * 2026-08-19 alongside grouping, docs/transfers-redesign-spec.md §3.1) -- kept on the client
 * exactly because the server-side scope (`DismissAllRequest.queue_id`) is kept too, per the
 * same task's own instruction not to remove it.
 *
 * `jobIds` (2026-08-19, prompts/2026-08-19-transfers-name-filter.md) scopes the same bulk call
 * to an explicit set of job ids -- kept on both the client and server for the identical reason
 * `queueId` is (`DismissAllRequest.job_ids`'s own docstring). No caller in this app passes it
 * either as of phase 1 stage 4b: "Dismiss list" now uses `nameFilter` below instead, since the
 * Complete box it scopes is server-paginated and an explicit id list can only ever name one
 * page's worth (`nameFilter`'s own comment).
 *
 * `nameFilter` (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase 1 stage 4b) --
 * "Dismiss list"'s own scope now that the Complete box is server-paginated: the same filter
 * text the box's own `getCompleteJobs` call is showing, so the server dismisses every matching
 * row, not just the current page (`models.py.DismissAllRequest.name_filter`'s own docstring has
 * the full reasoning). An empty string is a real, sendable value (`nameFilter === ''` would
 * mean "matches every row"), so this only omits the field when `nameFilter` is `undefined` --
 * `!= null`, not truthiness.
 *
 * `outcome` (2026-08-20, follow-up to phase 1 stage 4b, the Complete box's own "Dismiss" menu,
 * `lib/transferPanel.ts.dismissMenuOptions`) narrows the same bulk call to one terminal state.
 * **Composes with `nameFilter`** -- unlike `queueId`/`jobIds`, both may be sent together
 * (`models.py.DismissAllRequest`'s own restructured validator allows it; see that model's
 * docstring and `docs/decisions.md` for the decided reasoning). `TransfersPage.tsx.
 * handleDismissOutcome` sends the box's own current (debounced) name filter alongside whichever
 * outcome the user picked, so the dismiss always matches what the box is currently showing.
 *
 * `queueId` and `jobIds` stay mutually exclusive with every other scope, including each other
 * (`DismissAllRequest`'s own validator rejects an incoherent combination); no caller in this app
 * sends either alongside `outcome`/`nameFilter`. Threaded the same "omitted means not sent" way
 * every scope on this call already is, not five functions -- they're all optional narrowings
 * (or, for `queueId`/`jobIds`, exclusive scopes) of the same one bulk call.
 */
export function dismissAllJobs(
  queueId?: number,
  jobIds?: number[],
  nameFilter?: string,
  outcome?: JobOut['state'],
): Promise<DismissAllResponse> {
  const body: { queue_id?: number; job_ids?: number[]; name_filter?: string; outcome?: string } = {}
  if (queueId != null) body.queue_id = queueId
  if (jobIds != null) body.job_ids = jobIds
  if (nameFilter != null) body.name_filter = nameFilter
  if (outcome != null) body.outcome = outcome
  return sendJson<DismissAllResponse>(
    '/api/jobs/dismiss-all',
    'POST',
    Object.keys(body).length > 0 ? body : undefined,
  )
}

/** The Transfers panel's on-demand "processing story" (2026-08-15) -- one item's `event` rows,
 * newest first, server-capped. Fetched only when a row's panel is expanded, never eagerly for
 * the whole jobs list (`api/jobs.py.item_events`'s own docstring).
 */
export function getItemEvents(itemId: number, limit?: number): Promise<ItemEventsResponse> {
  return getJson<ItemEventsResponse>(`/api/items/${itemId}/events${limit != null ? `?limit=${limit}` : ''}`)
}

/** The Transfers row's per-file expansion (2026-08-20, docs/transfers-redesign-spec.md §3.3,
 * phase 1 stage 5) -- fetched once, when a directory row's panel is expanded, never eagerly for
 * the whole jobs list (`api/jobs.py.item_children`'s own docstring; the same "on demand" shape
 * `getItemEvents` above already establishes for this page). The response is already capped
 * server-side (`ItemChildrenResponse.total` says the true count); once expanded, live updates
 * come from the WebSocket this page already has open (`useLiveModel`'s `item_delta`/
 * `child_progress` messages, merged client-side by `lib/transferPanel.ts.mergeFileListChildren`)
 * rather than a second call here -- see `TransfersPage.tsx`'s own comment on why.
 */
export function getItemChildren(itemId: number): Promise<ItemChildrenResponse> {
  return getJson<ItemChildrenResponse>(`/api/items/${itemId}/children`)
}

/** "Start now" (DESIGN.md §4.5), now a menu -- 10%/25%/50%/75%/Max of the site total limit
 * (2026-08-19, prompts/done/2026-08-19-start-now-bandwidth-fractions.md). `ratePercent`
 * omitted sends no body at all -- byte-for-byte the pre-fraction request every caller before
 * this task made -- matching `api/jobs.py.start_now`'s own "omitted body means Max" contract
 * (the same `undefined`-omits-the-body idiom `dismissAllJobs` above already uses). A 409
 * (`core/queue.py.NoSiteLimitConfiguredError`) means a fraction was requested with no site
 * bandwidth limit configured -- `sendJson` throws, same as any other non-2xx.
 */
export function startJobNow(
  jobId: number,
  ratePercent?: 10 | 25 | 50 | 75 | 100,
): Promise<{ applied: boolean }> {
  return sendJson<{ applied: boolean }>(
    `/api/jobs/${jobId}/start-now`,
    'POST',
    ratePercent != null ? { rate_percent: ratePercent } : undefined,
  )
}

/** The Transfers -> Queue tab's Pause control (2026-08-20, `prompts/2026-08-20-queue-pause.md`).
 * `stopRunning` omitted/`false` is "pause after current" -- running jobs finish normally, nothing
 * new is admitted. `true` is "pause now" -- additionally stops every in-flight transfer and
 * returns it to `queued` at its same position (`core/queue.py.TransferQueue.pause`).
 *
 * `durationMinutes` (2026-08-21, `prompts/2026-08-21-pause-for-duration.md`) is one of the
 * dropdown's four offered durations, or `undefined` for an indefinite pause (the default,
 * unchanged) -- combines with either `stopRunning` value.
 */
export function pauseQueue(
  stopRunning = false,
  durationMinutes?: 1 | 10 | 30 | 60,
): Promise<void> {
  return sendJson<void>('/api/queue/pause', 'POST', {
    stop_running: stopRunning,
    duration_minutes: durationMinutes ?? null,
  })
}

/** Resume admission immediately, in queue-position order. */
export function unpauseQueue(): Promise<void> {
  return sendJson<void>('/api/queue/unpause', 'POST')
}

/** The Queue tab's bandwidth slider (2026-08-21,
 * `prompts/done/2026-08-21-bandwidth-from-the-queue-page.md`) -- writes the **site-wide
 * throttle**, bounded above by the `max_bandwidth_bps` ceiling Settings -> Transfer owns
 * (2026-08-21, `prompts/done/2026-08-21-bandwidth-ceiling-and-autocommit.md`). Still site-wide,
 * never a per-queue limit (DESIGN.md §4.5: one site, one set of transfer knobs).
 *
 * Its own endpoint rather than `putTransferSettings` because that PUT takes the whole
 * twelve-field settings object: sending it from here would mean read-modify-writing eleven
 * fields this page doesn't display, clobbering a concurrent Settings edit in the process.
 *
 * `applyToRunning` additionally stops and re-queues every in-flight transfer so the scheduler
 * re-admits it at the new limit -- a real interruption, which the "Apply to new items only"
 * checkbox (checked by default) is the deliberate opt-out from. A value above the ceiling is
 * clamped server-side; the response's `effective_bandwidth_bps` is what was applied.
 */
export function setQueueBandwidth(
  effectiveBandwidthBps: number,
  applyToRunning: boolean,
): Promise<QueueBandwidthResponse> {
  return sendJson<QueueBandwidthResponse>('/api/queue/bandwidth', 'POST', {
    effective_bandwidth_bps: effectiveBandwidthBps,
    apply_to_running: applyToRunning,
  })
}

/** The Queue tab's Preflight box (docs/transfers-redesign-spec.md §4, prefigured; this task's
 * own handoff prompt, prompts/done/2026-08-20-preflight-box.md) -- `hooks/usePreflight.ts` polls
 * this, same "hand-rolled fetch + poll hook, never TanStack Query" convention `useJobs`/
 * `usePoll` already establish for this page.
 */
export function getPreflight(): Promise<PreflightResponse> {
  return getJson<PreflightResponse>('/api/queue/preflight')
}

export function retryItem(itemId: number): Promise<JobOut> {
  return sendJson<JobOut>(`/api/items/${itemId}/retry`, 'POST')
}

/** Manually resolve a wedged row out of the Queue tab's Active/pending box (2026-08-20,
 * docs/transfers-redesign-spec.md §3.2's pipeline-completion rule) -- `'complete'`/`'failed'` to
 * file it with that outcome, `null` to undo a resolution set by mistake.
 *
 * **A classification only.** It moves a row between two boxes on a page and is evidence of
 * nothing: it never advances the `move`-mode delete ladder, is never read as a confirmed *arr
 * import, and never triggers notify/cleanup/post-processing. See `api/jobs.py.resolve_item` and
 * migration 025 for the full constraint. Rejects (409) while the item's own transfer is still
 * queued or running -- Stop is the control for that.
 */
export function resolveItem(
  itemId: number,
  outcome: 'complete' | 'failed' | null,
): Promise<{ item_id: number; manual_outcome: string | null; manual_outcome_at: string | null }> {
  return sendJson(`/api/items/${itemId}/resolve`, 'POST', { outcome })
}

/** Stop-by-item (DESIGN.md §9.2's Files-page Stop action) -- the Files page only knows the
 * item, never the job id an item may currently be running under.
 */
export function stopItem(itemId: number): Promise<{ applied: boolean }> {
  return sendJson<{ applied: boolean }>(`/api/items/${itemId}/stop`, 'POST')
}

/** Delete-by-item (DESIGN.md §9.2's Files-page delete dialog; prompts/open-issues.md "7 + 8").
 * A request that accomplishes nothing at all responds non-2xx, so `sendJson` throws -- this
 * rejects exactly the way `queueItem`/`stopItem` already do on failure, which is what lets
 * `FileTree.tsx`'s existing `Promise.allSettled` bulk-action reporting cover Delete with no new
 * mechanism (a combined request that partially succeeds resolves instead -- see
 * `DeleteItemResponse`'s own comment).
 *
 * `local`/`source` are both explicit, required parameters (2026-08-16, the dialog's independent
 * Local/Source checkboxes, prompts/2026-08-16-manual-delete-local-and-remote.md) -- every call
 * site says what it means rather than relying on the backend's own local-only default for an
 * omitted body.
 */
export function deleteItem(itemId: number, local: boolean, source: boolean): Promise<DeleteItemResponse> {
  return sendJson<DeleteItemResponse>(`/api/items/${itemId}/delete`, 'POST', { local, source })
}

// --- Reset item tracking (2026-08-13, prompts/2026-08-13-reset-item-tracking.md) -----------
//
// A different, more dangerous action than Delete above: this forgets an item's row (and its
// item_settle/deleted_archive bookkeeping) outright rather than removing bytes, so a
// suppressed or failed path can be reused. Also unrelated to Clear History (api/history.py) --
// see api/types.ts's own comment on why the two must never be confused.

/** Selected-item(s) scope -- one row, or a bulk selection resolved to one call per item
 * (`Promise.allSettled`, the identical shape `FileTree.tsx` already uses for bulk Delete).
 * A withheld guard responds non-2xx, so `sendJson` throws exactly like `deleteItem`.
 */
export function resetItem(itemId: number): Promise<ResetItemResponse> {
  return sendJson<ResetItemResponse>(`/api/items/${itemId}/reset`, 'POST')
}

/** Whole-queue scope -- the clean-slate case, and the most destructive action in the app.
 * `confirm_name` must equal the queue's own name exactly; the server checks this too (defense
 * in depth), so a mismatch is a 400. Never all-or-nothing: an item mid-transfer is withheld
 * (named in the response's `withheld`) while the rest of the queue still resets.
 */
export function resetQueue(
  queueId: number,
  body: QueueResetRequest,
): Promise<ResetSummaryResponse> {
  return sendJson<ResetSummaryResponse>(`/api/queues/${queueId}/reset-all`, 'POST', body)
}

/** The All scope's own preview (2026-08-14,
 * prompts/2026-08-14-reset-all-preview-undercounts.md) -- every top-level item this queue
 * tracks, read from the same `item`-table query `resetQueue`'s own execute path uses server-side
 * (`core/local_delete.py.reset_queue_targets`), so it can include a row the Files page's `nodes`
 * prop no longer publishes (a terminal `REMOVED_LOCAL`/`REMOVED_BOTH` row with nothing left in
 * either tree, `core/engine.py`) without disagreeing with what a confirmed reset will actually
 * do. Never resets anything itself.
 */
export function previewResetAll(queueId: number): Promise<ResetPatternPreviewResponse> {
  return sendJson<ResetPatternPreviewResponse>(`/api/queues/${queueId}/reset-all-preview`, 'POST')
}

/** The purge-by-pattern scope's own safety mechanism -- every top-level item `body.pattern`
 * would reset, single-queue only, with enough per-item data to compute the same real-numbers
 * warning the other two scopes show. Never resets anything itself.
 */
export function previewResetByPattern(
  queueId: number,
  body: ResetPatternPreviewRequest,
): Promise<ResetPatternPreviewResponse> {
  return sendJson<ResetPatternPreviewResponse>(
    `/api/queues/${queueId}/reset-preview`,
    'POST',
    body,
  )
}

/** Executes the purge-by-pattern scope reviewed via `previewResetByPattern` above -- same
 * pattern, same single-queue scope, same evaluator server-side.
 */
export function resetByPattern(
  queueId: number,
  body: ResetPatternPreviewRequest,
): Promise<ResetSummaryResponse> {
  return sendJson<ResetSummaryResponse>(`/api/queues/${queueId}/reset-by-pattern`, 'POST', body)
}

// --- Settings -> Transfer (phase 3a API, phase-9-follow-up UI -- DESIGN.md §4.5/§9.3) -----

export function getTransferSettings(): Promise<TransferSettingsOut> {
  return getJson<TransferSettingsOut>('/api/settings/transfer')
}

export function putTransferSettings(body: TransferSettingsIn): Promise<TransferSettingsOut> {
  return sendJson<TransferSettingsOut>('/api/settings/transfer', 'PUT', body)
}

export function getEffectiveLftpSettings(): Promise<EffectiveLftpSettingsOut> {
  return getJson<EffectiveLftpSettingsOut>('/api/settings/transfer/effective-lftp')
}

// --- History (phase 6, DESIGN.md §9.2 History page) ---------------------------------------

function queryString(params: object): string {
  const usp = new URLSearchParams()
  for (const [key, value] of Object.entries(params) as [string, string | number | undefined][]) {
    if (value !== undefined) usp.set(key, String(value))
  }
  const qs = usp.toString()
  return qs ? `?${qs}` : ''
}

/** Completed/failed/cancelled jobs (DESIGN.md §9.2) -- this is where a `succeeded` job's own
 * record lives; the Transfers page (`getJobs`) deliberately never shows it. Server-capped
 * and paginated (`total`/`limit`/`offset`) -- never assume `jobs.length` is everything.
 */
export function getHistoryJobs(filter: HistoryJobsFilter = {}): Promise<HistoryJobsResponse> {
  return getJson<HistoryJobsResponse>(`/api/history/jobs${queryString(filter)}`)
}

/** The on-demand fetch for a job's captured lftp output (~4KB, DESIGN.md §9.2) -- never
 * shipped inline in `getHistoryJobs`'s list payload; see `HistoryJobOut.has_output_tail`.
 */
export function getHistoryJobOutput(jobId: number): Promise<HistoryJobOutputOut> {
  return getJson<HistoryJobOutputOut>(`/api/history/jobs/${jobId}/output`)
}

/** The `event` table (DESIGN.md §3.1/§7.3/§7.4) -- every remote delete, every delete
 * withheld with its gating precondition, and every verify/extract/move outcome. Also
 * server-capped and paginated.
 */
export function getHistoryEvents(filter: HistoryEventsFilter = {}): Promise<HistoryEventsResponse> {
  return getJson<HistoryEventsResponse>(`/api/history/events${queryString(filter)}`)
}

// --- History: clearing (2026-08-13, prompts/2026-08-13-clear-history.md) ------------------
//
// `clearHistoryJob`/`clearHistoryJobs` (job clearing) were removed 2026-08-20
// (docs/transfers-redesign-spec.md §2, phase 1 stage 7) when `HistoryJobsSection.tsx` -- their
// only caller -- was deleted along with the rest of History's own `job` list; the backend
// `DELETE /api/history/jobs[/{id}]` endpoints they called stay (docs/decisions.md), just with
// no remaining frontend caller. `clearHistoryEvent`/`clearHistoryEvents` below are unaffected --
// `EventsSection.tsx` (formerly `HistoryEventsSection.tsx`) still calls them. A *different*
// action from `dismissJob` above: dismiss only hides a row from Transfers and leaves the
// underlying `job`/`event` rows untouched; these delete a row outright, and it's irreversible --
// every caller must confirm first (DESIGN.md's own instruction; see `EventsSection.tsx` for the
// confirmation panel). Bulk clears run server-side as one request (not a `Promise.allSettled`
// loop over ids) -- there's nothing per-row that can fail independently the way a stop-then-
// delete race can, so one `DELETE ... WHERE` is simpler and is what `api/history.py`'s own
// docstring documents as the choice made here.

/** Clear one event record from History -- no "active" concept the way jobs have, so this
 * always either deletes the row or 404s.
 */
export function clearHistoryEvent(eventId: number): Promise<HistoryClearResponse> {
  return sendJson<HistoryClearResponse>(`/api/history/events/${eventId}`, 'DELETE')
}

/** Clear every event matching `filter` -- the same filter shape `getHistoryEvents` takes.
 * No category is protected: the delete-audit kinds (`remote_delete` etc.) clear the same as
 * any other event kind (docs/decisions.md).
 */
export function clearHistoryEvents(
  filter: Omit<HistoryEventsFilter, 'limit' | 'offset'> = {},
): Promise<HistoryClearResponse> {
  return sendJson<HistoryClearResponse>(`/api/history/events${queryString(filter)}`, 'DELETE')
}

// --- Settings -> Logs (phase 7, DESIGN.md §10.1) -----------------------------------------

export function getLogFiles(): Promise<LogFilesResponse> {
  return getJson<LogFilesResponse>('/api/logs/files')
}

export function getLogTail(lines: number, level?: string): Promise<LogTailResponse> {
  return getJson<LogTailResponse>(`/api/logs/tail${queryString({ lines, level })}`)
}

export function logDownloadUrl(filename: string): string {
  return `/api/logs/${encodeURIComponent(filename)}/download`
}

// --- Support bundle (Settings -> Logs, 2026-08-17) ----------------------------------------

/** `POST /api/support-bundle`, then trigger the browser's normal "Save As" for the returned
 * zip. Unlike the log/backup GET downloads above (`logDownloadUrl`/`backupDownloadUrl`), a
 * POST body has no plain `<a href download>` equivalent, so this fetches the blob directly and
 * synthesizes the click a real download link would produce. The filename comes from the
 * response's own `Content-Disposition` header (`core/supportbundle.py.bundle_filename`) --
 * never guessed client-side, so the two can never drift.
 */
export async function downloadSupportBundle(body: SupportBundleRequest): Promise<void> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (csrfToken) headers['X-CSRF-Token'] = csrfToken
  const res = await fetch('/api/support-bundle', {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(
      `POST /api/support-bundle responded ${res.status}${detail ? `: ${detail}` : ''}`,
    )
  }
  const disposition = res.headers.get('Content-Disposition') ?? ''
  const match = /filename="([^"]+)"/.exec(disposition)
  const filename = match ? match[1] : 'lftpweb-support.zip'
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  try {
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}

// --- Settings -> Backup (phase 7, DESIGN.md §10.2) ---------------------------------------

export function getBackupSettings(): Promise<BackupSettingsOut> {
  return getJson<BackupSettingsOut>('/api/settings/backup')
}

export function putBackupSettings(body: BackupSettingsIn): Promise<BackupSettingsOut> {
  return sendJson<BackupSettingsOut>('/api/settings/backup', 'PUT', body)
}

export function listBackups(): Promise<BackupListResponse> {
  return getJson<BackupListResponse>('/api/settings/backup/list')
}

/** DESIGN.md §10.2's "Backup now" -- always takes one immediately, then prunes to the
 * configured keep count.
 */
export function backupNow(): Promise<BackupInfoOut> {
  return sendJson<BackupInfoOut>('/api/settings/backup/now', 'POST')
}

export function backupDownloadUrl(filename: string): string {
  return `/api/settings/backup/${encodeURIComponent(filename)}/download`
}

// --- Metrics / Dashboard (this task -- DESIGN.md new section proposed) -------------------

export function getMetricsSettings(): Promise<MetricsSettingsOut> {
  return getJson<MetricsSettingsOut>('/api/settings/metrics')
}

export function putMetricsSettings(body: MetricsSettingsIn): Promise<MetricsSettingsOut> {
  return sendJson<MetricsSettingsOut>('/api/settings/metrics', 'PUT', body)
}

/** Both Dashboard charts (DESIGN.md new section proposed) -- omit `queueId` for the
 * all-queues breakdown + site total (bytes/hour bar chart, "All queues" speed line); pass it
 * for one queue's own series (speed line with a queue selected). Server-side bucketed
 * (core/metrics.py) -- never raw rows to aggregate here.
 *
 * `group` (2026-08-21, chart grouping) is the bytes chart's group-by control -- omitted, the
 * server picks that range's own default (`api/metrics.py._DEFAULT_GROUP`); the speed chart never
 * passes one, since its `1h`/`12h` ranges have no group-by control at all.
 */
export function getThroughput(
  range: MetricsRange,
  queueId?: number,
  group?: MetricsGroup,
): Promise<MetricsThroughputResponse> {
  return getJson<MetricsThroughputResponse>(
    `/api/metrics/throughput${queryString({ range, queue_id: queueId, group })}`,
  )
}

/** Dashboard's "total downloaded" readout (2026-08-21, daily rollups) -- omit `queueId` for the
 * site-wide total, pass it for one queue's own.
 */
export function getMetricsTotal(queueId?: number): Promise<MetricsTotalOut> {
  return getJson<MetricsTotalOut>(`/api/metrics/total${queryString({ queue_id: queueId })}`)
}

// --- Auth (phase 8, DESIGN.md §8) ---------------------------------------------------------

/** Always reachable with no credentials — see `middleware.py.PUBLIC_API_PATHS` — because a
 * browser that isn't authenticated yet is exactly who needs to call this to find out.
 */
export function getAuthSession(): Promise<AuthSessionOut> {
  return getJson<AuthSessionOut>('/api/auth/session')
}

export function login(body: LoginIn): Promise<AuthSessionOut> {
  return sendJson<AuthSessionOut>('/api/auth/login', 'POST', body)
}

export function logout(): Promise<AuthSessionOut> {
  return sendJson<AuthSessionOut>('/api/auth/logout', 'POST')
}

export function getAuthSettings(): Promise<AuthSettingsOut> {
  return getJson<AuthSettingsOut>('/api/settings/auth')
}

export function putAuthSettings(body: AuthSettingsIn): Promise<AuthSettingsOut> {
  return sendJson<AuthSettingsOut>('/api/settings/auth', 'PUT', body)
}

export function changePassword(body: ChangePasswordIn): Promise<void> {
  return sendJson<void>('/api/settings/auth/password', 'POST', body)
}

export function listApiKeys(): Promise<ApiKeyOut[]> {
  return getJson<ApiKeyOut[]>('/api/settings/auth/api-keys')
}

/** The plaintext `key` on the returned object is shown exactly once — DESIGN.md §8. */
export function createApiKey(body: ApiKeyIn): Promise<ApiKeyCreatedOut> {
  return sendJson<ApiKeyCreatedOut>('/api/settings/auth/api-keys', 'POST', body)
}

export function deleteApiKey(id: number): Promise<void> {
  return sendJson<void>(`/api/settings/auth/api-keys/${id}`, 'DELETE')
}
