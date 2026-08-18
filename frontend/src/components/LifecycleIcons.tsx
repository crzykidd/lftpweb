import type { SVGProps } from 'react'
import type { FacetLevel, FileNode, SettleSettingsOut } from '../api/types'
import { arrChipOverlay, type ArrChipOverlay, arrHoverLabel, arrIconVariant } from '../lib/fileTree'
import { formatBytes, formatRelativeTimeIntl, settleWaitLabel } from '../lib/format'

// Lifecycle icons (2026-08-13, prompts/2026-08-13-lifecycle-icons.md): R(emote)/L(ocal)/
// V(erified)/E(xtracted), one glyph per facet in `FileNode.facets`
// (`core/itemview.py._lifecycle_facets`). **Inline SVG, not an icon package** -- this project
// has added exactly one frontend dependency since phase 1 (`@tanstack/react-virtual`,
// docs/decisions.md's own flagged deviation), and four small glyphs don't clear that bar. Path
// data below is copied verbatim, unmodified, from Lucide (https://lucide.dev, ISC License) --
// see NOTICE for the licence record. Distinct glyph shapes carry the meaning on their own
// (network/cloud, hard-drive, shield-check, package) so colour is reinforcement, never the only
// signal -- required for a status display, since red/green is the most common colour-vision
// deficiency.

const ICON_SIZE_PX = 14

interface IconProps extends Omit<SVGProps<SVGSVGElement>, 'children'> {
  /** Both the tooltip (native `<title>`, shown on hover) and the accessible name
   * (`aria-label`) -- every lifecycle icon carries detail, per the task's own requirement,
   * not just the facet name.
   */
  title: string
}

function IconBase({ title, children, className, ...props }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={ICON_SIZE_PX}
      height={ICON_SIZE_PX}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      role="img"
      aria-label={title}
      className={`shrink-0 ${className ?? ''}`}
      {...props}
    >
      <title>{title}</title>
      {children}
    </svg>
  )
}

/** R -- remote presence. Lucide `cloud`. */
function CloudIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z" />
    </IconBase>
  )
}

/** L -- local presence. Lucide `hard-drive`. */
function HardDriveIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M10 16h.01" />
      <path d="M2.212 11.577a2 2 0 0 0-.212.896V18a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-5.527a2 2 0 0 0-.212-.896L18.55 5.11A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
      <path d="M21.946 12.013H2.054" />
      <path d="M6 16h.01" />
    </IconBase>
  )
}

/** V -- verified. Lucide `shield-check`. */
function ShieldCheckIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z" />
      <path d="m9 12 2 2 4-4" />
    </IconBase>
  )
}

/** E -- extracted. Lucide `package`. */
function PackageIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z" />
      <path d="M12 22V12" />
      <polyline points="3.29 7 12 12 20.71 7" />
      <path d="m7.5 4.27 9 5.15" />
    </IconBase>
  )
}

/** The per-row detail-drawer affordance (2026-08-13, prompts/2026-08-13-files-detail-
 * inspector.md), not a lifecycle facet -- it is a *control* ("open the drawer"), not a
 * *state*, and `FileTree.tsx`'s row renders it with a plainer, quieter treatment than the four
 * status icons above for exactly that reason (`text-zinc-400`, never one of
 * `FACET_LEVEL_CLASSES`'s semantic colours). Lucide `info` -- unlike the four icons above, this
 * one *is* one of the handful of Lucide icons derived from the Feather project, so it carries
 * the additional Feather MIT notice in NOTICE alongside the ISC one.
 *
 * Exported since 2026-08-13 so `FieldHelp.tsx` (the Docs section's per-field help affordance)
 * can reuse this exact glyph rather than pasting the same licensed path data a second time --
 * "an info icon means there is more to read here" is already what it means on a Files row.
 */
export function InfoIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <circle cx="12" cy="12" r="10" />
      <path d="M12 16v-4" />
      <path d="M12 8h.01" />
    </IconBase>
  )
}

