// Guardrails: controller settings (owner), the current plan, the change gates and the
// action log. Writes to a thermostat only ever happen in the worker; the owner's manual
// hold, "Resume schedule" and "Back to automatic" here are queued actions that the worker
// executes and reads back, and "Hand back to ecobee" is a queued job.
import { defineStore } from 'pinia'
import { api } from '@/api/client'
import type {
  ChangeOut,
  ControlActionOut,
  ControllerInfo,
  DecisionBody,
  HandbackInfo,
  JobOut,
  ManualHoldBody,
  ModeBody,
  PlanOut,
  ProposePolicyBody,
  SettingsOut,
  SettingsUpdate,
  UnitBody,
} from '@/api/types'
import { loadInto, resource } from '@/components/analysis/resource'

export const ACTIONS_LIMIT = 100

export const useControl = defineStore('control', () => {
  const settings = resource<SettingsOut>()
  const plan = resource<PlanOut>()
  const changes = resource<ChangeOut[]>()
  const actions = resource<ControlActionOut[]>()
  const handback = resource<HandbackInfo>()

  function loadSettings() {
    return loadInto(settings, 'current', () => api.get<SettingsOut>('/control/settings'))
  }

  /** Load settings once (other screens need the current policy and sign-off ranges). */
  function ensureSettings() {
    if (settings.data || settings.loading) return Promise.resolve(settings.data)
    return loadSettings()
  }

  function loadPlan() {
    return loadInto(plan, 'now', () => api.get<PlanOut>('/control/plan'))
  }

  function loadChanges() {
    return loadInto(changes, 'all', () => api.get<ChangeOut[]>('/changes'))
  }

  function loadActions(unitKey: string | null) {
    return loadInto(actions, unitKey ?? 'all', () =>
      api.get<ControlActionOut[]>('/control/actions', { limit: ACTIONS_LIMIT, unit_key: unitKey ?? undefined }),
    )
  }

  async function saveSettings(update: SettingsUpdate): Promise<SettingsOut> {
    const out = await api.put<SettingsOut>('/control/settings', update)
    settings.data = out
    return out
  }

  async function setMode(mode: ModeBody['mode']): Promise<ControllerInfo> {
    const body: ModeBody = { mode }
    const info = await api.post<ControllerInfo>('/control/mode', body)
    if (settings.data) settings.data = { ...settings.data, control: { ...settings.data.control, mode: info.mode } }
    void loadPlan()
    return info
  }

  function upsertAction(a: ControlActionOut) {
    if (actions.key !== null && actions.key !== 'all' && actions.key !== a.unit_key) return
    const rows = actions.data ? [...actions.data] : []
    const i = rows.findIndex((r) => r.id === a.id)
    if (i >= 0) rows[i] = a
    else rows.unshift(a)
    actions.data = rows.slice(0, ACTIONS_LIMIT)
  }

  async function hold(body: ManualHoldBody): Promise<ControlActionOut> {
    const a = await api.post<ControlActionOut>('/control/hold', body)
    upsertAction(a)
    return a
  }

  /** "Resume schedule": cancel the running hold; the ecobee schedule runs for
   *  `resume_backoff_hours` before the controller steers again. */
  async function resume(unitKey: string): Promise<ControlActionOut> {
    const body: UnitBody = { unit_key: unitKey }
    const a = await api.post<ControlActionOut>('/control/resume', body)
    upsertAction(a)
    return a
  }

  /** "Back to automatic": cancel the running hold (and any wait after a Resume); the
   *  controller steers again at once. */
  async function automatic(unitKey: string): Promise<ControlActionOut> {
    const body: UnitBody = { unit_key: unitKey }
    const a = await api.post<ControlActionOut>('/control/automatic', body)
    upsertAction(a)
    return a
  }

  /** What "Hand back to ecobee" restores, the latest hand-back job and its steps. */
  function loadHandback() {
    return loadInto(handback, 'current', () => api.get<HandbackInfo>('/control/handback'))
  }

  /** Queue the hand-back job (409 while one is queued or running). */
  async function startHandback(): Promise<JobOut> {
    const job = await api.post<JobOut>('/control/handback')
    if (handback.data) handback.data = { ...handback.data, last_job: job }
    return job
  }

  function upsertChange(c: ChangeOut) {
    const rows = changes.data ? [...changes.data] : []
    const i = rows.findIndex((r) => r.id === c.id)
    if (i >= 0) rows[i] = c
    else rows.unshift(c)
    changes.data = rows
  }

  async function decideChange(id: number, body: DecisionBody): Promise<ChangeOut> {
    const c = await api.post<ChangeOut>(`/changes/${id}/decision`, body)
    upsertChange(c)
    return c
  }

  async function proposeChange(body: ProposePolicyBody): Promise<ChangeOut> {
    const c = await api.post<ChangeOut>('/changes', body)
    upsertChange(c)
    return c
  }

  return {
    settings,
    plan,
    changes,
    actions,
    handback,
    loadSettings,
    ensureSettings,
    loadPlan,
    loadChanges,
    loadActions,
    saveSettings,
    setMode,
    hold,
    resume,
    automatic,
    loadHandback,
    startHandback,
    decideChange,
    proposeChange,
  }
})
