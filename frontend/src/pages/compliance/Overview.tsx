/**
 * Uyum Durumu / Compliance Overview.
 *
 * Deliberately score-free.  A single "92% uyumlu" number lets the reader stop
 * reading, and it averages every UNKNOWN into the same bucket as a verified
 * control — which is the exact failure this module exists to prevent.  So the
 * screen reports counts with a state beside each of them: how many categories
 * need attention, how many controls have no evidence, how many findings are
 * still waiting on a human.  A gap stays a gap until a person closes it.
 *
 * Backend contract: GET /compliance/overview
 */
import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  FileWarning,
  HelpCircle,
  Link2,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  ShieldX,
  UserCheck,
} from 'lucide-react'
import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { Card, EmptyState, ErrorState, LoadingBlock, PageHeader, Spinner } from '@/components/ui'
import { api } from '@/lib/api'
import { formatDate, formatNumber } from '@/lib/format'
import { currentLanguage } from '@/lib/i18n'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
export interface ComplianceCategoryStatus {
  key: string
  label_tr?: string | null
  label_en?: string | null
  state: string
  controls_total: number
  controls_with_evidence: number
  controls_missing_evidence: number
  controls_review_required: number
  last_assessed_at?: string | null
  note_tr?: string | null
  note_en?: string | null
}

export interface ComplianceAttentionItem {
  key: string
  severity: string
  title_tr?: string | null
  title_en?: string | null
  detail_tr?: string | null
  detail_en?: string | null
  owner?: string | null
  category_key?: string | null
}

export interface ComplianceOverviewTotals {
  categories: number
  categories_needing_attention: number
  missing_evidence: number
  pending_human_review: number
  open_requests: number
  overdue_requests: number
  unknown_findings: number
  automated_decisions_without_appeal: number
}

export interface EvidenceChainState {
  verified: boolean
  entries: number
  checked_at?: string | null
  broken_at?: number | string | null
}

export interface ComplianceOverviewResponse {
  generated_at?: string | null
  last_scan_at?: string | null
  frameworks?: string[]
  categories?: ComplianceCategoryStatus[]
  attention?: ComplianceAttentionItem[]
  totals?: Partial<ComplianceOverviewTotals> | null
  evidence_chain?: EvidenceChainState | null
}

/* -------------------------------------------------------------------------- */
/* State presentation                                                         */
/* -------------------------------------------------------------------------- */
const STATE_BADGE: Record<string, string> = {
  OK: 'badge-ok',
  ATTENTION: 'badge-warn',
  GAP: 'badge-danger',
  REVIEW_REQUIRED: 'badge-info',
  UNKNOWN: 'badge-muted',
  NOT_APPLICABLE: 'badge-muted',
}

const STATE_BORDER: Record<string, string> = {
  OK: 'border-ok-200',
  ATTENTION: 'border-warn-200',
  GAP: 'border-danger-200',
  REVIEW_REQUIRED: 'border-info-200',
  UNKNOWN: 'border-shell-200',
  NOT_APPLICABLE: 'border-shell-200',
}

const SEVERITY_BADGE: Record<string, string> = {
  CRITICAL: 'badge-danger',
  HIGH: 'badge-danger',
  MEDIUM: 'badge-warn',
  LOW: 'badge-info',
  INFO: 'badge-muted',
}

function StateIcon({ state }: { state: string }) {
  if (state === 'OK') return <CheckCircle2 className="h-5 w-5 text-ok-600" />
  if (state === 'ATTENTION') return <AlertTriangle className="h-5 w-5 text-warn-600" />
  if (state === 'GAP') return <ShieldX className="h-5 w-5 text-danger-600" />
  if (state === 'REVIEW_REQUIRED') return <UserCheck className="h-5 w-5 text-info-600" />
  return <HelpCircle className="h-5 w-5 text-shell-400" />
}

/** State label falls back to the raw code so an unmapped backend value is
 *  visible rather than silently rendered as blank (blank reads as "fine"). */
function useStateLabel() {
  const { t } = useTranslation()
  return (state: string) => t(`compliance.state.${state}`, { defaultValue: state })
}

