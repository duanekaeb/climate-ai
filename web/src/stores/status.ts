// The house status (Live tab and the header), refreshed on websocket events and every 60 s.
import { defineStore } from 'pinia'
import { api } from '@/api/client'
import { onEvent } from '@/api/ws'
import type { HouseStatus } from '@/api/types'

export const useStatus = defineStore('status', {
  state: () => ({ data: null as HouseStatus | null, error: '' as string, loading: false, started: false }),
  actions: {
    async load() {
      if (this.loading) return
      this.loading = true
      try {
        this.data = await api.get<HouseStatus>('/status')
        this.error = ''
      } catch (e) {
        this.error = e instanceof Error ? e.message : String(e)
      } finally {
        this.loading = false
      }
    },
    start() {
      if (this.started) return
      this.started = true
      this.load()
      onEvent((e) => {
        if (e.type === 'status' || e.type === 'action' || e.type === 'alert' || e.type === 'homekit') this.load()
      })
      window.setInterval(() => document.visibilityState === 'visible' && this.load(), 60_000)
    },
  },
})
