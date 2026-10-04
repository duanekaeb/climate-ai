// Reports: daily / nightly / weekly / anomaly write-ups (system or Claude), markdown bodies.
import { defineStore } from 'pinia'
import { api } from '@/api/client'
import type { ReportOut } from '@/api/types'

export type ReportFilter = '' | 'daily' | 'nightly' | 'weekly' | 'anomaly'

export const REPORT_FILTERS: { key: ReportFilter; label: string }[] = [
  { key: '', label: 'All' },
  { key: 'daily', label: 'Daily' },
  { key: 'nightly', label: 'Nightly' },
  { key: 'weekly', label: 'Weekly' },
  { key: 'anomaly', label: 'Anomaly' },
]

export const KIND_LABELS: Record<ReportOut['kind'], string> = {
  daily: 'Daily',
  nightly: 'Nightly',
  weekly: 'Weekly',
  anomaly: 'Anomaly',
  note: 'Note',
}

export function isReportFilter(v: unknown): v is ReportFilter {
  return typeof v === 'string' && REPORT_FILTERS.some((f) => f.key === v)
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

let listSeq = 0
let detailSeq = 0

export const useReports = defineStore('reports', {
  state: () => ({
    kind: '' as ReportFilter,
    items: null as ReportOut[] | null,
    loading: false,
    error: '',
    detail: null as ReportOut | null,
    detailLoading: false,
    detailError: '',
  }),
  actions: {
    async load(kind: ReportFilter, opts: { quiet?: boolean } = {}) {
      const mine = ++listSeq
      if (kind !== this.kind) this.items = null
      this.kind = kind
      if (!opts.quiet) this.loading = true
      this.error = ''
      try {
        const items = await api.get<ReportOut[]>('/reports', { kind: kind || undefined, limit: 50 })
        if (mine === listSeq) this.items = items
      } catch (e) {
        if (mine === listSeq) this.error = message(e)
      } finally {
        if (mine === listSeq) this.loading = false
      }
    },
    /** Use the copy already in the list when there is one; otherwise fetch it (deep links). */
    async open(id: number) {
      const mine = ++detailSeq
      this.detailError = ''
      const known = this.items?.find((r) => r.id === id)
      if (known) {
        this.detail = known
        this.detailLoading = false
        return
      }
      if (this.detail?.id !== id) this.detail = null
      this.detailLoading = true
      try {
        const r = await api.get<ReportOut>(`/reports/${id}`)
        if (mine === detailSeq) this.detail = r
      } catch (e) {
        if (mine === detailSeq) this.detailError = message(e)
      } finally {
        if (mine === detailSeq) this.detailLoading = false
      }
    },
    close() {
      ++detailSeq
      this.detail = null
      this.detailError = ''
      this.detailLoading = false
    },
  },
})
