import { AlertCircle, Eye, EyeOff, Languages, Truck } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Navigate, useLocation } from 'react-router-dom'

import { useAuth } from '@/lib/auth'
import { currentLanguage, setLanguage } from '@/lib/i18n'
import { Spinner } from '@/components/ui'

export default function Login() {
  const { t } = useTranslation()
  const { session, login, loading } = useAuth()
  const location = useLocation()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const lang = currentLanguage()

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner className="h-6 w-6 text-brand-600" />
      </div>
    )
  }
  if (session) {
    const from = (location.state as { from?: string } | null)?.from ?? '/'
    return <Navigate to={from} replace />
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await login(username.trim(), password)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.generic'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-full">
      {/* Brand panel */}
      <div className="relative hidden flex-1 bg-shell-900 lg:flex lg:flex-col lg:justify-between lg:p-12">
        <div className="flex items-center gap-3">
          <div className="rounded-xl bg-brand-600 p-2.5">
            <Truck className="h-6 w-6 text-white" />
          </div>
          <span className="text-lg font-semibold text-white">{t('app.shortName')}</span>
        </div>

        <div className="max-w-lg">
          <h1 className="text-3xl font-semibold leading-tight text-white">
            {t('app.name')}
          </h1>
          <p className="mt-4 text-shell-400">{t('app.tagline')}</p>

          <dl className="mt-10 grid grid-cols-2 gap-6">
            {[
              ['FEFO', lang === 'en' ? 'Expiry-first stock' : 'SKT öncelikli stok'],
              ['VRP', lang === 'en' ? 'Route optimisation' : 'Rota optimizasyonu'],
              ['AI', lang === 'en' ? 'Local + cloud models' : 'Yerel + bulut modeller'],
              ['TR / EN', lang === 'en' ? 'Full bilingual UI' : 'Tam iki dilli arayüz'],
            ].map(([k, v]) => (
              <div key={k}>
                <dt className="text-sm font-semibold text-brand-400">{k}</dt>
                <dd className="mt-0.5 text-sm text-shell-400">{v}</dd>
              </div>
            ))}
          </dl>
        </div>

        <p className="text-2xs text-shell-600">v1.0.0</p>
      </div>

      {/* Form panel */}
      <div className="flex flex-1 flex-col justify-center px-6 py-12 sm:px-12">
        <div className="mx-auto w-full max-w-sm">
          <div className="mb-8 flex items-center justify-between">
            <div className="flex items-center gap-2.5 lg:hidden">
              <div className="rounded-lg bg-brand-600 p-2">
                <Truck className="h-5 w-5 text-white" />
              </div>
              <span className="font-semibold text-shell-900">{t('app.shortName')}</span>
            </div>
            <button
              type="button"
              className="btn-ghost btn-sm ml-auto gap-1.5"
              onClick={() => setLanguage(lang === 'tr' ? 'en' : 'tr')}
            >
              <Languages className="h-4 w-4" />
              <span className="text-xs font-semibold uppercase">{lang}</span>
            </button>
          </div>

          <h2 className="text-xl font-semibold text-shell-900">{t('auth.signIn')}</h2>
          <p className="mt-1 text-sm text-shell-500">{t('app.name')}</p>

          <form onSubmit={onSubmit} className="mt-8 space-y-4">
            <div>
              <label className="label" htmlFor="username">
                {t('auth.username')}
              </label>
              <input
                id="username"
                className="input"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                autoFocus
                required
              />
            </div>

            <div>
              <label className="label" htmlFor="password">
                {t('auth.password')}
              </label>
              <div className="relative">
                <input
                  id="password"
                  type={showPassword ? 'text' : 'password'}
                  className="input pr-10"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                  required
                />
                <button
                  type="button"
                  className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-shell-400 hover:text-shell-700"
                  onClick={() => setShowPassword((v) => !v)}
                  tabIndex={-1}
                >
                  {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </div>

            {error && (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-lg bg-danger-50 px-3 py-2.5 text-sm text-danger-700"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            <button type="submit" className="btn-primary w-full" disabled={busy}>
              {busy && <Spinner />}
              {busy ? t('auth.signingIn') : t('auth.login')}
            </button>
          </form>
        </div>
      </div>
    </div>
  )
}
