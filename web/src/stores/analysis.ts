// "Did it work?" and "Floor coupling": weather-normalized savings, the weekly waterfall,
// baselines, floor coupling and the natural-experiment history study.
import { defineStore } from 'pinia'
import { api } from '@/api/client'
import type { BaselineOut, Coupling, NaturalExperiments, Savings, Waterfall } from '@/api/types'
import { loadInto, resource } from '@/components/analysis/resource'

export const useAnalysis = defineStore('analysis', () => {
  const savings = resource<Savings>()
  const waterfall = resource<Waterfall>()
  const baselines = resource<BaselineOut[]>()
  const coupling = resource<Coupling>()
  const natural = resource<NaturalExperiments>()

  function loadSavings(start: string, end: string) {
    return loadInto(savings, `${start}..${end}`, () => api.get<Savings>('/analytics/savings', { start, end }))
  }

  /** week_start omitted = the backend's default (last full Monday week). */
  function loadWaterfall(weekStart?: string) {
    return loadInto(waterfall, weekStart ?? 'default', () =>
      api.get<Waterfall>('/analytics/waterfall', { week_start: weekStart }),
    )
  }

  function loadBaselines() {
    return loadInto(baselines, 'all', () => api.get<BaselineOut[]>('/analytics/baselines'))
  }

  function loadCoupling(days: number) {
    return loadInto(coupling, String(days), () => api.get<Coupling>('/analytics/coupling', { days }))
  }

  function loadNatural(days: number) {
    return loadInto(natural, String(days), () =>
      api.get<NaturalExperiments>('/analytics/natural-experiments', { days }),
    )
  }

  return { savings, waterfall, baselines, coupling, natural, loadSavings, loadWaterfall, loadBaselines, loadCoupling, loadNatural }
})
