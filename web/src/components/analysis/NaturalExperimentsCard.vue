<script setup lang="ts">
// The history study: past afternoons when the main floor floated warm, compared with
// similar-weather afternoons when it didn't. Two controls guard against being fooled: fake
// event days (placebo) and the bed wing, which shouldn't react to the main floor at all.
import { computed, ref, watch } from 'vue'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
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
// controls_ok === false: a control moved materially next to the estimate, so it is NOT a finding.
const controlsMoved = computed(() => data.value?.controls_ok === false)
const controls = computed(() => {
  const d = data.value
  if (!d) return []
  return [
    {
      key: 'placebo',
      title: 'Placebo: fake event days',
      estimate: d.placebo_estimate,
      ci: pair(d.placebo_ci90),
      help: 'Should be near zero; if not, something other than the floor is at work.',
    },
    {
      key: 'bed',
      title: 'Bed wing (negative control)',
      estimate: d.bed_wing_estimate,
      ci: pair(d.bed_wing_ci90),
      help: 'Should be near zero; the wing is not above the main floor, so an effect there points at sun or weather.',
    },
  ].map((c) => ({ ...c, moved: c.ci !== null && !crossesZero(c.ci[0], c.ci[1]) }))
})
const events = computed(() => [...(data.value?.events ?? [])].sort((a, b) => (a.date < b.date ? 1 : -1)))

/** Each control's size relative to the main estimate. */
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
        <div v-if="data.estimate_min_per_event !== null && controlsMoved" role="alert"
             class="flex items-start gap-2 rounded-xl border border-bad/40 bg-bad/10 p-3 text-sm">
          <Icon name="alert" :size="18" class="mt-px shrink-0 text-bad" />
          <div>
            <p class="font-semibold">A control moved: not a finding</p>
            <p class="mt-0.5 text-muted">
              The placebo or the bed wing shifted by a material amount next to the estimate, so something other than the
              main floor (sun, weather, schedules) may explain it. The number below is shown for reference only.
            </p>
          </div>
        </div>

        <div v-if="data.estimate_min_per_event !== null" class="space-y-2" :class="controlsMoved && 'text-muted'">
          <p class="text-sm">
            On afternoons the main floor floated warm, the upstairs ran
            <span class="num text-lg font-semibold" :class="controlsMoved && 'text-muted line-through decoration-2'">{{ signedMinutes(data.estimate_min_per_event) }}</span>
            more than its weather baseline predicts<template v-if="controlsMoved">, but a control moved</template>.
          </p>
          <div :class="controlsMoved && 'opacity-50 grayscale'">
            <IntervalBar
              :value="data.estimate_min_per_event"
              :low="ci ? ci[0] : null"
              :high="ci ? ci[1] : null"
              unit=" min"
              :digits="0"
              :min-span="10"
              label="Extra upstairs runtime per event"
            />
          </div>
          <p class="text-sm">
            90% interval: <span class="num font-medium">{{ interval(ci?.[0], ci?.[1], 0, ' min') }}</span>.
            <span v-if="controlsMoved" class="text-muted">Not a finding while a control moves.</span>
            <span v-else-if="!ci" class="text-muted">Without an interval this is not a finding.</span>
            <span v-else-if="crossesZero(ci[0], ci[1])" class="text-muted">It includes zero, so the history can't confirm the effect yet.</span>
          </p>
        </div>
        <p v-else class="rounded-xl border border-warn/40 bg-warn/10 p-3 text-sm">{{ data.note || 'Not enough events yet.' }}</p>

        <div class="grid gap-2 sm:grid-cols-2">
          <div v-for="c in controls" :key="c.key" class="rounded-xl p-3"
               :class="c.moved && controlsMoved ? 'bg-bad/10 ring-1 ring-bad/40' : 'bg-surface-2'">
            <div class="flex items-center justify-between gap-2">
              <p class="text-xs text-muted">{{ c.title }}</p>
              <span v-if="c.moved" class="chip shrink-0" :class="controlsMoved ? 'bg-bad/15 text-bad' : 'bg-warn/15 text-warn'">moved</span>
            </div>
            <p class="num font-semibold">{{ signedMinutes(c.estimate) }}</p>
            <p class="num text-xs">
              90% interval {{ interval(c.ci?.[0], c.ci?.[1], 0, ' min') }}<span v-if="c.ci" class="text-muted">{{
                c.moved ? ' (excludes zero)' : ' (includes zero)' }}</span>
            </p>
            <p class="mt-1 text-xs text-muted">{{ relative(c.estimate) }}{{ c.help }}</p>
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
