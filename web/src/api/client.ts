// The one fetch wrapper for /api. Spec: docs/specs/users-and-tokens.md ("Web").
//
// The owner signs in once per device. The server answers with a 15-minute access token, which
// lives only in this module's memory (never in storage, so a reload rebuilds it), and sets the
// refresh token as an HttpOnly cookie scoped to /api/auth. Every call sends
// `Authorization: Bearer <access token>`; only /api/auth/* calls send cookies
// (`credentials: 'include'`), and the browser holds no cookie for any other path.
//
// Recovery, in the order the client applies it:
//   - the token is about to expire, or a call answers 401 TOKEN_EXPIRED (or 401 when no token
//     was sent yet): one shared refresh, then that call is replayed once;
//   - 401 SESSION_REVOKED, or a refresh the server refuses: signed out; the auth store clears
//     and the app goes to /login (setSignedOutHandler);
//   - 403 REAUTHENTICATION_REQUIRED: the global password dialog (setReauthHandler,
//     ReauthDialog.vue) and, once confirmed, one replay.
// A refresh that cannot reach the server keeps the session: the call fails and the next one
// tries again.
//
// Several tabs share one refresh cookie, and the server treats a rotated-away refresh token
// presented again as theft (it signs the device out). So refreshes are serialized across tabs
// with the Web Locks API where the page is a secure context, and every tab announces its new
// access token on a BroadcastChannel so the others adopt it instead of refreshing themselves.
import type { AccessTokenOut } from './types'

export class ApiError extends Error {
  status: number
  detail: unknown
  /** The auth layer's machine-readable code (TOKEN_EXPIRED, INVALID_CREDENTIALS, ...), or
   *  NETWORK_ERROR when the server could not be reached; null for other errors. */
  code: string | null
  constructor(status: number, message: string, detail?: unknown, code: string | null = null) {
    super(message)
    this.status = status
    this.detail = detail
    this.code = code
  }
}

type Query = Record<string, string | number | boolean | null | undefined>

/** Auth routes that never need (or recover) a signed-in caller. */
const PUBLIC_AUTH = new Set(['/auth/state', '/auth/setup', '/auth/login', '/auth/refresh', '/auth/logout'])
/** Refresh this long before the access token expires, so calls rarely see TOKEN_EXPIRED. */
const EARLY_REFRESH_MS = 30_000
/** A refresh that takes longer than this gives up (so a stalled one cannot hold the lock). */
const REFRESH_TIMEOUT_MS = 20_000
const LOCK_NAME = 'climate-ai-refresh'
const CHANNEL_NAME = 'climate-ai-auth'

// --- the access token (memory only) ------------------------------------------------------

let accessToken: string | null = null
let expiresAt = 0 // ms since epoch
let sessionId: number | null = null
/** This tab's refresh in progress (shared by every caller in the tab). */
let refreshInFlight: Promise<RefreshResult> | null = null
/** Why the last refresh was refused (SESSION_REVOKED: signed out elsewhere; otherwise expired). */
let refreshError: ApiError | null = null

interface TokenMessage {
  type: 'token'
  access_token: string
  expires_at: number
  session_id: number
}

const channel: BroadcastChannel | null = (() => {
  try {
    return typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel(CHANNEL_NAME)
  } catch {
    return null
  }
})()

if (channel) {
  channel.onmessage = (m: MessageEvent) => {
    const d = m.data as Partial<TokenMessage> | null
    // Adopted only by a tab that is signed in or is refreshing right now (same browser, so the
    // same refresh cookie and device session); a signed-out tab stays signed out.
    if (!d || d.type !== 'token' || typeof d.access_token !== 'string') return
    if (!accessToken && !refreshInFlight) return
    if (typeof d.expires_at !== 'number' || d.expires_at <= expiresAt) return
    accessToken = d.access_token
    expiresAt = d.expires_at
    sessionId = typeof d.session_id === 'number' ? d.session_id : sessionId
  }
}

/** Keep a freshly issued access token (sign-in, first-run setup or refresh). */
export function setAccessToken(out: AccessTokenOut): void {
  accessToken = out.access_token
  expiresAt = Date.now() + Math.max(0, out.expires_in) * 1000
  sessionId = out.session_id
  try {
    channel?.postMessage({ type: 'token', access_token: accessToken, expires_at: expiresAt, session_id: sessionId } satisfies TokenMessage)
  } catch {
    /* another tab simply refreshes on its own */
  }
}

