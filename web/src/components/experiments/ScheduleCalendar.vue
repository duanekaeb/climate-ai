<script setup lang="ts">
// The switchback schedule as a Monday-first calendar: each day colored by its arm, today
// outlined, excluded days greyed and struck through.
import { computed } from 'vue'
import type { ArmIn, ExperimentDayOut } from '@/api/types'
import { minutes } from '@/lib/format'
import { WEEKDAYS, WEEKDAYS_SHORT, addDays, dayLabelLong, mondayOf, signedMinutes } from '@/components/analysis/stats'
import { armColor } from './arms'

const props = defineProps<{ schedule: ExperimentDayOut[]; arms: ArmIn[]; today: string }>()

const armIndex = computed(() => new Map(props.arms.map((a, i) => [a.key, i])))
const armLabel = computed(() => new Map(props.arms.map((a) => [a.key, a.label])))
const byDay = computed(() => new Map(props.schedule.map((d) => [d.day, d])))

interface Cell {
  day: string
  entry: ExperimentDayOut | null
  dom: string
}

const weeks = computed<Cell[][]>(() => {
  if (!props.schedule.length) return []
  const days = props.schedule.map((d) => d.day).sort()
  const first = mondayOf(days[0])
  const last = days[days.length - 1]
  const out: Cell[][] = []
  for (let monday = first; monday <= last; monday = addDays(monday, 7)) {
    const row: Cell[] = []
    for (let i = 0; i < 7; i++) {
      const day = addDays(monday, i)
      const dom = day.slice(8, 10).replace(/^0/, '')
      row.push({ day, entry: byDay.value.get(day) ?? null, dom: dom === '1' ? `${day.slice(5, 7)}/1` : dom })
    }
    out.push(row)
  }
  return out
})

function style(c: Cell): Record<string, string> {
  if (!c.entry) return {}
  if (!c.entry.included) return { background: 'var(--color-surface-2)' }
  const color = armColor(armIndex.value.get(c.entry.arm) ?? 0)
  const strength = c.day <= props.today ? 38 : 16
  return { background: `color-mix(in srgb, ${color} ${strength}%, transparent)`, borderColor: color }
}

function title(c: Cell): string {
  if (!c.entry) return dayLabelLong(c.day)
  const e = c.entry
  const parts = [dayLabelLong(c.day), `arm ${armLabel.value.get(e.arm) ?? e.arm}`]
  if (!e.included) parts.push(e.note ? `excluded from the analysis (${e.note})` : 'excluded from the analysis')
  if (e.actual_min !== null) parts.push(`ran ${minutes(e.actual_min)}`)
  if (e.expected_min !== null) parts.push(`expected ${minutes(e.expected_min)}`)
  if (e.residual_min !== null) parts.push(`residual ${signedMinutes(e.residual_min)}`)
  return parts.join(' · ')
}
</script>

<template>
  <div>
    <div class="grid grid-cols-7 gap-1 text-center text-[11px] text-muted" aria-hidden="true">
      <span v-for="(d, i) in WEEKDAYS_SHORT" :key="i" :title="WEEKDAYS[i]">{{ d }}</span>
    </div>
    <div class="mt-1 space-y-1" role="list" aria-label="Experiment schedule">
      <div v-for="(w, wi) in weeks" :key="wi" class="grid grid-cols-7 gap-1">
        <div
          v-for="c in w"
          :key="c.day"
          role="listitem"
          :title="title(c)"
          :aria-label="title(c)"
          class="num relative flex h-9 items-center justify-center rounded-lg border text-xs"
          :class="[
            c.entry ? 'border-transparent' : 'border-transparent text-muted/40',
            c.entry && !c.entry.included ? 'text-muted line-through' : '',
            c.day === today ? 'ring-2 ring-ink ring-offset-1 ring-offset-surface font-semibold' : '',
          ]"
          :style="style(c)"
        >
          {{ c.dom }}
          <span
            v-if="c.entry && c.entry.included"
            class="absolute right-1 bottom-0.5 text-[9px] leading-none font-semibold uppercase"
            >{{ c.entry.arm.slice(0, 2) }}</span
          >
        </div>
      </div>
    </div>
    <div class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
      <span v-for="(a, i) in arms" :key="a.key" class="inline-flex items-center gap-1.5">
        <span class="h-3 w-3 rounded" :style="{ background: armColor(i) }" />{{ a.label }}
      </span>
      <span class="inline-flex items-center gap-1.5"><span class="h-3 w-3 rounded bg-surface-2" />excluded</span>
      <span class="inline-flex items-center gap-1.5"><span class="h-3 w-3 rounded ring-2 ring-ink" />today</span>
      <span>faded = still to come</span>
    </div>
  </div>
</template>
