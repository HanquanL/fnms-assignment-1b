const API_URL = import.meta.env.VITE_API_URL
const TOKEN_KEY = 'fnms_token'

export const tokenStore = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (token) => localStorage.setItem(TOKEN_KEY, token),
  clear: () => localStorage.removeItem(TOKEN_KEY),
}

export class ApiError extends Error {
  constructor(status, message, data) {
    super(message)
    this.status = status
    this.data = data
  }
}

// AuthContext registers a callback here so any 401 on an authenticated
// request (expired/invalid token, deleted account) logs the user out.
let onUnauthorized = () => {}
export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn
}

function errorMessage(status, data) {
  const detail = data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    // Our 422 shape: [{ loc: ["body", "password"], msg: "..." }]
    return detail.map((e) => `${e.loc?.at(-1)}: ${e.msg}`).join('; ')
  }
  return `Request failed (${status})`
}

export async function api(path, { method = 'GET', body, auth = true } = {}) {
  const headers = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  const token = tokenStore.get()
  if (auth && token) headers.Authorization = `Bearer ${token}`

  let res
  try {
    res = await fetch(`${API_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    // Network error, backend down, or blocked by CORS
    throw new ApiError(0, 'Cannot reach the server. Is the backend running?')
  }

  if (res.status === 204) return null
  const data = await res.json().catch(() => null)

  if (!res.ok) {
    if (res.status === 401 && auth && token) onUnauthorized()
    throw new ApiError(res.status, errorMessage(res.status, data), data)
  }
  return data
}

export const authApi = {
  register: (payload) => api('/api/auth/register', { method: 'POST', body: payload, auth: false }),
  login: (payload) => api('/api/auth/login', { method: 'POST', body: payload, auth: false }),
  me: () => api('/api/auth/me'),
}

export const usersApi = {
  get: (id) => api(`/api/users/${id}`),
  update: (id, patch) => api(`/api/users/${id}`, { method: 'PATCH', body: patch }),
  remove: (id) => api(`/api/users/${id}`, { method: 'DELETE' }),
}