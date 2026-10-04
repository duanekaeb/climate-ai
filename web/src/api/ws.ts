// Live updates: the server pushes tiny {type, id} events on /api/ws; listeners refetch.
import type { WsEvent } from './types'

type Listener = (e: WsEvent) => void
const listeners = new Set<Listener>()
let socket: WebSocket | null = null
let retry = 0
let timer: number | undefined
export const wsState = { connected: false }

export function onEvent(fn: Listener): () => void {
  listeners.add(fn)
  ensure()
  return () => listeners.delete(fn)
}

function ensure() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  socket = new WebSocket(`${proto}://${location.host}/api/ws`)
  socket.onopen = () => {
    retry = 0
    wsState.connected = true
  }
  socket.onmessage = (m) => {
    try {
      const e = JSON.parse(m.data) as WsEvent
      listeners.forEach((fn) => fn(e))
    } catch {
      /* ignore malformed */
    }
  }
  socket.onclose = () => {
    wsState.connected = false
    socket = null
    window.clearTimeout(timer)
    timer = window.setTimeout(ensure, Math.min(30000, 1000 * 2 ** retry++))
  }
}

// iOS suspends sockets in the background; reconnect when the app comes back.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && listeners.size) ensure()
})
