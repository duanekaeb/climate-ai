// The owner's sign-in on this device. Spec: docs/specs/users-and-tokens.md.
//
// There is one login (the owner password) and no user accounts, so whoever is signed in here
// is the owner; API tokens are for services and never use the web app. `state` mirrors
// GET /api/auth/state (asked with this tab's bearer), so `state.role === 'owner'` is what the
// owner-only controls check.
//
// bootstrap() runs before the first route guard: it trades the refresh cookie for an access
// token (how a reload, a new tab or the iOS app relaunch signs back in without a password),
// then reads the state. It runs once per page load; the guards await the same promise.
import { defineStore } from 'pinia'
import type { Router } from 'vue-router'
import { api, ApiError, clearAccessToken, refreshAccessToken, setAccessToken, setSignedOutHandler } from '@/api/client'
import type { AccessTokenOut, AuthState, LoginBody, SetupBody } from '@/api/types'
import { useSecurity } from '@/stores/security'
import { stopLiveUpdates } from '@/stores/status'

const SIGNED_OUT: AuthState = { authenticated: false, role: null, password_set: true, setup_allowed: false }

let booting: Promise<void> | null = null

export const useAuth = defineStore('auth', {
  state: () => ({
    state: null as AuthState | null,
    /** bootstrap() has finished at least once. */
    loaded: false,
    /** The server could not be reached when this page loaded (the sign-in page says so). */
    unreachable: false,
    /** Why this device is on the sign-in page (signed out elsewhere, sign-in expired). */
    notice: '',
  }),
  getters: {
    signedIn: (s): boolean => !!s.state?.authenticated,
    role: (s) => s.state?.role ?? null,
    isOwner: (s): boolean => s.state?.role === 'owner',
  },
  actions: {
    /** Refresh, then read the state. Concurrent callers share one run. */
    bootstrap(): Promise<void> {
      if (!booting) {
        booting = (async () => {
          const r = await refreshAccessToken()
          this.unreachable = r === 'unavailable'
          await this.loadState(r === 'ok')
          this.loaded = true
        })().finally(() => (booting = null))
      }
      return booting
    },

    /** GET /auth/state. `haveSession`: a refresh just succeeded, so if the state call itself
     *  fails the device is still signed in (a session can only belong to the owner). */
    async loadState(haveSession = false) {
      try {
        this.state = await api.get<AuthState>('/auth/state')
      } catch {
        this.unreachable = true
        this.state = haveSession
          ? { authenticated: true, role: 'owner', password_set: true, setup_allowed: false }
          : { ...SIGNED_OUT, password_set: this.state?.password_set ?? true }
      }
    },

    async login(password: string, deviceName: string) {
      const body: LoginBody = { password, device_name: deviceName }
      setAccessToken(await api.post<AccessTokenOut>('/auth/login', body))
      await this.signedInHere()
    },

    /** First run: choose the owner password (home network only, unless the server allows it). */
    async setup(password: string, deviceName: string) {
      const body: SetupBody = { password, device_name: deviceName }
      setAccessToken(await api.post<AccessTokenOut>('/auth/setup', body))
      await this.signedInHere()
    },

    async signedInHere() {
      this.notice = ''
      this.unreachable = false
      await this.loadState(true)
    },

    /** Sign this device out (revokes its session and clears the refresh cookie). Throws, and
     *  stays signed in, if the server could not be told. */
    async logout() {
      await api.post('/auth/logout')
      this.signedOutLocally('')
    },

    /** Sign out every device, this one included. */
    async logoutAll() {
      await api.post('/auth/logout-all')
      this.signedOutLocally('You signed out on every device.')
    },

    /** Forget the sign-in here and stop the background calls. */
    signedOutLocally(notice: string) {
      clearAccessToken()
      stopLiveUpdates()
      useSecurity().clear()
      this.state = { ...(this.state ?? SIGNED_OUT), authenticated: false, role: null }
      this.notice = notice
    },
  },
})

/** Wire the api client to the store and router: a call that proves the session is over (the
 *  device was signed out elsewhere, the password changed, the sign-in expired) lands on the
 *  sign-in page, which says why and comes back to where you were. */
export function installSessionHandlers(router: Router): void {
  setSignedOutHandler((err: ApiError) => {
    useAuth().signedOutLocally(
      err.code === 'SESSION_REVOKED' ? err.message || 'This device was signed out. Sign in again.' : 'Your sign-in has expired. Sign in again.',
    )
    const here = router.currentRoute.value
    if (!here.meta.public) router.push({ path: '/login', query: { next: here.fullPath } })
  })
}
