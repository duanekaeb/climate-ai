// Thin fetch wrapper for /api. Cookies carry the owner session (same origin, so the iOS
// WKWebView wrapper and the PWA behave the same). 401 -> the router sends you to /login.
import { router } from '@/router'

export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

type Query = Record<string, string | number | boolean | null | undefined>

function withQuery(path: string, query?: Query): string {
  if (!query) return path
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null) q.set(k, String(v))
  const s = q.toString()
  return s ? `${path}?${s}` : path
}

async function request<T>(method: string, path: string, body?: unknown, query?: Query): Promise<T> {
  const res = await fetch(withQuery(`/api${path}`, query), {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? { Accept: 'application/json' } : { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (res.status === 401 && !path.startsWith('/auth')) {
    const here = router.currentRoute.value.fullPath
    if (!here.startsWith('/login')) router.push({ path: '/login', query: { next: here } })
  }
  if (!res.ok) {
    let detail: unknown
    try {
      detail = await res.json()
    } catch {
      detail = await res.text().catch(() => '')
    }
    const msg =
      typeof detail === 'object' && detail && 'detail' in detail
        ? formatDetail((detail as { detail: unknown }).detail)
        : `${res.status} ${res.statusText}`
    throw new ApiError(res.status, msg, detail)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

function formatDetail(d: unknown): string {
  if (typeof d === 'string') return d
  if (Array.isArray(d)) return d.map((e) => (e && typeof e === 'object' && 'msg' in e ? String(e.msg) : String(e))).join('; ')
  return JSON.stringify(d)
}

export const api = {
  get: <T>(path: string, query?: Query) => request<T>('GET', path, undefined, query),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body ?? {}),
  put: <T>(path: string, body: unknown) => request<T>('PUT', path, body),
  del: <T>(path: string) => request<T>('DELETE', path),
}
