<script setup lang="ts">
import { computed } from 'vue'
import type { RoomHistory } from '@/api/types'
import EChart from '@/components/EChart.vue'
import { chartTheme, useThemeKey } from '@/components/runtime/chartKit'
import { UNIT_COLORS } from '@/lib/format'
import { roomHistoryOption } from './roomChart'

const props = defineProps<{ history: RoomHistory; hasSensor: boolean; unitKey: string; tz: string }>()
const themeKey = useThemeKey()

const option = computed(() => {
  void themeKey.value
  return roomHistoryOption({ ...props, theme: chartTheme() })
})
const unitColor = computed(() => UNIT_COLORS[props.unitKey] ?? 'var(--color-accent)')
</script>

<template>
  <figure class="min-w-0">
    <EChart :option="option" height="240px" />
    <figcaption>
      <ul class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted">
        <li v-if="hasSensor" class="flex items-center gap-1">
          <span class="h-0.5 w-4 rounded" :style="{ background: unitColor }" aria-hidden="true" />Room temperature
        </li>
        <li class="flex items-center gap-1">
          <span class="w-4 border-t-2 border-dashed border-bad" aria-hidden="true" />Heat setpoint
        </li>
        <li class="flex items-center gap-1">
          <span class="w-4 border-t-2 border-dashed border-accent" aria-hidden="true" />Cool setpoint
        </li>
        <li class="flex items-center gap-1">
          <span class="h-3 w-3 rounded-sm bg-st-occupied/25" aria-hidden="true" />Occupied
        </li>
        <li class="flex items-center gap-1">
          <span class="h-3 w-3 rounded-sm bg-st-asleep/25" aria-hidden="true" />Asleep
        </li>
      </ul>
    </figcaption>
  </figure>
</template>
