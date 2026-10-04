// The house status (Live tab and the header), refreshed on websocket events and every 60 s.
import { defineStore } from 'pinia'
import { api } from '@/api/client'
import { closeSocket, onEvent } from '@/api/ws'
import type { HouseStatus } from '@/api/types'

let interval: number | undefined
let offEvents: (() => void) | undefined
// Bumped by stop(): a request still in flight when you sign out must not write back.
let generation = 0

export const useStatus = defineStore('status', {
  state: () => ({ data: null as HouseStatus | null, error: '' as string, loading: false, started: false }),
  actions: {
    async load() {
      if (this.loading) return
      const gen = generation
      this.loading = true
      try {
        const data = await api.get<HouseStatus>('/status')
        if (gen !== generation) return
        this.data = data
        this.error = ''
      } catch (e) {
        if (gen === generation) this.error = e instanceof Error ? e.message : String(e)
      } finally {
        if (gen === generation) this.loading = false
      }
    },
    start() {
      if (this.started) return
      this.started = true
      this.load()
      offEvents = onEvent((e) => {
        if (e.type === 'status' || e.type === 'action' || e.type === 'alert' || e.type === 'homekit') this.load()
      })
      interval = window.setInterval(() => document.visibilityState === 'visible' && this.load(), 60_000)
    },
    /** Stop polling and listening (sign-out). start() picks up again after signing back in. */
    stop() {
      generation++
      window.clearInterval(interval)
      interval = undefined
      offEvents?.()
      offEvents = undefined
      this.started = false
      this.loading = false
      this.data = null
      this.error = ''
    },
  },
})

/** Stop every background call to the API: the status poll and the live socket. Called on
 *  sign-out and when a 401 sends you to the sign-in page. */
export function stopLiveUpdates(): void {
  useStatus().stop()
  closeSocket()
}
