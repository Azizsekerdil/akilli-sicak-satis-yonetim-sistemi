/**
 * Aydınlatma ve Rıza / Privacy Notices and Consent.
 *
 * These are two different obligations and this screen refuses to blur them:
 * informing a person is something the controller owes regardless of consent,
 * while consent is a separate, freely given, withdrawable act.  Bundling them
 * behind one checkbox is the classic defect, so they live in separate tabs
 * with separate records, and a consent whose presented notice version is
 * unknown is flagged rather than accepted.
 *
 * Backend contract:
 *   GET  /compliance/notices                  -> Paged<PrivacyNotice>
 *   GET  /compliance/notices/{id}             -> PrivacyNoticeDetail
 *   GET  /compliance/consents                 -> Paged<ConsentRecord>
 *   POST /compliance/consents/{id}/withdraw   -> ConsentRecord
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Ban, Eye, FileText, ListChecks, Search, ShieldAlert } from 'lucide-react'
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
import { formatDate } from '@/lib/format'
import { currentLanguage } from '@/lib/i18n'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
export interface PrivacyNotice {
  id: number
  code: string
  version: string
  title_tr?: string | null
  title_en?: string | null
  purpose_code?: string | null
  channel: string
  state: string
  language: string
  published_at?: string | null
  superseded_at?: string | null
  effective_from?: string | null
  delivered_count?: number | null
  acknowledged_count?: number | null
  content_hash?: string | null
}

export interface PrivacyNoticeDetail extends PrivacyNotice {
  body_tr?: string | null
  body_en?: string | null
  data_categories?: string[]
  recipients?: string[]
  retention_summary_tr?: string | null
  retention_summary_en?: string | null
}

export interface ConsentRecord {
  id: number
  subject_ref: string
  subject_type: string
  purpose_code: string
  purpose_label_tr?: string | null
  purpose_label_en?: string | null
  state: string
  channel: string
  notice_code?: string | null
  notice_version?: string | null
  notice_shown_at?: string | null
  granted_at?: string | null
  withdrawn_at?: string | null
  expires_at?: string | null
  withdrawal_reason?: string | null
  evidence_hash?: string | null
  recorded_by?: string | null
}

const PAGE_SIZE = 25

const NOTICE_STATE_BADGE: Record<string, string> = {
  PUBLISHED: 'badge-ok',
  DRAFT: 'badge-muted',
  SUPERSEDED: 'badge-info',
  RETIRED: 'badge-muted',
  UNKNOWN: 'badge-warn',
}

const CONSENT_STATE_BADGE: Record<string, string> = {
  GRANTED: 'badge-ok',
  WITHDRAWN: 'badge-danger',
  REFUSED: 'badge-muted',
  EXPIRED: 'badge-warn',
  PENDING: 'badge-info',
  UNKNOWN: 'badge-warn',
}

const CONSENT_STATES = ['GRANTED', 'WITHDRAWN', 'REFUSED', 'EXPIRED', 'PENDING', 'UNKNOWN']

/* -------------------------------------------------------------------------- */
/* Notice detail                                                              */
/* -------------------------------------------------------------------------- */
function NoticeModal({ id, onClose }: { id: number | null; onClose: () => void }) {
  const { t } = useTranslation()
  const lang = currentLanguage()

  const detailQuery = useQuery({
    queryKey: ['compliance', 'notice', id],
    queryFn: () => api.get<PrivacyNoticeDetail>(`/compliance/notices/${id}`),
    enabled: id !== null,
  })

  if (id === null) return null
  const notice = detailQuery.data
  const body = lang === 'en' ? notice?.body_en : notice?.body_tr
  const retention = lang === 'en' ? notice?.retention_summary_en : notice?.retention_summary_tr

  return (
    <Modal
      open
      onClose={onClose}
      size="lg"
      title={
        notice
          ? `${(lang === 'en' ? notice.title_en : notice.title_tr) || notice.code} · v${notice.version}`
          : t('compliance.consents.noticeDetail')
      }
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
      ) : !notice ? (
        <EmptyState title={t('common.noData')} />
      ) : (
        <div className="space-y-4">
          <dl className="grid gap-2 sm:grid-cols-3">
            {[
              [t('common.code'), notice.code],
              [t('compliance.consents.version'), notice.version],
              [
                t('common.status'),
                t(`compliance.noticeState.${notice.state}`, { defaultValue: notice.state }),
              ],
              [t('compliance.consents.channel'), notice.channel],
              [t('compliance.consents.purpose'), notice.purpose_code ?? t('compliance.unknown')],
              [
                t('compliance.consents.publishedAt'),
                notice.published_at
                  ? formatDate(notice.published_at, { withTime: true })
                  : t('compliance.never'),
              ],
            ].map(([label, value]) => (
              <div key={label} className="rounded-lg border border-shell-200 p-3">
                <dt className="text-2xs uppercase tracking-wide text-shell-400">{label}</dt>
                <dd className="mt-0.5 break-words text-sm text-shell-800">{value}</dd>
              </div>
            ))}
          </dl>

          {(notice.data_categories?.length ?? 0) > 0 && (
            <div>
              <SectionTitle>{t('compliance.consents.dataCategories')}</SectionTitle>
              <div className="flex flex-wrap gap-1">
                {notice.data_categories?.map((c) => (
                  <span key={c} className="badge-muted">
                    {t(`compliance.dataCategory.${c}`, { defaultValue: c })}
                  </span>
                ))}
              </div>
            </div>
          )}

          {(notice.recipients?.length ?? 0) > 0 && (
            <div>
              <SectionTitle>{t('compliance.consents.recipients')}</SectionTitle>
              <div className="flex flex-wrap gap-1">
                {notice.recipients?.map((r) => (
                  <span key={r} className="badge-muted">
                    {r}
                  </span>
                ))}
              </div>
            </div>
          )}

          {retention && (
            <div>
              <SectionTitle>{t('compliance.consents.retentionSummary')}</SectionTitle>
              <p className="text-sm text-shell-700">{retention}</p>
            </div>
          )}

          <div>
            <SectionTitle>{t('compliance.consents.noticeText')}</SectionTitle>
            {body ? (
              <div className="whitespace-pre-wrap rounded-lg border border-shell-200 bg-shell-50 p-4 text-sm leading-relaxed text-shell-700">
                {body}
              </div>
            ) : (
              <p className="rounded-lg border border-warn-200 bg-warn-50 p-3 text-xs text-warn-700">
                {t('compliance.consents.noticeTextMissing')}
              </p>
            )}
          </div>

          {notice.content_hash && (
            <p className="break-all font-mono text-2xs text-shell-400">
              {t('compliance.consents.contentHash')}: {notice.content_hash}
            </p>
          )}
        </div>
      )}
    </Modal>
  )
}

