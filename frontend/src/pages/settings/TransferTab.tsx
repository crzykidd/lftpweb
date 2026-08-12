import { PagePlaceholder } from '../../components/PagePlaceholder'

// The old copy here said "phase 3" -- phase 3 shipped a complete, tested `TransferSettings`
// API (site bandwidth/concurrency/fast-lane/parallelism, §4.5, plus the free-text "extra
// lftp settings" box and the §9.3 live connection-count readout) with no UI, and every
// later phase left it that way (docs/decisions.md's phase 5 entry flagged this explicitly
// as "a separate, lower-stakes gap for whichever phase picks it up, likely 9"). Phase 9's
// own prompt scoped its UI work narrowly (Files bulk actions/filters, health readout) and
// did not name this tab, so it's still not built -- see README.md's "Known gaps" list and
// docs/decisions.md (phase 9) rather than silently building a whole settings page as
// unrequested scope creep on a "polish" phase.
export function TransferTab() {
  return (
    <PagePlaceholder title="Settings → Transfer has no UI yet — the bandwidth/concurrency API is complete; see README.md's Known gaps" />
  )
}