/** Forget the access token (signed out). The refresh cookie is the server's to clear. */
export function clearAccessToken(): void {
  accessToken = null
  expiresAt = 0
  sessionId = null
}

/** True while this tab holds an access token. */
export function hasAccessToken(): boolean {
  return accessToken !== null
}

/** The signed-in device's session id (marks "this device" in the devices list). */
export function currentSessionId(): number | null {
  return sessionId
}

function nearExpiry(): boolean {
  return accessToken !== null && Date.now() > expiresAt - EARLY_REFRESH_MS
}

// --- handlers the app registers ----------------------------------------------------------

type SignedOutHandler = (error: ApiError) => void
type ReauthHandler = () => Promise<boolean>

let signedOutHandler: SignedOutHandler | null = null
let reauthHandler: ReauthHandler | null = null
let reauthInFlight: Promise<boolean> | null = null

/** Called once a call proves the session is over (the auth store clears and routes to /login). */
export function setSignedOutHandler(fn: SignedOutHandler | null): void {
  signedOutHandler = fn
}

/** Registered by ReauthDialog.vue: ask for the password, resolve true once /auth/reauth passed. */
export function setReauthHandler(fn: ReauthHandler | null): void {
  reauthHandler = fn
}

function askReauth(): Promise<boolean> {
  if (!reauthHandler) return Promise.resolve(false)
  // Concurrent calls that all need the password share one prompt.
  if (!reauthInFlight) reauthInFlight = reauthHandler().catch(() => false).finally(() => (reauthInFlight = null))
  return reauthInFlight
}

function signedOut(error: ApiError): void {
  clearAccessToken()
  signedOutHandler?.(error)
}

// --- refresh -----------------------------------------------------------------------------

/** 'ok': a new access token is in memory. 'signed_out': the server refused the refresh cookie
 *  (missing, expired, revoked). 'unavailable': the server could not be reached or is not
 *  ready; the session may still be fine. */
export type RefreshResult = 'ok' | 'signed_out' | 'unavailable'

async function performRefresh(): Promise<RefreshResult> {
  let res: Response
  const abort = new AbortController()
  const timer = window.setTimeout(() => abort.abort(), REFRESH_TIMEOUT_MS)
  try {
    res = await fetch('/api/auth/refresh', {
      method: 'POST',
      credentials: 'include',
      headers: { Accept: 'application/json' },
      signal: abort.signal,
    })
  } catch {
    return 'unavailable'
  } finally {
    window.clearTimeout(timer)
  }
  if (res.ok) {
    try {
      const out = (await res.json()) as AccessTokenOut
      if (!out?.access_token) return 'unavailable'
      setAccessToken(out)
      return 'ok'
    } catch {
      return 'unavailable'
    }
  }
  // 429 and 5xx (including 503 AUTH_NOT_CONFIGURED) are the server's problem, not the session's.
  if (res.status === 429 || res.status >= 500) return 'unavailable'
  refreshError = await toError(res)
  clearAccessToken()
  return 'signed_out'
}

/** The error to report when a refresh was refused. */
function refusedError(fallback: ApiError): ApiError {
  return refreshError?.code === 'SESSION_REVOKED' ? refreshError : fallback
}

/** Exchange the refresh cookie for a new access token. Concurrent callers in this tab share
 *  one request, and tabs take turns (so two tabs never present the same refresh token). */
export function refreshAccessToken(): Promise<RefreshResult> {
  if (refreshInFlight) return refreshInFlight
  const tokenBefore = accessToken
  const run = async (): Promise<RefreshResult> => {
    // Another tab refreshed while this one waited for the lock (or the jitter below) and
    // broadcast its token: use that instead of rotating the cookie again.
    if (accessToken && accessToken !== tokenBefore && !nearExpiry()) return 'ok'
    return performRefresh()
  }
  const locks = typeof navigator !== 'undefined' && 'locks' in navigator ? navigator.locks : undefined
  const job: Promise<RefreshResult> = locks
    ? locks.request(LOCK_NAME, run).then((r) => r) // the lock is held until run() settles
    : // No Web Locks outside a secure context (plain http on the home network): a short random
      // wait lets a tab that is already refreshing broadcast first.
      new Promise<void>((resolve) => window.setTimeout(resolve, Math.random() * 200)).then(run)
  refreshInFlight = job.catch((): RefreshResult => 'unavailable').finally(() => (refreshInFlight = null))
  return refreshInFlight
}

