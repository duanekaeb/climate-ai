// Room history for the Rooms detail panel (the live room list itself comes from useStatus()).
import { defineStore } from 'pinia'
import { api } from '@/api/client'
import type { RoomHistory, RoomStatus } from '@/api/types'

export const FLOORS: { key: string; label: string }[] = [
  { key: 'main', label: 'Main floor' },
  { key: 'upstairs', label: 'Upstairs' },
  { key: 'wing', label: 'Bed / Office wing' },
]

export const STATE_LABELS: Record<RoomStatus['state'], string> = {
  occupied: 'Occupied',
  asleep: 'Asleep',
  empty: 'Empty',
  unknown: 'Unknown',
  no_target: 'No target',
}

/** Chip classes per occupancy state (literal strings so Tailwind generates them). */
export const STATE_CHIP: Record<RoomStatus['state'], string> = {
  occupied: 'bg-st-occupied/15 text-st-occupied',
  asleep: 'bg-st-asleep/15 text-st-asleep',
  empty: 'bg-st-empty/20 text-muted',
  unknown: 'bg-st-unknown/20 text-muted',
  no_target: 'bg-st-unknown/20 text-muted',
}

export const NO_SENSOR_TEXT = 'no sensor · temperature unknown'

export interface FloorGroup {
  key: string
  label: string
  rooms: RoomStatus[]
}

/** Rooms grouped by floor in house order (main, upstairs, wing); unknown floors last. */
export function groupByFloor(rooms: RoomStatus[]): FloorGroup[] {
  const groups: FloorGroup[] = FLOORS.map((f) => ({ ...f, rooms: [] }))
  for (const r of rooms) {
    let g = groups.find((x) => x.key === r.floor)
    if (!g) {
      g = { key: r.floor, label: r.floor, rooms: [] }
      groups.push(g)
    }
    g.rooms.push(r)
  }
  return groups.filter((g) => g.rooms.length > 0)
}

export function floorLabel(key: string): string {
  return FLOORS.find((f) => f.key === key)?.label ?? key
}

/** "45 s ago" / "12 min ago" / "3 h ago" / "2 d ago". */
export function secondsAgo(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return ''
  const s = Math.max(0, Math.round(seconds))
  if (s < 90) return `${s} s ago`
  if (s < 5400) return `${Math.round(s / 60)} min ago`
  if (s < 172800) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} d ago`
}

/** "motion 45 s ago" / "motion 12 min ago" / "no motion for 3 h". */
export function motionText(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return ''
  const s = Math.max(0, Math.round(seconds))
  if (s < 90) return `motion ${s} s ago`
  if (s < 5400) return `motion ${Math.round(s / 60)} min ago`
  if (s < 172800) return `no motion for ${Math.round(s / 3600)} h`
  return `no motion for ${Math.round(s / 86400)} d`
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

let seq = 0

export const useRooms = defineStore('rooms', {
  state: () => ({
    history: null as RoomHistory | null,
    roomKey: '',
    hours: 24,
    loading: false,
    error: '',
  }),
  actions: {
    async loadHistory(roomKey: string, hours: number) {
      const mine = ++seq
      if (roomKey !== this.roomKey || hours !== this.hours) this.history = null
      this.roomKey = roomKey
      this.hours = hours
      this.loading = true
      this.error = ''
      try {
        const h = await api.get<RoomHistory>(`/rooms/${encodeURIComponent(roomKey)}/history`, { hours })
        if (mine === seq) this.history = h
      } catch (e) {
        // A failed background refresh keeps the last good history on screen.
        if (mine === seq) this.error = message(e)
      } finally {
        if (mine === seq) this.loading = false
      }
    },
  },
})
