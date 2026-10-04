// Chart colors that follow the theme. ECharts needs real color values, so we read the CSS
// tokens with cssVar(); `useChartPalette()` recomputes when the `dark` class flips so every
// option built from it re-renders in the new theme.
import { computed, ref, type ComputedRef } from 'vue'
import type { ComposeOption } from 'echarts/core'
import type { BarSeriesOption, CustomSeriesOption, LineSeriesOption, ScatterSeriesOption } from 'echarts/charts'
import type {
  GridComponentOption,
  LegendComponentOption,
  MarkLineComponentOption,
  TooltipComponentOption,
  VisualMapComponentOption,
} from 'echarts/components'
import { cssVar } from '@/lib/format'

export type ChartOption = ComposeOption<
  | BarSeriesOption
  | LineSeriesOption
  | ScatterSeriesOption
  | CustomSeriesOption
  | GridComponentOption
  | LegendComponentOption
  | MarkLineComponentOption
  | TooltipComponentOption
  | VisualMapComponentOption
>

export interface Palette {
  ink: string
  muted: string
  line: string
  surface: string
  surface2: string
  accent: string
  good: string
  warn: string
  bad: string
  unit: Record<string, string>
}

const themeTick = ref(0)
let observing = false

function readPalette(): Palette {
  return {
    ink: cssVar('--color-ink'),
    muted: cssVar('--color-muted'),
    line: cssVar('--color-line'),
    surface: cssVar('--color-surface'),
    surface2: cssVar('--color-surface-2'),
    accent: cssVar('--color-accent'),
    good: cssVar('--color-good'),
    warn: cssVar('--color-warn'),
    bad: cssVar('--color-bad'),
    unit: {
      main: cssVar('--color-unit-main'),
      up: cssVar('--color-unit-up'),
      bed: cssVar('--color-unit-bed'),
    },
  }
}

export function useChartPalette(): ComputedRef<Palette> {
  if (!observing && typeof MutationObserver !== 'undefined') {
    observing = true
    new MutationObserver(() => themeTick.value++).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['class'],
    })
  }
  return computed(() => {
    void themeTick.value
    return readPalette()
  })
}

export function unitColor(p: Palette, unitKey: string, index = 0): string {
  return p.unit[unitKey] ?? [p.accent, p.warn, p.good][index % 3]
}

/** Shared axis / tooltip / text styling. Spread into every option. */
export function baseOption(p: Palette): ChartOption {
  return {
    animationDuration: 300,
    textStyle: { color: p.muted, fontFamily: 'inherit', fontSize: 11 },
    tooltip: {
      backgroundColor: p.surface,
      borderColor: p.line,
      textStyle: { color: p.ink, fontSize: 12 },
      confine: true,
    },
  }
}

export function axisStyle(p: Palette) {
  return {
    axisLine: { lineStyle: { color: p.line } },
    axisTick: { lineStyle: { color: p.line } },
    axisLabel: { color: p.muted, fontSize: 11 },
    splitLine: { lineStyle: { color: p.line, opacity: 0.6 } },
    nameTextStyle: { color: p.muted, fontSize: 11 },
  }
}
