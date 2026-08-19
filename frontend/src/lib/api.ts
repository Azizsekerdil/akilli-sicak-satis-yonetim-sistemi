/**
 * API client.
 *
 * One place that knows about the base URL, the bearer token, token refresh,
 * language headers and the backend's structured error envelope.  Everything
 * else in the app calls `api.get/post/...` and gets typed data or an ApiError.
 */

const BASE = import.meta.env.VITE_API_BASE ?? '/api/v1'

const ACCESS_KEY = 'vs.access'
const REFRESH_KEY = 'vs.refresh'
const LANG_KEY = 'vs.lang'

export type Lang = 'tr' | 'en'

/* -------------------------------------------------------------------------- */
/* Token storage                                                              */
/* -------------------------------------------------------------------------- */
export const tokens = {
  get access() {
    return localStorage.getItem(ACCESS_KEY)
  },
  get refresh() {
    return localStorage.getItem(REFRESH_KEY)
  },
  set(access: string, refresh: string) {
    localStorage.setItem(ACCESS_KEY, access)
    localStorage.setItem(REFRESH_KEY, refresh)
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY)
    localStorage.removeItem(REFRESH_KEY)
  },
}

export const langStore = {
  get(): Lang {
    const v = localStorage.getItem(LANG_KEY)
    return v === 'en' ? 'en' : 'tr'
  },
  set(l: Lang) {
    localStorage.setItem(LANG_KEY, l)
    document.documentElement.lang = l
  },
}

/* -------------------------------------------------------------------------- */
/* Errors                                                                     */
/* -------------------------------------------------------------------------- */
export interface FieldError {
  field: string
  message: string
  type?: string
}

export class ApiError extends Error {
  status: number
  code: string
  messageKey?: string
  fields?: FieldError[]
  requestId?: string

  constructor(
    status: number,
    code: string,
    message: string,
    opts: { messageKey?: string; fields?: FieldError[]; requestId?: string } = {},
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.messageKey = opts.messageKey
    this.fields = opts.fields
    this.requestId = opts.requestId
  }

  /** 401/403 mean "log in again" / "not allowed" — never worth retrying. */
  get isAuth() {
    return this.status === 401
  }
  get isForbidden() {
    return this.status === 403
  }
  get isOffline() {
    return this.status === 0
  }
}

/* -------------------------------------------------------------------------- */
/* Refresh handling                                                           */
/* -------------------------------------------------------------------------- */
let refreshing: Promise<boolean> | null = null
type Listener = () => void
const unauthorizedListeners = new Set<Listener>()

export function onUnauthorized(fn: Listener): () => void {
  unauthorizedListeners.add(fn)
  return () => unauthorizedListeners.delete(fn)
}

async function tryRefresh(): Promise<boolean> {
  // Collapse concurrent 401s into a single refresh round-trip.
  if (refreshing) return refreshing
  const rt = tokens.refresh
  if (!rt) return false

  refreshing = (async () => {
    try {
      const res = await fetch(`${BASE}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: rt }),
      })
      if (!res.ok) return false
      const data = await res.json()
      tokens.set(data.access_token, data.refresh_token)
      return true
    } catch {
      return false
    } finally {
      refreshing = null
    }
  })()

  return refreshing
}

/* -------------------------------------------------------------------------- */
/* Core request                                                               */
/* -------------------------------------------------------------------------- */
interface RequestOptions {
  params?: Record<string, unknown>
  signal?: AbortSignal
  raw?: boolean
  skipAuthRetry?: boolean
}

function buildUrl(path: string, params?: Record<string, unknown>): string {
  const url = `${BASE}${path.startsWith('/') ? path : `/${path}`}`
  const qs = new URLSearchParams()
  qs.set('lang', langStore.get())
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === '') continue
      if (Array.isArray(v)) v.forEach((item) => qs.append(k, String(item)))
      else qs.set(k, String(v))
    }
  }
  return `${url}?${qs.toString()}`
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  opts: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'Accept-Language': langStore.get(),
  }
  const access = tokens.access
  if (access) headers.Authorization = `Bearer ${access}`

  let payload: BodyInit | undefined
  if (body instanceof FormData) {
    payload = body
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }

  let res: Response
  try {
    res = await fetch(buildUrl(path, opts.params), {
      method,
      headers,
      body: payload,
      signal: opts.signal,
    })
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e
    throw new ApiError(0, 'network_error', 'Sunucuya ulaşılamıyor / Cannot reach the server')
  }

  if (res.status === 401 && !opts.skipAuthRetry && tokens.refresh) {
    if (await tryRefresh()) {
      return request<T>(method, path, body, { ...opts, skipAuthRetry: true })
    }
    tokens.clear()
    unauthorizedListeners.forEach((fn) => fn())
  }

  if (opts.raw) {
    if (!res.ok) throw await toError(res)
    return res as unknown as T
  }

  if (res.status === 204) return undefined as T

  const text = await res.text()
  const data = text ? safeJson(text) : null

  if (!res.ok) throw toErrorFromBody(res.status, data, res.headers.get('X-Request-ID'))
  return data as T
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return { message: text }
  }
}

async function toError(res: Response): Promise<ApiError> {
  const data = safeJson(await res.text().catch(() => ''))
  return toErrorFromBody(res.status, data, res.headers.get('X-Request-ID'))
}

function toErrorFromBody(status: number, data: any, requestId?: string | null): ApiError {
  return new ApiError(
    status,
    data?.error ?? 'error',
    data?.message ?? `HTTP ${status}`,
    {
      messageKey: data?.message_key,
      fields: data?.fields,
      requestId: requestId ?? undefined,
    },
  )
}

/* -------------------------------------------------------------------------- */
/* Public surface                                                             */
/* -------------------------------------------------------------------------- */
export const api = {
  get: <T>(path: string, params?: Record<string, unknown>, signal?: AbortSignal) =>
    request<T>('GET', path, undefined, { params, signal }),
  post: <T>(path: string, body?: unknown, params?: Record<string, unknown>) =>
    request<T>('POST', path, body, { params }),
  put: <T>(path: string, body?: unknown, params?: Record<string, unknown>) =>
    request<T>('PUT', path, body, { params }),
  patch: <T>(path: string, body?: unknown, params?: Record<string, unknown>) =>
    request<T>('PATCH', path, body, { params }),
  delete: <T>(path: string, params?: Record<string, unknown>) =>
    request<T>('DELETE', path, undefined, { params }),

  /** Downloads a generated file and hands the browser a Blob. */
  async download(
    path: string,
    body?: unknown,
    params?: Record<string, unknown>,
  ): Promise<{ blob: Blob; filename: string }> {
    const res = await request<Response>(body ? 'POST' : 'GET', path, body, {
      params,
      raw: true,
    })
    const disp = res.headers.get('Content-Disposition') ?? ''
    const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disp)
    return {
      blob: await res.blob(),
      filename: match ? decodeURIComponent(match[1]) : 'rapor',
    }
  },
}

/** Paged envelope returned by every list endpoint. */
export interface Paged<T> {
  items: T[]
  total: number
  page: number
  size: number
  pages: number
  has_next: boolean
  has_prev: boolean
}
