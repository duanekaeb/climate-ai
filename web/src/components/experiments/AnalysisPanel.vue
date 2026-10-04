<script setup lang="ts">
// The current analysis of a switchback: effect of arm B vs arm A on weather-normalized
// total-house runtime with its interval, and the decision, which only ever changes at a
// pre-planned checkpoint.
import { computed } from 'vue'
import type { ArmIn, Checkpoint, ExperimentAnalysis } from '@/api/types'
import IntervalBar from '@/components/analysis/IntervalBar.vue'
import { crossesZero, interval, num, signed } from '@/components/analysis/stats'
import { DECISION } from './arms'

const props = defineProps<{ analysis: ExperimentAnalysis; arms: ArmIn[]; checkpoints: Checkpoint[]; nDays: number }>()

const a = computed(() => props.arms[0]?.label ?? 'A')
const b = computed(() => props.arms[1]?.label ?? 'B')
const reached = computed(() => props.checkpoints.filter((c) => props.analysis.days_observed >= c.day).length)
const next = computed(() =>
  [...props.checkpoints].sort((x, y) => x.day - y.day).find((c) => props.analysis.days_observed < c.day) ?? null,
)
const progress = computed(() => Math.min(100, (props.analysis.days_observed / Math.max(1, props.nDays)) * 100))
const hasEffect = computed(() => props.analysis.effect_pct !== null)
const lo = computed(() => props.analysis.ci_low_pct)
const hi = computed(() => props.analysis.ci_high_pct)
</script>

<template>
  <div class="space-y-3">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <span class="chip" :class="DECISION[analysis.decision].chip">{{ DECISION[analysis.decision].label }}</span>
      <span class="num text-xs text-muted">
        day {{ analysis.days_observed }} of {{ nDays }} · checkpoint {{ reached }} of {{ checkpoints.length }} reached
      </span>
    </div>
    <div class="h-1.5 overflow-hidden rounded-full bg-surface-2" aria-hidden="true">
      <div class="h-full rounded-full bg-accent" :style="{ width: `${progress}%` }" />
    </div>

    <div v-if="hasEffect" class="space-y-2">
      <p class="text-sm">
        <span class="font-medium">{{ b }}</span> vs <span class="font-medium">{{ a }}</span>:
        <span class="num text-lg font-semibold">{{ signed(analysis.effect_pct, 1, '%') }}</span>
        <span class="text-muted"> weather-normalized house runtime (negative = {{ b }} ran less)</span>
      </p>
      <IntervalBar
        :value="analysis.effect_pct"
        :low="lo"
        :high="hi"
        unit="%"
        better="lower"
        :min-span="5"
        :label="`${b} vs ${a}`"
      />
      <p class="text-sm">
        Interval: <span class="num font-medium">{{ interval(lo, hi, 1, '%') }}</span>
        <span v-if="crossesZero(lo, hi)" class="text-muted"> — it still crosses zero, which is normal for a small difference in one house.</span>
      </p>
    </div>
    <p v-else class="text-sm text-muted">No effect estimate yet.</p>

    <p v-if="analysis.note" class="text-sm">{{ analysis.note }}</p>

    <p class="rounded-xl bg-surface-2 p-3 text-xs text-muted">
      Decisions happen only at the checkpoints fixed before the test began<template v-if="next">
        (next: day {{ next.day }}, needs |z| ≥ {{ num(next.z_crit, 2) }})</template
      >. Between checkpoints the interval above is informational: an early interval is wider on purpose, and stopping
      on a daily peek manufactures false wins.
    </p>
  </div>
</template>
