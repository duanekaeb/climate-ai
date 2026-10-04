<script setup lang="ts">
// 5-minute detail for one day: each unit's runtime (bars), zone temperature, setpoints (one
// unit at a time) and the outdoor temperature.
import { computed, ref } from 'vue'
import type { Intraday } from '@/api/types'
import EChart from '@/components/EChart.vue'
import OpenMeteoAttribution from '@/components/OpenMeteoAttribution.vue'
import { UNIT_COLORS, UNIT_NAMES, minutes } from '@/lib/format'
import { sortUnitKeys } from '@/stores/runtime'
import { chartTheme, useThemeKey } from './chartKit'
import { intradayOption, intradayTotals } from './runtimeCharts'

const props = defineProps<{ intraday: Intraday; tz: string }>()
const themeKey = useThemeKey()

const allKeys = computed(() => sortUnitKeys(props.intraday.units.map((u) => u.unit_key)))
const pick = ref<string>('all')
const keys = computed(() => (pick.value === 'all' || !allKeys.value.includes(pick.value) ? allKeys.value : [pick.value]))
const single = computed(() => keys.value.length === 1)

const option = computed(() => {
  void themeKey.value
  return intradayOption({ intraday: props.intraday, unitKeys: keys.value, tz: props.tz, theme: chartTheme() })
})
const totals = computed(() => intradayTotals(props.intraday))
const hasOutdoor = computed(() => props.intraday.outdoor.some((p) => p.temp_f !== null))
</script>

<template>
  <div class="space-y-3">
    <div class="flex flex-wrap gap-1.5" role="group" aria-label="Units shown">
      <button type="button" :aria-pressed="pick === 'all'" class="chip border !px-2.5 !py-1"
              :class="pick === 'all' ? 'border-accent bg-accent text-white' : 'border-line text-muted hover:bg-surface-2'"
              @click="pick = 'all'">All units</button>
      <button v-for="k in allKeys" :key="k" type="button" :aria-pressed="pick === k" class="chip border !px-2.5 !py-1"
              :class="pick === k ? 'border-accent bg-accent text-white' : 'border-line text-muted hover:bg-surface-2'"
              @click="pick = k">
        <span class="h-2 w-2 rounded-full" :style="{ background: UNIT_COLORS[k] ?? 'var(--color-accent)' }" aria-hidden="true" />
        {{ UNIT_NAMES[k] ?? k }}
      </button>
    </div>

    <figure class="min-w-0">
      <EChart :option="option" height="340px" />
      <figcaption>
        <ul class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted">
          <li class="flex items-center gap-1"><span class="h-0.5 w-4 rounded bg-ink" aria-hidden="true" />Zone temperature (top)</li>
          <li class="flex items-center gap-1"><span class="h-3 w-2 rounded-sm bg-ink/60" aria-hidden="true" />Runtime per 5 min (bottom)</li>
          <template v-if="single">
            <li class="flex items-center gap-1"><span class="w-4 border-t-2 border-dashed border-bad" aria-hidden="true" />Heat setpoint</li>
            <li class="flex items-center gap-1"><span class="w-4 border-t-2 border-dashed border-accent" aria-hidden="true" />Cool setpoint</li>
          </template>
          <li v-else>Pick one unit to see its setpoints.</li>
          <li v-if="hasOutdoor" class="flex items-center gap-1"><span class="w-4 border-t-2 border-dotted border-warn" aria-hidden="true" />Outdoor</li>
        </ul>
      </figcaption>
    </figure>

    <dl class="grid grid-cols-3 gap-2 text-center">
      <div v-for="k in allKeys" :key="k" class="rounded-xl bg-surface-2 p-2">
        <dt class="flex items-center justify-center gap-1 text-[11px] text-muted">
          <span class="h-2 w-2 rounded-full" :style="{ background: UNIT_COLORS[k] ?? 'var(--color-accent)' }" aria-hidden="true" />
          <span class="truncate">{{ UNIT_NAMES[k] ?? k }}</span>
        </dt>
        <dd class="num font-semibold">{{ minutes(totals[k] ?? 0) }}</dd>
      </div>
    </dl>
    <OpenMeteoAttribution v-if="hasOutdoor" />
  </div>
</template>