// --- requests ----------------------------------------------------------------------------

function withQuery(path: string, query?: Query): string {
  if (!query) return path
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null) q.set(k, String(v))
  const s = q.toString()
  return s ? `${path}?${s}` : path
}

interface Attempt {
  retried?: boolean // replayed after a refresh
  reauthed?: boolean // replayed after the password dialog
}

async function toError(res: Response): Promise<ApiError> {
  let body: unknown
  try {
    body = await res.json()
  } catch {
    body = await res.text().catch(() => '')
  }
  const detail = typeof body === 'object' && body && 'detail' in body ? (body as { detail: unknown }).detail : undefined
  const code =
    detail && typeof detail === 'object' && !Array.isArray(detail) && typeof (detail as { code?: unknown }).code === 'string'
      ? (detail as { code: string }).code
      : null
  const msg = detail !== undefined ? formatDetail(detail) : `${res.status} ${res.statusText}`.trim()
  return new ApiError(res.status, msg, body, code)
}

async function request<T>(method: string, path: string, body?: unknown, query?: Query, attempt: Attempt = {}): Promise<T> {
  const isAuthPath = path.startsWith('/auth/')
  const isPublic = PUBLIC_AUTH.has(path)
  if (!isPublic && nearExpiry() && !attempt.retried) {
    const r = await refreshAccessToken()
    if (r === 'signed_out') {
      const err = refusedError(new ApiError(401, 'Your sign-in has expired. Sign in again.', undefined, 'NOT_AUTHENTICATED'))
      signedOut(err)
      throw err
    }
    // 'unavailable': send it anyway; the token may still have a few seconds left.
  }

  const sent = accessToken
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (sent) headers.Authorization = `Bearer ${sent}`

  let res: Response
  try {
    res = await fetch(withQuery(`/api${path}`, query), {
      method,
      credentials: isAuthPath ? 'include' : 'same-origin',
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError(0, 'Could not reach Climate AI. Check the connection and try again.', undefined, 'NETWORK_ERROR')
  }

  if (res.ok) {
    if (res.status === 204) return undefined as T
    const text = await res.text()
    return (text ? JSON.parse(text) : undefined) as T
  }

  const err = await toError(res)
  const replay = (next: Attempt) => request<T>(method, path, body, query, { ...attempt, ...next })

  // A wrong password is never a lost session, whatever status it comes with.
  if (res.status === 401 && !isPublic && err.code !== 'INVALID_CREDENTIALS') {
    if (!attempt.retried) {
      // Another call refreshed while this one was in flight: just send it again.
      if (accessToken && accessToken !== sent) return replay({ retried: true })
      // Expired (or missing, or signed with an old server secret): one refresh, one replay.
      if (err.code !== 'SESSION_REVOKED') {
        const r = await refreshAccessToken()
        if (r === 'ok') return replay({ retried: true })
        if (r === 'unavailable') throw err
        const refused = refusedError(err)
        signedOut(refused)
        throw refused
      }
    }
    signedOut(err)
    throw err
  }

  if (res.status === 403 && err.code === 'REAUTHENTICATION_REQUIRED' && !attempt.reauthed) {
    if (await askReauth()) return replay({ reauthed: true })
  }

  throw err
}

function formatDetail(d: unknown): string {
  if (typeof d === 'string') return d
  if (Array.isArray(d)) return d.map((e) => (e && typeof e === 'object' && 'msg' in e ? String(e.msg) : String(e))).join('; ')
  if (d && typeof d === 'object' && typeof (d as { message?: unknown }).message === 'string') return (d as { message: string }).message
  return JSON.stringify(d)
}

export const api = {
  get: <T>(path: string, query?: Query) => request<T>('GET', path, undefined, query),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body ?? {}),
  put: <T>(path: string, body: unknown) => request<T>('PUT', path, body),
  del: <T>(path: string) => request<T>('DELETE', path),
}
