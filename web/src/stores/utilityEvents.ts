// Utility (demand-response) events for the Live card: the recent list and the owner's
// "Skip this event" with its undo. A skip is only requested here; the worker sends the
// opt-out once the event runs on that thermostat, reads it back and records the outcome.
import { defineStore } from 'pinia'
import { api } from '@/api/client'
import type { SkipEventBody, UtilityEventOut } from '@/api/types'
import { loadInto, resource } from '@/components/analysis/resource'

// Announced and running events always come back; this only bounds the finished ones.
const RECENT_DAYS = 1

export const useUtilityEvents = defineStore('utilityEvents', () => {
  const list = resource<UtilityEventOut[]>()

  function load() {
    return loadInto(list, 'recent', () => api.get<UtilityEventOut[]>('/utility-events', { days: RECENT_DAYS }))
  }

  /** Request an opt-out of this event on every thermostat it reaches (the default). */
  async function skip(id: number, allUnits = true): Promise<UtilityEventOut> {
    const body: SkipEventBody = { all_units: allUnits }
    const out = await api.post<UtilityEventOut>(`/utility-events/${id}/skip`, body)
    void load()
    return out
  }

  /** Take back a skip that has not been sent yet. */
  async function unskip(id: number, allUnits = true): Promise<UtilityEventOut> {
    const body: SkipEventBody = { all_units: allUnits }
    const out = await api.post<UtilityEventOut>(`/utility-events/${id}/unskip`, body)
    void load()
    return out
  }

  return { list, load, skip, unskip }
})
