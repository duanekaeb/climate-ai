<script setup lang="ts">
// Headline savings for a period: the weather-normalized % with its 90% interval, or, when the
// baselines don't pass their checks, the reason there is no number yet.
import { computed } from 'vue'
import type { Savings } from '@/api/types'
import { UNIT_NAMES, minutes } from '@/lib/format'
import IntervalBar from './IntervalBar.vue'
import { crossesZero, dayRange, interval, num, signedMinutes } from './stats'

const props = defineProps<{ savings: Savings }>()

const claim = computed(() => props.savings.baseline_ok && props.savings.savings_pct !== null)
const low = computed(() => props.savings.ci90_low_pct)
const high = computed(() => props.savings.ci90_high_pct)
const pctAbs = computed(() => Math.abs(props.savings.savings_pct ?? 0))
const direction = computed(() =>
  (props.savings.savings_pct ?? 0) >= 0 ? 'less runtime than the weather predicts' : 'more runtime than the weather predicts',
)
const undecided = computed(() => crossesZero(low.value, high.value))
/** Green or red only when the whole interval is on one side of zero. */
const headlineClass = computed(() => (undecided.value ? 'text-ink' : (low.value ?? 0) > 0 ? 'text-good' : 'text-bad'))
const units = computed(() =>
  [...props.savings.by_unit].sort(
    (a, b) => Object.keys(UNIT_NAMES).indexOf(a.unit_key) - Object.keys(UNIT_NAMES).indexOf(b.unit_key),
  ),
)
</script>

<template>
  <div class="space-y-4">
    <p class="text-xs text-muted">
      {{ dayRange(savings.start, savings.end) }} · {{ savings.n_days }} {{ savings.n_days === 1 ? 'day' : 'days' }}
    </p>

    <!-- A savings claim: point estimate + 90% interval -->
    <div v-if="claim" class="space-y-3">
      <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span class="num text-5xl font-semibold tracking-tight" :class="headlineClass">
          {{ num(pctAbs, 0) }}%
        </span>
        <span class="text-sm text-muted">{{ direction }}</span>
      </div>
      <IntervalBar
        :value="savings.savings_pct"
        :low="low"
        :high="high"
        unit="%"
        :digits="0"
        better="higher"
        :min-span="5"
        label="Savings"
      />
      <p class="text-sm">
        90% interval: <span class="num font-medium">{{ interval(low, high, 0, '%') }}</span>.
        <span v-if="undecided" class="text-muted">
          The interval includes zero, so this period can't be told apart from no change yet.
        </span>
        <span v-else-if="(low ?? 0) > 0" class="text-muted">The whole interval is above zero.</span>
        <span v-else class="text-muted">The whole interval is below zero: the house ran more than the weather explains.</span>
      </p>
    </div>

    <!-- No claim: say why -->
    <div v-else class="rounded-xl border border-warn/40 bg-warn/10 p-3">
      <p class="font-medium">No savings figure yet</p>
      <p class="mt-1 text-sm">{{ savings.note || 'The weather baselines have not passed their checks for this period.' }}</p>
    </div>

    <dl class="grid grid-cols-3 gap-2 text-center">
      <div class="rounded-xl bg-surface-2 p-2">
        <dt class="text-[11px] text-muted uppercase">Expected</dt>
        <dd class="num font-semibold">{{ minutes(savings.expected_min) }}</dd>
      </div>
      <div class="rounded-xl bg-surface-2 p-2">
        <dt class="text-[11px] text-muted uppercase">Actual</dt>
        <dd class="num font-semibold">{{ minutes(savings.actual_min) }}</dd>
      </div>
      <div class="rounded-xl bg-surface-2 p-2">
        <dt class="text-[11px] text-muted uppercase">Avoided</dt>
        <dd class="num font-semibold">{{ claim ? signedMinutes(savings.savings_min) : '—' }}</dd>
      </div>
    </dl>
    <p v-if="claim && savings.note" class="text-xs text-muted">{{ savings.note }}</p>

    <div v-if="units.length">
      <h3 class="mb-1 text-xs font-semibold text-muted uppercase">By unit</h3>
      <div class="overflow-x-auto">
        <table class="num w-full text-sm">
          <thead>
            <tr class="text-left text-xs text-muted">
              <th class="py-1 pr-2 font-medium">Unit</th>
              <th class="py-1 pr-2 text-right font-medium">Expected</th>
              <th class="py-1 pr-2 text-right font-medium">Actual</th>
              <th class="py-1 text-right font-medium">vs expected*</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="u in units" :key="u.unit_key" class="border-t border-line">
              <td class="py-1.5 pr-2">
                <span class="inline-flex items-center gap-1.5 whitespace-nowrap">
                  <span class="h-2.5 w-2.5 rounded-full" :style="{ background: `var(--color-unit-${u.unit_key})` }" />
                  {{ UNIT_NAMES[u.unit_key] ?? u.unit_key }}
                </span>
              </td>
              <td class="py-1.5 pr-2 text-right">{{ minutes(u.expected_min) }}</td>
              <td class="py-1.5 pr-2 text-right">{{ minutes(u.actual_min) }}</td>
              <td class="py-1.5 text-right text-muted">
                {{ claim && u.savings_pct !== null ? `${num(Math.abs(u.savings_pct), 0)}% ${u.savings_pct >= 0 ? 'under' : 'over'}` : '—' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="mt-1 text-xs text-muted">
        * Point estimates only. The 90% interval is computed for the whole house, so only the headline is a savings
        claim; heat moves between floors, so one unit's change can be another's cost.
      </p>
    </div>
  </div>
</template>
