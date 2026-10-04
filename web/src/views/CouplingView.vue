<script setup lang="ts">
// Floor coupling: how much the main floor floating warm loads the upstairs unit, measured
// hour by hour (with the bed wing as a placebo) and from past natural experiments.
import { computed, onMounted, ref, watch } from 'vue'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import OpenMeteoAttribution from '@/components/OpenMeteoAttribution.vue'
import CouplingScatter from '@/components/analysis/CouplingScatter.vue'
import IntervalBar from '@/components/analysis/IntervalBar.vue'
import NaturalExperimentsCard from '@/components/analysis/NaturalExperimentsCard.vue'
import Segmented from '@/components/analysis/Segmented.vue'
import { crossesZero, interval, num, pair, signed } from '@/components/analysis/stats'
import { useAnalysis } from '@/stores/analysis'
import { useStatus } from '@/stores/status'

const analysis = useAnalysis()
const status = useStatus()
const DAYS = [
  { value: 14, label: '14 d' },
  { value: 30, label: '30 d' },
  { value: 60, label: '60 d' },
  { value: 90, label: '90 d' },
]
const days = ref(30)
watch(days, (d) => analysis.loadCoupling(d), { immediate: true })

// The house timezone comes from the status store; start it if the shell hasn't.
onMounted(() => status.start())

const c = computed(() => analysis.coupling.data)
const ci = computed(() => pair(c.value?.ci90))
const placeboCi = computed(() => pair(c.value?.placebo_ci90))
const hasOutdoor = computed(() => (c.value?.points ?? []).some((p) => p.outdoor_f !== null))

/** The coefficient is upstairs runtime minutes per hour; the same thing as duty points. */
const dutyPoints = computed(() => {
  const coef = c.value?.coef_min_per_degf
  return coef === null || coef === undefined ? null : (coef / 60) * 100
})
const placeboVerdict = computed<'clean' | 'suspect' | 'unknown'>(() => {
  if (!placeboCi.value) return 'unknown'
  return crossesZero(placeboCi.value[0], placeboCi.value[1]) ? 'clean' : 'suspect'
})
</script>

<template>
  <div class="space-y-4">
    <Card>
      <p class="text-sm leading-relaxed">
        When nobody is on the main floor, ecobee's Smart Away lets it float warm (around 80°F). If someone is upstairs
        holding 77°F, that warmer air and the warm floor push heat up through the ceiling and the open stairwell, and the
        upstairs unit has to remove it on top of the sun on the roof. So the main floor "saves" runtime while the upstairs
        spends more. This page measures how much.
      </p>
    </Card>

    <Card title="Main floor → upstairs" subtitle="Hourly model on cooling hours, controlling for outdoor heat, sun and time of day">
      <Segmented v-model="days" :options="DAYS" label="Days of history" class="mb-3" />
      <AsyncState :loading="analysis.coupling.loading && !c" :error="analysis.coupling.error">
        <div v-if="c" class="space-y-4">
          <div v-if="c.coef_min_per_degf !== null" class="space-y-2">
            <p class="text-sm">
              Each 1°F the main floor sits above the upstairs adds
              <span class="num text-lg font-semibold">{{ signed(c.coef_min_per_degf, 1) }} min</span>
              of upstairs runtime per hour
              <span class="text-muted">(about {{ num(dutyPoints, 0) }} points of duty)</span>.
            </p>
            <IntervalBar
              :value="c.coef_min_per_degf"
              :low="ci ? ci[0] : null"
              :high="ci ? ci[1] : null"
              unit=" min"
              :digits="1"
              :min-span="2"
              label="Upstairs minutes per hour per °F"
            />
            <p class="text-sm">
              90% interval: <span class="num font-medium">{{ interval(ci?.[0], ci?.[1], 1, ' min/h') }}</span>
              <span class="text-muted"> · {{ c.n_hours }} cooling hours over {{ c.days }} days.</span>
              <span v-if="ci && crossesZero(ci[0], ci[1])" class="text-muted"> It includes zero: no link shown yet.</span>
            </p>
          </div>
          <p v-else class="rounded-xl border border-warn/40 bg-warn/10 p-3 text-sm">
            No estimate yet. {{ c.interpretation }}
          </p>

          <div class="rounded-xl bg-surface-2 p-3">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <p class="text-sm font-medium">Placebo: main floor → bed wing</p>
              <span
                class="chip"
                :class="{
                  'bg-good/15 text-good': placeboVerdict === 'clean',
                  'bg-warn/15 text-warn': placeboVerdict === 'suspect',
                  'bg-surface text-muted': placeboVerdict === 'unknown',
                }"
              >
                {{ placeboVerdict === 'clean' ? 'no link, as expected' : placeboVerdict === 'suspect' ? 'shows a link' : 'not estimated' }}
              </span>
            </div>
            <p class="num mt-1 text-sm">
              {{ c.placebo_coef === null ? '—' : `${signed(c.placebo_coef, 1)} min/h per °F` }}
              <span class="text-muted">· 90% interval {{ interval(placeboCi?.[0], placeboCi?.[1], 1) }}</span>
            </p>
            <p class="mt-1 text-xs text-muted">
              The wing is beside the main floor, not above it, so the same model should find nothing there. If it does, sun
              and weather are leaking into the main estimate.
            </p>
          </div>

          <p v-if="c.coef_min_per_degf !== null && c.interpretation" class="text-sm">{{ c.interpretation }}</p>
        </div>
      </AsyncState>
    </Card>

    <Card
      v-if="c && c.points.length"
      title="Every cooling hour"
      subtitle="Right of the dashed line the main floor is warmer than the upstairs. Color is the outdoor temperature."
    >
      <CouplingScatter :points="c.points" :tz="status.data?.tz" />
      <p class="mt-1 text-xs text-muted">
        Hot, sunny hours sit high on their own; the coefficient above separates the floor's share from the weather's.
      </p>
      <OpenMeteoAttribution v-if="hasOutdoor" class="mt-1" />
    </Card>

    <NaturalExperimentsCard />
  </div>
</template>
