import type { ComponentPropsWithoutRef, ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { classifyLink } from '../../lib/docLinks'
import { parseDocSource } from '../../lib/docMarkdown'
import { remarkCallouts } from '../../lib/remarkCallouts'
import { Code, DocsPage, Jump, Note, P, Section, Step, UL, Warn, Where } from './prose'

// Renders one of `docs/quick-start.md` / `docs/concepts.md` (2026-08-14,
// prompts/2026-08-14-docs-as-markdown-single-source.md) -- the Markdown file is the only place
// this prose lives; `QuickStartPage.tsx`/`ConceptsPage.tsx` are now five-line wrappers that
// import their file via Vite's `?raw` suffix and hand it to this component.
//
// Structure (title, lede, the Jump nav, where a `## ` heading starts a numbered `Step` versus a
// `Section`, a section's anchor id) is parsed by the pure functions in `lib/docMarkdown.ts` --
// deliberately *not* run through a Markdown engine, since none of that is prose that needs
// inline formatting. Each section's own body text *is* real Markdown (links, `**bold**`,
// `` `code` ``, GFM tables, the `> **Warning:**`/`> **Note:**` callout convention) and goes
// through `react-markdown` + `remark-gfm`, with `components` below mapping each element back
// onto `prose.tsx`'s existing styling vocabulary -- so a heading/list/link/table in the
// Markdown source renders with exactly the classes the old hand-written JSX used.

type DivProps = ComponentPropsWithoutRef<'div'> & { 'data-callout'?: string }

const bodyComponents = {
  // `h1`/`h2`/`h3` are only ever hit by `ReleaseNotesPage.tsx` (2026-08-17) -- a section's own
  // `body` here never contains a heading (the `## ` that would start one is exactly where
  // `docMarkdown.ts` cuts the section), but `CHANGELOG.md` is real Markdown with real headings
  // at every level, rendered through this same `SectionBody` unstructured (see that file for
  // why: the changelog must render verbatim, not be reshaped the way quick-start/concepts are).
  h1: ({ children }: ComponentPropsWithoutRef<'h1'>) => (
    <h1 className="text-xl font-semibold text-zinc-900 dark:text-zinc-100">{children}</h1>
  ),
  h2: ({ children }: ComponentPropsWithoutRef<'h2'>) => (
    <h2 className="border-b border-zinc-200 pb-1 text-base font-semibold text-zinc-900 dark:border-zinc-800 dark:text-zinc-100">
      {children}
    </h2>
  ),
  h3: ({ children }: ComponentPropsWithoutRef<'h3'>) => (
    <h3 className="text-sm font-semibold text-zinc-800 dark:text-zinc-200">{children}</h3>
  ),
  p: ({ children }: ComponentPropsWithoutRef<'p'>) => <P>{children}</P>,
  ul: ({ children }: ComponentPropsWithoutRef<'ul'>) => <UL>{children}</UL>,
  code: ({ children }: ComponentPropsWithoutRef<'code'>) => <Code>{children}</Code>,
  a: ({ href, children }: ComponentPropsWithoutRef<'a'>) => {
    if (!href) return <>{children}</>
    const kind = classifyLink(href)
    if (kind === 'internal') return <Where to={href}>{children}</Where>
    if (kind === 'anchor') return <a href={href}>{children}</a>
    return (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    )
  },
  // `remarkCallouts` retags a `> **Warning:**`/`> **Note:**` blockquote as
  // `<div data-callout="warn"|"note">` (the marker text stripped, never rendered) -- this is
  // where that comes back out as `Warn`/`Note`. Any other `div` (none in these two files today)
  // falls through unstyled rather than silently swallowing content.
  div: ({ children, 'data-callout': callout }: DivProps) => {
    if (callout === 'warn') return <Warn>{children}</Warn>
    if (callout === 'note') return <Note>{children}</Note>
    return <div>{children}</div>
  },
  table: ({ children }: ComponentPropsWithoutRef<'table'>) => (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[34rem] border-collapse text-left text-sm">{children}</table>
    </div>
  ),
  thead: ({ children }: ComponentPropsWithoutRef<'thead'>) => (
    <thead className="border-b border-zinc-200 text-xs tracking-wide text-zinc-500 uppercase dark:border-zinc-800 dark:text-zinc-400">
      {children}
    </thead>
  ),
  tr: ({ children }: ComponentPropsWithoutRef<'tr'>) => (
    <tr className="border-b border-zinc-100 align-top dark:border-zinc-900">{children}</tr>
  ),
  th: ({ children }: ComponentPropsWithoutRef<'th'>) => <th className="px-3 py-2 font-medium">{children}</th>,
  td: ({ children }: ComponentPropsWithoutRef<'td'>) => (
    <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300">{children}</td>
  ),
}

/** Exported for `ReleaseNotesPage.tsx` (2026-08-17), which feeds the *whole* raw
 * `CHANGELOG.md?raw` string through this directly rather than through `MarkdownDoc` below --
 * the changelog's own shape (a `# Changelog` title, an intro paragraph, an HTML-comment
 * skeleton, then real `## `/`### ` headings) doesn't match what `parseDocSource` expects
 * (a `# Title`, one lede paragraph, then *only* `## ` section boundaries) and would throw
 * partway through the file's own commented-out skeleton example. Rendering it as one opaque
 * Markdown blob through the same `bodyComponents` styling, with no structural parsing at all,
 * is what "renders the file verbatim" (that page's own comment) means in practice.
 */
export function SectionBody({ markdown }: { markdown: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm, remarkCallouts]} components={bodyComponents}>
      {markdown}
    </ReactMarkdown>
  )
}

export function MarkdownDoc({ source }: { source: string }) {
  const { title, lede, jump, sections } = parseDocSource(source)
  return (
    <DocsPage title={title} lede={lede}>
      {jump && <Jump items={jump} />}
      {sections.map((section): ReactNode =>
        section.variant === 'step' ? (
          <Step key={section.id} n={section.stepNumber ?? 0} title={section.title}>
            <SectionBody markdown={section.body} />
          </Step>
        ) : (
          <Section key={section.id} id={section.id} title={section.title}>
            <SectionBody markdown={section.body} />
          </Section>
        ),
      )}
    </DocsPage>
  )
}
