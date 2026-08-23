// The unattributed-clients banner's deep link (finding #13, 2026-08-23,
// prompts/2026-08-23-category-control-and-banner-link.md) -- `lib/eventsLink.ts`'s own shape,
// applied to a different destination: the banner already knows *which* instance is
// unattributable, so it should send the user straight to that instance's own row rather than
// naming a settings path for them to navigate by hand (and the path it used to name,
// "Settings → Integrations → API Clients", doesn't exist -- see this finding's own text).
//
// Pure functions, tested without mounting anything, same discipline `eventsLink.ts` and
// `nav.ts.tabsForPath` already establish for route logic.

/** The banner's own href for one unattributable client -- opens Settings → Clients with that
 * instance pre-selected for edit (`ClientsTab.tsx` reads `edit` back via
 * `parseClientEditParam` below and calls its own `startEdit`).
 */
export function clientEditHref(clientId: number): string {
  const params = new URLSearchParams({ edit: String(clientId) })
  return `/settings/clients?${params.toString()}`
}

/** `ClientsTab.tsx`'s own read side of `clientEditHref` above. `edit` must parse as a
 * non-negative integer or this reports no target at all -- a malformed or hand-edited URL
 * degrades to the plain instance list, never a crash.
 */
export function parseClientEditParam(search: string | URLSearchParams): number | null {
  const params = typeof search === 'string' ? new URLSearchParams(search) : search
  const raw = params.get('edit')
  return raw != null && /^\d+$/.test(raw) ? Number(raw) : null
}
