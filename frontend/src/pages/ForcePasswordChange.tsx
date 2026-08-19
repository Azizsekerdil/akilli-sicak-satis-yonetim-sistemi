/**
 * Mandatory password change.
 *
 * Shown instead of the application whenever the signed-in account still
 * carries `must_change_password` — the first-run administrator, and anyone
 * whose password an administrator has reset.
 *
 * This screen is a courtesy, not the control.  The server refuses every route
 * outside the password-change flow while the flag is set (see
 * `app/core/deps.py::get_current_user`), so removing or bypassing this page
 * gains a caller nothing.  It exists so the user is told *why* the rest of the
 * app is closed instead of meeting a wall of 403s.
 */
import { KeyRound, LogOut, ShieldAlert } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation } from '@tanstack/react-query'

import { Field, Spinner } from '@/components/ui'
import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/lib/auth'

export default function ForcePasswordChange() {
  const { t } = useTranslation()
  const { session, logout, refresh } = useAuth()

  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)

  const change = useMutation({
    mutationFn: () =>
      api.post('/auth/change-password', {
        old_password: oldPassword,
        new_password: newPassword,
      }),
    // Re-reading the session is what clears the flag on the client; the
    // server has already cleared it in the database.
    onSuccess: () => refresh(),
    onError: (e) => setError(e instanceof ApiError ? e.message : t('errors.generic')),
  })

  return (
    <div className="flex min-h-screen items-center justify-center bg-shell-100 p-4">
      <div className="w-full max-w-md rounded-xl border border-shell-200 bg-white p-6 shadow-card">
        <div className="mb-4 flex items-start gap-3">
          <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-amber-100 text-amber-700">
            <ShieldAlert className="h-5 w-5" />
          </span>
          <div>
            <h1 className="text-base font-semibold text-shell-900">
              {t('auth.mustChangePasswordTitle')}
            </h1>
            <p className="mt-1 text-sm text-shell-600">
              {t('auth.mustChangePasswordBody')}
            </p>
          </div>
        </div>

        <p className="mb-4 rounded-lg bg-shell-50 px-3 py-2 text-xs text-shell-600">
          {t('auth.mustChangePasswordScope')}
        </p>

        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault()
            setError(null)
            if (newPassword !== confirm) {
              setError(t('auth.passwordMismatch'))
              return
            }
            change.mutate()
          }}
        >
          <Field label={t('auth.currentPassword')} required>
            <input
              type="password"
              className="input"
              value={oldPassword}
              onChange={(e) => setOldPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </Field>
          <Field label={t('auth.newPassword')} required hint="min. 8, A-a-1">
            <input
              type="password"
              className="input"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              autoComplete="new-password"
              minLength={8}
              required
            />
          </Field>
          <Field label={t('auth.confirmPassword')} required error={error ?? undefined}>
            <input
              type="password"
              className="input"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="new-password"
              required
            />
          </Field>

          <div className="flex items-center gap-2">
            <button type="submit" className="btn-primary" disabled={change.isPending}>
              {change.isPending ? <Spinner /> : <KeyRound className="h-4 w-4" />}
              {t('common.save')}
            </button>
            <button type="button" className="btn-ghost" onClick={() => void logout()}>
              <LogOut className="h-4 w-4" />
              {t('auth.logout')}
            </button>
          </div>
        </form>

        {session?.user?.username && (
          <p className="mt-4 text-2xs text-shell-400">{session.user.username}</p>
        )}
      </div>
    </div>
  )
}
