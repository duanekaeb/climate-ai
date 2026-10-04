<script setup lang="ts">
// A point estimate with its 90% interval on an axis centred on zero. The bar is colored only
// when the whole interval sits on one side of zero; an interval that crosses zero stays
// neutral, because the data can't tell that effect from none.
import { computed } from 'vue'
import { crossesZero, interval, signed } from './stats'

const props = withDefaults(
  defineProps<{
    value: number | null
    low: number | null
    high: number | null
    unit?: string
    digits?: number
    /** Which side of zero is the good outcome ('lower' for runtime deltas). */
    better?: 'higher' | 'lower' | 'none'
    /** Smallest half-width of the axis, so a tiny interval doesn't fill the bar. */
    minSpan?: number
    label?: string
  }>(),
  { unit: '', digits: 1, better: 'none', minSpan: 1, label: 'Estimate' },
)

function niceCeil(v: number): number {
  if (v <= 0) return 1
  const p = 10 ** Math.floor(Math.log10(v))
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10]) if (m * p >= v) return m * p
  return 10 * p
}

const hasInterval = computed(() => props.low !== null && props.high !== null)
const span = computed(() =>
  niceCeil(
    Math.max(Math.abs(props.low ?? 0), Math.abs(props.high ?? 0), Math.abs(props.value ?? 0), props.minSpan) * 1.1,
  ),
)
function pos(v: number): number {
  const s = span.value
  return Math.min(100, Math.max(0, ((v + s) / (2 * s)) * 100))
}

type Tone = 'good' | 'bad' | 'neutral' | 'unknown'
const tone = computed<Tone>(() => {
  if (!hasInterval.value) return 'unknown'
  if (crossesZero(props.low, props.high)) return 'neutral'
  const positive = (props.low ?? 0) > 0
  if (props.better === 'none') return 'neutral'
  return (props.better === 'higher') === positive ? 'good' : 'bad'
})
const BAR: Record<Tone, string> = {
  good: 'bg-good/35',
  bad: 'bg-bad/35',
  neutral: 'bg-accent/30',
  unknown: 'bg-muted/25',
}
const DOT: Record<Tone, string> = {
  good: 'bg-good',
  bad: 'bg-bad',
  neutral: 'bg-accent',
  unknown: 'bg-muted',
}

const aria = computed(
  () =>
    `${props.label}: ${signed(props.value, props.digits, props.unit)}, 90% interval ${interval(
      props.low,
      props.high,
      props.digits,
      props.unit,
    )}`,
)
</script>

<template>
  <div role="img" :aria-label="aria" class="w-full min-w-0">
    <div class="relative h-7">
      <div class="absolute inset-x-0 top-1/2 h-px bg-line" />
      <div class="absolute top-0.5 bottom-0.5 w-px bg-muted/70" style="left: 50%" />
      <div
        v-if="hasInterval && low !== null && high !== null"
        class="absolute top-1/2 h-2.5 -translate-y-1/2 rounded-full"
        :class="BAR[tone]"
        :style="{ left: `${pos(low)}%`, width: `${Math.max(0.8, pos(high) - pos(low))}%` }"
      />
      <div
        v-if="value !== null"
        class="absolute top-1/2 h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-surface"
        :class="DOT[tone]"
        :style="{ left: `${pos(value)}%` }"
      />
    </div>
    <div class="num flex justify-between text-[11px] text-muted">
      <span>{{ signed(-span, span < 10 ? 1 : 0, unit) }}</span>
      <span>0</span>
      <span>{{ signed(span, span < 10 ? 1 : 0, unit) }}</span>
    </div>
  </div>
</template>