/** A row's "view details" button -- explicit and touch-safe (DESIGN.md §9.2's affordance
 * conflict this task resolves: row click already drives multi-select, which feeds bulk
 * Queue/Stop/**Delete**, so opening a drawer on the same click would sit behind a destructive
 * action). `stopPropagation` lives here, not on the caller -- every row that renders this
 * button gets the guarantee for free, the same way the row's own selection checkbox already
 * stops its own click from reaching a (currently nonexistent, but not guaranteed to stay that
 * way) row-level handler.
 */
export function DetailButton({ label, onOpen }: { label: string; onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation()
        onOpen()
      }}
      title={`View details for ${label}`}
      aria-label={`View details for ${label}`}
      className="flex shrink-0 items-center text-zinc-400 hover:text-zinc-700 dark:text-zinc-600 dark:hover:text-zinc-300"
    >
      <InfoIcon title={`View details for ${label}`} />
    </button>
  )
}

// Four colour treatments, not three (the task's own correction to the user's green/amber/red
// framing): green = done and good, amber = in progress, red = failed, dim = not applicable or
// *intentionally* gone -- a move-mode item's deleted-on-purpose remote copy is this last one,
// never red. Chosen for contrast in both themes (`dark:` variants throughout this codebase) --
// unverified against a real browser (no UI access in this environment), so treat the exact
// shades as a first pass, not a final answer.
const FACET_LEVEL_CLASSES: Record<FacetLevel, string> = {
  green: 'text-emerald-600 dark:text-emerald-400',
  amber: 'text-amber-500 dark:text-amber-400',
  red: 'text-red-600 dark:text-red-400',
  dim: 'text-zinc-300 dark:text-zinc-700',
}

/** Turns a facet's `reason` code plus this row's own raw size/timestamp fields into the
 * tooltip sentence the task asks for -- "not just the facet name but the fact behind it: sizes,
 * and the relevant timestamp where one exists." Deliberately built here, in the frontend, not
 * in `core/itemview.py`: the *classification* (level/reason) is the load-bearing logic and
 * lives server-side so every consumer agrees; turning a reason code into English is exactly the
 * kind of presentation `lib/format.ts.stateAgeLabel` already owns for `state`/`state_changed_at`.
 */
function remoteTooltip(node: FileNode, settle: SettleSettingsOut | null): string {
  if (node.substate === 'settling') {
    // The settle gate's own remote-facet override (below `LifecycleIcons`'s own docstring for
    // the full reasoning): the remote copy genuinely exists (`core/itemview.py._remote_facet`
    // would read green), but hasn't been *confirmed stable* yet, which is the fact worth
    // surfacing on hover here rather than plain presence.
    return `Remote: ${settleWaitLabel(node, settle)}`
  }
  const facet = node.facets.remote
  switch (facet.reason) {
    case 'present':
      return node.remote_size != null
        ? `Remote: ${formatBytes(node.remote_size)} on the seedbox`
        : 'Remote: present'
    case 'deleted_by_us':
      return node.remote_deleted_at != null
        ? `Remote: deleted after verification, ${formatRelativeTimeIntl(node.remote_deleted_at)}`
        : 'Remote: deleted after verification'
    default:
      return 'Remote: no copy on the seedbox'
  }
}

