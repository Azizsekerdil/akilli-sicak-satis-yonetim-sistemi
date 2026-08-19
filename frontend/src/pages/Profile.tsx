import { KeyRound, Monitor, User as UserIcon } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { currentLanguage } from '@/lib/i18n'
import { formatDate, formatRelative } from '@/lib/format'
import {
  Card,
  Field,
  LoadingBlock,
  PageHeader,
  Spinner,
  StatusBadge,
  useToast,
} from '@/components/ui'

interface SessionRow {
  id: number
  token_id: string
  ip_address?: string | null
  device_label?: string | null
  user_agent?: string | null
  is_active: boolean
  expires_at: string
  last_seen_at?: string | null
  created_at?: string | null
}

export default function Profile() {
  const { t } = useTranslation()
  const { session } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()
  const lang = currentLanguage()

  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)

  const sessions = useQuery({
    queryKey: ['my-sessions'],
    queryFn: () => api.get<SessionRow[]>('/auth/sessions'),
  })

  const changePassword = useMutation({
    mutationFn: () =>
      api.post('/auth/change-password', {
        old_password: oldPassword,
        new_password: newPassword,
      }),
    onSuccess: () => {
      push('success', t('common.success'))
      setOldPassword('')
      setNewPassword('')
      setConfirm('')
      setError(null)
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const revoke = useMutation({
    mutationFn: (tokenId: string) => api.delete(`/auth/sessions/${tokenId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-sessions'] }),
  })

  if (!session) return <LoadingBlock />
  const u = session.user

  return (
    <>
      <PageHeader
        title={t('auth.profile')}
        subtitle={u.full_name}
        icon={<UserIcon className="h-5 w-5" />}
      />

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title={t('auth.profile')}>
          <dl className="space-y-3 text-sm">
            {[
              [t('auth.username'), u.username],
              [t('common.name'), u.full_name],
              ['E-posta / Email', u.email ?? '—'],
              [
                t('nav.roles'),
                lang === 'en' ? (u.role?.name_en ?? '—') : (u.role?.name_tr ?? '—'),
              ],
              ['Veri kapsamı / Data scope', session.data_scope],
              ['Son giriş / Last login', formatDate(u.last_login_at, { withTime: true })],
              ['İzin sayısı / Permissions', String(session.permissions.length)],
            ].map(([k, v]) => (
              <div key={k} className="flex justify-between gap-4 border-b border-shell-100 pb-2">
                <dt className="text-shell-500">{k}</dt>
                <dd className="text-right font-medium text-shell-800">{v}</dd>
              </div>
            ))}
          </dl>
        </Card>

        <Card title={t('auth.changePassword')}>
          <form
            className="space-y-4"
            onSubmit={(e) => {
              e.preventDefault()
              setError(null)
              if (newPassword !== confirm) {
                setError(t('auth.passwordMismatch'))
                return
              }
              changePassword.mutate()
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
            <button type="submit" className="btn-primary" disabled={changePassword.isPending}>
              {changePassword.isPending ? <Spinner /> : <KeyRound className="h-4 w-4" />}
              {t('common.save')}
            </button>
          </form>
        </Card>

        <Card title={t('auth.sessions')} className="lg:col-span-2" bodyClassName="p-0">
          {sessions.isLoading ? (
            <LoadingBlock />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('nav.vehicles')}</th>
                    <th>IP</th>
                    <th>{t('common.status')}</th>
                    <th>Son görülme</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {(sessions.data ?? []).map((s) => (
                    <tr key={s.id}>
                      <td className="max-w-xs truncate">
                        <span className="inline-flex items-center gap-1.5">
                          <Monitor className="h-3.5 w-3.5 text-shell-400" />
                          {s.device_label ?? s.user_agent ?? '—'}
                        </span>
                      </td>
                      <td className="tabular">{s.ip_address ?? '—'}</td>
                      <td>
                        <StatusBadge status={s.is_active ? 'ACTIVE' : 'INACTIVE'} />
                      </td>
                      <td>{formatRelative(s.last_seen_at ?? s.created_at)}</td>
                      <td className="text-right">
                        {s.is_active && (
                          <button
                            type="button"
                            className="btn-ghost btn-sm text-danger-600"
                            onClick={() => revoke.mutate(s.token_id)}
                          >
                            {t('auth.logout')}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </>
  )
}
