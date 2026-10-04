// Security (More -> Security): the owner's password, signed-in devices, API tokens for
// services, and the recent audit trail. Owner only, like the endpoints behind it.
// Spec: docs/specs/users-and-tokens.md.
import { defineStore } from 'pinia'
import { api } from '@/api/client'
import type { ApiTokenCreateBody, ApiTokenCreated, ApiTokenOut, AuditEventOut, ChangePasswordBody, SessionOut } from '@/api/types'
import { loadInto, resource } from '@/components/analysis/resource'

export const AUDIT_LIMIT = 50

export const useSecurity = defineStore('security', () => {
  const sessions = resource<SessionOut[]>()
  const tokens = resource<ApiTokenOut[]>()
  const audit = resource<AuditEventOut[]>()

  function loadSessions() {
    return loadInto(sessions, 'all', () => api.get<SessionOut[]>('/auth/sessions'))
  }

  function loadTokens() {
    return loadInto(tokens, 'all', () => api.get<ApiTokenOut[]>('/tokens'))
  }

  /** `eventType` '' = every kind of event. */
  function loadAudit(eventType = '') {
    return loadInto(audit, eventType || 'all', () =>
      api.get<AuditEventOut[]>('/audit', { event_type: eventType || undefined, limit: AUDIT_LIMIT }),
    )
  }

  /** Reload the audit list (keeping the filter it was loaded with). */
  function reloadAudit() {
    const key = audit.key && audit.key !== 'all' ? audit.key : ''
    return loadAudit(key)
  }

  /** Needs the current password; the server signs out every other device. */
  async function changePassword(current: string, next: string) {
    const body: ChangePasswordBody = { current_password: current, new_password: next }
    await api.post('/auth/change-password', body)
    void loadSessions()
    void reloadAudit()
  }

  /** Sign one other device out. */
  async function revokeSession(id: number) {
    await api.del(`/auth/sessions/${id}`)
    void loadSessions()
    void reloadAudit()
  }

  /** Create a token. The response is the only time the full token exists outside the server;
   *  the caller shows it once and drops it. May ask for the password first (re-auth). */
  async function createToken(body: ApiTokenCreateBody): Promise<ApiTokenCreated> {
    const created = await api.post<ApiTokenCreated>('/tokens', body)
    const { token: _secret, ...listed } = created
    void _secret
    tokens.data = [listed, ...(tokens.data ?? []).filter((t) => t.id !== created.id)]
    void loadTokens()
    void reloadAudit()
    return created
  }

  async function revokeToken(id: number) {
    await api.del(`/tokens/${id}`)
    void loadTokens()
    void reloadAudit()
  }

  /** Forget everything loaded (sign-out). */
  function clear() {
    for (const r of [sessions, tokens, audit] as const) {
      r.seq++ // a response still in flight must not write back
      r.data = null
      r.error = ''
      r.loading = false
      r.key = null
    }
  }

  return { sessions, tokens, audit, loadSessions, loadTokens, loadAudit, reloadAudit, changePassword, revokeSession, createToken, revokeToken, clear }
})
