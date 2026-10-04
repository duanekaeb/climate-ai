// Model fits (baselines, RC house model, room offsets), refit jobs, backtests, simulations.
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '@/api/client'
import type { BacktestBody, BacktestOut, JobOut, ModelFitOut, SimulateBody, SimulateOut } from '@/api/types'
import { loadInto, resource } from '@/components/analysis/resource'
import { errorText } from '@/components/analysis/stats'

const JOB_POLL_MS = 2000
const JOB_MAX_POLLS = 600 // 20 minutes; a refit that takes longer is reported as still running

export const useModels = defineStore('models', () => {
  const fits = resource<ModelFitOut[]>()
  const job = ref<JobOut | null>(null)
  const jobError = ref('')
  let jobTimer: number | undefined
  let polls = 0

  function loadFits() {
    return loadInto(fits, 'latest', () => api.get<ModelFitOut[]>('/models'))
  }

  function stopPolling() {
    window.clearTimeout(jobTimer)
    jobTimer = undefined
  }

  async function pollJob(id: number) {
    try {
      job.value = await api.get<JobOut>(`/jobs/${id}`)
      jobError.value = ''
    } catch (e) {
      jobError.value = errorText(e)
    }
    const status = job.value?.status
    if (status === 'done' || status === 'failed') {
      stopPolling()
      if (status === 'done') await loadFits()
      return
    }
    if (++polls >= JOB_MAX_POLLS) {
      stopPolling()
      jobError.value = 'Still running after 20 minutes. The worker keeps going; check back later.'
      return
    }
    jobTimer = window.setTimeout(() => pollJob(id), JOB_POLL_MS)
  }

  async function refit(): Promise<void> {
    stopPolling()
    jobError.value = ''
    polls = 0
    const queued = await api.post<JobOut>('/models/refit')
    job.value = queued
    jobTimer = window.setTimeout(() => pollJob(queued.id), JOB_POLL_MS)
  }

  function backtest(body: BacktestBody): Promise<BacktestOut> {
    return api.post<BacktestOut>('/models/backtest', body)
  }

  function simulate(body: SimulateBody): Promise<SimulateOut> {
    return api.post<SimulateOut>('/models/simulate', body)
  }

  return { fits, job, jobError, loadFits, refit, stopPolling, backtest, simulate }
})
