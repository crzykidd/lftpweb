import { useCallback } from 'react'
import { Link } from 'react-router-dom'
import { getHealth } from '../api/client'
import { useAuth } from '../hooks/authContext'
import { usePoll } from '../hooks/usePoll'
import { lftpSettingsIgnoredMessage, showHostAllowlistBanner } from '../lib/securityBanners'

const POLL_INTERVAL_MS = 15000

const bannerClasses =
  'flex items-center justify-between gap-3 border-b border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200'

/** Audit batch C3 (DESIGN.md §8): DNS-rebinding protection is off. Informative only -- no
 * dismiss, no confirm -- and it disappears by itself once `LFTPWEB_ALLOWED_HOSTS` is set (or the
 * mode becomes `password`). In the app shell, not Settings → Auth, because the people it is for
 * are default-`none` installs that may never open that tab; one line keeps it out of the way.
 */
export function HostAllowlistBanner() {
  const { session } = useAuth()
  if (!showHostAllowlistBanner(session)) return null
  return (
    <div className={bannerClasses}>
      <span>
        DNS-rebinding protection is off: set <code>LFTPWEB_ALLOWED_HOSTS</code> to the name(s) you
        open lftpweb by, then restart. See the README's “Network exposure”.
      </span>
    </div>
  )
}

/** Audit batch C3: some stored "Extra lftp settings" lines fail validation (a value saved before
 * the restriction existed, or restored from an old backup) and are being ignored; transfers are
 * not held. Say what, link to the one place that fixes it.
 */
export function LftpSettingsBanner() {
  const fetcher = useCallback(getHealth, [])
  const health = usePoll(fetcher, POLL_INTERVAL_MS)
  const message = lftpSettingsIgnoredMessage(health)
  if (message == null) return null
  return (
    <div className={bannerClasses}>
      <span>{message} — fix in Settings → Transfer</span>
      <Link
        to="/settings/transfer"
        className="shrink-0 rounded-md border border-amber-400 px-2 py-1 font-medium hover:bg-amber-100 dark:border-amber-700 dark:hover:bg-amber-900"
      >
        Fix in Settings
      </Link>
    </div>
  )
}