/* -------------------------------------------------------------------------- */
/* Pieces                                                                     */
/* -------------------------------------------------------------------------- */
function CountTile({
  label,
  value,
  hint,
  icon,
  tone,
  to,
}: {
  label: string
  value: number
  hint?: string
  icon: ReactNode
  tone: 'neutral' | 'warn' | 'danger' | 'info'
  to?: string
}) {
  const toneRing = {
    neutral: 'border-shell-200',
    warn: 'border-warn-200',
    danger: 'border-danger-200',
    info: 'border-info-200',
  }[tone]
  const toneText = {
    neutral: 'text-shell-500',
    warn: 'text-warn-600',
    danger: 'text-danger-600',
    info: 'text-info-600',
  }[tone]

  const body = (
    <>
      <div className="flex items-start justify-between gap-2">
        <p className="text-xs font-medium uppercase tracking-wide text-shell-500">{label}</p>
        <span className={toneText}>{icon}</span>
      </div>
      <p className="tabular mt-1.5 text-2xl font-semibold text-shell-900">
        {formatNumber(value)}
      </p>
      {hint && <p className="mt-1 text-2xs text-shell-400">{hint}</p>}
    </>
  )

  if (to) {
    return (
      <Link to={to} className={`card border p-4 transition-colors hover:bg-shell-50 ${toneRing}`}>
        {body}
      </Link>
    )
  }
  return <div className={`card border p-4 ${toneRing}`}>{body}</div>
}

