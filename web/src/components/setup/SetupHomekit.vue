<script setup lang="ts">
// HomeKit: the local link to the thermostats (live motion/temperature, backup holds).
import { computed } from 'vue'
import type { HomekitSetup, RoomOut, UnitOut } from '@/api/types'
import Card from '@/components/Card.vue'
import HomekitDevice from './HomekitDevice.vue'

const props = defineProps<{ homekit: HomekitSetup; units: UnitOut[]; rooms: RoomOut[]; secretsOk: boolean }>()

const devices = computed(() =>
  [...props.homekit.devices].sort((a, b) => Number(b.online) - Number(a.online) || a.name.localeCompare(b.name)),
)
function taken(id: string): string[] {
  return props.homekit.devices.filter((d) => d.device_id !== id && d.alias).map((d) => d.alias as string)
}
</script>

<template>
  <Card title="HomeKit">
    <template #actions>
      <span class="chip" :class="homekit.service_online ? 'bg-good/15 text-good' : 'bg-bad/15 text-bad'">
        Service {{ homekit.service_online ? 'online' : 'offline' }}
      </span>
    </template>
    <div class="space-y-3">
      <p v-if="!homekit.enabled" class="rounded-xl border border-line bg-surface-2 p-2.5 text-sm text-muted">
        The HomeKit link is turned off. Turn it on under Data source to use paired thermostats.
      </p>
      <p v-if="!homekit.service_online" class="rounded-xl border border-warn/40 bg-warn/10 p-2.5 text-sm text-warn">
        The HomeKit service isn't running or hasn't checked in. Discovery and pairing need it.
      </p>
      <ul class="list-disc space-y-1 pl-5 text-sm text-muted">
        <li>
          Before pairing, remove the thermostat from Apple Home, or choose <span class="font-medium text-ink">Disconnect from HomeKit</span>
          in the thermostat's settings. A thermostat pairs with one controller at a time.
        </li>
        <li>
          The HomeKit service needs a Linux host on the same network (LAN) as the thermostats, because discovery uses
          mDNS on the host network.
        </li>
        <li>After you start pairing, the thermostat shows an 8-digit code; type it here.</li>
      </ul>
      <p v-if="!secretsOk" class="text-sm text-bad">Pairing is disabled until the server's secret key is set.</p>

      <p v-if="!devices.length" class="text-sm text-muted">
        {{ homekit.service_online ? 'No HomeKit thermostats found on the network yet.' : 'Devices appear here once the HomeKit service is running.' }}
      </p>
      <ul v-else class="space-y-2">
        <HomekitDevice v-for="d in devices" :key="d.device_id" :device="d" :units="units" :rooms="rooms"
                       :taken-aliases="taken(d.device_id)" :secrets-ok="secretsOk" />
      </ul>
    </div>
  </Card>
</template>
