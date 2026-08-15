import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

// The Docs section's shared page furniture (2026-08-13, prompts/2026-08-13-docs-section.md;
// re-scoped 2026-08-14, prompts/2026-08-14-docs-as-markdown-single-source.md).
//
// **The prose itself now lives in `docs/*.md`, not here** -- `docs/`'s whole point is that
// someone reading this repo on GitHub can read the user docs without running the app. What
// stays in this file is styling only: the small vocabulary `MarkdownDoc.tsx` maps Markdown
// constructs onto (a numbered `Step`, a `Section` with a stable anchor, `Warn`/`Note` asides,
// `Where` for an internal link that must go through the router instead of a full page load,
// `Jump` for the in-page nav). `Table`'s old aggregate `{head, rows}` shape is gone --
// `MarkdownDoc.tsx` renders a GFM table element-by-element (table/thead/tr/th/td), the shape
// `react-markdown` actually walks, with its own matching Tailwind classes.

const HEADING = 'text-zinc-900 dark:text-zinc-100'

export function DocsPage({ title, lede, children }: { title: string; lede: ReactNode; children: ReactNode }) {
  return (
    <div className="flex max-w-3xl flex-col gap-8 pb-12">
      <div className="flex flex-col gap-2">
        <h1 className={`text-xl font-semibold ${HEADING}`}>{title}</h1>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">{lede}</p>
      </div>
      {children}
    </div>
  )
}

/** One documentation section. `id` is a real anchor so a link can be shared or bookmarked, and
 * so `Jump` below can point at it.
 */
export function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} className="flex scroll-mt-4 flex-col gap-3">
      <h2 className={`border-b border-zinc-200 pb-1 text-base font-semibold dark:border-zinc-800 ${HEADING}`}>
        {title}
      </h2>
      {children}
    </section>
  )
}

/** A numbered step in the quick start. The number is rendered, not an `<ol>` marker, so the
 * step's title can sit on the same line as it at a consistent width.
 */
export function Step({ n, title, children }: { n: number; title: ReactNode; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
      <h2 className={`flex items-baseline gap-2 text-base font-semibold ${HEADING}`}>
        <span className="shrink-0 rounded bg-zinc-100 px-2 py-0.5 text-sm font-mono text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
          {n}
        </span>
        <span>{title}</span>
      </h2>
      {children}
    </section>
  )
}

export function P({ children }: { children: ReactNode }) {
  return <p className="text-sm leading-relaxed text-zinc-700 dark:text-zinc-300">{children}</p>
}

export function UL({ children }: { children: ReactNode }) {
  return (
    <ul className="ml-5 flex list-disc flex-col gap-1.5 text-sm leading-relaxed text-zinc-700 dark:text-zinc-300">
      {children}
    </ul>
  )
}

/** An amber aside for the things that delete data or that people get wrong. Used sparingly --
 * a page where everything is highlighted has highlighted nothing.
 */
export function Warn({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm leading-relaxed text-amber-900 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-200">
      {children}
    </div>
  )
}

/** A neutral aside for a caveat that isn't a hazard -- "this has no UI yet", "this is a UTC
 * calendar day". Visually quieter than `Warn` on purpose.
 */
export function Note({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-md border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm leading-relaxed text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900/60 dark:text-zinc-400">
      {children}
    </div>
  )
}

export function Code({ children }: { children: ReactNode }) {
  return (
    <code className="rounded bg-zinc-100 px-1 py-0.5 font-mono text-[0.85em] text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200">
      {children}
    </code>
  )
}

/** A link to the page being described -- the whole reason these docs live in the app rather
 * than only in `README.md`.
 */
export function Where({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link to={to} className="font-medium underline decoration-zinc-400 underline-offset-2 hover:decoration-zinc-900 dark:hover:decoration-zinc-100">
      {children}
    </Link>
  )
}

/** An in-page jump list. Someone reading this is stuck, not studying — the fastest thing the
 * page can do is let them go straight to their symptom.
 */
export function Jump({ items }: { items: { id: string; label: string }[] }) {
  return (
    <nav className="flex flex-wrap gap-x-4 gap-y-1 rounded-md border border-zinc-200 px-3 py-2 text-sm dark:border-zinc-800">
      {items.map((item) => (
        <a
          key={item.id}
          href={`#${item.id}`}
          className="text-zinc-600 underline decoration-zinc-300 underline-offset-2 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
        >
          {item.label}
        </a>
      ))}
    </nav>
  )
}
