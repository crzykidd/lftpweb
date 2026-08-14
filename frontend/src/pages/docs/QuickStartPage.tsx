import quickStartSource from '../../../../docs/quick-start.md?raw'
import { MarkdownDoc } from './MarkdownDoc'

/** Docs → Quick start. The prose lives in `docs/quick-start.md` (2026-08-14,
 * prompts/2026-08-14-docs-as-markdown-single-source.md) -- readable straight from the repo, not
 * only through this page -- and is imported here as a raw string and rendered by `MarkdownDoc`.
 * See that file and `lib/docMarkdown.ts` for how a Markdown `## ` heading becomes a numbered
 * `Step`.
 */
export function QuickStartPage() {
  return <MarkdownDoc source={quickStartSource} />
}
