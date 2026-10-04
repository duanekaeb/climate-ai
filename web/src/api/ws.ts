// Live updates: the server pushes tiny {type, id} events on /api/ws; listeners refetch.
//
// A browser cannot put a bearer header on a WebSocket, so each connection first asks for a
// single-use, 30-second ticket (POST /api/auth/ws-ticket, through the api client, so an expired
// access token is refreshed first) and opens /api/ws?ticket=…. Every reconnect gets a new
// ticket. If the session is over, the ticket request is what notices: the api client signs
// out, which calls closeSocket() and stops the reconnects.
import { api } from './client'
import type { WsEvent, WsTicketOut } from './types'

type Listener = (e: WsEvent) => void
const listeners = new Set<Listener>()
let socket: WebSocket | null = null
let retry = 0
let timer: number | undefined
// Set by closeSocket() (sign-out): no reconnects until someone subscribes again.
let closed = false
// A ticket request is in flight (so ensure() does not start a second one).
let opening = false
// Bumped by closeSocket(): a ticket that arrives after sign-out is thrown away.
let generation = 0
export const wsState = { connected: false }

export function onEvent(fn: Listener): () => void {
  listeners.add(fn)
  closed = false
  ensure()
  return () => listeners.delete(fn)
}

/** Close the socket and stop reconnecting (sign-out, or the session expired). The listeners
 *  are dropped too; the next onEvent() after signing back in opens a fresh socket. */
export function closeSocket(): void {
  closed = true
  generation++
  opening = false
  listeners.clear()
  window.clearTimeout(timer)
  timer = undefined
  retry = 0
  wsState.connected = false
  const s = socket
  socket = null
  if (s) {
    s.onopen = null
    s.onmessage = null
    s.onclose = null
    s.close()
  }
}

function scheduleReconnect() {
  window.clearTimeout(timer)
  if (!closed) timer = window.setTimeout(ensure, Math.min(30000, 1000 * 2 ** retry++))
}

function ensure() {
  if (closed || opening) return
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return
  opening = true
  const gen = generation
  api.post<WsTicketOut>('/auth/ws-ticket').then(
    (t) => {
      if (gen !== generation) return
      opening = false
      if (!closed) connect(t.ticket)
    },
    () => {
      if (gen !== generation) return
      opening = false
      scheduleReconnect() // offline or the server is restarting; a sign-out already set `closed`
    },
  )
}

function connect(ticket: string) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  socket = new WebSocket(`${proto}://${location.host}/api/ws?ticket=${encodeURIComponent(ticket)}`)
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
  // 4401 (ticket refused or the device was signed out) and 4403 (wrong origin) land here too;
  // the next attempt asks for a new ticket, which tells a signed-out device so.
  socket.onclose = () => {
    wsState.connected = false
    socket = null
    scheduleReconnect()
  }
}

// iOS suspends sockets in the background; reconnect when the app comes back.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && listeners.size && !closed) ensure()
})