/* -------------------------------------------------------------------------- */
/* Notices tab                                                                */
/* -------------------------------------------------------------------------- */
function NoticesTab() {
  const { t } = useTranslation()
  const lang = currentLanguage()
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [openNotice, setOpenNotice] = useState<number | null>(null)

  const listQuery = useQuery({
    queryKey: ['compliance', 'notices', page, search],
    queryFn: () =>
      api.get<Paged<PrivacyNotice>>('/compliance/notices', {
        page,
        size: PAGE_SIZE,
        q: search || undefined,
      }),
  })

  const data = listQuery.data

  return (
    <>
      <Card className="mb-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[18rem] flex-1">
            <Field label={t('common.search')}>
              <div className="relative">
                <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-shell-400" />
                <input
                  className="input pl-9"
                  value={search}
                  placeholder={t('compliance.consents.noticeSearchPlaceholder')}
                  onChange={(e) => {
                    setSearch(e.target.value)
                    setPage(1)
                  }}
                />
              </div>
            </Field>
          </div>
        </div>
      </Card>

      <Card bodyClassName="p-0">
        {listQuery.isLoading ? (
          <SkeletonRows rows={6} cols={6} />
        ) : listQuery.isError ? (
          <ErrorState error={listQuery.error} onRetry={() => void listQuery.refetch()} />
        ) : (data?.items.length ?? 0) === 0 ? (
          <EmptyState
            title={t('compliance.consents.noNotices')}
            description={t('compliance.consents.noNoticesHint')}
          />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('compliance.consents.notice')}</th>
                    <th>{t('compliance.consents.version')}</th>
                    <th>{t('compliance.consents.purpose')}</th>
                    <th>{t('compliance.consents.channel')}</th>
                    <th>{t('compliance.consents.publishedAt')}</th>
                    <th>{t('common.status')}</th>
                    <th className="w-10" />
                  </tr>
                </thead>
                <tbody>
                  {data?.items.map((notice) => (
                    <tr key={notice.id}>
                      <td>
                        <p className="text-sm font-medium text-shell-800">
                          {(lang === 'en' ? notice.title_en : notice.title_tr) || notice.code}
                        </p>
                        <p className="font-mono text-2xs text-shell-400">{notice.code}</p>
                      </td>
                      <td className="tabular whitespace-nowrap text-xs">v{notice.version}</td>
                      <td className="text-xs">
                        {notice.purpose_code ?? (
                          <span className="badge-warn">{t('compliance.unknown')}</span>
                        )}
                      </td>
                      <td className="text-xs">
                        {t(`compliance.channel.${notice.channel}`, {
                          defaultValue: notice.channel,
                        })}
                      </td>
                      <td className="tabular whitespace-nowrap text-xs">
                        {notice.published_at
                          ? formatDate(notice.published_at, { withTime: true })
                          : '—'}
                      </td>
                      <td>
                        <span className={NOTICE_STATE_BADGE[notice.state] ?? 'badge-muted'}>
                          {t(`compliance.noticeState.${notice.state}`, {
                            defaultValue: notice.state,
                          })}
                        </span>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn-ghost btn-sm"
                          onClick={() => setOpenNotice(notice.id)}
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

      <NoticeModal id={openNotice} onClose={() => setOpenNotice(null)} />
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Consents tab                                                               */
/* -------------------------------------------------------------------------- */
function WithdrawModal({
  consent,
  onClose,
}: {
  consent: ConsentRecord | null
  onClose: () => void
}) {
  const { t } = useTranslation()
  const toast = useToast()
  const qc = useQueryClient()
  const [reason, setReason] = useState('')

  const withdraw = useMutation({
    mutationFn: (payload: { reason: string }) =>
      api.post<ConsentRecord>(`/compliance/consents/${consent?.id}/withdraw`, payload),
    onSuccess: () => {
      toast.push('success', t('compliance.consents.withdrawDone'))
      void qc.invalidateQueries({ queryKey: ['compliance', 'consents'] })
      setReason('')
      onClose()
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof ApiError ? error.message : t('errors.generic')),
  })

  if (!consent) return null

  return (
    <Modal
      open
      onClose={onClose}
      size="sm"
      title={t('compliance.consents.withdrawTitle')}
      footer={
        <>
          <button type="button" className="btn-secondary btn-sm" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="btn-primary btn-sm"
            disabled={reason.trim().length < 3 || withdraw.isPending}
            onClick={() => withdraw.mutate({ reason: reason.trim() })}
          >
            {withdraw.isPending ? <Spinner /> : <Ban className="h-4 w-4" />}
            {t('compliance.consents.withdraw')}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        <p className="text-sm text-shell-700">
          {t('compliance.consents.withdrawExplain', {
            subject: consent.subject_ref,
            purpose: consent.purpose_code,
          })}
        </p>
        {/* Withdrawal is appended as a new state with its own timestamp; the
            original grant stays on record as evidence of what happened when. */}
        <p className="rounded-lg border border-info-200 bg-info-50 p-3 text-xs text-info-700">
          {t('compliance.consents.withdrawAppendOnly')}
        </p>
        <Field label={t('common.reason')} required>
          <textarea
            className="input"
            rows={3}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={t('compliance.consents.withdrawReasonPlaceholder')}
          />
        </Field>
      </div>
    </Modal>
  )
}

function ConsentsTab() {
  const { t } = useTranslation()
  const lang = currentLanguage()
  const { can } = useAuth()
  const mayWithdraw = can('compliance.consent', 'UPDATE')

  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [state, setState] = useState('')
  const [withdrawing, setWithdrawing] = useState<ConsentRecord | null>(null)

  const listQuery = useQuery({
    queryKey: ['compliance', 'consents', page, search, state],
    queryFn: () =>
      api.get<Paged<ConsentRecord>>('/compliance/consents', {
        page,
        size: PAGE_SIZE,
        q: search || undefined,
        state: state || undefined,
      }),
  })

  const data = listQuery.data

  return (
    <>
      <Card className="mb-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[18rem] flex-1">
            <Field label={t('common.search')}>
              <div className="relative">
                <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-shell-400" />
                <input
                  className="input pl-9"
                  value={search}
                  placeholder={t('compliance.consents.consentSearchPlaceholder')}
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
              value={state}
              onChange={(e) => {
                setState(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {CONSENT_STATES.map((s) => (
                <option key={s} value={s}>
                  {t(`compliance.consentState.${s}`, { defaultValue: s })}
                </option>
              ))}
            </select>
          </Field>
        </div>
      </Card>

      <Card bodyClassName="p-0">
        {listQuery.isLoading ? (
          <SkeletonRows rows={6} cols={7} />
        ) : listQuery.isError ? (
          <ErrorState error={listQuery.error} onRetry={() => void listQuery.refetch()} />
        ) : (data?.items.length ?? 0) === 0 ? (
          <EmptyState
            title={t('compliance.consents.noConsents')}
            description={t('compliance.consents.noConsentsHint')}
          />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('compliance.consents.subject')}</th>
                    <th>{t('compliance.consents.purpose')}</th>
                    <th>{t('compliance.consents.presentedNotice')}</th>
                    <th>{t('compliance.consents.channel')}</th>
                    <th>{t('compliance.consents.grantedAt')}</th>
                    <th>{t('common.status')}</th>
                    <th className="w-24" />
                  </tr>
                </thead>
                <tbody>
                  {data?.items.map((consent) => (
                    <tr key={consent.id}>
                      <td>
                        <p className="font-mono text-xs text-shell-800">{consent.subject_ref}</p>
                        <p className="text-2xs text-shell-400">
                          {t(`compliance.subjectType.${consent.subject_type}`, {
                            defaultValue: consent.subject_type,
                          })}
                        </p>
                      </td>
                      <td className="text-xs">
                        {(lang === 'en' ? consent.purpose_label_en : consent.purpose_label_tr) ||
                          consent.purpose_code}
                      </td>
                      <td className="text-xs">
                        {/* Consent without a recorded notice version cannot show
                            that the person was informed first — flag, never hide. */}
                        {consent.notice_code && consent.notice_version ? (
                          <>
                            <span className="font-mono text-2xs text-shell-700">
                              {consent.notice_code} v{consent.notice_version}
                            </span>
                            <p className="tabular text-2xs text-shell-400">
                              {consent.notice_shown_at
                                ? formatDate(consent.notice_shown_at, { withTime: true })
                                : '—'}
                            </p>
                          </>
                        ) : (
                          <span className="badge-warn">
                            {t('compliance.consents.noticeEvidenceMissing')}
                          </span>
                        )}
                      </td>
                      <td className="text-xs">
                        {t(`compliance.channel.${consent.channel}`, {
                          defaultValue: consent.channel,
                        })}
                      </td>
                      <td className="tabular whitespace-nowrap text-xs">
                        {consent.granted_at
                          ? formatDate(consent.granted_at, { withTime: true })
                          : '—'}
                        {consent.withdrawn_at && (
                          <p className="text-2xs text-danger-600">
                            {t('compliance.consents.withdrawnAt')}:{' '}
                            {formatDate(consent.withdrawn_at, { withTime: true })}
                          </p>
                        )}
                      </td>
                      <td>
                        <span className={CONSENT_STATE_BADGE[consent.state] ?? 'badge-muted'}>
                          {t(`compliance.consentState.${consent.state}`, {
                            defaultValue: consent.state,
                          })}
                        </span>
                      </td>
                      <td>
                        {mayWithdraw && consent.state === 'GRANTED' && (
                          <button
                            type="button"
                            className="btn-secondary btn-sm"
                            onClick={() => setWithdrawing(consent)}
                          >
                            <Ban className="h-3.5 w-3.5" />
                            {t('compliance.consents.withdraw')}
                          </button>
                        )}
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

      <WithdrawModal consent={withdrawing} onClose={() => setWithdrawing(null)} />
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
type TabKey = 'notices' | 'consents'

export default function ComplianceConsents() {
  const { t } = useTranslation()
  const [tab, setTab] = useState<TabKey>('notices')

  const tabs: { key: TabKey; label: string; icon: typeof FileText }[] = [
    { key: 'notices', label: t('compliance.consents.tabNotices'), icon: FileText },
    { key: 'consents', label: t('compliance.consents.tabConsents'), icon: ListChecks },
  ]

  return (
    <div>
      <PageHeader
        title={t('compliance.consents.title')}
        subtitle={t('compliance.consents.subtitle')}
        icon={<ListChecks className="h-5 w-5" />}
      />

      {/* The separation is the point of the screen, so it is stated on it. */}
      <div className="mb-4 flex items-start gap-3 rounded-lg border border-warn-200 bg-warn-50 p-4 text-warn-700">
        <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0" />
        <div>
          <p className="text-sm font-medium">{t('compliance.consents.separationTitle')}</p>
          <p className="mt-0.5 text-xs">{t('compliance.consents.separationBody')}</p>
        </div>
      </div>

      <div
        className="mb-4 flex flex-wrap gap-1 border-b border-shell-200"
        role="tablist"
        aria-label={t('compliance.consents.title')}
      >
        {tabs.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            onClick={() => setTab(key)}
            className={`-mb-px flex items-center gap-2 border-b-2 px-4 py-2.5 text-sm font-medium transition-colors ${
              tab === key
                ? 'border-brand-600 text-brand-700'
                : 'border-transparent text-shell-500 hover:text-shell-800'
            }`}
          >
            <Icon className="h-4 w-4" />
            {label}
          </button>
        ))}
      </div>

      {tab === 'notices' ? <NoticesTab /> : <ConsentsTab />}
    </div>
  )
}
