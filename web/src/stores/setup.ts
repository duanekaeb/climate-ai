// Owner onboarding (GET /setup and its POSTs). Every write returns the new SetupState.
// The ecobee password is passed straight through to the API and is never kept here.
import { defineStore } from 'pinia'
import { api } from '@/api/client'
import type {
  EcobeeLoginResult,
  HomekitDeviceOut,
  LocationSettings,
  SensorMapBody,
  SetupState,
  SourceBody,
} from '@/api/types'

/** HomeKit pairing states the homekit service is still working through. */
export const PAIRING_PENDING = new Set(['requested', 'awaiting_code', 'code_submitted', 'unpair_requested'])

export function isPairingPending(d: HomekitDeviceOut): boolean {
  return PAIRING_PENDING.has(d.pairing_state)
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

let pollTimer: number | undefined

export const useSetup = defineStore('setup', {
  state: () => ({
    data: null as SetupState | null,
    loading: false,
    error: '',
  }),
  getters: {
    pairingActive: (s): boolean => !!s.data?.homekit.devices.some(isPairingPending),
  },
  actions: {
    async load(opts: { quiet?: boolean } = {}) {
      if (!opts.quiet) this.loading = true
      try {
        this.data = await api.get<SetupState>('/setup')
        this.error = ''
      } catch (e) {
        this.error = message(e)
      } finally {
        this.loading = false
      }
    },
    async saveLocation(loc: LocationSettings) {
      this.data = await api.put<SetupState>('/setup/location', loc)
    },
    async setSource(body: SourceBody) {
      this.data = await api.post<SetupState>('/setup/source', body)
    },
    /** Returns the sign-in result; reloads the setup state afterwards. */
    async ecobeeLogin(email: string, password: string): Promise<EcobeeLoginResult> {
      const res = await api.post<EcobeeLoginResult>('/setup/ecobee/login', { email, password })
      await this.load({ quiet: true })
      return res
    },
    async ecobeeMfa(code: string): Promise<EcobeeLoginResult> {
      const res = await api.post<EcobeeLoginResult>('/setup/ecobee/mfa', { code })
      await this.load({ quiet: true })
      return res
    },
    async ecobeeSignout() {
      this.data = await api.post<SetupState>('/setup/ecobee/signout')
    },
    async mapThermostat(identifier: string, unitKey: string | null) {
      this.data = await api.post<SetupState>('/setup/ecobee/map', { identifier, unit_key: unitKey })
    },
    async mapSensor(body: SensorMapBody) {
      this.data = await api.post<SetupState>('/setup/sensors/map', body)
    },
    async homekitPair(deviceId: string, alias: string, unitKey: string) {
      this.data = await api.post<SetupState>('/setup/homekit/pair', { device_id: deviceId, alias, unit_key: unitKey })
      this.pollWhilePairing()
    },
    async homekitCode(deviceId: string, code: string) {
      this.data = await api.post<SetupState>('/setup/homekit/code', { device_id: deviceId, code })
      this.pollWhilePairing()
    },
    async homekitUnpair(deviceId: string) {
      this.data = await api.post<SetupState>('/setup/homekit/unpair', { device_id: deviceId })
      this.pollWhilePairing()
    },
    /** Poll /setup every 2 s while any device is mid-handshake; stops by itself. */
    pollWhilePairing() {
      if (pollTimer !== undefined || !this.pairingActive) return
      pollTimer = window.setInterval(async () => {
        if (document.visibilityState !== 'visible') return
        await this.load({ quiet: true })
        if (!this.pairingActive) this.stopPolling()
      }, 2000)
    },
    stopPolling() {
      window.clearInterval(pollTimer)
      pollTimer = undefined
    },
  },
})
