<script setup lang="ts">
// Where the house is: drives weather (Open-Meteo), sunrise and the household schedule's clock.
import { computed, reactive, watch } from 'vue'
import type { LocationSettings } from '@/api/types'
import Card from '@/components/Card.vue'
import { useSetup } from '@/stores/setup'
import { useAction } from './useAction'

const props = defineProps<{ location: LocationSettings }>()
const setup = useSetup()
const { busy, error, done, run } = useAction()

const form = reactive({ label: '', lat: '', lon: '', tz: '', zip: '' })
const geo = reactive({ busy: false, error: '' })

function fromProps(l: LocationSettings) {
  form.label = l.label ?? ''
  form.lat = l.lat === null ? '' : String(l.lat)
  form.lon = l.lon === null ? '' : String(l.lon)
  form.tz = l.tz
  form.zip = l.zip ?? ''
}
const dirty = computed(() => {
  const l = props.location
  return (
    form.label !== (l.label ?? '') ||
    form.lat !== (l.lat === null ? '' : String(l.lat)) ||
    form.lon !== (l.lon === null ? '' : String(l.lon)) ||
    form.tz !== l.tz ||
    form.zip !== (l.zip ?? '')
  )
})
fromProps(props.location)
// Later server updates replace the form only while the owner hasn't started editing it. The
// old value decides that: comparing against the new one would always look "edited".
watch(
  () => props.location,
  (l, old) => {
    const untouched =
      form.label === (old.label ?? '') &&
      form.lat === (old.lat === null ? '' : String(old.lat)) &&
      form.lon === (old.lon === null ? '' : String(old.lon)) &&
      form.tz === old.tz &&
      form.zip === (old.zip ?? '')
    if (untouched) fromProps(l)
  },
)

const timeZones = (() => {
  try {
    return Intl.supportedValuesOf('timeZone')
  } catch {
    return [] as string[]
  }
})()

function validTz(tz: string): boolean {
  try {
    new Intl.DateTimeFormat([], { timeZone: tz })
    return !!tz
  } catch {
    return false
  }
}

const problems = computed(() => {
  const out: string[] = []
  const lat = Number(form.lat)
  const lon = Number(form.lon)
  if (form.lat.trim() === '' || form.lon.trim() === '') {
    out.push('Enter the latitude and longitude (or use this device\'s location).')
  } else {
    if (!Number.isFinite(lat) || lat < -90 || lat > 90) out.push('Latitude must be between -90 and 90.')
    if (!Number.isFinite(lon) || lon < -180 || lon > 180) out.push('Longitude must be between -180 and 180.')
  }
  if (!validTz(form.tz.trim())) out.push('Pick a time zone such as America/Chicago.')
  return out
})

const mapUrl = computed(() => {
  const lat = Number(form.lat)
  const lon = Number(form.lon)
  if (problems.value.some((p) => p.startsWith('Lat') || p.startsWith('Long') || p.startsWith('Enter'))) return ''
  return `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=15/${lat}/${lon}`
})

function useDevice() {
  geo.error = ''
  if (!('geolocation' in navigator)) {
    geo.error = "This browser can't share its location. Type the coordinates instead."
    return
  }
  geo.busy = true
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      // Three decimals (about 100 m) is plenty for weather and keeps the address vague.
      form.lat = pos.coords.latitude.toFixed(3)
      form.lon = pos.coords.longitude.toFixed(3)
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone
      if (tz) form.tz = tz
      geo.busy = false
    },
    (err) => {
      geo.busy = false
      geo.error =
        err.code === err.PERMISSION_DENIED
          ? 'Location permission was denied (it also needs HTTPS). Type the coordinates instead.'
          : "Couldn't get this device's location. Type the coordinates instead."
    },
    { enableHighAccuracy: false, timeout: 15000, maximumAge: 600000 },
  )
}

async function save() {
  if (problems.value.length) return
  const ok = await run(
    () =>
      setup.saveLocation({
        label: form.label.trim() || null,
        lat: Number(form.lat),
        lon: Number(form.lon),
        tz: form.tz.trim(),
        zip: form.zip.trim() || null,
        confirmed: true,
      }),
    'Location saved and confirmed.',
  )
  if (ok && setup.data) fromProps(setup.data.location)
}
</script>

<template>
  <Card title="Location" subtitle="Used for weather, sunrise and the household schedule's clock.">
    <template #actions>
      <span class="chip" :class="location.confirmed ? 'bg-good/15 text-good' : 'bg-warn/15 text-warn'">
        {{ location.confirmed ? 'Confirmed' : 'Not confirmed' }}
      </span>
    </template>
    <form class="space-y-3" novalidate @submit.prevent="save">
      <div class="grid gap-3 sm:grid-cols-2">
        <label class="block space-y-1">
          <span class="text-sm font-medium">Latitude</span>
          <input v-model.trim="form.lat" class="input num" inputmode="decimal" autocomplete="off" placeholder="41.878" />
        </label>
        <label class="block space-y-1">
          <span class="text-sm font-medium">Longitude</span>
          <input v-model.trim="form.lon" class="input num" inputmode="decimal" autocomplete="off" placeholder="-87.630" />
        </label>
        <label class="block space-y-1">
          <span class="text-sm font-medium">Time zone</span>
          <input v-model.trim="form.tz" class="input" list="setup-tz-list" autocomplete="off" placeholder="America/Chicago" />
          <datalist id="setup-tz-list"><option v-for="z in timeZones" :key="z" :value="z" /></datalist>
        </label>
        <label class="block space-y-1">
          <span class="text-sm font-medium">ZIP code <span class="font-normal text-muted">(optional)</span></span>
          <input v-model.trim="form.zip" class="input num" inputmode="numeric" autocomplete="postal-code" />
        </label>
        <label class="block space-y-1 sm:col-span-2">
          <span class="text-sm font-medium">Name <span class="font-normal text-muted">(optional)</span></span>
          <input v-model="form.label" class="input" autocomplete="off" placeholder="Home" />
        </label>
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <button type="button" class="btn" :disabled="geo.busy" @click="useDevice">
          {{ geo.busy ? 'Finding you…' : "Use this device's location" }}
        </button>
        <a v-if="mapUrl" :href="mapUrl" target="_blank" rel="noopener" class="text-sm text-accent underline">Check on a map</a>
      </div>
      <p v-if="geo.error" role="alert" class="text-sm text-warn">{{ geo.error }}</p>

      <ul v-if="problems.length && (dirty || !location.confirmed)" class="list-disc space-y-0.5 pl-5 text-sm text-muted">
        <li v-for="p in problems" :key="p">{{ p }}</li>
      </ul>
      <p v-if="error" role="alert" class="rounded-xl border border-bad/40 bg-bad/10 p-2.5 text-sm text-bad">{{ error }}</p>
      <p v-if="done && !dirty" role="status" class="text-sm text-good">{{ done }}</p>

      <button type="submit" class="btn btn-primary" :disabled="busy || problems.length > 0 || (!dirty && location.confirmed)">
        {{ busy ? 'Saving…' : location.confirmed && !dirty ? 'Location confirmed' : 'Save and confirm' }}
      </button>
    </form>
  </Card>
</template>
