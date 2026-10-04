<script setup lang="ts">
// Weather baselines per unit and mode, with the ASHRAE Guideline 14 checks that decide
// whether any savings number may be shown (CV(RMSE) <= 20%, |NMBE| <= 0.5%).
import { computed } from 'vue'
import type { BaselineOut } from '@/api/types'
import { UNIT_NAMES } from '@/lib/format'
import { dayRange, num } from './stats'

const props = defineProps<{ baselines: BaselineOut[] }>()

const CV_MAX = 0.2
const NMBE_MAX = 0.005
const order = Object.keys(UNIT_NAMES)
const rows = computed(() =>
  [...props.baselines].sort(
    (a, b) => order.indexOf(a.unit_key) - order.indexOf(b.unit_key) || a.mode.localeCompare(b.mode),
  ),
)
const cvBad = (b: BaselineOut) => b.cvrmse > CV_MAX
const nmbeBad = (b: BaselineOut) => Math.abs(b.nmbe) > NMBE_MAX
const pctOf = (f: number, d = 1) => num(f * 100, d, '%')
</script>

<template>
  <div>
    <!-- phones: one block per baseline -->
    <ul class="space-y-2 md:hidden">
      <li v-for="b in rows" :key="`${b.unit_key}-${b.mode}`" class="rounded-xl border border-line p-3">
        <div class="flex items-center justify-between gap-2">
          <span class="inline-flex min-w-0 items-center gap-1.5 font-medium">
            <span class="h-2.5 w-2.5 shrink-0 rounded-full" :style="{ background: `var(--color-unit-${b.unit_key})` }" />
            <span class="truncate">{{ UNIT_NAMES[b.unit_key] ?? b.unit_key }} · {{ b.mode }}</span>
          </span>
          <span class="chip" :class="b.passes ? 'bg-good/15 text-good' : 'bg-bad/15 text-bad'">
            {{ b.passes ? 'passes' : 'fails' }}
          </span>
        </div>
        <dl class="num mt-2 grid grid-cols-3 gap-x-2 gap-y-1 text-sm">
          <div><dt class="text-[11px] text-muted">Balance pt</dt><dd>{{ num(b.balance_point_f, 0, '°F') }}</dd></div>
          <div><dt class="text-[11px] text-muted">Slope</dt><dd>{{ num(b.slope_min_per_dd, 1) }}<span class="text-xs text-muted"> min/DD</span></dd></div>
          <div><dt class="text-[11px] text-muted">R²</dt><dd>{{ num(b.r2, 2) }}</dd></div>
          <div><dt class="text-[11px] text-muted">CV(RMSE)</dt><dd :class="cvBad(b) && 'text-bad'">{{ pctOf(b.cvrmse) }}</dd></div>
          <div><dt class="text-[11px] text-muted">NMBE</dt><dd :class="nmbeBad(b) && 'text-bad'">{{ pctOf(b.nmbe, 2) }}</dd></div>
          <div><dt class="text-[11px] text-muted">Days</dt><dd>{{ b.n_days }}</dd></div>
        </dl>
        <p class="mt-1 text-xs text-muted">Trained {{ dayRange(b.train_start, b.train_end) }}</p>
      </li>
    </ul>

    <!-- wider screens: a table -->
    <div class="hidden overflow-x-auto md:block">
      <table class="num w-full text-sm">
        <thead>
          <tr class="text-left text-xs text-muted">
            <th class="py-1.5 pr-3 font-medium">Unit</th>
            <th class="py-1.5 pr-3 font-medium">Mode</th>
            <th class="py-1.5 pr-3 text-right font-medium">Balance point</th>
            <th class="py-1.5 pr-3 text-right font-medium">Slope</th>
            <th class="py-1.5 pr-3 text-right font-medium">R²</th>
            <th class="py-1.5 pr-3 text-right font-medium">CV(RMSE)</th>
            <th class="py-1.5 pr-3 text-right font-medium">NMBE</th>
            <th class="py-1.5 pr-3 font-medium">Check</th>
            <th class="py-1.5 font-medium">Training window</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="b in rows" :key="`${b.unit_key}-${b.mode}`" class="border-t border-line">
            <td class="py-2 pr-3">
              <span class="inline-flex items-center gap-1.5 whitespace-nowrap">
                <span class="h-2.5 w-2.5 rounded-full" :style="{ background: `var(--color-unit-${b.unit_key})` }" />
                {{ UNIT_NAMES[b.unit_key] ?? b.unit_key }}
              </span>
            </td>
            <td class="py-2 pr-3">{{ b.mode }}</td>
            <td class="py-2 pr-3 text-right">{{ num(b.balance_point_f, 0, '°F') }}</td>
            <td class="py-2 pr-3 text-right whitespace-nowrap">{{ num(b.slope_min_per_dd, 1) }} <span class="text-xs text-muted">min/DD</span></td>
            <td class="py-2 pr-3 text-right">{{ num(b.r2, 2) }}</td>
            <td class="py-2 pr-3 text-right" :class="cvBad(b) && 'text-bad'">{{ pctOf(b.cvrmse) }}</td>
            <td class="py-2 pr-3 text-right" :class="nmbeBad(b) && 'text-bad'">{{ pctOf(b.nmbe, 2) }}</td>
            <td class="py-2 pr-3">
              <span class="chip" :class="b.passes ? 'bg-good/15 text-good' : 'bg-bad/15 text-bad'">
                {{ b.passes ? 'passes' : 'fails' }}
              </span>
            </td>
            <td class="py-2 whitespace-nowrap text-muted">{{ dayRange(b.train_start, b.train_end) }} · {{ b.n_days }} d</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="mt-2 text-xs text-muted">
      Runtime per day = base + slope × degree-days past the balance point. A baseline passes when CV(RMSE) ≤ 20% and
      |NMBE| ≤ 0.5% (ASHRAE Guideline 14). Savings are only claimed while every baseline involved passes.
    </p>
  </div>
</template>
