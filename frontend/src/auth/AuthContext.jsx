import { useCallback, useEffect, useMemo, useState } from 'react'
import { authApi, setUnauthorizedHandler, tokenStore, usersApi } from '../lib/api'
import { AuthContext } from './useAuth.js'

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  // Only "loading" if there's a stored token we still need to verify with the server
  const [loading, setLoading] = useState(() => tokenStore.get() !== null)

  const logout = useCallback(() => {
    tokenStore.clear()
    setUser(null)
  }, [])

  useEffect(() => {
    setUnauthorizedHandler(logout)
  }, [logout])

  // On page load: if we have a token, ask the server who it belongs to.
  // The server is the source of truth -- we never decode the JWT client-side.
  useEffect(() => {
    if (!tokenStore.get()) return
    authApi
      .me()
      .then(setUser)
      .catch(() => {}) // 401 already triggered logout via the handler
      .finally(() => setLoading(false))
  }, [])

  const login = useCallback(async (identifier, password) => {
    const field = identifier.includes('@') ? 'email' : 'username'
    const data = await authApi.login({ [field]: identifier.trim(), password })
    tokenStore.set(data.access_token)
    setUser(data.user)
    return data.user
  }, [])

  const register = useCallback(
    async ({ username, email, password }) => {
      await authApi.register({ username, password, ...(email ? { email } : {}) })
      return login(username, password) // sign in right after creating the account
    },
    [login],
  )

  const updateAccount = useCallback(
    async (patch) => {
      const updated = await usersApi.update(user.id, patch)
      setUser(updated)
      return updated
    },
    [user],
  )

  const deleteAccount = useCallback(async () => {
    await usersApi.remove(user.id)
    logout()
  }, [user, logout])

  const value = useMemo(
    () => ({ user, loading, login, register, logout, updateAccount, deleteAccount }),
    [user, loading, login, register, logout, updateAccount, deleteAccount],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}