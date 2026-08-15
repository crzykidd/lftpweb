import { useCallback } from 'react'
import { Link } from 'react-router-dom'
import { getHost } from '../api/client'
import { usePoll } from '../hooks/usePoll'

const POLL_INTERVAL_MS = 15000

/** DESIGN.md §8's "credentials need re-entry" banner -- the restore-to-a-fresh-install case
 * (§10.2: the encryption key is deliberately excluded from backups). `core/queue.py._admit`
 * and `core/engine.py.scan_queue` already hold transfers and skip scanning cleanly for this
 * condition (see docs/decisions.md); this is the UI half -- a persistent, hard-to-miss
 * notice pointing at the one place that fixes it, rather than the user discovering it only
 * by noticing nothing is downloading.
 */
export function CredentialsBanner() {
  const fetcher = useCallback(getHost, [])
  const host = usePoll(fetcher, POLL_INTERVAL_MS)

  if (!host?.credentials_need_reentry) return null

  return (
    <div className="flex items-center justify-between gap-3 border-b border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
      <span>
        The seedbox password could not be decrypted with this install's key (common after
        restoring a database backup, DESIGN.md §10.2) — transfers for this host are held until
        it's re-entered.
      </span>
      <Link
        to="/settings/connection"
        className="shrink-0 rounded-md border border-amber-400 px-2 py-1 font-medium hover:bg-amber-100 dark:border-amber-700 dark:hover:bg-amber-900"
      >
        Fix in Settings
      </Link>
    </div>
  )
}
