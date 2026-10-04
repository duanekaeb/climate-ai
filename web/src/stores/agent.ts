// Claude (the analyst): sign-in status and the agent_runs queue. Live updates come from the
// 'agent_run' websocket event; while a run is queued or running we also poll every 5 s, in
// case the socket is down (iOS suspends it in the background).
import { defineStore } from 'pinia'
import { computed } from 'vue'
import { api } from '@/api/client'
import { onEvent } from '@/api/ws'
import type { AgentInfo, AgentRunOut, AskBody, RunBody } from '@/api/types'
import { loadInto, resource } from '@/components/analysis/resource'

const POLL_MS = 5000
const RUNS_LIMIT = 20

export function isActive(r: AgentRunOut): boolean {
  return r.status === 'queued' || r.status === 'running'
}

export const useAgent = defineStore('agent', () => {
  const info = resource<AgentInfo>()
  const runs = resource<AgentRunOut[]>()
  let timer: number | undefined
  let unsubscribe: (() => void) | null = null
  let watchers = 0

  const anyActive = computed(() => (runs.data ?? []).some(isActive))

  function loadInfo() {
    return loadInto(info, 'status', () => api.get<AgentInfo>('/agent/status'))
  }

  function loadRuns() {
    return loadInto(runs, 'recent', () => api.get<AgentRunOut[]>('/agent/runs', { limit: RUNS_LIMIT }))
  }

  function upsert(run: AgentRunOut) {
    const rows = runs.data ? [...runs.data] : []
    const i = rows.findIndex((r) => r.id === run.id)
    if (i >= 0) rows[i] = run
    else rows.unshift(run)
    rows.sort((a, b) => b.id - a.id)
    runs.data = rows.slice(0, RUNS_LIMIT)
  }

  async function refreshRun(id: number) {
    try {
      upsert(await api.get<AgentRunOut>(`/agent/runs/${id}`))
    } catch {
      await loadRuns()
    }
  }

  async function ask(question: string): Promise<AgentRunOut> {
    const body: AskBody = { question }
    const run = await api.post<AgentRunOut>('/agent/ask', body)
    upsert(run)
    schedule()
    return run
  }

  async function runNow(kind: RunBody['kind']): Promise<AgentRunOut> {
    const body: RunBody = { kind }
    const run = await api.post<AgentRunOut>('/agent/run', body)
    upsert(run)
    schedule()
    void loadInfo()
    return run
  }

  async function tick() {
    timer = undefined
    if (document.visibilityState === 'visible') {
      const active = (runs.data ?? []).filter(isActive)
      if (active.length) {
        await Promise.all(active.map((r) => refreshRun(r.id)))
        if (!anyActive.value) void loadInfo()
      }
    }
    schedule()
  }

  /** Keep polling while something is queued or running and the view is open. */
  function schedule() {
    if (timer !== undefined || watchers === 0 || !anyActive.value) return
    timer = window.setTimeout(tick, POLL_MS)
  }

  /** Called by the view on mount; returns the matching stop function for unmount. */
  function follow(): () => void {
    watchers++
    if (!unsubscribe) {
      unsubscribe = onEvent((e) => {
        if (e.type !== 'agent_run') return
        if (e.id !== null) void refreshRun(e.id).then(schedule)
        else void loadRuns().then(schedule)
        void loadInfo()
      })
    }
    void Promise.all([loadInfo(), loadRuns()]).then(schedule)
    return () => {
      watchers = Math.max(0, watchers - 1)
      if (watchers === 0) {
        window.clearTimeout(timer)
        timer = undefined
        unsubscribe?.()
        unsubscribe = null
      }
    }
  }

  return { info, runs, anyActive, loadInfo, loadRuns, ask, runNow, follow }
})
