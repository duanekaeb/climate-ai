<script setup lang="ts">
// The pre-planned looks. Early looks spend little of the false-win budget (alpha), so they
// need a much larger z to stop; together the looks keep the false-win rate at alpha.
import { computed } from 'vue'
import type { Checkpoint } from '@/api/types'
import { num } from '@/components/analysis/stats'

const props = defineProps<{ checkpoints: Checkpoint[]; daysObserved: number; alpha: number }>()

const rows = computed(() => {
  const sorted = [...props.checkpoints].sort((a, b) => a.day - b.day)
  const nextIdx = sorted.findIndex((c) => props.daysObserved < c.day)
  return sorted.map((c, i) => ({
    ...c,
    n: i + 1,
    state: props.daysObserved >= c.day ? 'reached' : i === nextIdx ? 'next' : 'later',
  }))
})
</script>

<template>
  <div>
    <div class="overflow-x-auto">
      <table class="num w-full text-sm">
        <thead>
          <tr class="text-left text-xs text-muted">
            <th class="py-1 pr-2 font-medium">Look</th>
            <th class="py-1 pr-2 text-right font-medium">Day</th>
            <th class="py-1 pr-2 text-right font-medium">Info</th>
            <th class="py-1 pr-2 text-right font-medium">Alpha spent</th>
            <th class="py-1 text-right font-medium">z needed</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in rows" :key="c.n" class="border-t border-line" :class="c.state === 'later' && 'text-muted'">
            <td class="py-1.5 pr-2 whitespace-nowrap">
              {{ c.n }} of {{ rows.length }}
              <span v-if="c.state === 'reached'" class="text-good" title="reached">✓</span>
              <span v-else-if="c.state === 'next'" class="text-[11px] text-accent">next</span>
            </td>
            <td class="py-1.5 pr-2 text-right">{{ c.day }}</td>
            <td class="py-1.5 pr-2 text-right">{{ num(c.info_fraction * 100, 0, '%') }}</td>
            <td class="py-1.5 pr-2 text-right">{{ num(c.alpha_spent, c.alpha_spent < 0.01 ? 4 : 3) }}</td>
            <td class="py-1.5 text-right">{{ num(c.z_crit, 2) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="mt-1 text-xs text-muted">
      Fixed before the test began. "Alpha spent" is how much of the {{ num(alpha * 100, 0, '%') }} false-win budget
      has been used up by that look (cumulative); "z needed" is how strong the difference must be to stop there.
    </p>
  </div>
</template>
