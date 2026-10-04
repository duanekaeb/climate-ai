<script setup lang="ts">
// Weekday toggles, Monday = 0 … Sunday = 6 (the backend's convention).
import { WEEKDAYS, WEEKDAYS_SHORT } from '@/components/analysis/stats'

defineProps<{ label: string; disabled?: boolean }>()
const model = defineModel<number[]>({ required: true })

function toggle(d: number) {
  const set = new Set(model.value)
  if (set.has(d)) set.delete(d)
  else set.add(d)
  model.value = [...set].sort((a, b) => a - b)
}
</script>

<template>
  <div role="group" :aria-label="label" class="flex gap-1">
    <button
      v-for="(d, i) in WEEKDAYS_SHORT"
      :key="i"
      type="button"
      :aria-pressed="model.includes(i)"
      :aria-label="WEEKDAYS[i]"
      :title="WEEKDAYS[i]"
      :disabled="disabled"
      class="h-8 w-8 rounded-lg border text-xs font-medium disabled:opacity-50"
      :class="model.includes(i) ? 'border-accent bg-accent text-white' : 'border-line bg-surface text-muted'"
      @click="toggle(i)"
    >
      {{ d }}
    </button>
  </div>
</template>
