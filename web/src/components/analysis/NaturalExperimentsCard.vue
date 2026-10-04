<script setup lang="ts">
// The history study: past afternoons when the main floor floated warm, compared with
// similar-weather afternoons when it didn't. Two controls guard against being fooled: fake
// event days (placebo) and the bed wing, which shouldn't react to the main floor at all.
import { computed, ref, watch } from 'vue'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import { minutes } from '@/lib/format'
import { useAnalysis } from '@/stores/analysis'
import IntervalBar from './IntervalBar.vue'
import Segmented from './Segmented.vue'
import { crossesZero, dayLabel, interval, pair, signed, signedMinutes } from './stats'

const analysis = useAnalysis()
const DAYS = [
  { value: 30, label: '30 d' },
  { value: 90, label: '90 d' },
  { value: 180, label: '180 d' },
  { value: 365, label: '1 yr' },
]
const days = ref(90)
watch(days, (d) => analysis.loadNatural(d), { immediate: true })

const data = computed(() => analysis.natural.data)
const ci = computed(() => pair(data.value?.ci90))
const events = computed(() => [...(data.value?.events ?? [])].sort((a, b) => (a.date < b.date ? 1 : -1)))

/** Controls carry no interval of their own; show their size relative to the main estimate. */
function relative(v: number | null): string {
  const est = data.value?.estimate_min_per_event
  if (v === null || est === null || est === undefined || est === 0) return ''
  return `${Math.round((Math.abs(v) / Math.abs(est)) * 100)}% the size of the main estimate. `
}
</script>

<template>
  <Card title="Natural experiments" subtitle="Past warm-main-floor afternoons vs similar days without">
    <Segmented v-model="days" :options="DAYS" label="History window" class="mb-3" />
    <AsyncState
      :loading="analysis.natural.loading && !data"
      :error="analysis.natural.error"
    >
      <div v-if="data" class="space-y-4">
        <div v-if="data.estimate_min_per_event !== null" class="space-y-2">
          <p class="text-sm">
            On afternoons the main floor floated warm, the upstairs ran
            <span class="num text-lg font-semibold">{{ signedMinutes(data.estimate_min_per_event) }}</span>
            more than its weather baseline predicts.
          </p>
          <IntervalBar
            :value="data.estimate_min_per_event"
            :low="ci ? ci[0] : null"
            :high="ci ? ci[1] : null"
            unit=" min"
            :digits="0"
            :min-span="10"
            label="Extra upstairs runtime per event"
          />
          <p class="text-sm">
            90% interval: <span class="num font-medium">{{ interval(ci?.[0], ci?.[1], 0, ' min') }}</span>.
            <span v-if="!ci" class="text-muted">Without an interval this is not a finding.</span>
            <span v-else-if="crossesZero(ci[0], ci[1])" class="text-muted">It includes zero, so the history can't confirm the effect yet.</span>
          </p>
        </div>
        <p v-else class="rounded-xl border border-warn/40 bg-warn/10 p-3 text-sm">{{ data.note || 'Not enough events yet.' }}</p>

        <div class="grid gap-2 sm:grid-cols-2">
          <div class="rounded-xl bg-surface-2 p-3">
            <p class="text-xs text-muted">Placebo: fake event days</p>
            <p class="num font-semibold">{{ signedMinutes(data.placebo_estimate) }}</p>
            <p class="text-xs text-muted">{{ relative(data.placebo_estimate) }}Should be near zero; if not, something other than the floor is at work.</p>
          </div>
          <div class="rounded-xl bg-surface-2 p-3">
            <p class="text-xs text-muted">Bed wing (negative control)</p>
            <p class="num font-semibold">{{ signedMinutes(data.bed_wing_estimate) }}</p>
            <p class="text-xs text-muted">{{ relative(data.bed_wing_estimate) }}Should be near zero; the wing is not above the main floor, so an effect there points at sun or weather.</p>
          </div>
        </div>

        <p v-if="data.estimate_min_per_event !== null && data.note" class="text-xs text-muted">{{ data.note }}</p>

        <div v-if="events.length">
          <h3 class="mb-1 text-xs font-semibold text-muted uppercase">{{ events.length }} events in {{ data.days }} days</h3>
          <div class="max-h-80 overflow-auto">
            <table class="num w-full text-sm">
              <thead class="sticky top-0 bg-surface">
                <tr class="text-left text-xs text-muted">
                  <th class="py-1 pr-2 font-medium">Day</th>
                  <th class="py-1 pr-2 text-right font-medium">Main floated</th>
                  <th class="py-1 pr-2 text-right font-medium">Upstairs ran</th>
                  <th class="py-1 pr-2 text-right font-medium">Expected</th>
                  <th class="py-1 text-right font-medium">Extra</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="e in events" :key="e.date" class="border-t border-line">
                  <td class="py-1.5 pr-2 whitespace-nowrap">{{ dayLabel(e.date) }}</td>
                  <td class="py-1.5 pr-2 text-right whitespace-nowrap">{{ signed(e.main_floor_float_f, 1, '°') }}</td>
                  <td class="py-1.5 pr-2 text-right whitespace-nowrap">{{ minutes(e.up_runtime_min) }}</td>
                  <td class="py-1.5 pr-2 text-right whitespace-nowrap text-muted">{{ minutes(e.expected_up_runtime_min) }}</td>
                  <td class="py-1.5 text-right whitespace-nowrap">
                    {{ e.expected_up_runtime_min === null ? '—' : signedMinutes(e.up_runtime_min - e.expected_up_runtime_min) }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="mt-1 text-xs text-muted">
            "Main floated" is how far the main floor sat above its occupied cool setpoint. Single days are noisy; only
            the estimate above, with its interval, is a finding.
          </p>
        </div>
        <p v-else class="text-sm text-muted">No warm-main-floor afternoons found in this window.</p>
      </div>
    </AsyncState>
  </Card>
</template>
