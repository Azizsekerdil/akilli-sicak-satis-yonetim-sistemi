/**
 * İlgili Kişi Başvuruları / Data Subject Requests.
 *
 * A request is a clock that starts the moment it arrives, so the list is built
 * around the deadline rather than around the status: an overdue request is red
 * wherever it sits in the workflow.  Identity verification is tracked as its
 * own field because answering an unverified requester is itself a disclosure.
 *
 * Every state change is recorded as an appended event with a note — the detail
 * view shows the chain, not just the latest value, so "who decided this and
 * when" survives the next person to open the record.
 *
 * Backend contract:
 *   GET  /compliance/data-subject-requests             -> Paged<DataSubjectRequest>
 *   POST /compliance/data-subject-requests             -> DataSubjectRequest
 *   GET  /compliance/data-subject-requests/{id}        -> DataSubjectRequestDetail
 *   POST /compliance/data-subject-requests/{id}/transition -> DataSubjectRequestDetail
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Clock, Eye, Inbox, Plus, Search, Send, ShieldAlert } from 'lucide-react'
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
  SectionTitle,
  SkeletonRows,
  Spinner,
  useToast,
} from '@/components/ui'
import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { daysUntil, formatDate } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
export interface DataSubjectRequest {
  id: number
  reference: string
  subject_ref: string
  subject_type: string
  request_type: string
  channel: string
  status: string
  identity_verification: string
  received_at: string
  due_at?: string | null
  closed_at?: string | null
  assignee?: string | null
  summary?: string | null
  is_overdue?: boolean | null
}

export interface DataSubjectRequestEvent {
  id: number
  at: string
  from_status?: string | null
  to_status: string
  actor?: string | null
  note?: string | null
}

export interface DataSubjectRequestDetail extends DataSubjectRequest {
  detail?: string | null
  contact?: string | null
  response_summary?: string | null
  rejection_reason?: string | null
  affected_tables?: string[]
  events?: DataSubjectRequestEvent[]
}

const PAGE_SIZE = 25

const REQUEST_TYPES = [
  'ACCESS',
  'RECTIFICATION',
  'ERASURE',
  'RESTRICTION',
  'PORTABILITY',
  'OBJECTION',
  'AUTOMATED_DECISION_REVIEW',
  'INFORMATION',
  'OTHER',
]

const STATUSES = [
  'RECEIVED',
  'IDENTITY_PENDING',
  'IN_PROGRESS',
  'AWAITING_SUBJECT',
  'COMPLETED',
  'PARTIALLY_COMPLETED',
  'REJECTED',
  'WITHDRAWN',
]

const CHANNELS = ['EMAIL', 'WEB_FORM', 'POST', 'IN_PERSON', 'PHONE', 'OTHER']

const STATUS_BADGE: Record<string, string> = {
  RECEIVED: 'badge-info',
  IDENTITY_PENDING: 'badge-warn',
  IN_PROGRESS: 'badge-info',
  AWAITING_SUBJECT: 'badge-warn',
  COMPLETED: 'badge-ok',
  PARTIALLY_COMPLETED: 'badge-warn',
  REJECTED: 'badge-danger',
  WITHDRAWN: 'badge-muted',
}

const IDENTITY_BADGE: Record<string, string> = {
  VERIFIED: 'badge-ok',
  PENDING: 'badge-warn',
  FAILED: 'badge-danger',
  NOT_REQUIRED: 'badge-muted',
  UNKNOWN: 'badge-warn',
}

/** Deadline chip.  Anything past due is red regardless of workflow status —
 *  a request that is "in progress" and three days late is still late. */
function DueBadge({ dueAt, closedAt }: { dueAt?: string | null; closedAt?: string | null }) {
  const { t } = useTranslation()
  if (!dueAt) return <span className="badge-muted">{t('compliance.unknown')}</span>
  if (closedAt) return <span className="badge-ok">{formatDate(dueAt, { short: true })}</span>

  const days = daysUntil(dueAt)
  if (days === null) return <span className="badge-muted">—</span>
  const tone = days < 0 ? 'badge-danger' : days <= 5 ? 'badge-warn' : 'badge-ok'
  return (
    <span className={tone}>
      {days < 0
        ? t('compliance.dsr.overdueBy', { count: Math.abs(days) })
        : t('compliance.dsr.dueIn', { count: days })}
    </span>
  )
}

