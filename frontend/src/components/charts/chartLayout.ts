// Shared sizing cap for both Dashboard charts (2026-08-17,
// prompts/done/2026-08-17-chart-height-cap-and-single-scroll.md). An SVG sized only by
// `viewBox` + `w-full` has no height ceiling -- the browser derives height from width via
// the intrinsic aspect ratio, so a wider window makes the chart arbitrarily taller. `max-h-80`
// (320px) puts a hard ceiling on the rendered `<svg>`.
//
// Pairing that with `max-w-4xl` (896px) on the chart's outer block (title, svg, legend --
// not just the svg) keeps the block itself from ballooning sideways, AND, for both charts'
// actual viewBox proportions, keeps the svg's natural height under the 320px ceiling at that
// width -- 896 * (260/760) ≈ 306px for BytesChart, 896 * (220/760) ≈ 259px for SpeedLineChart
// -- so `preserveAspectRatio`'s default letterboxing (which the max-height cap would otherwise
// trigger, pillarboxing the chart on a very wide window) never actually engages in practice.
// The max-height stays in place as a hard ceiling regardless -- e.g. for a future chart with a
// taller viewBox than these two -- it just isn't the everyday mechanism here.
export const CHART_BLOCK_CLASSES = 'mx-auto w-full max-w-4xl'
export const CHART_SVG_MAX_HEIGHT_CLASS = 'max-h-80'
