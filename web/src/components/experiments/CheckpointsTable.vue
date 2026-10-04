<script setup lang="ts">
// The pre-planned looks. alpha_spent is the INCREMENTAL two-sided alpha spent at each look;
// the looks sum to the experiment's alpha. Early looks spend little, so they need a much larger
// z to stop. A false win for the treatment (one side) is at most alpha / 2.
import { computed } from 'vue'
import type { Checkpoint } from '@/api/types'
import { num } from '@/components/analysis/stats'

const props = defineProps<{ checkpoints: Checkpoint[]; daysObserved: number; alpha: number }>()

const rows = computed(() => {
  const sorted = [...props.checkpoints].sort((a, b) => a.day - b.day)
  const nextIdx = sorted.findIndex((c) => props.daysObserved < c.day)
  let total = 0
  return sorted.map((c, i) => {
    total += c.alpha_spent
    return {
      ...c,
      n: i + 1,
      cumulative: total,
      state: props.daysObserved >= c.day ? 'reached' : i === nextIdx ? 'next' : 'later',
    }
  })
})
const alphaDigits = (v: number) => (v < 0.01 ? 4 : 3)
const pctText = (v: number) => `${Number((v * 100).toFixed(1))}%`
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
            <th class="py-1 pr-2 text-right font-medium">Alpha at this look</th>
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
            <td class="py-1.5 pr-2 text-right">
              {{ num(c.alpha_spent, alphaDigits(c.alpha_spent)) }}
              <span class="block text-[11px] text-muted">{{ num(c.cumulative, alphaDigits(c.cumulative)) }} so far</span>
            </td>
            <td class="py-1.5 text-right">{{ num(c.z_crit, 2) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="mt-1 text-xs text-muted">
      Fixed before the test began. "Alpha at this look" is the share of the experiment's two-sided alpha
      ({{ num(alpha, 2) }}) spent at that look alone; the looks add up to {{ num(alpha, 2) }}. A false win for the
      treatment (calling a change a saving when it isn't) is at most half of that, {{ pctText(alpha / 2) }}.
      "z needed" is how strong the difference must be to stop there.
    </p>
  </div>
</template>
