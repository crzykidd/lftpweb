import type { SVGProps } from 'react'
import type { FacetLevel, FileNode, SettleSettingsOut } from '../api/types'
import { arrHoverLabel, arrIconVariant } from '../lib/fileTree'
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

/** The Files-row *arr icon (docs/arr-integration-spec.md "UI" -- "multi-faceted," the user's
 * own 2026-08-15 decision): renders nothing at all for `arr_status: null` (a queue with no
 * bound instance, or an item the poller hasn't matched yet -- everything-off-by-default means
 * this is the common case on most installs). Otherwise the mark itself plus, for the states
 * that need to read as visually distinct from "still being watched," a small colored glyph
 * beside it -- a green **✓** once the *arr has confirmed import (`imported`, and `cleaned`,
 * 2026-08-16: with "Delete when imported" on, `imported` is a seconds-long transient before
 * the next poller beat cleans it up, so the green check would flash and never actually be
 * seen if `cleaned` dimmed back to neutral -- it keeps the same green ✓ instead, alongside the
 * removal-grace countdown chip), an amber **⚠** once a release left the *arr's queue without
 * ever importing (`gone`, the one state that usually needs a human, per the spec's own note).
 * `detected`/`notified` render the plain neutral mark. The hover text (`arrHoverLabel`) still
 * distinguishes "imported" from "imported and cleaned up locally," so the two states stay
 * tellable apart despite sharing an icon.
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
  const markClass =
    variant === 'imported'
      ? 'text-emerald-600 dark:text-emerald-400'
      : variant === 'gone'
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
      {variant === 'gone' && (
        <span className="text-[10px] leading-none text-amber-500 dark:text-amber-400" aria-hidden="true">
          ⚠
        </span>
      )}
    </span>
  )
}
