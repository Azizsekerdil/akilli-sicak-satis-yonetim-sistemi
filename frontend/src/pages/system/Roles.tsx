/**
 * Roller / Roles.
 *
 * The role catalogue is defined in application code, not in the database, so
 * this screen is deliberately read-only: it shows the hierarchy (rank, data
 * scope, how many permissions and users each role carries) and, for the
 * selected role, the full resource × action matrix grouped by module.
 */
import { useQuery } from '@tanstack/react-query'
import { Lock, ShieldCheck } from 'lucide-react'
import { Fragment, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
  PageHeader,
  SkeletonRows,
} from '@/components/ui'
import { api } from '@/lib/api'
import { formatNumber } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface RoleRow {
  code: string
  name: string
  name_tr: string
  name_en: string
  rank: number
  data_scope: string
  permission_count: number
  user_count: number
  is_system: boolean
}

interface MatrixResource {
  key: string
  module: string
  name: string
  name_tr: string
  name_en: string
  actions: string[]
  is_sensitive: boolean
}

interface RoleMatrix {
  resources: MatrixResource[]
  roles: RoleRow[]
  matrix: Record<string, string[]>
}

/** Fixed column order — the same order the backend documents. */
const ACTIONS = ['VIEW', 'CREATE', 'UPDATE', 'DELETE', 'APPROVE', 'EXPORT', 'EXECUTE'] as const

const SCOPE_TONE: Record<string, string> = {
  ALL: 'badge-danger',
  REGION: 'badge-warn',
  TEAM: 'badge-info',
  OWN: 'badge-ok',
  NONE: 'badge-muted',
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function Roles() {
  const { t } = useTranslation()
  const [selected, setSelected] = useState<string | null>(null)

  const rolesQuery = useQuery({
    queryKey: ['system', 'roles'],
    queryFn: () => api.get<RoleRow[]>('/system/roles'),
  })

  const matrixQuery = useQuery({
    queryKey: ['system', 'roles', 'matrix'],
    queryFn: () => api.get<RoleMatrix>('/system/roles/matrix'),
  })

  const roles = rolesQuery.data ?? []
  const granted = useMemo(
    () => new Set(selected ? (matrixQuery.data?.matrix[selected] ?? []) : []),
    [matrixQuery.data, selected],
  )

  const modules = useMemo(() => {
    const byModule = new Map<string, MatrixResource[]>()
    for (const resource of matrixQuery.data?.resources ?? []) {
      const list = byModule.get(resource.module) ?? []
      list.push(resource)
      byModule.set(resource.module, list)
    }
    return [...byModule.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [matrixQuery.data])

  const selectedRole = roles.find((r) => r.code === selected) ?? null

  return (
    <div>
      <PageHeader
        title={t('sysRoles.title')}
        subtitle={t('sysRoles.subtitle')}
        icon={<ShieldCheck className="h-5 w-5" />}
      />

      <p className="mb-4 flex items-start gap-2 rounded-lg border border-info-200 bg-info-50 p-3 text-xs text-info-700">
        <Lock className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        {t('sysRoles.readOnlyNote')}
      </p>

      <Card bodyClassName="p-0" className="mb-4">
        {rolesQuery.isLoading ? (
          <SkeletonRows rows={6} cols={5} />
        ) : rolesQuery.isError ? (
          <ErrorState error={rolesQuery.error} onRetry={() => void rolesQuery.refetch()} />
        ) : roles.length === 0 ? (
          <EmptyState title={t('sysRoles.noRoles')} />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th className="text-right">{t('sysRoles.rank')}</th>
                  <th>{t('common.name')}</th>
                  <th>{t('common.code')}</th>
                  <th>{t('sysRoles.dataScope')}</th>
                  <th className="text-right">{t('sysRoles.permissionCount')}</th>
                  <th className="text-right">{t('sysRoles.userCount')}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {roles.map((role) => (
                  <tr
                    key={role.code}
                    className={`cursor-pointer ${
                      selected === role.code ? 'bg-brand-50' : ''
                    }`}
                    onClick={() => setSelected(role.code)}
                  >
                    <td className="tabular text-right">{role.rank}</td>
                    <td className="font-medium text-shell-800">{role.name}</td>
                    <td className="font-mono text-2xs">{role.code}</td>
                    <td>
                      <span className={SCOPE_TONE[role.data_scope] ?? 'badge-muted'}>
                        {role.data_scope}
                      </span>
                    </td>
                    <td className="tabular text-right">{formatNumber(role.permission_count)}</td>
                    <td className="tabular text-right">{formatNumber(role.user_count)}</td>
                    <td className="text-right">
                      {role.is_system && (
                        <span className="badge-muted">{t('sysRoles.systemRole')}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card
        title={
          selectedRole
            ? `${t('sysRoles.matrix')} — ${selectedRole.name}`
            : t('sysRoles.matrix')
        }
        bodyClassName="p-0"
      >
        {!selected ? (
          <EmptyState title={t('sysRoles.selectRole')} />
        ) : matrixQuery.isLoading ? (
          <LoadingBlock />
        ) : matrixQuery.isError ? (
          <ErrorState error={matrixQuery.error} onRetry={() => void matrixQuery.refetch()} />
        ) : modules.length === 0 ? (
          <EmptyState title={t('common.noData')} />
        ) : (
          <div className="table-wrap max-h-[70vh] overflow-auto">
            <table className="table">
              <thead>
                <tr>
                  <th>{t('sysRoles.resource')}</th>
                  {ACTIONS.map((action) => (
                    <th key={action} className="text-center">
                      {action}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {modules.map(([module, resources]) => (
                  <Fragment key={module}>
                    <tr>
                      <td
                        colSpan={ACTIONS.length + 1}
                        className="bg-shell-50 text-2xs font-semibold uppercase tracking-wide text-shell-500"
                      >
                        {module}
                      </td>
                    </tr>
                    {resources.map((resource) => (
                      <tr key={resource.key}>
                        <td>
                          <span className="block font-medium text-shell-800">{resource.name}</span>
                          <span className="font-mono text-2xs text-shell-400">
                            {resource.key}
                            {resource.is_sensitive ? ` · ${t('sysRoles.sensitive')}` : ''}
                          </span>
                        </td>
                        {ACTIONS.map((action) => {
                          const supported = resource.actions.includes(action)
                          const has = granted.has(`${resource.key}:${action}`)
                          return (
                            <td key={action} className="text-center">
                              {!supported ? (
                                <span className="text-shell-300">·</span>
                              ) : has ? (
                                <span className="badge-ok">✓</span>
                              ) : (
                                <span className="badge-muted">—</span>
                              )}
                            </td>
                          )
                        })}
                      </tr>
                    ))}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
