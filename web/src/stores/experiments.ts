// Switchback experiments: list, detail (schedule + analysis at checkpoints), proposals,
// owner decisions and the power calculator.
import { defineStore } from 'pinia'
import { api } from '@/api/client'
import type {
  ExperimentDecisionBody,
  ExperimentDetail,
  ExperimentOut,
  PowerOut,
  ProposeExperimentBody,
} from '@/api/types'
import { loadInto, resource } from '@/components/analysis/resource'

export const useExperiments = defineStore('experiments', () => {
  const list = resource<ExperimentOut[]>()
  const detail = resource<ExperimentDetail>()
  const power = resource<PowerOut>()

  function loadList() {
    return loadInto(list, 'all', () => api.get<ExperimentOut[]>('/experiments'))
  }

  function loadDetail(id: number) {
    return loadInto(detail, String(id), () => api.get<ExperimentDetail>(`/experiments/${id}`))
  }

  function loadPower(effectPct: number, alpha: number, powerTarget: number) {
    return loadInto(power, `${effectPct}|${alpha}|${powerTarget}`, () =>
      api.get<PowerOut>('/experiments/power', { effect_pct: effectPct, alpha, power: powerTarget }),
    )
  }

  function upsert(e: ExperimentOut) {
    const rows = list.data ? [...list.data] : []
    const i = rows.findIndex((r) => r.id === e.id)
    if (i >= 0) rows[i] = e
    else rows.unshift(e)
    list.data = rows
    if (detail.data && detail.data.experiment.id === e.id) detail.data = { ...detail.data, experiment: e }
  }

  async function propose(body: ProposeExperimentBody): Promise<ExperimentOut> {
    const e = await api.post<ExperimentOut>('/experiments', body)
    upsert(e)
    return e
  }

  async function decide(id: number, body: ExperimentDecisionBody): Promise<ExperimentOut> {
    const e = await api.post<ExperimentOut>(`/experiments/${id}/decision`, body)
    upsert(e)
    await loadDetail(id)
    return e
  }

  return { list, detail, power, loadList, loadDetail, loadPower, propose, decide }
})
