<script setup lang="ts">
// "Did it work?": weather-normalized savings with a 90% interval (or why there is no number
// yet), expected vs actual per day, the weekly waterfall, and the baselines behind it all.
import { computed, onMounted, ref, watch } from 'vue'
import AsyncState from '@/components/AsyncState.vue'
import Card from '@/components/Card.vue'
import Icon from '@/components/Icon.vue'
import OpenMeteoAttribution from '@/components/OpenMeteoAttribution.vue'
import BaselinesTable from '@/components/analysis/BaselinesTable.vue'
import SavingsChart from '@/components/analysis/SavingsChart.vue'
import SavingsSummary from '@/components/analysis/SavingsSummary.vue'
import Segmented from '@/components/analysis/Segmented.vue'
import WaterfallCard from '@/components/analysis/WaterfallCard.vue'
import { addDays, daysBetween, todayIn } from '@/components/analysis/stats'
import { useAnalysis } from '@/stores/analysis'
import { useStatus } from '@/stores/status'

type Preset = 7 | 14 | 30 | 'custom'
const PRESETS: { value: Preset; label: string }[] = [
  { value: 7, label: '7 days' },
  { value: 14, label: '14 days' },
  { value: 30, label: '30 days' },
  { value: 'custom', label: 'Custom' },
]
const MAX_DAYS = 366

const analysis = useAnalysis()
const status = useStatus()
// The house's days, not the device's: wait for the house time zone before picking a period.
// If the status can't load at all, fall back to the device's zone rather than never loading.
const tzKnown = computed(() => !!status.data || !!status.error)
const tz = computed(() => status.data?.tz)

// Complete days only: the period ends yesterday (house time).
const today = computed(() => todayIn(tz.value))
const yesterday = computed(() => addDays(today.value, -1))
const preset = ref<Preset>(14)
const customStart = ref(addDays(yesterday.value, -13))
const customEnd = ref(yesterday.value)
// The custom-range defaults follow the house's "yesterday" (it changes when the time zone
// arrives) until the owner edits them.
let customEdited = false
watch(yesterday, (y) => {
  if (customEdited) return
  customStart.value = addDays(y, -13)
  customEnd.value = y
})
function editCustom(which: 'start' | 'end', value: string) {
  customEdited = true
  if (which === 'start') customStart.value = value
  else customEnd.value = value
}

const range = computed(() => {
  if (preset.value === 'custom') return { start: customStart.value, end: customEnd.value }
  return { start: addDays(yesterday.value, -(preset.value - 1)), end: yesterday.value }
})

const rangeError = computed(() => {
  if (preset.value !== 'custom') return ''
  const { start, end } = range.value
  if (!start || !end) return 'Pick a start and an end date.'
  if (start > end) return 'The start must be on or before the end.'
  if (end > yesterday.value) return 'Pick days up to yesterday; today is not complete yet.'
  if (daysBetween(start, end) + 1 > MAX_DAYS) return `Pick at most ${MAX_DAYS} days.`
  return ''
})

watch(
  [range, tzKnown],
  ([r, known]) => {
    if (known && !rangeError.value) void analysis.loadSavings(r.start, r.end)
  },
  { immediate: true },
)

onMounted(() => {
  // The house timezone and room names come from the status store; start it if the shell hasn't.
  status.start()
  void analysis.loadBaselines()
})

const savings = computed(() => analysis.savings.data)
const hasOutdoor = computed(() => (savings.value?.days ?? []).some((d) => d.outdoor_mean_f !== null))
</script>

<template>
  <div class="space-y-4">
    <p class="text-sm text-muted">
      <strong class="font-medium text-ink">Weather-normalized</strong> means each day's runtime is compared with what that
      day's weather predicts from the house's own history, so a cooler week doesn't count as savings.
    </p>

    <Card title="Savings vs the weather">
      <template #actions>
        <button
          type="button"
          class="btn !px-2 !py-1"
          aria-label="Reload savings"
          :disabled="analysis.savings.loading || !!rangeError || !tzKnown"
          @click="analysis.loadSavings(range.start, range.end)"
        >
          <Icon name="refresh" :size="14" />
        </button>
      </template>
      <div class="mb-3 space-y-2">
        <Segmented v-model="preset" :options="PRESETS" label="Period" />
        <div v-if="preset === 'custom'" class="grid grid-cols-2 gap-2">
          <label class="text-xs text-muted">
            From
            <input :value="customStart" type="date" class="input mt-1 dark:[color-scheme:dark]" :max="yesterday"
                   @input="editCustom('start', ($event.target as HTMLInputElement).value)" />
          </label>
          <label class="text-xs text-muted">
            To
            <input :value="customEnd" type="date" class="input mt-1 dark:[color-scheme:dark]" :max="yesterday"
                   @input="editCustom('end', ($event.target as HTMLInputElement).value)" />
          </label>
        </div>
        <p v-if="rangeError" class="text-xs text-bad">{{ rangeError }}</p>
      </div>
      <AsyncState
        :loading="(analysis.savings.loading && !savings) || !tzKnown"
        :error="rangeError ? '' : analysis.savings.error"
        :empty="!rangeError && !!savings && savings.n_days === 0"
        empty-text="No complete days of runtime in this period."
      >
        <SavingsSummary v-if="savings && !rangeError" :savings="savings" />
      </AsyncState>
    </Card>

    <Card
      v-if="savings && savings.days.length && !rangeError"
      title="Expected vs actual, per day"
      subtitle="Bars are what the house ran; the dashed line is what the weather predicts."
    >
      <SavingsChart :days="savings.days" />
      <OpenMeteoAttribution v-if="hasOutdoor" class="mt-1" />
    </Card>

    <WaterfallCard :tz="tz" />

    <Card title="Weather baselines" subtitle="One per unit and mode, refit nightly.">
      <AsyncState
        :loading="analysis.baselines.loading && !analysis.baselines.data"
        :error="analysis.baselines.error"
        :empty="!!analysis.baselines.data && !analysis.baselines.data.length"
        empty-text="No baselines yet. They need at least 21 days of runtime in a mode."
      >
        <BaselinesTable v-if="analysis.baselines.data" :baselines="analysis.baselines.data" />
      </AsyncState>
    </Card>
  </div>
</template>
