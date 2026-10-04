<script setup lang="ts">
// Totals per unit over the selected range, plus the house total.
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { DailyRuntime } from '@/api/types'
import { UNIT_COLORS, UNIT_NAMES, minutes } from '@/lib/format'
import { type FailingBaselines, baselineFailShort, failingInUse, summarizeDays, unitTotals } from '@/stores/runtime'

const props = defineProps<{ rows: DailyRuntime[]; days: number; failing: FailingBaselines }>()

const totals = computed(() => unitTotals(props.rows, props.failing))
// The house expectation is summed per day, and only on days where every unit that ran has an
// expectation from a passing baseline (the same rule as the chart and the summary above).
const houseDays = computed(() => summarizeDays(props.rows, props.failing))
const failingNotes = computed(() => failingInUse(props.rows, props.failing).map(baselineFailShort))
const house = computed(() => {
  const t = totals.value
  const covered = houseDays.value.filter((d) => d.expected !== null)
  return {
    active: t.reduce((s, u) => s + u.active, 0),
    cool: t.reduce((s, u) => s + u.cool, 0),
    heat: t.reduce((s, u) => s + u.heat, 0),
    aux: t.reduce((s, u) => s + u.aux, 0),
    fan: t.reduce((s, u) => s + u.fan, 0),
    maxed: t.reduce((s, u) => s + u.maxed, 0),
    expected: covered.length ? covered.reduce((s, d) => s + (d.expected ?? 0), 0) : null,
    partial: covered.length > 0 && covered.length < houseDays.value.length,
  }
})
const partial = computed(
  () => house.value.partial || totals.value.some((u) => u.expected !== null && u.daysWithExpected < u.days),
)
</script>

<template>
  <div>
    <table class="w-full text-sm">
      <caption class="sr-only">Runtime totals per unit for the last {{ days }} days</caption>
      <thead>
        <tr class="border-b border-line text-left text-xs text-muted">
          <th scope="col" class="py-2 pr-2 font-medium">Unit</th>
          <th scope="col" class="px-1 py-2 text-right font-medium">Runtime</th>
          <th scope="col" class="px-1 py-2 text-right font-medium">Expected</th>
          <th scope="col" class="px-1 py-2 text-right font-medium">Maxed out</th>
          <th scope="col" class="hidden px-1 py-2 text-right font-medium sm:table-cell">Cooling</th>
          <th scope="col" class="hidden px-1 py-2 text-right font-medium sm:table-cell">Heating</th>
          <th scope="col" class="hidden px-1 py-2 text-right font-medium md:table-cell">Aux heat</th>
          <th scope="col" class="hidden py-2 pl-1 text-right font-medium md:table-cell">Fan</th>
        </tr>
      </thead>
      <tbody class="num whitespace-nowrap">
        <tr v-for="u in totals" :key="u.unit_key" class="border-b border-line/60">
          <th scope="row" class="py-2 pr-2 text-left font-medium">
            <span class="flex items-center gap-1.5">
              <span class="h-2.5 w-2.5 shrink-0 rounded-sm" :style="{ background: UNIT_COLORS[u.unit_key] ?? 'var(--color-accent)' }" aria-hidden="true" />
              <span class="min-w-0">{{ UNIT_NAMES[u.unit_key] ?? u.unit_key }}</span>
            </span>
          </th>
          <td class="px-1 py-2 text-right">{{ minutes(u.active) }}</td>
          <td class="px-1 py-2 text-right">
            {{ minutes(u.expected) }}<span v-if="u.expected !== null && u.daysWithExpected < u.days" class="text-muted">*</span><span
              v-if="u.daysFailing" class="text-warn" title="Baseline fails its checks">†</span>
          </td>
          <td class="px-1 py-2 text-right" :class="u.unit_key === 'up' && u.maxed > 0 ? 'font-semibold text-bad' : ''">{{ minutes(u.maxed) }}</td>
          <td class="hidden px-1 py-2 text-right sm:table-cell">{{ minutes(u.cool) }}</td>
          <td class="hidden px-1 py-2 text-right sm:table-cell">{{ minutes(u.heat) }}</td>
          <td class="hidden px-1 py-2 text-right md:table-cell">{{ minutes(u.aux) }}</td>
          <td class="hidden py-2 pl-1 text-right md:table-cell">{{ minutes(u.fan) }}</td>
        </tr>
      </tbody>
      <tfoot class="num font-semibold whitespace-nowrap">
        <tr>
          <th scope="row" class="py-2 pr-2 text-left">House</th>
          <td class="px-1 py-2 text-right">{{ minutes(house.active) }}</td>
          <td class="px-1 py-2 text-right">
            {{ minutes(house.expected) }}<span v-if="house.partial" class="font-normal text-muted">*</span><span
              v-if="failingNotes.length" class="font-normal text-warn" title="Leaves out baselines that fail their checks">†</span>
          </td>
          <td class="px-1 py-2 text-right">{{ minutes(house.maxed) }}</td>
          <td class="hidden px-1 py-2 text-right sm:table-cell">{{ minutes(house.cool) }}</td>
          <td class="hidden px-1 py-2 text-right sm:table-cell">{{ minutes(house.heat) }}</td>
          <td class="hidden px-1 py-2 text-right md:table-cell">{{ minutes(house.aux) }}</td>
          <td class="hidden py-2 pl-1 text-right md:table-cell">{{ minutes(house.fan) }}</td>
        </tr>
      </tfoot>
    </table>
    <p class="mt-2 text-xs text-muted">
      Runtime counts cooling on cooling days and heating on heating days. Expected is the weather-normalized baseline.
      <template v-if="partial">* The baseline covers only some of these days. </template>
      <span v-if="failingNotes.length" class="text-warn">
        † Leaves out days whose baseline fails its checks ({{ failingNotes.length === 1 ? failingNotes[0] : 'listed under Daily runtime' }}).
      </span>
      Whether a change saved anything, with a 90% interval, is on
      <RouterLink to="/results" class="text-accent underline">Did it work?</RouterLink>
    </p>
  </div>
</template>
