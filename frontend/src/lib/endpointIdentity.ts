/** Endpoint identity, mirroring the backend's `core/endpoint_identity.py` (audit batch C4, F6).
 *
 * A stored write-only secret is only sent to the endpoint it was saved for, so when a user edits
 * an endpoint field away from its saved value the secret field must be filled in again. These
 * helpers decide "did the endpoint move?" with the same normalisation as the server -- scheme,
 * lowercased host, port with the scheme default filled in, embedded credentials, and the path
 * with no trailing slash -- so a trailing slash, hostname case, an explicit default port or stray
 * whitespace does not raise the hint.
 *
 * **Advisory only.** The form never blocks on this: the server's 422 is the real gate, and if the
 * two ever disagree the server wins and its message is shown. No confirm dialog, by design.
 */

const DEFAULT_PORTS: Record<string, string> = { 'http:': '80', 'https:': '443' }

function normPath(path: string | null | undefined): string {
  const stripped = (path ?? '').trim().replace(/^\/+|\/+$/g, '')
  return stripped ? `/${stripped}` : ''
}

export function urlIdentityKey(value: string | null | undefined, extraPath?: string | null): string {
  const raw = (value ?? '').trim()
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    return JSON.stringify(['raw', raw.replace(/\/+$/, ''), normPath(extraPath)])
  }
  if (!url.hostname) return JSON.stringify(['raw', raw.replace(/\/+$/, ''), normPath(extraPath)])
  // `URL` already lowercases scheme and host and drops a default port from `.port`.
  const port = url.port || DEFAULT_PORTS[url.protocol] || ''
  return JSON.stringify([
    'url',
    url.protocol,
    url.hostname,
    port,
    url.username,
    url.password,
    normPath(url.pathname) + normPath(extraPath),
    url.search.replace(/^\?/, ''),
  ])
}

export function sshIdentityKey(
  address: string | null | undefined,
  port: string | number | null | undefined,
  username: string | null | undefined,
): string {
  const p = port === '' || port == null ? '22' : String(Number(port))
  return JSON.stringify(['ssh', (address ?? '').trim().toLowerCase(), p, (username ?? '').trim()])
}
