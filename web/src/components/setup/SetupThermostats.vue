<script setup lang="ts">
// Which ecobee thermostat is which of our three units.
import { computed, ref } from 'vue'
import type { EcobeeThermostatOut, UnitOut } from '@/api/types'
import Card from '@/components/Card.vue'
import { timeAgo } from '@/lib/format'
import { useSetup } from '@/stores/setup'
import { errorText } from './useAction'

const props = defineProps<{ thermostats: EcobeeThermostatOut[]; units: UnitOut[]; signedIn: boolean }>()
const setup = useSetup()

const busyId = ref<string | null>(null)
const errors = ref<Record<string, string>>({})

const unitName = (key: string | null) => props.units.find((u) => u.key === key)?.name ?? key ?? ''
const duplicates = computed(() => {
  const seen = new Map<string, number>()
  for (const t of props.thermostats) if (t.unit_key) seen.set(t.unit_key, (seen.get(t.unit_key) ?? 0) + 1)
  return new Set([...seen.entries()].filter(([, n]) => n > 1).map(([k]) => k))
})

async function map(t: EcobeeThermostatOut, value: string) {
  busyId.value = t.identifier
  errors.value = { ...errors.value, [t.identifier]: '' }
  try {
    await setup.mapThermostat(t.identifier, value || null)
  } catch (e) {
    errors.value = { ...errors.value, [t.identifier]: errorText(e) }
  } finally {
    busyId.value = null
  }
}
</script>

<template>
  <Card title="Thermostats" subtitle="Match each ecobee to the unit it controls.">
    <p v-if="!thermostats.length" class="text-sm text-muted">
      {{ signedIn ? 'No thermostats found on this ecobee account yet. They appear within a few minutes of signing in.' : 'Sign in to ecobee to list your thermostats.' }}
    </p>
    <ul v-else class="divide-y divide-line">
      <li v-for="t in thermostats" :key="t.identifier" class="flex flex-col gap-2 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-center">
        <div class="min-w-0 flex-1">
          <p class="font-medium">{{ t.name }}</p>
          <p class="text-xs break-all text-muted">
            {{ t.model_number ?? 'ecobee' }} · <span class="font-mono">{{ t.identifier }}</span> ·
            {{ t.sensors.length }} sensor{{ t.sensors.length === 1 ? '' : 's' }} · seen {{ timeAgo(t.last_seen_at) }}
          </p>
          <p v-if="t.unit_key && duplicates.has(t.unit_key)" class="mt-1 text-xs text-warn">
            Another thermostat is also mapped to {{ unitName(t.unit_key) }}.
          </p>
          <p v-if="errors[t.identifier]" role="alert" class="mt-1 text-xs text-bad">{{ errors[t.identifier] }}</p>
        </div>
        <label class="flex items-center gap-2 text-sm sm:shrink-0">
          <span class="text-muted">Unit</span>
          <select class="input !w-auto" :value="t.unit_key ?? ''" :disabled="busyId === t.identifier"
                  :aria-label="`Unit for ${t.name}`" @change="map(t, ($event.target as HTMLSelectElement).value)">
            <option value="">Not used</option>
            <option v-for="u in units" :key="u.key" :value="u.key">{{ u.name }}</option>
          </select>
        </label>
      </li>
    </ul>
  </Card>
</template>
