/**
 * Kullanıcılar / Users — account administration.
 *
 * Create, edit, reset passwords, activate/deactivate, and layer per-user
 * grants and revokes on top of whatever the role already gives.  The override
 * editor always shows what the role contributes, so an administrator can see
 * the difference between "has it because of the role" and "has it because
 * someone granted it".
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Pencil, Power, ShieldCheck, UserPlus, Users as UsersIcon } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  Modal,
  PageHeader,
  Pagination,
  SkeletonRows,
  Spinner,
  StatusBadge,
  useToast,
} from '@/components/ui'
import { api, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatNumber } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface AdminUser {
  id: number
  username: string
  full_name: string
  email?: string | null
  phone?: string | null
  role_id: number
  role_code?: string | null
  role_name?: string | null
  role_rank?: number | null
  region_id?: number | null
  status: string
  language: string
  data_scope?: string | null
  must_change_password: boolean
  is_deleted: boolean
  last_login_at?: string | null
  permission_count: number
}

interface RoleRow {
  code: string
  name: string
  rank: number
  data_scope: string
  permission_count: number
  user_count: number
  is_system: boolean
}

interface PermissionSnapshot {
  role: string
  role_permissions: string[]
  effective: string[]
  grant: string[]
  revoke: string[]
  data_scope: string
}

interface MatrixResource {
  key: string
  module: string
  name: string
  actions: string[]
  is_sensitive: boolean
}

interface RoleMatrix {
  resources: MatrixResource[]
}

const STATUSES = ['ACTIVE', 'INACTIVE', 'LOCKED', 'SUSPENDED'] as const
const SCOPES = ['ALL', 'REGION', 'TEAM', 'OWN', 'NONE'] as const

type Override = 'inherit' | 'grant' | 'revoke'

/* -------------------------------------------------------------------------- */
/* Permission override editor                                                 */
/* -------------------------------------------------------------------------- */
function PermissionEditor({ user, onClose }: { user: AdminUser; onClose: () => void }) {
  const { t } = useTranslation()
  const { can } = useAuth()
  const toast = useToast()
  const queryClient = useQueryClient()

  const snapshotQuery = useQuery({
    queryKey: ['system', 'user-permissions', user.id],
    queryFn: () => api.get<PermissionSnapshot>(`/system/users/${user.id}/permissions`),
  })

  const matrixQuery = useQuery({
    queryKey: ['system', 'roles', 'matrix'],
    queryFn: () => api.get<RoleMatrix>('/system/roles/matrix'),
    enabled: can('system.roles', 'VIEW'),
  })

  const [overrides, setOverrides] = useState<Record<string, Override>>({})
  const [scope, setScope] = useState<string>('')

  useEffect(() => {
    const snapshot = snapshotQuery.data
    if (!snapshot) return
    const next: Record<string, Override> = {}
    snapshot.grant.forEach((code) => (next[code] = 'grant'))
    snapshot.revoke.forEach((code) => (next[code] = 'revoke'))
    setOverrides(next)
    setScope(snapshot.data_scope ?? '')
  }, [snapshotQuery.data])

  const roleSet = useMemo(
    () => new Set(snapshotQuery.data?.role_permissions ?? []),
    [snapshotQuery.data?.role_permissions],
  )

  /** Every code we can offer: the whole resource×action grid when we may read it. */
  const grouped = useMemo(() => {
    const byModule = new Map<string, { code: string; resource: string; action: string }[]>()
    const push = (module: string, resource: string, action: string) => {
      const list = byModule.get(module) ?? []
      list.push({ code: `${resource}:${action}`, resource, action })
      byModule.set(module, list)
    }
    if (matrixQuery.data?.resources?.length) {
      matrixQuery.data.resources.forEach((r) =>
        r.actions.forEach((a) => push(r.module, r.key, a)),
      )
    } else {
      const codes = new Set<string>([
        ...(snapshotQuery.data?.role_permissions ?? []),
        ...(snapshotQuery.data?.effective ?? []),
        ...(snapshotQuery.data?.grant ?? []),
        ...(snapshotQuery.data?.revoke ?? []),
      ])
      codes.forEach((code) => {
        const [resource, action] = code.split(':')
        push(resource?.split('.')[0] ?? 'other', resource ?? code, action ?? 'VIEW')
      })
    }
    return [...byModule.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [matrixQuery.data, snapshotQuery.data])

  const save = useMutation({
    mutationFn: () =>
      api.put(`/system/users/${user.id}/permissions`, {
        grant: Object.entries(overrides)
          .filter(([, v]) => v === 'grant')
          .map(([k]) => k),
        revoke: Object.entries(overrides)
          .filter(([, v]) => v === 'revoke')
          .map(([k]) => k),
        data_scope: scope || null,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['system', 'users'] })
      await queryClient.invalidateQueries({ queryKey: ['system', 'user-permissions', user.id] })
      toast.push('success', t('sysUsers.permissionsSaved'))
      onClose()
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  const setOverride = (code: string, value: Override) =>
    setOverrides((prev) => {
      const next = { ...prev }
      if (value === 'inherit') delete next[code]
      else next[code] = value
      return next
    })

  return (
    <Modal
      open
      onClose={onClose}
      size="xl"
      title={`${t('sysUsers.permissionEditor')} — ${user.full_name}`}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={() => save.mutate()}
            disabled={save.isPending}
          >
            {save.isPending && <Spinner />}
            {t('common.save')}
          </button>
        </>
      }
    >
      {snapshotQuery.isLoading ? (
        <LoadingBlock />
      ) : snapshotQuery.isError ? (
        <ErrorState error={snapshotQuery.error} />
      ) : (
        <div className="space-y-4">
          <Field label={t('sysUsers.dataScope')}>
            <select className="input" value={scope} onChange={(e) => setScope(e.target.value)}>
              <option value="">{t('sysUsers.inherit')}</option>
              {SCOPES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>

          {grouped.map(([module, items]) => (
            <div key={module}>
              <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-shell-500">
                {module}
              </h4>
              <div className="table-wrap rounded-lg border border-shell-200">
                <table className="table">
                  <thead>
                    <tr>
                      <th>{t('sysRoles.resource')}</th>
                      <th>{t('sysUsers.inherited')}</th>
                      <th className="text-right">{t('common.actions')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map(({ code }) => {
                      const state = overrides[code] ?? 'inherit'
                      const inherited = roleSet.has(code)
                      return (
                        <tr key={code}>
                          <td className="font-mono text-2xs">{code}</td>
                          <td>
                            <span className={inherited ? 'badge-ok' : 'badge-muted'}>
                              {inherited ? t('common.yes') : t('common.no')}
                            </span>
                          </td>
                          <td className="text-right">
                            <div className="inline-flex gap-1">
                              {(['inherit', 'grant', 'revoke'] as Override[]).map((option) => (
                                <button
                                  key={option}
                                  type="button"
                                  onClick={() => setOverride(code, option)}
                                  className={`rounded-md px-2 py-1 text-2xs font-medium ${
                                    state === option
                                      ? option === 'grant'
                                        ? 'bg-ok-600 text-white'
                                        : option === 'revoke'
                                          ? 'bg-danger-600 text-white'
                                          : 'bg-shell-700 text-white'
                                      : 'bg-shell-100 text-shell-600 hover:bg-shell-200'
                                  }`}
                                >
                                  {option === 'inherit'
                                    ? t('sysUsers.inherit')
                                    : option === 'grant'
                                      ? t('sysUsers.grant')
                                      : t('sysUsers.revoke')}
                                </button>
                              ))}
                            </div>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      )}
    </Modal>
  )
}

/* -------------------------------------------------------------------------- */
/* Create / edit form                                                         */
/* -------------------------------------------------------------------------- */
function UserForm({
  user,
  roles,
  onClose,
}: {
  user: AdminUser | null
  roles: RoleRow[]
  onClose: () => void
}) {
  const { t } = useTranslation()
  const toast = useToast()
  const queryClient = useQueryClient()

  const [username, setUsername] = useState(user?.username ?? '')
  const [fullName, setFullName] = useState(user?.full_name ?? '')
  const [roleCode, setRoleCode] = useState(user?.role_code ?? roles[0]?.code ?? '')
  const [regionId, setRegionId] = useState(user?.region_id ? String(user.region_id) : '')
  const [email, setEmail] = useState(user?.email ?? '')
  const [phone, setPhone] = useState(user?.phone ?? '')
  const [language, setLanguage] = useState(user?.language ?? 'tr')
  const [status, setStatus] = useState(user?.status ?? 'ACTIVE')
  const [password, setPassword] = useState('')

  const mutation = useMutation({
    mutationFn: () =>
      user
        ? api.put<AdminUser>(`/system/users/${user.id}`, {
            full_name: fullName,
            email: email || null,
            phone: phone || null,
            role_code: roleCode,
            region_id: regionId ? Number(regionId) : null,
            status,
            language,
          })
        : api.post<AdminUser>('/system/users', {
            username,
            password,
            full_name: fullName,
            role_code: roleCode,
            email: email || null,
            phone: phone || null,
            region_id: regionId ? Number(regionId) : null,
            language,
          }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['system', 'users'] })
      toast.push('success', user ? t('sysUsers.updated') : t('sysUsers.created'))
      onClose()
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title={user ? t('sysUsers.editUser') : t('sysUsers.newUser')}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending || !fullName || !roleCode || (!user && (!username || password.length < 8))}
          >
            {mutation.isPending && <Spinner />}
            {t('common.save')}
          </button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t('sysUsers.username')} required>
          <input
            className="input"
            value={username}
            disabled={Boolean(user)}
            onChange={(e) => setUsername(e.target.value)}
          />
        </Field>
        <Field label={t('sysUsers.fullName')} required>
          <input className="input" value={fullName} onChange={(e) => setFullName(e.target.value)} />
        </Field>
        <Field label={t('sysUsers.role')} required>
          <select className="input" value={roleCode} onChange={(e) => setRoleCode(e.target.value)}>
            {roles.map((r) => (
              <option key={r.code} value={r.code}>
                {r.name} ({r.code})
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('sysUsers.region')}>
          <input
            type="number"
            className="input tabular"
            value={regionId}
            onChange={(e) => setRegionId(e.target.value)}
          />
        </Field>
        <Field label={t('sysUsers.email')}>
          <input className="input" value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field label={t('sysUsers.phone')}>
          <input className="input" value={phone} onChange={(e) => setPhone(e.target.value)} />
        </Field>
        <Field label={t('sysUsers.language')}>
          <select className="input" value={language} onChange={(e) => setLanguage(e.target.value)}>
            <option value="tr">Türkçe</option>
            <option value="en">English</option>
          </select>
        </Field>
        {user ? (
          <Field label={t('common.status')}>
            <select className="input" value={status} onChange={(e) => setStatus(e.target.value)}>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>
        ) : (
          <Field label={t('sysUsers.password')} required hint={t('sysUsers.passwordHint')}>
            <input
              type="password"
              className="input"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
        )}
      </div>
    </Modal>
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function Users() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const toast = useToast()
  const queryClient = useQueryClient()

  const [page, setPage] = useState(1)
  const [term, setTerm] = useState('')
  const [roleFilter, setRoleFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [includeDeleted, setIncludeDeleted] = useState(false)

  const [formUser, setFormUser] = useState<AdminUser | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [permUser, setPermUser] = useState<AdminUser | null>(null)
  const [resetUser, setResetUser] = useState<AdminUser | null>(null)
  const [newPassword, setNewPassword] = useState('')

  const rolesQuery = useQuery({
    queryKey: ['system', 'roles'],
    queryFn: () => api.get<RoleRow[]>('/system/roles'),
    enabled: can('system.roles', 'VIEW'),
  })

  const usersQuery = useQuery({
    queryKey: ['system', 'users', page, term, roleFilter, statusFilter, includeDeleted],
    queryFn: () =>
      api.get<Paged<AdminUser>>('/system/users', {
        page,
        size: 25,
        term: term || undefined,
        role_code: roleFilter || undefined,
        status: statusFilter || undefined,
        include_deleted: includeDeleted || undefined,
      }),
  })

  const toggleStatus = useMutation({
    mutationFn: (user: AdminUser) =>
      user.status === 'ACTIVE'
        ? api.delete(`/system/users/${user.id}`)
        : api.put(`/system/users/${user.id}`, { status: 'ACTIVE' }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['system', 'users'] })
      toast.push('success', t('sysUsers.updated'))
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  const resetPassword = useMutation({
    mutationFn: () =>
      api.post(`/system/users/${resetUser?.id}/reset-password`, { new_password: newPassword }),
    onSuccess: () => {
      toast.push('success', t('sysUsers.passwordReset'))
      setResetUser(null)
      setNewPassword('')
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  const roles = rolesQuery.data ?? []
  const data = usersQuery.data

  return (
    <div>
      <PageHeader
        title={t('sysUsers.title')}
        subtitle={t('sysUsers.subtitle')}
        icon={<UsersIcon className="h-5 w-5" />}
        actions={
          can('system.users', 'CREATE') && (
            <button
              type="button"
              className="btn-primary btn-sm"
              onClick={() => {
                setFormUser(null)
                setFormOpen(true)
              }}
            >
              <UserPlus className="h-4 w-4" />
              {t('sysUsers.newUser')}
            </button>
          )
        }
      />

      <Card className="mb-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[200px] flex-1">
            <Field label={t('common.search')}>
              <input
                className="input"
                value={term}
                placeholder={t('sysUsers.searchPlaceholder')}
                onChange={(e) => {
                  setTerm(e.target.value)
                  setPage(1)
                }}
              />
            </Field>
          </div>
          <Field label={t('sysUsers.role')}>
            <select
              className="input"
              value={roleFilter}
              onChange={(e) => {
                setRoleFilter(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {roles.map((r) => (
                <option key={r.code} value={r.code}>
                  {r.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('common.status')}>
            <select
              className="input"
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>
          <label className="flex items-center gap-2 pb-2 text-xs text-shell-600">
            <input
              type="checkbox"
              checked={includeDeleted}
              onChange={(e) => {
                setIncludeDeleted(e.target.checked)
                setPage(1)
              }}
            />
            {t('sysUsers.includeDeleted')}
          </label>
        </div>
      </Card>

      <Card bodyClassName="p-0">
        {usersQuery.isLoading ? (
          <SkeletonRows rows={6} cols={6} />
        ) : usersQuery.isError ? (
          <ErrorState error={usersQuery.error} onRetry={() => void usersQuery.refetch()} />
        ) : (data?.items.length ?? 0) === 0 ? (
          <EmptyState title={t('sysUsers.noUsers')} />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('sysUsers.username')}</th>
                    <th>{t('sysUsers.fullName')}</th>
                    <th>{t('sysUsers.role')}</th>
                    <th>{t('sysUsers.dataScope')}</th>
                    <th className="text-right">{t('sysUsers.permissions')}</th>
                    <th>{t('common.status')}</th>
                    <th>{t('sysUsers.lastLogin')}</th>
                    <th className="text-right">{t('common.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {data?.items.map((user) => (
                    <tr key={user.id} className={user.is_deleted ? 'opacity-60' : ''}>
                      <td className="font-mono text-xs">{user.username}</td>
                      <td className="font-medium text-shell-800">{user.full_name}</td>
                      <td>
                        <span className="badge-info">{user.role_name ?? user.role_code}</span>
                      </td>
                      <td className="text-xs">{user.data_scope ?? '—'}</td>
                      <td className="tabular text-right">{formatNumber(user.permission_count)}</td>
                      <td>
                        <StatusBadge status={user.status} />
                      </td>
                      <td className="tabular text-xs">
                        {formatDate(user.last_login_at, { short: true, withTime: true })}
                      </td>
                      <td className="text-right">
                        <div className="inline-flex gap-1">
                          {can('system.users', 'UPDATE') && (
                            <>
                              <button
                                type="button"
                                className="btn-ghost btn-sm"
                                title={t('common.edit')}
                                onClick={() => {
                                  setFormUser(user)
                                  setFormOpen(true)
                                }}
                              >
                                <Pencil className="h-3.5 w-3.5" />
                              </button>
                              <button
                                type="button"
                                className="btn-ghost btn-sm"
                                title={t('sysUsers.resetPassword')}
                                onClick={() => setResetUser(user)}
                              >
                                <KeyRound className="h-3.5 w-3.5" />
                              </button>
                              <button
                                type="button"
                                className="btn-ghost btn-sm"
                                title={t('sysUsers.permissionEditor')}
                                onClick={() => setPermUser(user)}
                              >
                                <ShieldCheck className="h-3.5 w-3.5" />
                              </button>
                            </>
                          )}
                          {can('system.users', 'DELETE') && (
                            <button
                              type="button"
                              className="btn-ghost btn-sm"
                              title={
                                user.status === 'ACTIVE'
                                  ? t('sysUsers.deactivate')
                                  : t('sysUsers.activate')
                              }
                              onClick={() => {
                                if (
                                  user.status !== 'ACTIVE' ||
                                  window.confirm(
                                    t('sysUsers.deactivateConfirm', { name: user.full_name }),
                                  )
                                ) {
                                  toggleStatus.mutate(user)
                                }
                              }}
                            >
                              <Power
                                className={`h-3.5 w-3.5 ${
                                  user.status === 'ACTIVE' ? 'text-danger-500' : 'text-ok-600'
                                }`}
                              />
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {data && (
              <Pagination
                page={data.page}
                pages={data.pages}
                total={data.total}
                size={data.size}
                onPage={setPage}
              />
            )}
          </>
        )}
      </Card>

      {formOpen && (
        <UserForm user={formUser} roles={roles} onClose={() => setFormOpen(false)} />
      )}
      {permUser && <PermissionEditor user={permUser} onClose={() => setPermUser(null)} />}
      {resetUser && (
        <Modal
          open
          onClose={() => setResetUser(null)}
          size="sm"
          title={`${t('sysUsers.resetPassword')} — ${resetUser.full_name}`}
          footer={
            <>
              <button type="button" className="btn-secondary" onClick={() => setResetUser(null)}>
                {t('common.cancel')}
              </button>
              <button
                type="button"
                className="btn-primary"
                disabled={newPassword.length < 8 || resetPassword.isPending}
                onClick={() => resetPassword.mutate()}
              >
                {resetPassword.isPending && <Spinner />}
                {t('common.save')}
              </button>
            </>
          }
        >
          <Field label={t('sysUsers.newPassword')} required hint={t('sysUsers.passwordHint')}>
            <input
              type="password"
              className="input"
              autoComplete="new-password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
            />
          </Field>
        </Modal>
      )}
    </div>
  )
}
