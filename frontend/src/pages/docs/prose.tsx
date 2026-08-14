import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

// The Docs section's shared page furniture (2026-08-13, prompts/2026-08-13-docs-section.md).
//
// **Deliberately components and Tailwind classes, not Markdown.** The task's own constraint was
// no markdown-renderer dependency -- this project has added exactly one runtime frontend
// dependency since phase 1 and flagged it as a deviation, and a docs page is the weakest
// possible case for adding a second. The upside beyond bundle size is the one a README can
// never have: `<Where to="/settings/queues">` below is a real router link, so every instruction
// that names a settings page can *take you there*.

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

/** A small comparison table. Wrapped in its own horizontal scroller so a narrow viewport
 * scrolls the table, never the page.
 */
export function Table({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[34rem] border-collapse text-left text-sm">
        <thead className="border-b border-zinc-200 text-xs tracking-wide text-zinc-500 uppercase dark:border-zinc-800 dark:text-zinc-400">
          <tr>
            {head.map((cell) => (
              <th key={cell} className="px-3 py-2 font-medium">
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-b border-zinc-100 align-top dark:border-zinc-900">
              {row.map((cell, j) => (
                <td key={j} className="px-3 py-2 text-zinc-700 dark:text-zinc-300">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
