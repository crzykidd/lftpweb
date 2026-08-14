import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import conceptsSource from '../../../../docs/concepts.md?raw'
import quickStartSource from '../../../../docs/quick-start.md?raw'
import { MarkdownDoc } from './MarkdownDoc'

// A real render, not just "parses without throwing" -- this is the actual pipeline the app
// runs (react-markdown + remark-gfm + the `remarkCallouts` plugin), against the actual shipped
// `docs/*.md` content, so a broken callout/table/link mapping fails a test instead of only
// being discoverable by a human clicking through the app (2026-08-14,
// prompts/2026-08-14-docs-as-markdown-single-source.md -- "you cannot see the UI" applies to
// the agent, not to what a static render can assert).

function renderDoc(source: string): string {
  return renderToStaticMarkup(
    <MemoryRouter>
      <MarkdownDoc source={source} />
    </MemoryRouter>,
  )
}

describe('MarkdownDoc against docs/quick-start.md', () => {
  const html = renderDoc(quickStartSource)

  it('renders without throwing and includes the title', () => {
    expect(html).toContain('Quick start')
  })

  it('renders six numbered steps', () => {
    for (const n of [1, 2, 3, 4, 5, 6]) {
      expect(html).toContain(`>${n}<`)
    }
  })

  it('maps an internal link to a router-relative href, not a full-page anchor', () => {
    expect(html).toMatch(/href="\/settings\/queues"/)
  })

  it('renders the move-mode Warning as a distinct callout, with the marker word stripped', () => {
    expect(html).toContain('hardlink pickup directory')
    expect(html).not.toContain('Warning:')
  })

  it('renders inline code spans', () => {
    expect(html).toMatch(/<code[^>]*>\/config<\/code>/)
  })
})

describe('MarkdownDoc against docs/concepts.md', () => {
  const html = renderDoc(conceptsSource)

  it('renders the Jump nav with anchor hrefs', () => {
    expect(html).toMatch(/href="#settle"/)
    expect(html).toMatch(/href="#inherit"/)
  })

  it('gives each section a matching id attribute', () => {
    for (const id of ['settle', 'suppression', 'blast-radius', 'icons', 'copy-move', 'inherit']) {
      expect(html).toMatch(new RegExp(`id="${id}"`))
    }
  })

  it('renders the suppression table as a real GFM table', () => {
    expect(html).toContain('<table')
    expect(html).toContain('user_stopped')
    expect(html).toContain('retries_exhausted')
  })

  it('renders the settle-gate Note callout with the marker word stripped', () => {
    expect(html).toContain('on by default')
    expect(html).not.toContain('Note:')
    expect(html).not.toContain('Warning:')
  })
})
