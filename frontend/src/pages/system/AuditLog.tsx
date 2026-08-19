/**
 * Denetim Kaydı / Audit Log.
 *
 * Every write in the system lands here with a checksum that chains to the
 * previous record, so tampering is detectable rather than merely discouraged.
 * "Verify chain" walks that chain server-side and reports the first break.
 *
 * The backend has no CSV endpoint for audit rows, so export is built from the
 * page currently loaded — the header says as much by exporting exactly the
 * filtered rows on screen.
 */
import { useMutation, useQuery } from '@tanstack/react-query'
import { Download, FileSearch, ShieldCheck, ShieldX } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  Modal,
  PageHeader,
  Pagination,
  SkeletonRows,
  Spinner,
  useToast,
} from '@/components/ui'
import { api, type Paged } from '@/lib/api'
import { formatDate } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface AuditRow {
  id: number
  action: string
  entity_type?: string | null
  entity_id?: number | null
  entity_label?: string | null
  user_id?: number | null
  username?: string | null
  role_code?: string | null
  ip_address?: string | null
  request_path?: string | null
  request_method?: string | null
  is_ai_action: boolean
  ai_agent_kind?: string | null
  summary?: string | null
  old_values?: Record<string, unknown> | null
  new_values?: Record<string, unknown> | null
  amount?: number | string | null
  checksum?: string | null
  created_at: string
}

interface VerifyResult {
  valid: boolean
  checked: number
  broken_at?: number | null
  reason?: string | null
}

const ACTIONS = [
  'LOGIN', 'LOGIN_FAILED', 'LOGOUT', 'CREATE', 'UPDATE', 'DELETE', 'CANCEL', 'SALE',
  'PRICE_CHANGE', 'DISCOUNT_APPLIED', 'STOCK_ADJUSTMENT', 'STOCK_VARIANCE', 'PAYMENT',
  'PERMISSION_CHANGE', 'AI_ACTION', 'BACKUP', 'RESTORE', 'EXPORT', 'SETTING_CHANGE',
] as const

const CSV_COLUMNS: (keyof AuditRow)[] = [
  'id', 'created_at', 'action', 'entity_type', 'entity_id', 'entity_label',
  'username', 'role_code', 'ip_address', 'request_method', 'request_path',
  'is_ai_action', 'summary', 'checksum',
]

function csvCell(value: unknown): string {
  if (value === null || value === undefined) return ''
  const text = typeof value === 'object' ? JSON.stringify(value) : String(value)
  return `"${text.replace(/"/g, '""')}"`
}

