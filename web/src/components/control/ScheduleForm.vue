<script setup lang="ts">
// Household rhythm used to pick each unit's priority room: school hours (School Room), office
// hours (Office) and the evening (Living Room).
import { computed } from 'vue'
import type { Schedule, SettingsOut } from '@/api/types'
import Card from '@/components/Card.vue'
import { useControl } from '@/stores/control'
import DaysPicker from './DaysPicker.vue'
import SaveBar from './SaveBar.vue'
import { useDraft, useSaver } from './draft'

const props = defineProps<{ settings: SettingsOut; isOwner: boolean }>()
const control = useControl()

const { draft, dirty, reset } = useDraft<Schedule>(() => ({ ...props.settings.control.schedule }))
const saver = useSaver()

const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/
const problems = computed(() => {
  const d = draft.value
  const out: string[] = []
  const times: [string, string][] = [
    [d.school_start, 'School start'],
    [d.school_end, 'School end'],
    [d.office_start, 'Office start'],
    [d.office_end, 'Office end'],
    [d.evening_start, 'Evening start'],
  ]
  for (const [v, label] of times) if (!HHMM.test(v)) out.push(`${label}: pick a time.`)
  if (out.length) return out
  if (d.school_start >= d.school_end) out.push('School must end after it starts.')
  if (d.office_start >= d.office_end) out.push('Office hours must end after they start.')
  return out
})

async function save() {
  const ok = await saver.run(() =>
    control.saveSettings({ control: { ...props.settings.control, schedule: draft.value }, occupancy: null, location: null }),
  )
  if (ok) reset()
}
</script>

<template>
  <Card title="Household schedule" subtitle="Decides which room each unit steers for">
    <fieldset :disabled="!isOwner" class="space-y-4">
      <legend class="sr-only">Household schedule</legend>
      <section>
        <h3 class="text-sm font-medium">School <span class="font-normal text-muted">· School Room has priority</span></h3>
        <div class="mt-1 flex flex-wrap items-end gap-2">
          <DaysPicker v-model="draft.school_days" label="School days" :disabled="!isOwner" />
          <label class="text-xs text-muted">From <input v-model="draft.school_start" type="time" class="input num mt-1 !w-28" /></label>
          <label class="text-xs text-muted">Until <input v-model="draft.school_end" type="time" class="input num mt-1 !w-28" /></label>
        </div>
      </section>
      <section>
        <h3 class="text-sm font-medium">Office <span class="font-normal text-muted">· Office has priority in the wing</span></h3>
        <div class="mt-1 flex flex-wrap items-end gap-2">
          <DaysPicker v-model="draft.office_days" label="Office days" :disabled="!isOwner" />
          <label class="text-xs text-muted">From <input v-model="draft.office_start" type="time" class="input num mt-1 !w-28" /></label>
          <label class="text-xs text-muted">Until <input v-model="draft.office_end" type="time" class="input num mt-1 !w-28" /></label>
        </div>
      </section>
      <section>
        <h3 class="text-sm font-medium">Evening <span class="font-normal text-muted">· Living Room has priority</span></h3>
        <label class="mt-1 block text-xs text-muted">
          Starts at <input v-model="draft.evening_start" type="time" class="input num mt-1 !w-28" />
        </label>
      </section>
    </fieldset>
    <ul v-if="problems.length" class="mt-2 space-y-0.5 text-xs text-bad">
      <li v-for="p in problems" :key="p">{{ p }}</li>
    </ul>
    <SaveBar
      :dirty="dirty"
      :valid="!problems.length"
      :busy="saver.busy.value"
      :error="saver.error.value"
      :saved="saver.saved.value"
      :disabled="!isOwner"
      @save="save"
      @reset="reset"
    />
  </Card>
</template>
