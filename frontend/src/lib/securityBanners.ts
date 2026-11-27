// Audit batch C3 (DESIGN.md §8): the two app-shell banners' visibility, as pure functions --
// this repo's component-testing story is `lib/*.test.ts` (README's Known gaps: no component
// rendering is tested), so the decision lives here and `components/SecurityBanners.tsx` only
// renders it.

import type { AuthSessionOut, HealthResponse } from '../api/types'

/** DNS-rebinding protection is off: show the shell banner in `none`/`proxy` mode while
 * `LFTPWEB_ALLOWED_HOSTS` is unset. Not in `password` mode -- a rebound page cannot use the
 * session cookie (it is bound to lftpweb's own name), so the exposure the banner warns about is
 * the unauthenticated modes'. Hidden until the session is known and authenticated, so the login
 * page and the brief "still loading" state never flash it.
 */
export function showHostAllowlistBanner(session: AuthSessionOut | null): boolean {
  if (session == null || !session.authenticated) return false
  if (session.mode !== 'none' && session.mode !== 'proxy') return false
  return !session.allowed_hosts_set
}

/** Stored "Extra lftp settings" lines that fail validation are being ignored (transfers carry
 * on without them): the message to show (a count and the first offending line), or `null` for no
 * banner.
 */
export function lftpSettingsIgnoredMessage(
  health: HealthResponse | null | undefined,
): string | null {
  const message = health?.lftp_settings_ignored
  return message ? message : null
}