function ValueTable({ values }: { values: Record<string, unknown> | null | undefined }) {
  const { t } = useTranslation()
  if (!values || Object.keys(values).length === 0) {
    return <p className="text-xs text-shell-400">{t('sysAudit.noValues')}</p>
  }
  return (
    <div className="table-wrap rounded-lg border border-shell-200">
      <table className="table">
        <tbody>
          {Object.entries(values).map(([key, value]) => (
            <tr key={key}>
              <td className="w-1/3 font-mono text-2xs text-shell-500">{key}</td>
              <td className="tabular break-all text-xs">
                {value === null || value === undefined
                  ? '—'
                  : typeof value === 'object'
                    ? JSON.stringify(value)
                    : String(value)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function AuditLog() {
  const { t } = useTranslation()
  const toast = useToast()

  const [page, setPage] = useState(1)
  const [action, setAction] = useState('')
  const [entityType, setEntityType] = useState('')
  const [userId, setUserId] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [aiOnly, setAiOnly] = useState(false)
  const [detail, setDetail] = useState<AuditRow | null>(null)
  const [verifyResult, setVerifyResult] = useState<VerifyResult | null>(null)

  const logsQuery = useQuery({
    queryKey: ['system', 'audit', page, action, entityType, userId, start, end, aiOnly],
    queryFn: () =>
      api.get<Paged<AuditRow>>('/system/audit', {
        page,
        size: 50,
        action: action || undefined,
        entity_type: entityType || undefined,
        user_id: userId || undefined,
        start: start || undefined,
        end: end || undefined,
        is_ai_action: aiOnly ? true : undefined,
      }),
  })

  const verify = useMutation({
    mutationFn: () => api.get<VerifyResult>('/system/audit/verify'),
    onSuccess: (result) => {
      setVerifyResult(result)
      toast.push(
        result.valid ? 'success' : 'error',
        result.valid
          ? t('sysAudit.chainValid', { count: result.checked })
          : t('sysAudit.chainBroken', { id: result.broken_at ?? '?', reason: result.reason ?? '' }),
      )
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  const exportCsv = () => {
    const rows = logsQuery.data?.items ?? []
    if (rows.length === 0) return
    const header = CSV_COLUMNS.join(';')
    const body = rows
      .map((row) => CSV_COLUMNS.map((column) => csvCell(row[column])).join(';'))
      .join('\n')
    // Leading BOM so Excel opens the Turkish characters as UTF-8.
    const blob = new Blob([`\uFEFF${header}\n${body}`], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `audit-${new Date().toISOString().slice(0, 10)}.csv`
    link.click()
    URL.revokeObjectURL(url)
  }

  const data = logsQuery.data

  return (
    <div>
      <PageHeader
        title={t('sysAudit.title')}
        subtitle={t('sysAudit.subtitle')}
        icon={<FileSearch className="h-5 w-5" />}
        actions={
          <>
            <button
              type="button"
              className="btn-secondary btn-sm"
              onClick={exportCsv}
              disabled={(data?.items.length ?? 0) === 0}
            >
              <Download className="h-4 w-4" />
              {t('sysAudit.exportCsv')}
            </button>
            <button
              type="button"
              className="btn-primary btn-sm"
              onClick={() => verify.mutate()}
              disabled={verify.isPending}
            >
              {verify.isPending ? <Spinner /> : <ShieldCheck className="h-4 w-4" />}
              {t('sysAudit.verifyChain')}
            </button>
          </>
        }
      />

      {verifyResult && (
        <div
          className={`mb-4 flex items-start gap-3 rounded-lg border p-4 ${
            verifyResult.valid
              ? 'border-ok-200 bg-ok-50 text-ok-700'
              : 'border-danger-200 bg-danger-50 text-danger-700'
          }`}
        >
          {verifyResult.valid ? (
            <ShieldCheck className="mt-0.5 h-5 w-5 shrink-0" />
          ) : (
            <ShieldX className="mt-0.5 h-5 w-5 shrink-0" />
          )}
          <p className="text-sm font-medium">
            {verifyResult.valid
              ? t('sysAudit.chainValid', { count: verifyResult.checked })
              : t('sysAudit.chainBroken', {
                  id: verifyResult.broken_at ?? '?',
                  reason: verifyResult.reason ?? '',
                })}
          </p>
        </div>
      )}

      <Card className="mb-4">
        <div className="flex flex-wrap items-end gap-3">
          <Field label={t('sysAudit.action')}>
            <select
              className="input"
              value={action}
              onChange={(e) => {
                setAction(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {ACTIONS.map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('sysAudit.entity')}>
            <input
              className="input"
              value={entityType}
              onChange={(e) => {
                setEntityType(e.target.value)
                setPage(1)
              }}
            />
          </Field>
          <Field label={t('sysAudit.user')}>
            <input
              type="number"
              className="input tabular"
              value={userId}
              onChange={(e) => {
                setUserId(e.target.value)
                setPage(1)
              }}
            />
          </Field>
          <Field label={t('common.from')}>
            <input
              type="date"
              className="input"
              value={start}
              onChange={(e) => {
                setStart(e.target.value)
                setPage(1)
              }}
            />
          </Field>
          <Field label={t('common.to')}>
            <input
              type="date"
              className="input"
              value={end}
              onChange={(e) => {
                setEnd(e.target.value)
                setPage(1)
              }}
            />
          </Field>
          <label className="flex items-center gap-2 pb-2 text-xs text-shell-600">
            <input
              type="checkbox"
              checked={aiOnly}
              onChange={(e) => {
                setAiOnly(e.target.checked)
                setPage(1)
              }}
            />
            {t('sysAudit.aiOnly')}
          </label>
        </div>
      </Card>

      <Card bodyClassName="p-0">
        {logsQuery.isLoading ? (
          <SkeletonRows rows={8} cols={6} />
        ) : logsQuery.isError ? (
          <ErrorState error={logsQuery.error} onRetry={() => void logsQuery.refetch()} />
        ) : (data?.items.length ?? 0) === 0 ? (
          <EmptyState title={t('sysAudit.noLogs')} />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th className="text-right">#</th>
                    <th>{t('common.date')}</th>
                    <th>{t('sysAudit.action')}</th>
                    <th>{t('sysAudit.entity')}</th>
                    <th>{t('sysUsers.username')}</th>
                    <th>{t('common.summary')}</th>
                    <th>{t('sysAudit.ip')}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {data?.items.map((row) => (
                    <tr
                      key={row.id}
                      className="cursor-pointer"
                      onClick={() => setDetail(row)}
                    >
                      <td className="tabular text-right text-2xs text-shell-400">{row.id}</td>
                      <td className="tabular whitespace-nowrap text-xs">
                        {formatDate(row.created_at, { short: true, withTime: true })}
                      </td>
                      <td>
                        <span className="badge-muted">{row.action}</span>
                        {row.is_ai_action && (
                          <span className="badge-info ml-1">{t('sysAudit.aiAction')}</span>
                        )}
                      </td>
                      <td className="text-xs">
                        {row.entity_type ?? '—'}
                        {row.entity_id ? ` #${row.entity_id}` : ''}
                        {row.entity_label ? ` · ${row.entity_label}` : ''}
                      </td>
                      <td className="text-xs">{row.username ?? '—'}</td>
                      <td className="max-w-md truncate text-xs">{row.summary ?? '—'}</td>
                      <td className="tabular text-2xs text-shell-400">{row.ip_address ?? '—'}</td>
                      <td className="text-right">
                        <span className="text-2xs text-brand-600">{t('common.details')}</span>
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

      {detail && (
        <Modal
          open
          onClose={() => setDetail(null)}
          size="lg"
          title={`${t('sysAudit.detail')} #${detail.id}`}
          footer={
            <button type="button" className="btn-secondary" onClick={() => setDetail(null)}>
              {t('common.close')}
            </button>
          }
        >
          <div className="space-y-4">
            <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-3">
              {[
                [t('sysAudit.action'), detail.action],
                [t('common.date'), formatDate(detail.created_at, { withTime: true })],
                [t('sysUsers.username'), detail.username ?? '—'],
                [t('sysUsers.role'), detail.role_code ?? '—'],
                [t('sysAudit.entity'), `${detail.entity_type ?? '—'} ${detail.entity_id ?? ''}`],
                [t('sysAudit.ip'), detail.ip_address ?? '—'],
                [
                  t('sysAudit.path'),
                  `${detail.request_method ?? ''} ${detail.request_path ?? '—'}`,
                ],
                [t('sysAudit.aiAction'), detail.is_ai_action ? (detail.ai_agent_kind ?? t('common.yes')) : t('common.no')],
                [t('sysAudit.checksum'), detail.checksum ? detail.checksum.slice(0, 16) : '—'],
              ].map(([label, value]) => (
                <div key={label}>
                  <dt className="text-2xs uppercase tracking-wide text-shell-400">{label}</dt>
                  <dd className="tabular break-all font-medium text-shell-800">{value}</dd>
                </div>
              ))}
            </dl>
            {detail.summary && (
              <p className="rounded-lg bg-shell-50 p-3 text-sm text-shell-700">{detail.summary}</p>
            )}
            <div className="grid gap-4 md:grid-cols-2">
              <div>
                <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-shell-500">
                  {t('sysAudit.oldValues')}
                </h4>
                <ValueTable values={detail.old_values} />
              </div>
              <div>
                <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-shell-500">
                  {t('sysAudit.newValues')}
                </h4>
                <ValueTable values={detail.new_values} />
              </div>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}
