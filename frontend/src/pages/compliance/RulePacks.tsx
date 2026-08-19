/**
 * Kural Paketleri / Rule Packs.
 *
 * A rule pack is a versioned, checksummed set of machine-readable obligations.
 * Nothing in it takes effect because it was imported — a named person approves
 * a specific version, and that approval is what the engine binds to.
 *
 * Citations are shown with their verification state and never repaired by the
 * UI: an obligation whose source reference is UNVERIFIED or UNKNOWN says so.
 * Approving a pack that still contains unverified citations is possible, but
 * only after the approver ticks an explicit acknowledgement — the point is
 * that no one can approve one by accident and later claim they were not told.
 *
 * Backend contract:
 *   GET  /compliance/rule-packs              -> Paged<RulePack>
 *   GET  /compliance/rule-packs/{id}         -> RulePackDetail
 *   POST /compliance/rule-packs/{id}/approve -> RulePackDetail   { note, acknowledge_unverified }
 *   POST /compliance/rule-packs/{id}/reject  -> RulePackDetail   { note }
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Eye, FileWarning, ListChecks, Search, ShieldAlert, X } from 'lucide-react'
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
import { formatDate, formatNumber } from '@/lib/format'
import { currentLanguage } from '@/lib/i18n'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
export interface RulePack {
  id: number
  code: string
  name_tr?: string | null
  name_en?: string | null
  framework: string
  version: string
  status: string
  rule_count: number
  verified_citation_count: number
  unverified_citation_count: number
  checksum?: string | null
  source_kind?: string | null
  effective_from?: string | null
  approved_by?: string | null
  approved_at?: string | null
  rejected_reason?: string | null
  superseded_by_version?: string | null
}

export interface RulePackRule {
  id: number
  code: string
  title_tr?: string | null
  title_en?: string | null
  obligation_tr?: string | null
  obligation_en?: string | null
  severity: string
  citation_status: string
  citation_ref?: string | null
  citation_url?: string | null
  applies_to?: string[]
  requires_human_review?: boolean | null
}

export interface RulePackDetail extends RulePack {
  description_tr?: string | null
  description_en?: string | null
  rules?: RulePackRule[]
}

const PAGE_SIZE = 25

const PACK_STATUSES = [
  'DRAFT',
  'PENDING_APPROVAL',
  'APPROVED',
  'REJECTED',
  'SUPERSEDED',
  'ARCHIVED',
]

const PACK_BADGE: Record<string, string> = {
  APPROVED: 'badge-ok',
  PENDING_APPROVAL: 'badge-warn',
  DRAFT: 'badge-muted',
  REJECTED: 'badge-danger',
  SUPERSEDED: 'badge-info',
  ARCHIVED: 'badge-muted',
}

const CITATION_BADGE: Record<string, string> = {
  VERIFIED: 'badge-ok',
  UNVERIFIED: 'badge-warn',
  UNKNOWN: 'badge-warn',
  NOT_APPLICABLE: 'badge-muted',
}

const SEVERITY_BADGE: Record<string, string> = {
  CRITICAL: 'badge-danger',
  HIGH: 'badge-danger',
  MEDIUM: 'badge-warn',
  LOW: 'badge-info',
  INFO: 'badge-muted',
}

/* -------------------------------------------------------------------------- */
/* Detail + approval                                                          */
/* -------------------------------------------------------------------------- */
function DetailModal({ id, onClose }: { id: number | null; onClose: () => void }) {
  const { t } = useTranslation()
  const lang = currentLanguage()
  const toast = useToast()
  const qc = useQueryClient()
  const { can } = useAuth()
  const mayApprove = can('compliance.rulepacks', 'APPROVE')

  const [note, setNote] = useState('')
  const [acknowledged, setAcknowledged] = useState(false)

  const detailQuery = useQuery({
    queryKey: ['compliance', 'rulePack', id],
    queryFn: () => api.get<RulePackDetail>(`/compliance/rulepacks/${id}`),
    enabled: id !== null,
  })

  // One endpoint, two decisions.  The backend records APPROVED / REJECTED as
  // the same review action, so the four-eyes rule and the decision log live in
  // one place; posting to two different URLs would have meant two code paths
  // for a single governance step.
  const decide = useMutation({
    mutationFn: (payload: { decision: 'approve' | 'reject'; note: string; ack: boolean }) =>
      api.post<RulePackDetail>(`/compliance/rulepacks/${id}/approve`, {
        decision: payload.decision === 'approve' ? 'APPROVED' : 'REJECTED',
        comment: payload.ack
          ? `${payload.note} [acknowledged: unverified sources]`
          : payload.note,
      }),
    onSuccess: (_result, variables) => {
      toast.push(
        'success',
        variables.decision === 'approve'
          ? t('compliance.rulePacks.approved')
          : t('compliance.rulePacks.rejected'),
      )
      void qc.invalidateQueries({ queryKey: ['compliance', 'rulePacks'] })
      void qc.invalidateQueries({ queryKey: ['compliance', 'rulePack', id] })
      setNote('')
      setAcknowledged(false)
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof ApiError ? error.message : t('errors.generic')),
  })

  if (id === null) return null
  const pack = detailQuery.data
  const rules = pack?.rules ?? []
  const unverified = pack?.unverified_citation_count ?? 0
  const decidable = pack?.status === 'PENDING_APPROVAL' || pack?.status === 'DRAFT'
  const noteOk = note.trim().length >= 3
  const ackOk = unverified === 0 || acknowledged

  return (
    <Modal
      open
      onClose={onClose}
      size="xl"
      title={
        pack
          ? `${(lang === 'en' ? pack.name_en : pack.name_tr) || pack.code} · v${pack.version}`
          : t('compliance.rulePacks.packDetail')
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
      ) : !pack ? (
        <EmptyState title={t('common.noData')} />
      ) : (
        <div className="space-y-4">
          <dl className="grid gap-2 sm:grid-cols-3">
            {[
              [t('common.code'), pack.code],
              [t('compliance.rulePacks.framework'), pack.framework],
              [t('compliance.rulePacks.version'), `v${pack.version}`],
              [
                t('common.status'),
                t(`compliance.packStatus.${pack.status}`, { defaultValue: pack.status }),
              ],
              [t('compliance.rulePacks.ruleCount'), formatNumber(pack.rule_count)],
              [
                t('compliance.rulePacks.sourceKind'),
                pack.source_kind
                  ? t(`compliance.sourceKind.${pack.source_kind}`, {
                      defaultValue: pack.source_kind,
                    })
                  : t('compliance.unknown'),
              ],
              [
                t('compliance.rulePacks.effectiveFrom'),
                pack.effective_from ? formatDate(pack.effective_from) : t('compliance.unknown'),
              ],
              [
                t('compliance.rulePacks.approvedBy'),
                pack.approved_by
                  ? `${pack.approved_by}${
                      pack.approved_at
                        ? ` · ${formatDate(pack.approved_at, { withTime: true })}`
                        : ''
                    }`
                  : t('compliance.rulePacks.notApproved'),
              ],
              [
                t('compliance.rulePacks.supersededBy'),
                pack.superseded_by_version ? `v${pack.superseded_by_version}` : '—',
              ],
            ].map(([label, value]) => (
              <div key={label} className="rounded-lg border border-shell-200 p-3">
                <dt className="text-2xs uppercase tracking-wide text-shell-400">{label}</dt>
                <dd className="mt-0.5 break-words text-sm text-shell-800">{value}</dd>
              </div>
            ))}
          </dl>

          {pack.checksum && (
            <p className="break-all font-mono text-2xs text-shell-400">
              {t('compliance.rulePacks.checksum')}: {pack.checksum}
            </p>
          )}

          {unverified > 0 && (
            <div className="flex items-start gap-2.5 rounded-lg border border-warn-200 bg-warn-50 p-3 text-warn-700">
              <FileWarning className="mt-0.5 h-4 w-4 shrink-0" />
              <p className="text-xs">
                {t('compliance.rulePacks.unverifiedWarning', { count: unverified })}
              </p>
            </div>
          )}

          <div>
            <SectionTitle>{t('compliance.rulePacks.rules')}</SectionTitle>
            {rules.length === 0 ? (
              <EmptyState title={t('compliance.rulePacks.noRules')} />
            ) : (
              <div className="table-wrap rounded-lg border border-shell-200">
                <table className="table">
                  <thead>
                    <tr>
                      <th>{t('compliance.rulePacks.rule')}</th>
                      <th>{t('compliance.rulePacks.obligation')}</th>
                      <th>{t('compliance.rulePacks.severity')}</th>
                      <th>{t('compliance.rulePacks.citation')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rules.map((rule) => (
                      <tr key={rule.id}>
                        <td className="align-top">
                          <p className="font-mono text-2xs text-shell-500">{rule.code}</p>
                          <p className="text-xs font-medium text-shell-800">
                            {(lang === 'en' ? rule.title_en : rule.title_tr) || rule.code}
                          </p>
                          {rule.requires_human_review && (
                            <span className="badge-info mt-1">
                              {t('compliance.rulePacks.humanReviewRequired')}
                            </span>
                          )}
                        </td>
                        <td className="align-top text-xs text-shell-600">
                          {(lang === 'en' ? rule.obligation_en : rule.obligation_tr) || '—'}
                          {(rule.applies_to?.length ?? 0) > 0 && (
                            <div className="mt-1 flex flex-wrap gap-1">
                              {rule.applies_to?.map((scope) => (
                                <span key={scope} className="badge-muted">
                                  {scope}
                                </span>
                              ))}
                            </div>
                          )}
                        </td>
                        <td className="align-top">
                          <span className={SEVERITY_BADGE[rule.severity] ?? 'badge-muted'}>
                            {t(`compliance.severity.${rule.severity}`, {
                              defaultValue: rule.severity,
                            })}
                          </span>
                        </td>
                        <td className="align-top">
                          <span className={CITATION_BADGE[rule.citation_status] ?? 'badge-muted'}>
                            {t(`compliance.citationStatus.${rule.citation_status}`, {
                              defaultValue: rule.citation_status,
                            })}
                          </span>
                          {/* The reference is printed exactly as stored.  The UI
                              never fabricates or "tidies" a source reference. */}
                          <p className="mt-1 break-all text-2xs text-shell-500">
                            {rule.citation_ref ?? t('compliance.rulePacks.noCitation')}
                          </p>
                          {rule.citation_url && (
                            <a
                              className="break-all text-2xs text-brand-600 underline"
                              href={rule.citation_url}
                              target="_blank"
                              rel="noreferrer noopener"
                            >
                              {t('compliance.rulePacks.openSource')}
                            </a>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {mayApprove && decidable && (
            <div className="rounded-lg border border-shell-200 p-3">
              <SectionTitle>{t('compliance.rulePacks.decision')}</SectionTitle>
              <Field
                label={t('compliance.rulePacks.decisionNote')}
                required
                hint={t('compliance.rulePacks.decisionNoteHint')}
              >
                <textarea
                  className="input"
                  rows={3}
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                />
              </Field>
              {unverified > 0 && (
                <label className="mt-3 flex cursor-pointer items-start gap-2 rounded-lg border border-warn-200 bg-warn-50 p-3 text-xs text-warn-700">
                  <input
                    type="checkbox"
                    className="mt-0.5 h-4 w-4 rounded border-warn-300"
                    checked={acknowledged}
                    onChange={(e) => setAcknowledged(e.target.checked)}
                  />
                  <span>{t('compliance.rulePacks.acknowledgeUnverified', { count: unverified })}</span>
                </label>
              )}
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  className="btn-primary btn-sm"
                  disabled={!noteOk || !ackOk || decide.isPending}
                  onClick={() =>
                    decide.mutate({ decision: 'approve', note: note.trim(), ack: acknowledged })
                  }
                >
                  {decide.isPending ? <Spinner /> : <Check className="h-4 w-4" />}
                  {t('compliance.rulePacks.approve')}
                </button>
                <button
                  type="button"
                  className="btn-secondary btn-sm"
                  disabled={!noteOk || decide.isPending}
                  onClick={() =>
                    decide.mutate({ decision: 'reject', note: note.trim(), ack: false })
                  }
                >
                  <X className="h-4 w-4" />
                  {t('compliance.rulePacks.reject')}
                </button>
              </div>
            </div>
          )}

          {pack.rejected_reason && (
            <div>
              <SectionTitle>{t('compliance.rulePacks.rejectionReason')}</SectionTitle>
              <p className="whitespace-pre-wrap rounded-lg border border-danger-200 bg-danger-50 p-3 text-sm text-danger-700">
                {pack.rejected_reason}
              </p>
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
export default function RulePacks() {
  const { t } = useTranslation()
  const lang = currentLanguage()

  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('')
  const [detailId, setDetailId] = useState<number | null>(null)

  const listQuery = useQuery({
    queryKey: ['compliance', 'rulePacks', page, search, status],
    queryFn: () =>
      api.get<Paged<RulePack>>('/compliance/rulepacks', {
        page,
        size: PAGE_SIZE,
        q: search || undefined,
        status: status || undefined,
      }),
  })

  const data = listQuery.data

  return (
    <div>
      <PageHeader
        title={t('compliance.rulePacks.title')}
        subtitle={t('compliance.rulePacks.subtitle')}
        icon={<ListChecks className="h-5 w-5" />}
      />

      <div className="mb-4 flex items-start gap-3 rounded-lg border border-info-200 bg-info-50 p-4 text-info-700">
        <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0" />
        <div>
          <p className="text-sm font-medium">{t('compliance.rulePacks.approvalTitle')}</p>
          <p className="mt-0.5 text-xs">{t('compliance.rulePacks.approvalBody')}</p>
        </div>
      </div>

      <Card className="mb-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[18rem] flex-1">
            <Field label={t('common.search')}>
              <div className="relative">
                <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-shell-400" />
                <input
                  className="input pl-9"
                  value={search}
                  placeholder={t('compliance.rulePacks.searchPlaceholder')}
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
              {PACK_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {t(`compliance.packStatus.${s}`, { defaultValue: s })}
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
            title={t('compliance.rulePacks.empty')}
            description={t('compliance.rulePacks.emptyHint')}
          />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('compliance.rulePacks.pack')}</th>
                    <th>{t('compliance.rulePacks.framework')}</th>
                    <th>{t('compliance.rulePacks.version')}</th>
                    <th>{t('compliance.rulePacks.ruleCount')}</th>
                    <th>{t('compliance.rulePacks.citations')}</th>
                    <th>{t('compliance.rulePacks.approval')}</th>
                    <th>{t('common.status')}</th>
                    <th className="w-10" />
                  </tr>
                </thead>
                <tbody>
                  {data?.items.map((pack) => (
                    <tr key={pack.id}>
                      <td>
                        <p className="text-sm font-medium text-shell-800">
                          {(lang === 'en' ? pack.name_en : pack.name_tr) || pack.code}
                        </p>
                        <p className="font-mono text-2xs text-shell-400">{pack.code}</p>
                      </td>
                      <td className="text-xs">{pack.framework}</td>
                      <td className="tabular whitespace-nowrap text-xs">v{pack.version}</td>
                      <td className="tabular text-xs">{formatNumber(pack.rule_count)}</td>
                      <td>
                        <div className="flex flex-wrap items-center gap-1">
                          <span className="badge-ok">
                            {t('compliance.rulePacks.verifiedCount', {
                              count: pack.verified_citation_count,
                            })}
                          </span>
                          {pack.unverified_citation_count > 0 && (
                            <span className="badge-warn">
                              {t('compliance.rulePacks.unverifiedCount', {
                                count: pack.unverified_citation_count,
                              })}
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="text-xs">
                        {pack.approved_by ? (
                          <>
                            <span className="text-shell-800">{pack.approved_by}</span>
                            <p className="tabular text-2xs text-shell-400">
                              {pack.approved_at
                                ? formatDate(pack.approved_at, { withTime: true })
                                : '—'}
                            </p>
                          </>
                        ) : (
                          <span className="badge-muted">
                            {t('compliance.rulePacks.notApproved')}
                          </span>
                        )}
                      </td>
                      <td>
                        <span className={PACK_BADGE[pack.status] ?? 'badge-muted'}>
                          {t(`compliance.packStatus.${pack.status}`, {
                            defaultValue: pack.status,
                          })}
                        </span>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn-ghost btn-sm"
                          onClick={() => setDetailId(pack.id)}
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

      <DetailModal id={detailId} onClose={() => setDetailId(null)} />
    </div>
  )
}
