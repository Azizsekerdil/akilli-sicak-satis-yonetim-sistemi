/**
 * Yedekleme / Backup.
 *
 * List, create, verify, restore and delete backups, plus the retention and
 * schedule settings that live in the `backup` settings category.
 *
 * Restore is the only genuinely destructive action here, so it is behind two
 * deliberate steps: an explanation that a safety backup is taken first, then a
 * typed confirmation keyword.  Nothing is sent to the server until both pass.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Database, HardDriveDownload, RotateCcw, ShieldCheck, Trash2 } from 'lucide-react'
import { useState } from 'react'
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
interface BackupRow {
  id: number
  backup_type: string
  status: string
  trigger: string
  file_name: string
  file_path: string
  size_bytes: number
  checksum_sha256?: string | null
  database_engine?: string | null
  app_version?: string | null
  table_count: number
  row_count: number
  includes_files: boolean
  started_at?: string | null
  completed_at?: string | null
  duration_seconds: number
  verified_at?: string | null
  verify_message?: string | null
  restored_at?: string | null
  error_message?: string | null
  notes?: string | null
  created_at?: string | null
}

interface SettingItem {
  id: number
  category: string
  key: string
  value?: string | null
  value_type: string
  default_value?: string | null
  label: string
  description?: string | null
  is_secret: boolean
  is_editable: boolean
  requires_restart: boolean
  sort_order: number
}

interface SettingGroup {
  category: string
  label: string
  label_tr: string
  label_en: string
  items: SettingItem[]
}

const TYPES = ['FULL', 'DATABASE', 'FILES', 'SETTINGS', 'INCREMENTAL'] as const

function humanSize(bytes: number): string {
  if (!bytes) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${formatNumber(value, { decimals: unit === 0 ? 0 : 1 })} ${units[unit]}`
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function Backup() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const toast = useToast()
  const queryClient = useQueryClient()

  const [page, setPage] = useState(1)
  const [typeFilter, setTypeFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')

  const [createOpen, setCreateOpen] = useState(false)
  const [createType, setCreateType] = useState<string>('FULL')
  const [includeFiles, setIncludeFiles] = useState(false)
  const [notes, setNotes] = useState('')

  const [restoreTarget, setRestoreTarget] = useState<BackupRow | null>(null)
  const [restoreStep, setRestoreStep] = useState<1 | 2>(1)
  const [restoreConfirm, setRestoreConfirm] = useState('')

  const [retention, setRetention] = useState<Record<string, string>>({})

  const keyword = t('sysBackup.restoreKeyword')

  const backupsQuery = useQuery({
    queryKey: ['system', 'backups', page, typeFilter, statusFilter],
    queryFn: () =>
      api.get<Paged<BackupRow>>('/system/backups', {
        page,
        size: 20,
        backup_type: typeFilter || undefined,
        status: statusFilter || undefined,
      }),
  })

  const settingsQuery = useQuery({
    queryKey: ['system', 'settings'],
    queryFn: () => api.get<SettingGroup[]>('/system/settings'),
    enabled: can('system.settings', 'VIEW'),
  })

  const backupSettings = (settingsQuery.data ?? []).find((g) => g.category === 'backup')

  const create = useMutation({
    mutationFn: () =>
      api.post<BackupRow>('/system/backups', {
        backup_type: createType,
        include_files: includeFiles,
        notes: notes || null,
      }),
    onSuccess: async () => {
      setCreateOpen(false)
      setNotes('')
      await queryClient.invalidateQueries({ queryKey: ['system', 'backups'] })
      toast.push('success', t('sysBackup.created'))
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  const verify = useMutation({
    mutationFn: (id: number) => api.post<BackupRow>(`/system/backups/${id}/verify`),
    onSuccess: async (row) => {
      await queryClient.invalidateQueries({ queryKey: ['system', 'backups'] })
      toast.push(
        row.status === 'CORRUPT' ? 'error' : 'success',
        row.verify_message || t('sysBackup.verified'),
      )
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  const restore = useMutation({
    mutationFn: (id: number) => api.post<BackupRow>(`/system/backups/${id}/restore`, { confirm: true }),
    onSuccess: async () => {
      setRestoreTarget(null)
      setRestoreStep(1)
      setRestoreConfirm('')
      await queryClient.invalidateQueries({ queryKey: ['system', 'backups'] })
      toast.push('success', t('sysBackup.restored'))
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  const remove = useMutation({
    mutationFn: (id: number) => api.delete(`/system/backups/${id}`),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['system', 'backups'] })
      toast.push('success', t('sysBackup.deleted'))
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  const saveRetention = useMutation({
    mutationFn: () =>
      api.put('/system/settings', {
        items: Object.entries(retention).map(([key, value]) => ({
          category: 'backup',
          key,
          value,
        })),
      }),
    onSuccess: async () => {
      setRetention({})
      await queryClient.invalidateQueries({ queryKey: ['system', 'settings'] })
      toast.push('success', t('sysSettings.saved'))
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  const data = backupsQuery.data

  return (
    <div>
      <PageHeader
        title={t('sysBackup.title')}
        subtitle={t('sysBackup.subtitle')}
        icon={<Database className="h-5 w-5" />}
        actions={
          can('system.backup', 'CREATE') && (
            <button type="button" className="btn-primary btn-sm" onClick={() => setCreateOpen(true)}>
              <HardDriveDownload className="h-4 w-4" />
              {t('sysBackup.backupNow')}
            </button>
          )
        }
      />

      <Card className="mb-4">
        <div className="flex flex-wrap items-end gap-3">
          <Field label={t('sysBackup.type')}>
            <select
              className="input"
              value={typeFilter}
              onChange={(e) => {
                setTypeFilter(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {TYPES.map((type) => (
                <option key={type} value={type}>
                  {type}
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
              {['RUNNING', 'COMPLETED', 'FAILED', 'VERIFIED', 'CORRUPT', 'RESTORED'].map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>
        </div>
      </Card>

      <Card bodyClassName="p-0" className="mb-4">
        {backupsQuery.isLoading ? (
          <SkeletonRows rows={5} cols={7} />
        ) : backupsQuery.isError ? (
          <ErrorState error={backupsQuery.error} onRetry={() => void backupsQuery.refetch()} />
        ) : (data?.items.length ?? 0) === 0 ? (
          <EmptyState title={t('sysBackup.noBackups')} />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('sysBackup.type')}</th>
                    <th>{t('common.name')}</th>
                    <th className="text-right">{t('sysBackup.size')}</th>
                    <th>{t('sysBackup.checksum')}</th>
                    <th>{t('common.status')}</th>
                    <th>{t('sysBackup.createdAt')}</th>
                    <th>{t('sysBackup.verifiedAt')}</th>
                    <th className="text-right">{t('common.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {data?.items.map((row) => (
                    <tr key={row.id}>
                      <td>
                        <span className="badge-info">{row.backup_type}</span>
                        <span className="tabular ml-1 text-2xs text-shell-400">{row.trigger}</span>
                      </td>
                      <td>
                        <span className="block max-w-xs truncate font-medium text-shell-800">
                          {row.file_name}
                        </span>
                        <span className="tabular text-2xs text-shell-400">
                          {formatNumber(row.table_count)} {t('sysBackup.tables')} ·{' '}
                          {formatNumber(row.row_count)} {t('sysBackup.rows')}
                        </span>
                      </td>
                      <td className="tabular text-right">{humanSize(row.size_bytes)}</td>
                      <td className="font-mono text-2xs">
                        {row.checksum_sha256 ? row.checksum_sha256.slice(0, 12) : '—'}
                      </td>
                      <td>
                        <StatusBadge status={row.status} />
                        {row.error_message && (
                          <span className="mt-1 block text-2xs text-danger-600">
                            {row.error_message}
                          </span>
                        )}
                      </td>
                      <td className="tabular text-xs">
                        {formatDate(row.created_at, { short: true, withTime: true })}
                      </td>
                      <td className="tabular text-xs">
                        {formatDate(row.verified_at, { short: true, withTime: true })}
                      </td>
                      <td className="text-right">
                        <div className="inline-flex gap-1">
                          {can('system.backup', 'EXECUTE') && (
                            <>
                              <button
                                type="button"
                                className="btn-ghost btn-sm"
                                title={t('sysBackup.verify')}
                                disabled={verify.isPending}
                                onClick={() => verify.mutate(row.id)}
                              >
                                <ShieldCheck className="h-3.5 w-3.5" />
                              </button>
                              <button
                                type="button"
                                className="btn-ghost btn-sm"
                                title={t('sysBackup.restore')}
                                onClick={() => {
                                  setRestoreTarget(row)
                                  setRestoreStep(1)
                                  setRestoreConfirm('')
                                }}
                              >
                                <RotateCcw className="h-3.5 w-3.5 text-warn-600" />
                              </button>
                              <button
                                type="button"
                                className="btn-ghost btn-sm"
                                title={t('common.delete')}
                                onClick={() => {
                                  if (
                                    window.confirm(
                                      t('sysBackup.deleteConfirm', { name: row.file_name }),
                                    )
                                  ) {
                                    remove.mutate(row.id)
                                  }
                                }}
                              >
                                <Trash2 className="h-3.5 w-3.5 text-danger-500" />
                              </button>
                            </>
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

      {/* Retention & schedule */}
      <Card title={t('sysBackup.retention')}>
        {settingsQuery.isLoading ? (
          <LoadingBlock />
        ) : !backupSettings || backupSettings.items.length === 0 ? (
          <EmptyState title={t('sysBackup.settingsUnavailable')} />
        ) : (
          <>
            <p className="mb-3 text-xs text-shell-500">{t('sysBackup.retentionNote')}</p>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {backupSettings.items.map((item) => {
                const value = retention[item.key] ?? item.value ?? ''
                return (
                  <Field key={item.id} label={item.label} hint={item.description ?? undefined}>
                    {item.value_type === 'bool' ? (
                      <select
                        className="input"
                        value={String(value).toLowerCase() === 'true' ? 'true' : 'false'}
                        disabled={!item.is_editable || !can('system.settings', 'UPDATE')}
                        onChange={(e) =>
                          setRetention((prev) => ({ ...prev, [item.key]: e.target.value }))
                        }
                      >
                        <option value="true">{t('common.yes')}</option>
                        <option value="false">{t('common.no')}</option>
                      </select>
                    ) : (
                      <input
                        className="input tabular"
                        type={item.value_type === 'int' || item.value_type === 'float' ? 'number' : 'text'}
                        value={value}
                        disabled={!item.is_editable || !can('system.settings', 'UPDATE')}
                        onChange={(e) =>
                          setRetention((prev) => ({ ...prev, [item.key]: e.target.value }))
                        }
                      />
                    )}
                  </Field>
                )
              })}
            </div>
            {can('system.settings', 'UPDATE') && Object.keys(retention).length > 0 && (
              <button
                type="button"
                className="btn-primary btn-sm mt-4"
                onClick={() => saveRetention.mutate()}
                disabled={saveRetention.isPending}
              >
                {saveRetention.isPending && <Spinner />}
                {t('common.save')}
              </button>
            )}
          </>
        )}
      </Card>

      {/* Create */}
      {createOpen && (
        <Modal
          open
          onClose={() => setCreateOpen(false)}
          size="sm"
          title={t('sysBackup.backupNow')}
          footer={
            <>
              <button type="button" className="btn-secondary" onClick={() => setCreateOpen(false)}>
                {t('common.cancel')}
              </button>
              <button
                type="button"
                className="btn-primary"
                onClick={() => create.mutate()}
                disabled={create.isPending}
              >
                {create.isPending && <Spinner />}
                {create.isPending ? t('sysBackup.creating') : t('sysBackup.backupNow')}
              </button>
            </>
          }
        >
          <div className="space-y-3">
            <Field label={t('sysBackup.type')}>
              <select
                className="input"
                value={createType}
                onChange={(e) => setCreateType(e.target.value)}
              >
                {TYPES.map((type) => (
                  <option key={type} value={type}>
                    {type}
                  </option>
                ))}
              </select>
            </Field>
            <label className="flex items-center gap-2 text-sm text-shell-700">
              <input
                type="checkbox"
                checked={includeFiles}
                onChange={(e) => setIncludeFiles(e.target.checked)}
              />
              {t('sysBackup.includeFiles')}
            </label>
            <Field label={t('common.notes')}>
              <textarea
                className="input"
                rows={2}
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
              />
            </Field>
          </div>
        </Modal>
      )}

      {/* Restore — two deliberate steps */}
      {restoreTarget && (
        <Modal
          open
          onClose={() => {
            setRestoreTarget(null)
            setRestoreStep(1)
            setRestoreConfirm('')
          }}
          size="sm"
          title={
            restoreStep === 1
              ? t('sysBackup.restoreStep1Title')
              : t('sysBackup.restoreStep2Title')
          }
          footer={
            restoreStep === 1 ? (
              <>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => setRestoreTarget(null)}
                >
                  {t('common.cancel')}
                </button>
                <button type="button" className="btn-danger" onClick={() => setRestoreStep(2)}>
                  {t('sysBackup.continue')}
                </button>
              </>
            ) : (
              <>
                <button type="button" className="btn-secondary" onClick={() => setRestoreStep(1)}>
                  {t('common.back')}
                </button>
                <button
                  type="button"
                  className="btn-danger"
                  disabled={
                    restoreConfirm.trim().toLocaleUpperCase('tr-TR') !==
                      keyword.toLocaleUpperCase('tr-TR') || restore.isPending
                  }
                  onClick={() => restore.mutate(restoreTarget.id)}
                >
                  {restore.isPending && <Spinner />}
                  {t('sysBackup.restore')}
                </button>
              </>
            )
          }
        >
          {restoreStep === 1 ? (
            <p className="text-sm leading-relaxed text-shell-700">{t('sysBackup.restoreStep1')}</p>
          ) : (
            <div className="space-y-3">
              <p className="text-sm leading-relaxed text-danger-700">
                {t('sysBackup.restoreStep2', { name: restoreTarget.file_name, keyword })}
              </p>
              <input
                className="input font-mono"
                value={restoreConfirm}
                placeholder={keyword}
                onChange={(e) => setRestoreConfirm(e.target.value)}
              />
            </div>
          )}
        </Modal>
      )}
    </div>
  )
}