function localTooltip(node: FileNode): string {
  // 2026-08-14 (prompts/2026-08-14-extracted-archives-rest-as-extracted.md): a spent archive
  // volume reads `EXCLUDED` server-side (`core/engine.py._persist`'s vanished-row sweep), the
  // same state a `file_exclude` pattern match produces -- but the ordinary `'excluded'` reading
  // below ("never meant to download") would be false for this row, which *was* fetched and
  // extracted before this codebase removed it. Checked first, ahead of `facet.reason`, since
  // the underlying state is identical for both causes and only this raw field tells them apart
  // -- see `lib/format.ts.deletedArchiveLabel`'s own docstring for the fuller sentence.
  if (node.deleted_archive_at != null) {
    return `Local: archive volume removed after extraction, ${formatRelativeTimeIntl(node.deleted_archive_at)}`
  }
  const facet = node.facets.local
  switch (facet.reason) {
    case 'complete':
      return node.local_size ? `Local: ${formatBytes(node.local_size)} on disk` : 'Local: complete'
    case 'missing':
      return node.first_missing_at != null
        ? `Local: missing since ${formatRelativeTimeIntl(node.first_missing_at)} -- was downloaded, not found on disk`
        : 'Local: missing -- was downloaded, not found on disk'
    case 'removed_by_us':
      return 'Local: deleted by lftpweb'
    case 'excluded':
      return 'Local: excluded by pattern -- never meant to download'
    case 'local_only':
      return node.local_size != null
        ? `Local: ${formatBytes(node.local_size)} on disk (never tracked on the seedbox)`
        : 'Local: present (never tracked on the seedbox)'
    case 'partial':
      return node.local_size != null && node.remote_size != null
        ? `Local: ${formatBytes(node.local_size)} of ${formatBytes(node.remote_size)}`
        : 'Local: partial'
    default:
      return 'Local: not downloaded yet'
  }
}

function verifiedTooltip(node: FileNode): string {
  const facet = node.facets.verified
  switch (facet.reason) {
    case 'verified':
      return node.verified_at != null ? `Verified ${formatRelativeTimeIntl(node.verified_at)}` : 'Verified'
    case 'corrupt':
      return 'Verification failed -- corrupt'
    case 'in_progress':
      return 'Verifying now'
    default:
      return 'Not verified'
  }
}

function extractedTooltip(node: FileNode): string {
  const facet = node.facets.extracted
  switch (facet.reason) {
    case 'extracted':
      return node.extracted_at != null ? `Extracted ${formatRelativeTimeIntl(node.extracted_at)}` : 'Extracted'
    case 'failed':
      return 'Extraction failed'
    case 'in_progress':
      return 'Extracting now'
    default:
      return 'Not extracted'
  }
}

/** The four-icon cluster for one Files-tree row -- always renders all four (a variable-width
 * set would be harder to scan than a consistently dim one), colour and tooltip driven entirely
 * by `node.facets` (`core/itemview.py`), never re-derived here.
 *
 * **The one exception: R during the settle gate's wait** (2026-08-13,
 * prompts/2026-08-13-files-ux-pass.md item 3). `core/itemview.py._remote_facet` only ever
 * produces green ("present") or dim ("no copy") -- it has no amber reading of its own, by
 * design (confirmed by reading that function, not assumed). While `node.substate ===
 * 'settling'`, this component overrides R to amber here, client-side: the remote copy genuinely
 * exists (green would be accurate) but has not yet held still long enough to trust, and amber is
 * the fact worth surfacing. **Never L** -- the task's own instruction, confirmed against
 * `_local_facet` too: local is legitimately empty during settling (nothing has been queued yet),
 * so amber there would read as activity that isn't happening.
 */
export function LifecycleIcons({ node, settle }: { node: FileNode; settle: SettleSettingsOut | null }) {
  const { remote, local, verified, extracted } = node.facets
  const isSettling = node.substate === 'settling'
  const remoteLevel: FacetLevel = isSettling ? 'amber' : remote.level
  return (
    <span className="flex shrink-0 items-center gap-1">
      <CloudIcon title={remoteTooltip(node, settle)} className={FACET_LEVEL_CLASSES[remoteLevel]} />
      <HardDriveIcon title={localTooltip(node)} className={FACET_LEVEL_CLASSES[local.level]} />
      <ShieldCheckIcon title={verifiedTooltip(node)} className={FACET_LEVEL_CLASSES[verified.level]} />
      <PackageIcon title={extractedTooltip(node)} className={FACET_LEVEL_CLASSES[extracted.level]} />
    </span>
  )
}

// --- Sonarr/Radarr integration icon (docs/arr-integration-spec.md "UI") -------------------

