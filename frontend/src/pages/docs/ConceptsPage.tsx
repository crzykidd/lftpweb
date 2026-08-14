import conceptsSource from '../../../../docs/concepts.md?raw'
import { MarkdownDoc } from './MarkdownDoc'

/** Docs → Concepts. The prose lives in `docs/concepts.md` (2026-08-14,
 * prompts/2026-08-14-docs-as-markdown-single-source.md) -- readable straight from the repo, not
 * only through this page -- and is imported here as a raw string and rendered by `MarkdownDoc`.
 * The in-page Jump nav is a hand-authored ```jump fenced block near the top of that file; see
 * `lib/docMarkdown.ts` for its `label|#id` line format.
 */
export function ConceptsPage() {
  return <MarkdownDoc source={conceptsSource} />
}
