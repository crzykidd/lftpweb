import howItWorksSource from '../../../../docs/how-it-works.md?raw'
import { MarkdownDoc } from './MarkdownDoc'

/** Docs → How it works (2026-08-14). The prose lives in `docs/how-it-works.md` — same
 * single-source arrangement as Quick start and Concepts: readable straight from the repo, and
 * imported here as a raw string for `MarkdownDoc` to render, so the two can never disagree.
 *
 * Deliberately the *first* tab: Quick start tells someone what to click, Concepts explains the
 * behaviours that surprise them, and this explains why the thing is shaped the way it is. Someone
 * evaluating the project reads this one; `README.md`'s own "How it works" section is a two-line
 * summary that links here rather than repeating it.
 */
export function HowItWorksPage() {
  return <MarkdownDoc source={howItWorksSource} />
}