/** The *arr mark itself -- a generic "linked to an external system" glyph (Lucide `link-2`,
 * ISC License, see NOTICE), deliberately not a Sonarr/Radarr brand mark: this project ships no
 * third-party logos, and one shared glyph covers both `kind`s (the hover text, not the icon
 * shape, is what names the specific instance -- `arrHoverLabel`, `lib/fileTree.ts`).
 */
function ArrMarkIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M9 17H7A5 5 0 0 1 7 7h2" />
      <path d="M15 7h2a5 5 0 1 1 0 10h-2" />
      <line x1="8" x2="16" y1="12" y2="12" />
    </IconBase>
  )
}

/** The generic *arr mark (docs/arr-integration-spec.md "UI") -- originally the Files-row icon,
 * until the Files tree unified onto the real-brand-logo `ArrRowChip` below (2026-08-16,
 * prompts/2026-08-16-files-brand-logo-icons.md, "one visual language everywhere" -- user
 * feedback that the real Sonarr/Radarr logos already shown on Transfers/History should show on
 * Files too). Still used for exactly one remaining spot: the Transfers/History job-detail
 * drawer's own "*arr" section (`TransfersPage.tsx`'s expand panel), which pairs the icon with a
 * full sentence of its own rather than needing brand recognition or an overlay badge.
 *
 * Renders nothing at all for `arr_status: null` (a queue with no bound instance, or an item the
 * poller hasn't matched yet -- everything-off-by-default means this is the common case on most
 * installs). Otherwise the mark itself plus, for the states that need to read as visually
 * distinct from "still being watched," a small colored glyph beside it -- a green **✓** once the
 * *arr has confirmed import (`imported`, and `cleaned`, 2026-08-16: with "Delete when imported"
 * on, `imported` is a seconds-long transient before the next poller beat cleans it up, so the
 * green check would flash and never actually be seen if `cleaned` dimmed back to neutral -- it
 * keeps the same green ✓ instead, alongside the removal-grace countdown chip), an amber **⚠**
 * once a release left the *arr's queue without ever importing (`gone`, the one state that
 * usually needs a human, per the spec's own note). `detected`/`notified` render the plain
 * neutral mark. The hover text (`arrHoverLabel`) still distinguishes "imported" from "imported
 * and cleaned up locally," so the two states stay tellable apart despite sharing an icon.
 *
 * `instanceName` is resolved by the caller from the item's *queue* binding, never invented here
 * -- see `lib/fileTree.ts.arrHoverLabel`'s own docstring for why the item projection alone
 * can't name the instance.
 */
export function ArrIcon({
  arrStatus,
  arrStatusAt,
  instanceName,
}: {
  arrStatus: string | null
  arrStatusAt: string | null
  instanceName: string | null
}) {
  const variant = arrIconVariant(arrStatus)
  if (variant === 'none') return null
  const hoverLabel = arrHoverLabel({ arr_status: arrStatus, arr_status_at: arrStatusAt }, instanceName)
  // `dropped` (2026-08-18) shares `gone`'s amber treatment here -- this drawer already reads
  // `gone` as amber rather than the row chip's red (the one place the two specs differ, per
  // this component's own docstring), so `dropped`'s "held for confirmation, not yet actionable"
  // amber lands in the same visual slot; the full-sentence hover text (`arrHoverLabel`) is what
  // keeps the two tellable apart here, same as `imported`/`cleaned` already share a slot.
  const markClass =
    variant === 'imported'
      ? 'text-emerald-600 dark:text-emerald-400'
      : variant === 'gone' || variant === 'dropped'
        ? 'text-amber-500 dark:text-amber-400'
        : 'text-zinc-400 dark:text-zinc-500'
  return (
    <span className="flex shrink-0 items-center gap-0.5" title={hoverLabel ?? undefined}>
      <ArrMarkIcon title={hoverLabel ?? '*arr'} className={markClass} />
      {variant === 'imported' && (
        <span className="text-[10px] leading-none text-emerald-600 dark:text-emerald-400" aria-hidden="true">
          ✓
        </span>
      )}
      {(variant === 'gone' || variant === 'dropped') && (
        <span className="text-[10px] leading-none text-amber-500 dark:text-amber-400" aria-hidden="true">
          ⚠
        </span>
      )}
    </span>
  )
}

