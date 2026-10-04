<script setup lang="ts">
// Our nine temperature points and the ecobee sensor id / HomeKit accessory id each one reads.
import { computed, reactive, ref, watch } from 'vue'
import type { EcobeeThermostatOut, HomekitDeviceOut, RoomOut, SensorOut, UnitOut } from '@/api/types'
import Card from '@/components/Card.vue'
import { sortUnitKeys } from '@/stores/runtime'
import { useSetup } from '@/stores/setup'
import { errorText } from './useAction'

const props = defineProps<{
  sensors: SensorOut[]
  rooms: RoomOut[]
  units: UnitOut[]
  thermostats: EcobeeThermostatOut[]
  devices: HomekitDeviceOut[]
}>()
const setup = useSetup()

interface Draft {
  ecobee: string
  aid: string
}
const drafts = reactive<Record<string, Draft>>({})
const busyKey = ref<string | null>(null)
const errors = ref<Record<string, string>>({})
const saved = ref<Record<string, boolean>>({})

const original = (s: SensorOut): Draft => ({ ecobee: s.ecobee_sensor_id ?? '', aid: s.homekit_aid === null ? '' : String(s.homekit_aid) })
const isDirty = (s: SensorOut) => {
  const d = drafts[s.key]
  const o = original(s)
  return !!d && (d.ecobee.trim() !== o.ecobee || d.aid.trim() !== o.aid)
}

// New server data replaces a row's draft unless the owner has edited that row (judged
// against the previous server value, not the new one).
watch(
  () => props.sensors,
  (list, oldList) => {
    for (const s of list) {
      const d = drafts[s.key]
      const prev = oldList?.find((x) => x.key === s.key)
      const o = prev ? original(prev) : null
      if (!d || !o || (d.ecobee.trim() === o.ecobee && d.aid.trim() === o.aid)) drafts[s.key] = original(s)
    }
  },
  { immediate: true },
)

const groups = computed(() =>
  sortUnitKeys(props.sensors.map((s) => s.unit_key)).map((k) => ({
    key: k,
    name: props.units.find((u) => u.key === k)?.name ?? k,
    sensors: props.sensors.filter((s) => s.unit_key === k),
  })),
)
const roomName = (key: string) => props.rooms.find((r) => r.key === key)?.name ?? key

const str = (v: unknown) => (typeof v === 'string' ? v : typeof v === 'number' ? String(v) : '')

/** ecobee sensor ids seen on the account (thermostat sensor dicts carry 'id' and 'name'). */
const ecobeeOptions = computed(() =>
  props.thermostats.flatMap((t) =>
    t.sensors
      .map((s) => ({ id: str(s.id), label: `${str(s.name) || 'Sensor'} · ${t.name}` }))
      .filter((o) => o.id),
  ),
)
/** HomeKit accessory ids (aid) from paired devices' accessory lists. */
const aidOptions = computed(() =>
  props.devices.flatMap((d) =>
    (d.accessories ?? [])
      .map((a) => ({ aid: typeof a.aid === 'number' ? a.aid : NaN, label: `${str(a.name) || 'Accessory'} · ${d.alias ?? d.name}` }))
      .filter((o) => Number.isInteger(o.aid)),
  ),
)

async function save(s: SensorOut) {
  const d = drafts[s.key]
  const aidText = d.aid.trim()
  const aid = aidText === '' ? null : Number(aidText)
  if (aid !== null && (!Number.isInteger(aid) || aid < 1)) {
    errors.value = { ...errors.value, [s.key]: 'The HomeKit accessory id is a whole number (1 or more).' }
    return
  }
  busyKey.value = s.key
  errors.value = { ...errors.value, [s.key]: '' }
  saved.value = { ...saved.value, [s.key]: false }
  try {
    await setup.mapSensor({ sensor_key: s.key, ecobee_sensor_id: d.ecobee.trim() || null, homekit_aid: aid })
    const fresh = setup.data?.sensors.find((x) => x.key === s.key)
    if (fresh) drafts[s.key] = original(fresh)
    saved.value = { ...saved.value, [s.key]: true }
  } catch (e) {
    errors.value = { ...errors.value, [s.key]: errorText(e) }
  } finally {
    busyKey.value = null
  }
}
</script>

<template>
  <Card title="Sensors" subtitle="Each temperature point and the ecobee sensor / HomeKit accessory it reads. Most map themselves by name.">
    <datalist id="setup-ecobee-sensors"><option v-for="o in ecobeeOptions" :key="o.id" :value="o.id">{{ o.label }}</option></datalist>
    <datalist id="setup-homekit-aids"><option v-for="o in aidOptions" :key="`${o.label}-${o.aid}`" :value="o.aid">{{ o.label }}</option></datalist>

    <p v-if="!sensors.length" class="text-sm text-muted">No sensors defined.</p>
    <div v-else class="space-y-4">
      <section v-for="g in groups" :key="g.key" :aria-labelledby="`sensors-${g.key}`">
        <h3 :id="`sensors-${g.key}`" class="mb-2 text-xs font-semibold text-muted">{{ g.name }}</h3>
        <ul class="space-y-2">
          <li v-for="s in g.sensors" :key="s.key" class="rounded-xl border border-line p-3">
            <div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
              <p class="font-medium">{{ s.name }}</p>
              <p class="text-xs text-muted">
                {{ roomName(s.room_key) }} · {{ s.kind === 'thermostat' ? 'thermostat' : 'SmartSensor' }}<template v-if="s.has_occupancy"> · occupancy</template><template v-if="s.has_humidity"> · humidity</template>
              </p>
            </div>
            <form class="mt-2 grid gap-2 sm:grid-cols-[minmax(0,1fr)_9rem_auto] sm:items-end" @submit.prevent="save(s)">
              <label class="block min-w-0 space-y-1">
                <span class="text-xs text-muted">ecobee sensor id</span>
                <input v-model="drafts[s.key].ecobee" class="input font-mono !text-xs" list="setup-ecobee-sensors" autocomplete="off"
                       :aria-label="`ecobee sensor id for ${s.name}`" placeholder="not mapped" />
              </label>
              <label class="block space-y-1">
                <span class="text-xs text-muted">HomeKit accessory id</span>
                <input v-model="drafts[s.key].aid" class="input num" inputmode="numeric" list="setup-homekit-aids" autocomplete="off"
                       :aria-label="`HomeKit accessory id for ${s.name}`" placeholder="not mapped" />
              </label>
              <button type="submit" class="btn" :disabled="busyKey === s.key || !isDirty(s)">
                {{ busyKey === s.key ? 'Saving…' : 'Save' }}
              </button>
            </form>
            <p v-if="errors[s.key]" role="alert" class="mt-1 text-xs text-bad">{{ errors[s.key] }}</p>
            <p v-else-if="saved[s.key] && !isDirty(s)" role="status" class="mt-1 text-xs text-good">Saved.</p>
          </li>
        </ul>
      </section>
    </div>
  </Card>
</template>
