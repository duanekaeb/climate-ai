import { defineStore } from 'pinia'
import { api } from '@/api/client'
import type { AuthState } from '@/api/types'

export const useAuth = defineStore('auth', {
  state: () => ({ state: null as AuthState | null, loaded: false }),
  actions: {
    async refresh() {
      try {
        this.state = await api.get<AuthState>('/auth/state')
      } catch {
        this.state = { authenticated: false, role: null, password_set: true }
      }
      this.loaded = true
    },
    async login(password: string) {
      this.state = await api.post<AuthState>('/auth/login', { password })
    },
    async setup(password: string) {
      this.state = await api.post<AuthState>('/auth/setup', { password })
    },
    async logout() {
      this.state = await api.post<AuthState>('/auth/logout')
    },
  },
})