// --- Sonarr/Radarr row chip (Files + Transfers + History, 2026-08-16,
// prompts/2026-08-16-arr-chip-on-row-lines.md; Files unified onto it the same day,
// prompts/2026-08-16-files-brand-logo-icons.md) --------------------------------------------------
//
// Originally a *second*, deliberately distinct *arr indicator from `ArrIcon` above, introduced
// for the Transfers/History row line only -- `ArrIcon` is a generic "linked to an external
// system" mark shared by both kinds (this codebase's original choice not to ship third-party
// brand logos). This one was the user's own later decision (2026-08-16, refined same day): the
// row line shows the **real** Sonarr/Radarr logo, in its own brand colour, with the outcome
// layered on as a small status-overlay badge -- "green when the *arr processed it, red when it
// failed out" -- rather than folded into the mark's own colour the way `ArrIcon`'s ✓/⚠ text
// glyphs are. Same day, user feedback asked for "one visual language everywhere": the Files tree
// now renders this exact component too (`FileTree.tsx`'s *arr column), so `gone` reads **red**
// on all three surfaces -- Files, Transfers, History -- not the amber `ArrIcon` used to show on
// Files. `ArrIcon`'s amber survives only in the Transfers/History job-detail drawer's own "*arr"
// section, a different affordance (full sentence beside a plain mark, no brand recognition or
// overlay needed there).

const ARR_LOGO_SIZE_PX = 16

/** Sonarr's own brand mark -- path data copied verbatim, unmodified, from the simple-icons
 * dataset (CC0 License, https://github.com/simple-icons/simple-icons), itself sourced from
 * Sonarr's own repository (https://github.com/Sonarr/Sonarr/blob/main/Logo/Sonarr.svg) -- see
 * NOTICE for the full record and the exact commit simple-icons pins. Rendered in Sonarr's own
 * brand blue (`#2596be`, simple-icons' own recorded hex) unconditionally -- recognition is the
 * whole point of using the real logo, so this colour is never tinted by the row's own *arr
 * status; the status reads through `ArrChipOverlayBadge` layered on top instead.
 */