function CategoryCard({ category }: { category: ComplianceCategoryStatus }) {
  const { t } = useTranslation()
  const lang = currentLanguage()
  const stateLabel = useStateLabel()

  const label =
    (lang === 'en' ? category.label_en : category.label_tr) ||
    t(`compliance.category.${category.key}`, { defaultValue: category.key })
  const note = lang === 'en' ? category.note_en : category.note_tr
  const border = STATE_BORDER[category.state] ?? STATE_BORDER.UNKNOWN

  return (
    <div className={`card border p-4 ${border}`}>
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="flex items-start gap-2">
          <StateIcon state={category.state} />
          <p className="text-sm font-semibold text-shell-800">{label}</p>
        </div>
        <span className={STATE_BADGE[category.state] ?? 'badge-muted'}>
          {stateLabel(category.state)}
        </span>
      </div>

      {/* Counts, never a ratio bar: 8/10 evidenced is not "80% compliant". */}
      <dl className="grid grid-cols-3 gap-2 text-center">
        <div className="rounded-lg border border-shell-200 py-2">
          <dt className="text-2xs uppercase tracking-wide text-shell-400">
            {t('compliance.overview.controls')}
          </dt>
          <dd className="tabular text-base font-semibold text-shell-900">
            {formatNumber(category.controls_total)}
          </dd>
        </div>
        <div className="rounded-lg border border-shell-200 py-2">
          <dt className="text-2xs uppercase tracking-wide text-shell-400">
            {t('compliance.overview.withEvidence')}
          </dt>
          <dd className="tabular text-base font-semibold text-ok-700">
            {formatNumber(category.controls_with_evidence)}
          </dd>
        </div>
        <div className="rounded-lg border border-shell-200 py-2">
          <dt className="text-2xs uppercase tracking-wide text-shell-400">
            {t('compliance.overview.missingEvidenceShort')}
          </dt>
          <dd className="tabular text-base font-semibold text-danger-700">
            {formatNumber(category.controls_missing_evidence)}
          </dd>
        </div>
      </dl>

      {category.controls_review_required > 0 && (
        <p className="mt-2 flex items-center gap-1.5 text-2xs font-medium text-info-700">
          <UserCheck className="h-3.5 w-3.5" />
          {t('compliance.overview.awaitingReviewCount', {
            count: category.controls_review_required,
          })}
        </p>
      )}
      {note && <p className="mt-2 text-xs text-shell-600">{note}</p>}
      <p className="tabular mt-2 text-2xs text-shell-400">
        {t('compliance.overview.lastAssessed')}:{' '}
        {category.last_assessed_at
          ? formatDate(category.last_assessed_at, { withTime: true })
          : t('compliance.never')}
      </p>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function ComplianceOverview() {
  const { t } = useTranslation()
  const lang = currentLanguage()

  const overviewQuery = useQuery({
    queryKey: ['compliance', 'overview'],
    queryFn: () => api.get<ComplianceOverviewResponse>('/compliance/overview'),
  })

  const data = overviewQuery.data
  const totals = data?.totals ?? {}
  const categories = data?.categories ?? []
  const attention = data?.attention ?? []
  const chain = data?.evidence_chain ?? null

  const num = (v: number | undefined | null) => (typeof v === 'number' ? v : 0)

  return (
    <div>
      <PageHeader
        title={t('compliance.overview.title')}
        subtitle={t('compliance.overview.subtitle')}
        icon={<ShieldAlert className="h-5 w-5" />}
        actions={
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={() => void overviewQuery.refetch()}
            disabled={overviewQuery.isFetching}
          >
            {overviewQuery.isFetching ? <Spinner /> : <RefreshCw className="h-4 w-4" />}
            {t('common.refresh')}
          </button>
        }
      />

      {/* The disclaimer is part of the product, not decoration: this screen
          reports technical findings, it does not clear anyone legally. */}
      <div className="mb-4 flex items-start gap-3 rounded-lg border border-info-200 bg-info-50 p-4 text-info-700">
        <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0" />
        <div>
          <p className="text-sm font-medium">{t('compliance.disclaimerTitle')}</p>
          <p className="mt-0.5 text-xs">{t('compliance.disclaimerBody')}</p>
        </div>
      </div>

      {overviewQuery.isLoading ? (
        <LoadingBlock />
      ) : overviewQuery.isError ? (
        <ErrorState error={overviewQuery.error} onRetry={() => void overviewQuery.refetch()} />
      ) : (
        <>
          <div className="mb-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <CountTile
              label={t('compliance.overview.categoriesNeedingAttention')}
              value={num(totals.categories_needing_attention)}
              hint={t('compliance.overview.ofCategories', {
                count: num(totals.categories) || categories.length,
              })}
              icon={<AlertTriangle className="h-4 w-4" />}
              tone="warn"
            />
            <CountTile
              label={t('compliance.overview.missingEvidence')}
              value={num(totals.missing_evidence)}
              hint={t('compliance.overview.missingEvidenceHint')}
              icon={<FileWarning className="h-4 w-4" />}
              tone="danger"
            />
            <CountTile
              label={t('compliance.overview.pendingHumanReview')}
              value={num(totals.pending_human_review)}
              hint={t('compliance.overview.pendingHumanReviewHint')}
              icon={<UserCheck className="h-4 w-4" />}
              tone="info"
            />
            <CountTile
              label={t('compliance.overview.openRequests')}
              value={num(totals.open_requests)}
              hint={t('compliance.overview.overdueRequests', {
                count: num(totals.overdue_requests),
              })}
              icon={<Clock className="h-4 w-4" />}
              tone={num(totals.overdue_requests) > 0 ? 'danger' : 'neutral'}
              to="/compliance/data-subject-requests"
            />
          </div>

          <div className="mb-4 grid gap-3 sm:grid-cols-2">
            <CountTile
              label={t('compliance.overview.unknownFindings')}
              value={num(totals.unknown_findings)}
              hint={t('compliance.unknownIsNotCompliant')}
              icon={<HelpCircle className="h-4 w-4" />}
              tone={num(totals.unknown_findings) > 0 ? 'warn' : 'neutral'}
              to="/compliance/inventory"
            />
            <CountTile
              label={t('compliance.overview.decisionsWithoutAppeal')}
              value={num(totals.automated_decisions_without_appeal)}
              hint={t('compliance.overview.decisionsWithoutAppealHint')}
              icon={<ShieldX className="h-4 w-4" />}
              tone={num(totals.automated_decisions_without_appeal) > 0 ? 'danger' : 'neutral'}
              to="/compliance/hsp-receipts"
            />
          </div>

          <Card title={t('compliance.overview.categories')} className="mb-4">
            {categories.length === 0 ? (
              <EmptyState
                title={t('compliance.overview.noCategories')}
                description={t('compliance.overview.noCategoriesHint')}
              />
            ) : (
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {categories.map((category) => (
                  <CategoryCard key={category.key} category={category} />
                ))}
              </div>
            )}
          </Card>

          <div className="grid gap-4 lg:grid-cols-3">
            <Card title={t('compliance.overview.attention')} className="lg:col-span-2">
              {attention.length === 0 ? (
                <EmptyState
                  title={t('compliance.overview.noAttention')}
                  description={t('compliance.overview.noAttentionHint')}
                  icon={<CheckCircle2 className="h-6 w-6" />}
                />
              ) : (
                <ul className="space-y-3">
                  {attention.map((item) => (
                    <li
                      key={item.key}
                      className="rounded-lg border border-shell-200 p-3 hover:bg-shell-50/70"
                    >
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <p className="text-sm font-medium text-shell-800">
                          {(lang === 'en' ? item.title_en : item.title_tr) || item.key}
                        </p>
                        <div className="flex items-center gap-1.5">
                          <span className={SEVERITY_BADGE[item.severity] ?? 'badge-muted'}>
                            {t(`compliance.severity.${item.severity}`, {
                              defaultValue: item.severity,
                            })}
                          </span>
                          {item.owner && (
                            <span className="badge-muted">
                              {t(`compliance.owner.${item.owner}`, { defaultValue: item.owner })}
                            </span>
                          )}
                        </div>
                      </div>
                      <p className="mt-1 text-xs text-shell-600">
                        {(lang === 'en' ? item.detail_en : item.detail_tr) || '—'}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </Card>

            <Card title={t('compliance.overview.evidenceIntegrity')}>
              <div className="space-y-3">
                <div
                  className={`flex items-start gap-2.5 rounded-lg border p-3 ${
                    chain?.verified
                      ? 'border-ok-200 bg-ok-50 text-ok-700'
                      : 'border-warn-200 bg-warn-50 text-warn-700'
                  }`}
                >
                  {chain?.verified ? (
                    <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0" />
                  ) : (
                    <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
                  )}
                  <p className="text-xs font-medium">
                    {chain
                      ? chain.verified
                        ? t('compliance.overview.chainVerified', { count: chain.entries })
                        : t('compliance.overview.chainBroken', {
                            id: String(chain.broken_at ?? '?'),
                          })
                      : t('compliance.overview.chainUnknown')}
                  </p>
                </div>
                <dl className="space-y-2 text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <dt className="text-shell-500">{t('compliance.overview.chainEntries')}</dt>
                    <dd className="tabular font-medium text-shell-800">
                      {chain ? formatNumber(chain.entries) : '—'}
                    </dd>
                  </div>
                  <div className="flex items-center justify-between gap-2">
                    <dt className="text-shell-500">{t('compliance.overview.chainCheckedAt')}</dt>
                    <dd className="tabular font-medium text-shell-800">
                      {chain?.checked_at
                        ? formatDate(chain.checked_at, { withTime: true })
                        : t('compliance.never')}
                    </dd>
                  </div>
                  <div className="flex items-center justify-between gap-2">
                    <dt className="text-shell-500">{t('compliance.overview.lastScan')}</dt>
                    <dd className="tabular font-medium text-shell-800">
                      {data?.last_scan_at
                        ? formatDate(data.last_scan_at, { withTime: true })
                        : t('compliance.never')}
                    </dd>
                  </div>
                  <div className="flex items-center justify-between gap-2">
                    <dt className="text-shell-500">{t('compliance.overview.generatedAt')}</dt>
                    <dd className="tabular font-medium text-shell-800">
                      {data?.generated_at
                        ? formatDate(data.generated_at, { withTime: true })
                        : '—'}
                    </dd>
                  </div>
                </dl>
                {(data?.frameworks?.length ?? 0) > 0 && (
                  <div>
                    <p className="mb-1.5 text-2xs uppercase tracking-wide text-shell-400">
                      {t('compliance.overview.frameworks')}
                    </p>
                    <div className="flex flex-wrap gap-1">
                      {data?.frameworks?.map((framework) => (
                        <span key={framework} className="badge-muted">
                          <Link2 className="h-3 w-3" />
                          {framework}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </Card>
          </div>
        </>
      )}
    </div>
  )
}
