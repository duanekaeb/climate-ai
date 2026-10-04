<script setup lang="ts">
// Setup (owner onboarding): location, data source, ecobee sign-in, thermostat and sensor
// mapping (with each thermostat's utility enrollment), HomeKit pairing, and "Hand back to
// ecobee". Polls /setup every 2 s while a HomeKit pairing is in progress.
import { onBeforeUnmount, onMounted, watch } from 'vue'
import { onEvent } from '@/api/ws'
import AsyncState from '@/components/AsyncState.vue'
import Icon from '@/components/Icon.vue'
import SetupEcobee from '@/components/setup/SetupEcobee.vue'
import SetupHandback from '@/components/setup/SetupHandback.vue'
import SetupHomekit from '@/components/setup/SetupHomekit.vue'
import SetupLocation from '@/components/setup/SetupLocation.vue'
import SetupSensors from '@/components/setup/SetupSensors.vue'
import SetupSource from '@/components/setup/SetupSource.vue'
import SetupThermostats from '@/components/setup/SetupThermostats.vue'
import { useSetup } from '@/stores/setup'

const setup = useSetup()

const SECTIONS = [
  { id: 'setup-location', label: 'Location' },
  { id: 'setup-source', label: 'Data source' },
  { id: 'setup-ecobee', label: 'ecobee' },
  { id: 'setup-thermostats', label: 'Thermostats' },
  { id: 'setup-sensors', label: 'Sensors' },
  { id: 'setup-homekit', label: 'HomeKit' },
  { id: 'setup-handback', label: 'Hand back' },
]

function jump(id: string) {
  const el = document.getElementById(id)
  if (!el) return
  el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  el.focus({ preventScroll: true })
}

watch(() => setup.pairingActive, (active) => active && setup.pollWhilePairing(), { immediate: true })

let off: (() => void) | undefined
onMounted(() => {
  setup.load()
  off = onEvent((e) => {
    if (e.type === 'homekit') setup.load({ quiet: true })
  })
})
onBeforeUnmount(() => {
  off?.()
  setup.stopPolling()
})
</script>

<template>
  <div>
    <AsyncState :loading="setup.loading && !setup.data" :error="setup.data ? '' : setup.error">
      <div v-if="setup.data" class="space-y-4">
        <div v-if="!setup.data.secrets_ok" role="alert" class="flex items-start gap-2 rounded-xl border border-bad/50 bg-bad/10 p-3 text-sm text-bad">
          <Icon name="alert" :size="18" class="mt-0.5 shrink-0" />
          <div>
            <p class="font-semibold">Set CLIMATE_SECRET_KEY on the server before going further.</p>
            <p class="mt-0.5">
              Without it the ecobee sign-in token and HomeKit pairing keys can't be stored encrypted, so signing in and
              pairing are disabled. Set the key in the server's environment and restart the app.
            </p>
          </div>
        </div>
        <p v-if="setup.error" role="status" class="text-xs text-warn">Couldn't refresh: {{ setup.error }}</p>

        <nav aria-label="Setup sections" class="flex flex-wrap gap-1.5">
          <button v-for="s in SECTIONS" :key="s.id" type="button" class="chip border border-line bg-surface !px-2.5 !py-1 text-muted hover:bg-surface-2"
                  @click="jump(s.id)">
            {{ s.label }}
          </button>
        </nav>

        <div id="setup-location" tabindex="-1" class="scroll-mt-20 outline-none">
          <SetupLocation :location="setup.data.location" />
        </div>
        <div id="setup-source" tabindex="-1" class="scroll-mt-20 outline-none">
          <SetupSource :source="setup.data.source" :ecobee-signed-in="setup.data.ecobee.signed_in" />
        </div>
        <div id="setup-ecobee" tabindex="-1" class="scroll-mt-20 outline-none">
          <SetupEcobee :ecobee="setup.data.ecobee" :secrets-ok="setup.data.secrets_ok" />
        </div>
        <div id="setup-thermostats" tabindex="-1" class="scroll-mt-20 outline-none">
          <SetupThermostats :thermostats="setup.data.ecobee.thermostats" :units="setup.data.units" :signed-in="setup.data.ecobee.signed_in" />
        </div>
        <div id="setup-sensors" tabindex="-1" class="scroll-mt-20 outline-none">
          <SetupSensors :sensors="setup.data.sensors" :rooms="setup.data.rooms" :units="setup.data.units"
                        :thermostats="setup.data.ecobee.thermostats" :devices="setup.data.homekit.devices" />
        </div>
        <div id="setup-homekit" tabindex="-1" class="scroll-mt-20 outline-none">
          <SetupHomekit :homekit="setup.data.homekit" :units="setup.data.units" :rooms="setup.data.rooms" :secrets-ok="setup.data.secrets_ok" />
        </div>
        <div id="setup-handback" tabindex="-1" class="scroll-mt-20 outline-none">
          <SetupHandback :units="setup.data.units" :sensors="setup.data.sensors" />
        </div>
      </div>
    </AsyncState>
    <div v-if="!setup.data && setup.error" class="mt-3 text-center">
      <button type="button" class="btn" :disabled="setup.loading" @click="setup.load()"><Icon name="refresh" :size="16" />Try again</button>
    </div>
  </div>
</template>