function SonarrLogo({ title }: { title: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={ARR_LOGO_SIZE_PX}
      height={ARR_LOGO_SIZE_PX}
      viewBox="0 0 24 24"
      role="img"
      aria-label={title}
      className="shrink-0"
    >
      <title>{title}</title>
      <path
        fill="#2596be"
        d="M21.212 4.282c1.851 2.204 2.777 4.776 2.777 7.718 0 2.848-.867 5.344-2.602 7.489a934.355 934.355 0 0 1-2.101-2.095c-1.477-1.477-1.792-3.293-1.792-5.278 0-2.224.127-3.486 1.577-4.935l2.478-2.478a13.209 13.209 0 0 0-.337-.421Zm-17.7 16.193C1.708 18.678.6 16.59.188 14.213A11.84 11.84 0 0 1 .011 12c0-.28.006-.548.017-.802 0-.026.007-.052.022-.078.153-2.601 1.076-4.889 2.767-6.865-.108.127-.214.256-.316.387 0 0 1.351 1.346 2.329 2.323 1.408 1.409 1.726 3.215 1.726 5.151 0 1.985-.249 3.762-1.781 5.295-1.035 1.035-2.119 2.124-2.119 2.124.112.136.229.271.349.404.029-.027 1.297-1.348 2.123-2.175 1.638-1.637 1.928-3.528 1.928-5.648 0-2.072-.365-3.997-1.873-5.504a620.045 620.045 0 0 0-2.366-2.357c.168-.196.342-.388.523-.576l3.117 3.106-.194.195 1.903 1.898.547-.549L6.81 6.432l-.196.196L3.495 3.52c.01-.009.436-.416.643-.597.009.011 2.28 2.283 2.28 2.283 1.538 1.537 3.5 1.955 5.621 1.955 2.18 0 4.134-.442 5.731-2.038.907-.908 2.153-2.149 2.162-2.16.17.151.491.461.56.528l.013.013-3.111 3.028-.001.002-.197-.194-1.876 1.903.552.543 1.875-1.903-.197-.194 3.109-3.026c.193.203.377.41.553.619-.03.025-2.495 2.546-2.495 2.546-1.556 1.556-1.723 2.9-1.723 5.288 0 2.121.361 4.054 1.939 5.632a576.91 576.91 0 0 0 2.133 2.124c-.183.208-.599.645-.613.66l-3.066-3.174.195-.196-1.995-1.986-.546.549 1.995 1.986.195-.196 3.065 3.172c-.021.019-.385.362-.552.506-.01-.013-1.974-1.978-1.974-1.978-1.842-1.842-3.299-2.039-5.731-2.039-2.338 0-3.92.239-5.632 1.95-.944.944-2.078 2.085-2.089 2.099-.275-.23-.649-.594-.649-.594l3.019-3.024.199.192 1.854-1.925-.558-.538-1.854 1.926.199.191-3.016 3.022ZM12 8.672A3.33 3.33 0 0 0 8.672 12 3.33 3.33 0 0 0 12 15.328 3.33 3.33 0 0 0 15.328 12 3.33 3.33 0 0 0 12 8.672ZM4.52 2.6C6.665.867 9.162 0 12.011 0c2.88 0 5.394.88 7.541 2.639 0 0-1.215 1.209-2.136 2.13-1.496 1.496-3.334 1.892-5.377 1.892-1.985 0-3.829-.37-5.267-1.809L4.52 2.6Zm14.837 18.909a9.507 9.507 0 0 1-.342.256C16.994 23.255 14.659 24 12.011 24c-2.652 0-4.983-.745-6.993-2.235-.104-.074-.208-.15-.31-.227 0 0 1.096-1.101 2.053-2.058 1.602-1.602 3.09-1.804 5.278-1.804 2.28 0 3.651.166 5.377 1.892l1.941 1.941Z"
      />
    </svg>
  )
}

/** Radarr's own brand mark -- same provenance and licence as `SonarrLogo` above, sourced from
 * Radarr's own repository (https://github.com/Radarr/Radarr/blob/develop/Logo/Radarr.svg) via
 * the simple-icons dataset (CC0). Rendered in Radarr's own brand gold (`#ffcb3d`).
 */
function RadarrLogo({ title }: { title: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={ARR_LOGO_SIZE_PX}
      height={ARR_LOGO_SIZE_PX}
      viewBox="0 0 24 24"
      role="img"
      aria-label={title}
      className="shrink-0"
    >
      <title>{title}</title>
      <path
        fill="#ffcb3d"
        d="M5.274 0C3.189.039 1.19 1.547 1.19 4.705l.184 14.518c0 1.47 1.103 2.205 2.573 2.021L3.764 3.786c0-1.654.919-1.838 2.022-1.103l14.7 8.27c1.103.734 1.655 1.47 1.838 2.756.92-1.654.552-4.043-1.286-5.33L7.991.846A4.559 4.559 0 0 0 5.274.001zm1.982 6.91-.184 10.107 9.004-5.146Zm13.598 6.064-15.068 8.82c-.92.552-2.022.736-3.124.368.918 1.47 3.307 2.389 5.145 1.47l12.68-7.35c1.102-.736 1.286-2.022.367-3.308z"
      />
    </svg>
  )
}

/** The unknown/future-`kind` fallback (this task's own instruction: "never render nothing for a
 * tracked item just because the logo is missing") -- a small text chip of the instance's own
 * name, colour-coded with the same status vocabulary the logo's overlay badge uses
 * (green/red/neutral) since there is no separate logo here to layer a badge onto.
 */
