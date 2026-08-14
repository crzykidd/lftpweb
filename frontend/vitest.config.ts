import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Separate from vite.config.ts (2026-08-13, prompts/2026-08-13-frontend-test-runner.md) --
// vite.config.ts's `server` block (dev-only proxy/host settings, docs/decisions.md) has
// nothing to do with running tests, and merging the two would mean either dragging that block
// along for no reason or splitting vite.config.ts's plugins list out to share. A dedicated
// config is the smaller diff and keeps "how the dev server runs" and "how tests run" from
// drifting into each other's concerns.
//
// `@vitejs/plugin-react` is included so any .tsx module a test imports (even indirectly, e.g.
// FileTree.test.ts importing FileTree.tsx to reach its pure helpers) transforms exactly the way
// the real build does -- no react plugin at all would still parse JSX (Vite's default esbuild
// pipeline handles that), but this keeps the transform identical to production rather than
// merely compatible with it. `@tailwindcss/vite` is deliberately left out: nothing under test
// imports a stylesheet, and pulling in a CSS-processing plugin for tests that never touch CSS
// would just be dead weight in every test run.
export default defineConfig({
  plugins: [react()],
  test: {
    // happy-dom over jsdom (2026-08-13, docs/decisions.md): the suite here is pure-function unit
    // tests -- format/storage/tree-sorting/collapse-preference logic -- that need `localStorage`,
    // `window`, and `Intl` to exist, not pixel-accurate layout or exhaustive DOM-spec fidelity.
    // happy-dom implements that surface and starts noticeably faster per test file than jsdom;
    // faithfulness to obscure DOM edge cases only starts to matter once this suite grows real
    // component tests, at which point revisit.
    environment: 'happy-dom',
    include: ['src/**/*.test.{ts,tsx}'],
    // No coverage thresholds (prompts/2026-08-13-frontend-test-runner.md's own instruction) --
    // a threshold on a suite this young produces busywork, not confidence.
  },
})