/* -------------------------------------------------------------------------- */
/* Create                                                                     */
/* -------------------------------------------------------------------------- */
interface CreateForm {
  subject_ref: string
  subject_type: string
  request_type: string
  channel: string
  contact: string
  detail: string
}

const EMPTY_FORM: CreateForm = {
  subject_ref: '',
  subject_type: 'CUSTOMER',
  request_type: 'ACCESS',
  channel: 'EMAIL',
  contact: '',
  detail: '',
}

function CreateModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useTranslation()
  const toast = useToast()
  const qc = useQueryClient()
  const [form, setForm] = useState<CreateForm>(EMPTY_FORM)

  const create = useMutation({
    mutationFn: (payload: CreateForm) =>
      api.post<DataSubjectRequest>('/compliance/dsr', payload),
    onSuccess: (created) => {
      toast.push('success', t('compliance.dsr.created', { reference: created.reference }))
      void qc.invalidateQueries({ queryKey: ['compliance', 'dsr'] })
      setForm(EMPTY_FORM)
      onClose()
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof ApiError ? error.message : t('errors.generic')),
  })

  const set = <K extends keyof CreateForm>(key: K, value: CreateForm[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  const valid = form.subject_ref.trim().length > 1 && form.detail.trim().length > 2

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('compliance.dsr.newRequest')}
      footer={
        <>
          <button type="button" className="btn-secondary btn-sm" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="btn-primary btn-sm"
            disabled={!valid || create.isPending}
            onClick={() => create.mutate(form)}
          >
            {create.isPending ? <Spinner /> : <Plus className="h-4 w-4" />}
            {t('common.create')}
          </button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t('compliance.dsr.subject')} required hint={t('compliance.dsr.subjectHint')}>
          <input
            className="input"
            value={form.subject_ref}
            onChange={(e) => set('subject_ref', e.target.value)}
          />
        </Field>
        <Field label={t('compliance.dsr.subjectType')}>
          <select
            className="input"
            value={form.subject_type}
            onChange={(e) => set('subject_type', e.target.value)}
          >
            {['CUSTOMER', 'EMPLOYEE', 'CONTACT', 'VISITOR', 'OTHER'].map((s) => (
              <option key={s} value={s}>
                {t(`compliance.subjectType.${s}`, { defaultValue: s })}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('compliance.dsr.requestType')} required>
          <select
            className="input"
            value={form.request_type}
            onChange={(e) => set('request_type', e.target.value)}
          >
            {REQUEST_TYPES.map((r) => (
              <option key={r} value={r}>
                {t(`compliance.requestType.${r}`, { defaultValue: r })}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('compliance.dsr.channel')}>
          <select
            className="input"
            value={form.channel}
            onChange={(e) => set('channel', e.target.value)}
          >
            {CHANNELS.map((c) => (
              <option key={c} value={c}>
                {t(`compliance.channel.${c}`, { defaultValue: c })}
              </option>
            ))}
          </select>
        </Field>
        <div className="sm:col-span-2">
          <Field label={t('compliance.dsr.contact')} hint={t('compliance.dsr.contactHint')}>
            <input
              className="input"
              value={form.contact}
              onChange={(e) => set('contact', e.target.value)}
            />
          </Field>
        </div>
        <div className="sm:col-span-2">
          <Field label={t('compliance.dsr.detail')} required>
            <textarea
              className="input"
              rows={4}
              value={form.detail}
              onChange={(e) => set('detail', e.target.value)}
              placeholder={t('compliance.dsr.detailPlaceholder')}
            />
          </Field>
        </div>
        <div className="sm:col-span-2 flex items-start gap-2.5 rounded-lg border border-info-200 bg-info-50 p-3 text-info-700">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
          <p className="text-xs">{t('compliance.dsr.createNotice')}</p>
        </div>
      </div>
    </Modal>
  )
}

/* -------------------------------------------------------------------------- */
/* Detail                                                                     */
/* -------------------------------------------------------------------------- */
function DetailModal({ id, onClose }: { id: number | null; onClose: () => void }) {
  const { t } = useTranslation()
  const toast = useToast()
  const qc = useQueryClient()
  const { can } = useAuth()
  const mayUpdate = can('compliance.dsr', 'UPDATE')

  const [toStatus, setToStatus] = useState('')
  const [note, setNote] = useState('')

  const detailQuery = useQuery({
    queryKey: ['compliance', 'dsr', 'detail', id],
    queryFn: () => api.get<DataSubjectRequestDetail>(`/compliance/dsr/${id}`),
    enabled: id !== null,
  })

  const transition = useMutation({
    mutationFn: (payload: { to_status: string; note: string }) =>
      api.post<DataSubjectRequestDetail>(
        `/compliance/dsr/${id}/transition`,
        payload,
      ),
    onSuccess: () => {
      toast.push('success', t('compliance.dsr.transitionDone'))
      void qc.invalidateQueries({ queryKey: ['compliance', 'dsr'] })
      setToStatus('')
      setNote('')
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof ApiError ? error.message : t('errors.generic')),
  })

  if (id === null) return null
  const request = detailQuery.data

  return (
    <Modal
      open
      onClose={onClose}
      size="lg"
      title={request ? request.reference : t('compliance.dsr.requestDetail')}
      footer={
        <button type="button" className="btn-secondary btn-sm" onClick={onClose}>
          {t('common.close')}
        </button>
      }
    >
      {detailQuery.isLoading ? (
        <SkeletonRows rows={6} cols={1} />
      ) : detailQuery.isError ? (
        <ErrorState error={detailQuery.error} onRetry={() => void detailQuery.refetch()} />
      ) : !request ? (
        <EmptyState title={t('common.noData')} />
      ) : (
        <div className="space-y-4">
          <dl className="grid gap-2 sm:grid-cols-3">
            {[
              [t('compliance.dsr.subject'), request.subject_ref],
              [
                t('compliance.dsr.requestType'),
                t(`compliance.requestType.${request.request_type}`, {
                  defaultValue: request.request_type,
                }),
              ],
              [
                t('common.status'),
                t(`compliance.dsrStatus.${request.status}`, { defaultValue: request.status }),
              ],
              [t('compliance.dsr.receivedAt'), formatDate(request.received_at, { withTime: true })],
              [
                t('compliance.dsr.dueAt'),
                request.due_at
                  ? formatDate(request.due_at, { withTime: true })
                  : t('compliance.unknown'),
              ],
              [
                t('compliance.dsr.identity'),
                t(`compliance.identity.${request.identity_verification}`, {
                  defaultValue: request.identity_verification,
                }),
              ],
              [
                t('compliance.dsr.channel'),
                t(`compliance.channel.${request.channel}`, { defaultValue: request.channel }),
              ],
              [t('compliance.dsr.assignee'), request.assignee ?? '—'],
              [t('compliance.dsr.contact'), request.contact ?? '—'],
            ].map(([label, value]) => (
              <div key={label} className="rounded-lg border border-shell-200 p-3">
                <dt className="text-2xs uppercase tracking-wide text-shell-400">{label}</dt>
                <dd className="mt-0.5 break-words text-sm text-shell-800">{value}</dd>
              </div>
            ))}
          </dl>

          {request.detail && (
            <div>
              <SectionTitle>{t('compliance.dsr.detail')}</SectionTitle>
              <p className="whitespace-pre-wrap rounded-lg border border-shell-200 bg-shell-50 p-3 text-sm text-shell-700">
                {request.detail}
              </p>
            </div>
          )}

          {(request.affected_tables?.length ?? 0) > 0 && (
            <div>
              <SectionTitle>{t('compliance.dsr.affectedTables')}</SectionTitle>
              <div className="flex flex-wrap gap-1">
                {request.affected_tables?.map((table) => (
                  <span key={table} className="badge-muted font-mono">
                    {table}
                  </span>
                ))}
              </div>
            </div>
          )}

          {request.response_summary && (
            <div>
              <SectionTitle>{t('compliance.dsr.response')}</SectionTitle>
              <p className="whitespace-pre-wrap rounded-lg border border-ok-200 bg-ok-50 p-3 text-sm text-ok-700">
                {request.response_summary}
              </p>
            </div>
          )}

          {request.rejection_reason && (
            <div>
              <SectionTitle>{t('compliance.dsr.rejectionReason')}</SectionTitle>
              <p className="whitespace-pre-wrap rounded-lg border border-danger-200 bg-danger-50 p-3 text-sm text-danger-700">
                {request.rejection_reason}
              </p>
            </div>
          )}

          <div>
            <SectionTitle>{t('compliance.dsr.timeline')}</SectionTitle>
            {(request.events?.length ?? 0) === 0 ? (
              <p className="text-xs text-shell-400">{t('compliance.dsr.noEvents')}</p>
            ) : (
              <ol className="space-y-2">
                {request.events?.map((event) => (
                  <li
                    key={event.id}
                    className="flex gap-3 rounded-lg border border-shell-200 p-3"
                  >
                    <Clock className="mt-0.5 h-4 w-4 shrink-0 text-shell-400" />
                    <div className="min-w-0">
                      <p className="text-xs font-medium text-shell-800">
                        {event.from_status
                          ? `${t(`compliance.dsrStatus.${event.from_status}`, {
                              defaultValue: event.from_status,
                            })} → `
                          : ''}
                        {t(`compliance.dsrStatus.${event.to_status}`, {
                          defaultValue: event.to_status,
                        })}
                      </p>
                      <p className="tabular text-2xs text-shell-400">
                        {formatDate(event.at, { withTime: true })}
                        {event.actor ? ` · ${event.actor}` : ''}
                      </p>
                      {event.note && (
                        <p className="mt-1 break-words text-xs text-shell-600">{event.note}</p>
                      )}
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </div>

          {mayUpdate && (
            <div className="rounded-lg border border-shell-200 p-3">
              <SectionTitle>{t('compliance.dsr.changeStatus')}</SectionTitle>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label={t('compliance.dsr.newStatus')} required>
                  <select
                    className="input"
                    value={toStatus}
                    onChange={(e) => setToStatus(e.target.value)}
                  >
                    <option value="">{t('common.select')}</option>
                    {STATUSES.filter((s) => s !== request.status).map((s) => (
                      <option key={s} value={s}>
                        {t(`compliance.dsrStatus.${s}`, { defaultValue: s })}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field
                  label={t('common.notes')}
                  required
                  hint={t('compliance.dsr.transitionNoteHint')}
                >
                  <input
                    className="input"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                  />
                </Field>
              </div>
              <button
                type="button"
                className="btn-primary btn-sm mt-3"
                disabled={!toStatus || note.trim().length < 3 || transition.isPending}
                onClick={() => transition.mutate({ to_status: toStatus, note: note.trim() })}
              >
                {transition.isPending ? <Spinner /> : <Send className="h-4 w-4" />}
                {t('compliance.dsr.applyTransition')}
              </button>
            </div>
          )}
        </div>
      )}
    </Modal>
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function DataSubjectRequests() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const mayCreate = can('compliance.dsr', 'CREATE')

  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('')
  const [requestType, setRequestType] = useState('')
  const [overdueOnly, setOverdueOnly] = useState(false)
  const [creating, setCreating] = useState(false)
  const [detailId, setDetailId] = useState<number | null>(null)

  const listQuery = useQuery({
    queryKey: ['compliance', 'dsr', page, search, status, requestType, overdueOnly],
    queryFn: () =>
      api.get<Paged<DataSubjectRequest>>('/compliance/dsr', {
        page,
        size: PAGE_SIZE,
        q: search || undefined,
        status: status || undefined,
        request_type: requestType || undefined,
        overdue_only: overdueOnly ? true : undefined,
      }),
  })

  const data = listQuery.data

  return (
    <div>
      <PageHeader
        title={t('compliance.dsr.title')}
        subtitle={t('compliance.dsr.subtitle')}
        icon={<Inbox className="h-5 w-5" />}
        actions={
          mayCreate ? (
            <button type="button" className="btn-primary btn-sm" onClick={() => setCreating(true)}>
              <Plus className="h-4 w-4" />
              {t('compliance.dsr.newRequest')}
            </button>
          ) : undefined
        }
      />

      <div className="mb-4 flex items-start gap-3 rounded-lg border border-info-200 bg-info-50 p-4 text-info-700">
        <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0" />
        <div>
          <p className="text-sm font-medium">{t('compliance.dsr.identityTitle')}</p>
          <p className="mt-0.5 text-xs">{t('compliance.dsr.identityBody')}</p>
        </div>
      </div>

      <Card className="mb-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[16rem] flex-1">
            <Field label={t('common.search')}>
              <div className="relative">
                <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-shell-400" />
                <input
                  className="input pl-9"
                  value={search}
                  placeholder={t('compliance.dsr.searchPlaceholder')}
                  onChange={(e) => {
                    setSearch(e.target.value)
                    setPage(1)
                  }}
                />
              </div>
            </Field>
          </div>
          <Field label={t('common.status')}>
            <select
              className="input"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {t(`compliance.dsrStatus.${s}`, { defaultValue: s })}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('compliance.dsr.requestType')}>
            <select
              className="input"
              value={requestType}
              onChange={(e) => {
                setRequestType(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {REQUEST_TYPES.map((r) => (
                <option key={r} value={r}>
                  {t(`compliance.requestType.${r}`, { defaultValue: r })}
                </option>
              ))}
            </select>
          </Field>
          <label className="mb-1 flex cursor-pointer items-center gap-2 text-xs text-shell-700">
            <input
              type="checkbox"
              className="h-4 w-4 rounded border-shell-300"
              checked={overdueOnly}
              onChange={(e) => {
                setOverdueOnly(e.target.checked)
                setPage(1)
              }}
            />
            {t('compliance.dsr.overdueOnly')}
          </label>
        </div>
      </Card>

      <Card bodyClassName="p-0">
        {listQuery.isLoading ? (
          <SkeletonRows rows={8} cols={7} />
        ) : listQuery.isError ? (
          <ErrorState error={listQuery.error} onRetry={() => void listQuery.refetch()} />
        ) : (data?.items.length ?? 0) === 0 ? (
          <EmptyState
            title={t('compliance.dsr.empty')}
            description={t('compliance.dsr.emptyHint')}
          />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('compliance.dsr.reference')}</th>
                    <th>{t('compliance.dsr.subject')}</th>
                    <th>{t('compliance.dsr.requestType')}</th>
                    <th>{t('compliance.dsr.receivedAt')}</th>
                    <th>{t('compliance.dsr.deadline')}</th>
                    <th>{t('compliance.dsr.identity')}</th>
                    <th>{t('common.status')}</th>
                    <th className="w-10" />
                  </tr>
                </thead>
                <tbody>
                  {data?.items.map((request) => (
                    <tr key={request.id}>
                      <td className="font-mono text-xs text-shell-800">{request.reference}</td>
                      <td>
                        <p className="font-mono text-xs text-shell-800">{request.subject_ref}</p>
                        <p className="text-2xs text-shell-400">
                          {t(`compliance.subjectType.${request.subject_type}`, {
                            defaultValue: request.subject_type,
                          })}
                        </p>
                      </td>
                      <td className="text-xs">
                        {t(`compliance.requestType.${request.request_type}`, {
                          defaultValue: request.request_type,
                        })}
                      </td>
                      <td className="tabular whitespace-nowrap text-xs">
                        {formatDate(request.received_at, { withTime: true })}
                      </td>
                      <td>
                        <DueBadge dueAt={request.due_at} closedAt={request.closed_at} />
                      </td>
                      <td>
                        <span
                          className={
                            IDENTITY_BADGE[request.identity_verification] ?? 'badge-muted'
                          }
                        >
                          {t(`compliance.identity.${request.identity_verification}`, {
                            defaultValue: request.identity_verification,
                          })}
                        </span>
                      </td>
                      <td>
                        <span className={STATUS_BADGE[request.status] ?? 'badge-muted'}>
                          {t(`compliance.dsrStatus.${request.status}`, {
                            defaultValue: request.status,
                          })}
                        </span>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn-ghost btn-sm"
                          onClick={() => setDetailId(request.id)}
                          aria-label={t('common.details')}
                        >
                          <Eye className="h-4 w-4" />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination
              page={data?.page ?? 1}
              pages={data?.pages ?? 1}
              total={data?.total ?? 0}
              size={data?.size ?? PAGE_SIZE}
              onPage={setPage}
            />
          </>
        )}
      </Card>

      <CreateModal open={creating} onClose={() => setCreating(false)} />
      <DetailModal id={detailId} onClose={() => setDetailId(null)} />
    </div>
  )
}