function ArrTextChip({
  instanceName,
  overlay,
  title,
}: {
  instanceName: string | null
  overlay: ArrChipOverlay
  title: string | null
}) {
  const colorClass =
    overlay === 'check'
      ? 'border-emerald-300 text-emerald-700 dark:border-emerald-800 dark:text-emerald-400'
      : overlay === 'warn'
        ? 'border-red-300 text-red-700 dark:border-red-800 dark:text-red-400'
        : 'border-zinc-300 text-zinc-500 dark:border-zinc-700 dark:text-zinc-400'
  return (
    <span
      className={`rounded border px-1 text-[9px] leading-tight font-semibold uppercase ${colorClass}`}
      title={title ?? undefined}
    >
      {instanceName ?? '*arr'}
    </span>
  )
}

/** A plain brand-logo mark, no status overlay -- introduced (2026-08-17,
 * prompts/2026-08-17-queues-list-arr-brand-icon.md) for Settings → Queues' queue-list Name
 * cell, which needs a *binding* indicator ("is this queue bound to an *arr instance"), never
 * an *item-status* one -- `ArrRowChip` below is the wrong component there since it renders
 * `null` without an `arr_status`, which a queue row never has. Same real Sonarr/Radarr logo,
 * same `ArrTextChip`-style text fallback for an unrecognized/future `kind` (or `null`, the
 * "bound instance not found" case), so there is still exactly one kind → logo mapping in this
 * file -- `ArrRowChip` is rebuilt on top of this mark rather than duplicating the switch a
 * second time. `muted` renders the mark at reduced opacity, this task's own rule for "the
 * queue is bound, but the instance itself is currently disabled" -- the binding is real but
 * inert.
 */
export function ArrBrandMark({
  kind,
  title,
  muted = false,
}: {
  kind: string | null
  title: string
  muted?: boolean
}) {
  return (
    <span className={`inline-flex shrink-0 items-center ${muted ? 'opacity-50' : ''}`}>
      {kind === 'sonarr' ? (
        <SonarrLogo title={title} />
      ) : kind === 'radarr' ? (
        <RadarrLogo title={title} />
      ) : (
        <ArrTextChip instanceName={kind} overlay={null} title={title} />
      )}
    </span>
  )
}

/** The row chip's status-overlay badge -- green check ("processed"), amber dot ("dropped --
 * rechecking", 2026-08-18), or red dot ("gone"), absolutely positioned over the bottom-right
 * corner of the logo/text-chip beside it. `null` renders nothing, for the `detected`/`notified`
 * mid-flight case (`arrChipOverlay`, `lib/fileTree.ts`).
 */
function ArrChipOverlayBadge({ overlay }: { overlay: ArrChipOverlay }) {
  if (overlay === 'check') {
    return (
      <span
        className="absolute -right-1 -bottom-1 flex h-2.5 w-2.5 items-center justify-center rounded-full bg-emerald-600 text-[7px] leading-none text-white ring-1 ring-white dark:bg-emerald-500 dark:ring-zinc-900"
        aria-hidden="true"
      >
        ✓
      </span>
    )
  }
  if (overlay === 'pending') {
    // Same size/positioning as the red `warn` dot below, per this task's own instruction --
    // only the color differs, so the two read as siblings on the same scale (amber = "still
    // being decided," red = "decided, and it needs you") rather than unrelated shapes.
    return (
      <span
        className="absolute -right-1 -bottom-1 h-2 w-2 rounded-full bg-amber-500 ring-1 ring-white dark:bg-amber-400 dark:ring-zinc-900"
        aria-hidden="true"
      />
    )
  }
  if (overlay === 'warn') {
    return (
      <span
        className="absolute -right-1 -bottom-1 h-2 w-2 rounded-full bg-red-600 ring-1 ring-white dark:bg-red-500 dark:ring-zinc-900"
        aria-hidden="true"
      />
    )
  }
  return null
}

