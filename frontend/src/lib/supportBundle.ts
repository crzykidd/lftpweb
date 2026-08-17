import type { ArrInstanceOut, SupportBundleRequest } from '../api/types'

/** The support bundle dialog's own state (Settings -> Logs, 2026-08-17,
 * prompts/done/2026-08-17-support-bundle.md): everything except `arrInstanceIds` mirrors one
 * fixed checkbox each -- lftpweb's own logs are **not** a field here at all, since the server
 * always includes them regardless of what's sent (the dialog shows that row checked and
 * disabled, never a state this module needs to track). `arrInstanceIds` is inherently
 * variable-length -- one row per *enabled* *arr instance -- so "select everything" is expressed
 * by naming every id, not a single bool.
 */
export interface SupportBundleSelection {
  includeEnvironment: boolean
  includeSettings: boolean
  includeEvents: boolean
  includeJobs: boolean
  arrInstanceIds: number[]
}

/** Every checkbox defaults ON (the settled design) -- including every *arr row, pre-checked
 * with whatever `enabledArrInstances` currently returns.
 */
export function defaultSupportBundleSelection(
  enabledInstanceIds: readonly number[],
): SupportBundleSelection {
  return {
    includeEnvironment: true,
    includeSettings: true,
    includeEvents: true,
    includeJobs: true,
    arrInstanceIds: [...enabledInstanceIds],
  }
}

/** One dialog row per *enabled* instance, never a disabled one -- a disabled instance has
 * nothing running to fetch logs from, so a checkbox for it would just always fail. Also the
 * "hidden entirely when none are enabled" rule for the section as a whole: an empty result here
 * is what the dialog reads as "don't render the *arr section at all."
 */
export function enabledArrInstances(instances: readonly ArrInstanceOut[]): ArrInstanceOut[] {
  return instances.filter((i) => i.enabled)
}

export function toggleArrInstance(
  selection: SupportBundleSelection,
  instanceId: number,
): SupportBundleSelection {
  const checked = selection.arrInstanceIds.includes(instanceId)
  return {
    ...selection,
    arrInstanceIds: checked
      ? selection.arrInstanceIds.filter((id) => id !== instanceId)
      : [...selection.arrInstanceIds, instanceId],
  }
}

export function toSupportBundleRequest(selection: SupportBundleSelection): SupportBundleRequest {
  return {
    include_environment: selection.includeEnvironment,
    include_settings: selection.includeSettings,
    include_events: selection.includeEvents,
    include_jobs: selection.includeJobs,
    arr_instance_ids: selection.arrInstanceIds,
  }
}
