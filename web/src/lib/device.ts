// Sign-in helpers shared by the sign-in page and the Security page: the password rule, the
// device name that labels this sign-in in "Signed-in devices", and plain-words auth errors.
import { ApiError } from '@/api/client'

/** The server's default `password_min_length`; the server checks it again and explains if it
 *  was configured longer. Existing shorter passwords still sign in. */
export const PASSWORD_MIN_LENGTH = 10

const KEY = 'climate.deviceName'

/** A readable name for this device, guessed from the platform: "iPhone (Climate AI app)",
 *  "Mac (Safari)", "Android phone (Chrome)". */
export function guessDeviceName(): string {
  if (typeof navigator === 'undefined') return 'This device'
  const ua = navigator.userAgent
  const touch = (navigator.maxTouchPoints ?? 0) > 1
  let device = 'Computer'
  if (/iPhone/.test(ua)) device = 'iPhone'
  else if (/iPad/.test(ua) || (/Macintosh/.test(ua) && touch)) device = 'iPad' // iPadOS reports a Mac
  else if (/Android/.test(ua)) device = /Mobile/.test(ua) ? 'Android phone' : 'Android tablet'
  else if (/Macintosh|Mac OS X/.test(ua)) device = 'Mac'
  else if (/Windows/.test(ua)) device = 'Windows PC'
  else if (/CrOS/.test(ua)) device = 'Chromebook'
  else if (/Linux/.test(ua)) device = 'Linux computer'

  let app = ''
  if (/ClimateAI-iOS/.test(ua)) app = 'Climate AI app'
  else if (/Edg\//.test(ua)) app = 'Edge'
  else if (/Firefox\/|FxiOS/.test(ua)) app = 'Firefox'
  else if (/Chrome\/|CriOS/.test(ua)) app = 'Chrome'
  else if (/Safari\//.test(ua)) app = 'Safari'
  return app ? `${device} (${app})` : device
}

/** The name this device signed in with last time, or the platform guess. */
export function deviceName(): string {
  try {
    const saved = localStorage.getItem(KEY)
    if (saved && saved.trim()) return saved.trim().slice(0, 100)
  } catch {
    /* private mode / blocked storage */
  }
  return guessDeviceName()
}

/** Remember a name the owner typed (not a secret; it only labels this device). */
export function rememberDeviceName(name: string): void {
  try {
    const v = name.trim().slice(0, 100)
    if (v && v !== guessDeviceName()) localStorage.setItem(KEY, v)
    else localStorage.removeItem(KEY)
  } catch {
    /* nothing to remember then */
  }
}

/** Why first-run setup can be refused even at home, and the ways round it. The name in the
 *  address bar matters, not only where the device is: setup accepts an IP address, localhost
 *  or a .local name, and any other name only once it is listed in CLIMATE_SETUP_HOSTS. */
export const SETUP_HOST_HINT =
  'Open Climate AI by its IP address (for example http://192.168.1.20:8470), localhost or a .local name, ' +
  'not by another name such as a Tailscale MagicDNS name; or add that name to CLIMATE_SETUP_HOSTS on the ' +
  'server; or run "make password" on the server.'

/** Plain-words text for an error from the sign-in, setup, password or re-auth calls. */
export function authErrorText(e: unknown): string {
  if (e instanceof ApiError) {
    switch (e.code) {
      case 'INVALID_CREDENTIALS':
        return 'That password is not right.'
      case 'TOO_MANY_ATTEMPTS':
        return 'Too many attempts. Wait a few minutes, then try again.'
      case 'SETUP_NOT_ALLOWED':
        // The server says why: the address is not a home one, or the name in the address bar
        // (a Tailscale MagicDNS or LAN DNS name) is not one it accepts for setup. Show its words.
        return e.message || SETUP_HOST_HINT
      case 'ALREADY_SET':
        return 'A password has already been set. Sign in instead.'
      case 'AUTH_NOT_CONFIGURED':
        return `${e.message} Set it in the server's .env, restart, and run "python -m climate.cli doctor".`
      case 'LOGIN_PAUSED':
      case 'WEAK_PASSWORD':
      case 'NETWORK_ERROR':
        return e.message
    }
    if (e.status === 429) return 'Too many attempts. Wait a few minutes, then try again.'
    return e.message
  }
  return e instanceof Error ? e.message : 'Something went wrong. Try again.'
}