/** The Files/Transfers/History row-line *arr chip (2026-08-16, prompts/2026-08-16-arr-chip-on-
 * row-lines.md, prompts/2026-08-16-files-brand-logo-icons.md) -- the real Sonarr/Radarr brand
 * logo, in its own brand colour, with the outcome as a small status overlay: green check once
 * the *arr processed it (`imported`/`cleaned`), amber pending dot once a release drops out of
 * the *arr's queue and lftpweb is rechecking every pass (`dropped`, 2026-08-18 -- see
 * `lib/fileTree.ts.arrChipOverlay`'s own docstring), red dot once that grace window expires with
 * neither a reappearance nor an import confirmed (`gone`), the logo alone while still mid-flight
 * (`detected`/`notified`), and **nothing at all** when `arrStatus` is null -- the item isn't
 * *arr-tracked, per this integration's "everything off by default" rule. One component, one
 * visual language, across all three surfaces.
 *
 * `instanceKind` selects the logo (`'sonarr'` | `'radarr'`). Transfers/History read it straight
 * off the wire (`JobOut.arr_instance_kind`/`HistoryJobOut.arr_instance_kind`, joined server-side
 * alongside `arr_instance_name`); the Files tree has no such per-item field (`arr_status`/
 * `arr_status_at` are the only *arr fields `FileNode` carries -- see
 * `lib/fileTree.ts.arrHoverLabel`'s own docstring for why), so `FilesPage.tsx` resolves it the
 * same way it already resolves `arrInstanceName`: from the item's *queue* binding
 * (`path_queue.arr_instance_id` -> `GET /api/settings/arr`), threaded down through
 * `FileTree`/`Row` as a prop. Any other value -- `null` (no bound instance, a fetch that hasn't
 * resolved yet, or a row whose queue's instance predates this field... though it never does,
 * since `arr_status` itself would also be null then) or an unrecognized future kind -- falls
 * back to `ArrTextChip` rather than rendering nothing for a tracked item. Reuses
 * `arrIconVariant`/`arrChipOverlay` (`lib/fileTree.ts`) for the status categorization -- one
 * mapping, consumed by `ArrIcon` above and this component both -- and `arrHoverLabel` for the
 * hover text, unchanged from `ArrIcon`.
 *
 * The logo itself is `ArrBrandMark` above -- this component supplies the status-overlay badge
 * layered on top, `ArrBrandMark` supplies the kind → logo mapping, and the two together are
 * exactly this component's old, undivided behaviour (2026-08-17, this task: extracted so
 * Settings → Queues can reuse the mapping without a bound `arr_status` to key off). The
 * `sonarr`/`radarr` fallback stays `ArrTextChip` here, not `ArrBrandMark`'s own fallback --
 * `ArrBrandMark` has no `instanceName` prop, so this call keeps the status-aware `overlay`
 * colouring (green/red/neutral) on the pill, which `ArrBrandMark`'s plain neutral fallback
 * doesn't carry.
 */
export function ArrRowChip({
  arrStatus,
  arrStatusAt,
  instanceName,
  instanceKind,
}: {
  arrStatus: string | null
  arrStatusAt: string | null
  instanceName: string | null
  instanceKind: string | null
}) {
  const variant = arrIconVariant(arrStatus)
  if (variant === 'none') return null
  const overlay = arrChipOverlay(variant)
  const hoverLabel = arrHoverLabel({ arr_status: arrStatus, arr_status_at: arrStatusAt }, instanceName)
  const title = hoverLabel ?? instanceName ?? '*arr'

  return (
    <span className="relative inline-flex shrink-0 items-center" title={hoverLabel ?? undefined}>
      {instanceKind === 'sonarr' || instanceKind === 'radarr' ? (
        <ArrBrandMark kind={instanceKind} title={title} />
      ) : (
        <ArrTextChip instanceName={instanceName} overlay={overlay} title={title} />
      )}
      <ArrChipOverlayBadge overlay={overlay} />
    </span>
  )
}
