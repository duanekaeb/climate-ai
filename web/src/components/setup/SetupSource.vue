<script setup lang="ts">
// Where readings come from: the built-in simulator or the real thermostats via ecobee's cloud,
// plus the optional local HomeKit link.
import { computed, reactive, watch } from 'vue'
import type { SourceSettings } from '@/api/types'
import Card from '@/components/Card.vue'
import { localDate, localTime } from '@/lib/format'
import { useSetup } from '@/stores/setup'
import { useStatus } from '@/stores/status'
import { useAction } from './useAction'

const props = defineProps<{ source: SourceSettings; ecobeeSignedIn: boolean }>()
const setup = useSetup()
const status = useStatus()
// House times are shown in the house's time zone, not the device's.
const tz = computed(() => status.data?.tz)
const { busy, error, done, run } = useAction()

const form = reactive({ kind: props.source.kind, homekit: props.source.homekit_enabled })
const dirty = computed(() => form.kind !== props.source.kind || form.homekit !== props.source.homekit_enabled)
// Take server updates unless the owner has changed the form (judged against the old value).
watch(
  () => props.source,
  (s, old) => {
    if (form.kind !== old.kind || form.homekit !== old.homekit_enabled) return
    form.kind = s.kind
    form.homekit = s.homekit_enabled
  },
)

const paused = computed(() => {
  const until = props.source.cloud_circuit_open_until
  return until && new Date(until).getTime() > Date.now() ? until : null
})

const OPTIONS = [
  { kind: 'simulator' as const, title: 'Simulator', body: 'A simulated house. No credentials needed; good for trying things out.' },
  { kind: 'ecobee' as const, title: 'ecobee', body: "Your three thermostats through ecobee's cloud. Sign in below first." },
]

async function save() {
  await run(() => setup.setSource({ kind: form.kind, homekit_enabled: form.homekit }), 'Saved. The worker switches over within a minute.')
}
</script>

<template>
  <Card title="Data source">
    <form class="space-y-3" @submit.prevent="save">
      <fieldset>
        <legend class="sr-only">Where readings come from</legend>
        <div class="grid gap-2 sm:grid-cols-2">
          <label v-for="o in OPTIONS" :key="o.kind" class="flex cursor-pointer items-start gap-3 rounded-xl border p-3"
                 :class="form.kind === o.kind ? 'border-accent bg-accent/5' : 'border-line hover:bg-surface-2'">
            <input v-model="form.kind" type="radio" name="setup-source" :value="o.kind" class="mt-1 accent-accent" />
            <span>
              <span class="block font-medium">{{ o.title }}</span>
              <span class="block text-sm text-muted">{{ o.body }}</span>
            </span>
          </label>
        </div>
      </fieldset>

      <p v-if="form.kind === 'ecobee' && !ecobeeSignedIn" class="rounded-xl border border-warn/40 bg-warn/10 p-2.5 text-sm text-warn">
        Not signed in to ecobee yet. Until you are, the worker has nothing to read.
      </p>
      <p v-if="paused" class="rounded-xl border border-warn/40 bg-warn/10 p-2.5 text-sm text-warn">
        ecobee cloud calls are paused after repeated errors, until {{ localDate(paused, tz) }}, {{ localTime(paused, tz) }}.
      </p>

      <label class="flex cursor-pointer items-start gap-3">
        <input v-model="form.homekit" type="checkbox" class="mt-1 h-4 w-4 accent-accent" />
        <span>
          <span class="block text-sm font-medium">Use the local HomeKit link</span>
          <span class="block text-sm text-muted">Live motion and temperatures in seconds, and a backup path for holds. Pair the thermostats under HomeKit below.</span>
        </span>
      </label>

      <p v-if="error" role="alert" class="rounded-xl border border-bad/40 bg-bad/10 p-2.5 text-sm text-bad">{{ error }}</p>
      <p v-if="done && !dirty" role="status" class="text-sm text-good">{{ done }}</p>
      <button type="submit" class="btn btn-primary" :disabled="busy || !dirty">{{ busy ? 'Saving…' : 'Save' }}</button>
    </form>
  </Card>
</template>
