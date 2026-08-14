// Link-mapping for the Docs section's Markdown renderer (2026-08-14,
// prompts/2026-08-14-docs-as-markdown-single-source.md). `docs/quick-start.md` and
// `docs/concepts.md` use plain Markdown links for everything the old `Where` component used to
// do -- `[Settings → Queues](/settings/queues)` -- so the renderer has to decide, per link,
// whether that's a route the SPA router should own (no full page load) or a real navigation.
//
// A pure function on purpose: this is exactly the "does href route internally" decision the
// migration prompt asked to be unit-testable in isolation, not something to work out by staring
// at MarkdownDoc.tsx's JSX.

export type LinkKind = 'internal' | 'anchor' | 'external'

/** `/foo` -- an in-app route; render with the router's `Link` so it never triggers a full page
 * load. `#foo` -- an in-page anchor; a plain `<a>` is correct (no router involvement, no
 * reload -- the browser just scrolls). Anything else (`https://...`, `mailto:...`, a bare
 * domain) is `external` and gets a plain `<a target="_blank">`.
 */
export function classifyLink(href: string): LinkKind {
  if (href.startsWith('#')) return 'anchor'
  if (href.startsWith('/')) return 'internal'
  return 'external'
}
