/**
 * Hak Makbuzları / Human Sovereignty Protocol Receipts.
 *
 * One receipt per decision the engine was asked to make about a person.  A
 * receipt is only worth something if it answers all six questions without the
 * reader having to dig, so every row and every detail view carries:
 *
 *     ne soruldu · verdict · gerekçe · politika · zaman · itiraz yolu
 *
 * Two behaviours are load-bearing and therefore visible in the UI:
 *
 *   - A timeout or a missing policy is an explicit DENY with its own verdict
 *     code, never a silent pass.  Those verdicts get the same red treatment as
 *     a deliberate refusal, and the detail view says why.
 *   - The appeal path is shown on *every* receipt, including the allowed ones
 *     and including receipts this user cannot file an appeal for — a right the
 *     person is not told about is not a right they have.
 *
 * Receipts are append-only: an appeal never rewrites the original decision, it
 * is recorded against it, which is why the hash chain is on screen.
 *
 * Backend contract:
 *   GET  /compliance/hsp/receipts             -> Paged<HspReceipt>
 *   GET  /compliance/hsp/receipts/{id}        -> HspReceiptDetail
 *   POST /compliance/hsp/receipts/{id}/appeal -> HspReceiptDetail  { reason, contact }
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Clock,
  Eye,
  Gavel,
  Link2,
  Scale,
  Search,
  Send,
  ShieldAlert,
  ShieldCheck,
  ShieldX,
} from 'lucide-react'
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
export interface HspReceipt {
  id: number
  receipt_ref: string
  decided_at: string
  domain: string
  question_code: string
  question_tr?: string | null
  question_en?: string | null
  subject_ref?: string | null
  subject_type?: string | null
  actor?: string | null
  verdict: string
  reason_code?: string | null
  reason_tr?: string | null
  reason_en?: string | null
  policy_pack_code?: string | null
  policy_pack_version?: string | null
  policy_rule_code?: string | null
  appeal_status: string
  appeal_deadline_at?: string | null
  human_in_loop?: boolean | null
}

export interface HspReceiptDetail extends HspReceipt {
  requested_action?: string | null
  requested_resource?: string | null
  purpose_code?: string | null
  input_summary?: string | null
  policy_citation_status?: string | null
  policy_approved_by?: string | null
  evaluation_ms?: number | null
  timeout_ms?: number | null
  expires_at?: string | null
  sequence_no?: number | null
  entry_hash?: string | null
  prev_hash?: string | null
  appeal_channel?: string | null
  appeal_contact?: string | null
  appeal_submitted_at?: string | null
  appeal_reason?: string | null
  appeal_outcome?: string | null
  appeal_decided_at?: string | null
  appeal_decided_by?: string | null
}

const PAGE_SIZE = 25

const VERDICTS = [
  'ALLOW',
  'ALLOW_WITH_CONDITIONS',
  'DENY',
  'DENY_TIMEOUT',
  'DENY_NO_POLICY',
  'REVIEW_REQUIRED',
]

/** Every DENY_* variant is red.  A refusal caused by a timeout is still a
 *  refusal and must not read as a softer outcome than a deliberate one. */
const VERDICT_BADGE: Record<string, string> = {
  ALLOW: 'badge-ok',
  ALLOW_WITH_CONDITIONS: 'badge-warn',
  DENY: 'badge-danger',
  DENY_TIMEOUT: 'badge-danger',
  DENY_NO_POLICY: 'badge-danger',
  REVIEW_REQUIRED: 'badge-info',
}

const APPEAL_BADGE: Record<string, string> = {
  NONE: 'badge-muted',
  AVAILABLE: 'badge-info',
  SUBMITTED: 'badge-warn',
  IN_REVIEW: 'badge-warn',
  UPHELD: 'badge-danger',
  OVERTURNED: 'badge-ok',
  WITHDRAWN: 'badge-muted',
  EXPIRED: 'badge-muted',
}

function VerdictIcon({ verdict }: { verdict: string }) {
  if (verdict === 'ALLOW') return <ShieldCheck className="h-5 w-5 text-ok-600" />
  if (verdict === 'REVIEW_REQUIRED') return <Scale className="h-5 w-5 text-info-600" />
  if (verdict === 'ALLOW_WITH_CONDITIONS')
    return <ShieldAlert className="h-5 w-5 text-warn-600" />
  return <ShieldX className="h-5 w-5 text-danger-600" />
}

const isDenied = (verdict: string) => verdict.startsWith('DENY')

/* -------------------------------------------------------------------------- */
/* Appeal                                                                     */
/* -------------------------------------------------------------------------- */
function AppealModal({
  receipt,
  onClose,
}: {
  receipt: HspReceiptDetail | null
  onClose: () => void
}) {
  const { t } = useTranslation()
  const toast = useToast()
  const qc = useQueryClient()
  const [reason, setReason] = useState('')
  const [contact, setContact] = useState('')

  const appeal = useMutation({
    mutationFn: (payload: { reason: string; contact: string }) =>
      api.post<HspReceiptDetail>(`/compliance/hsp/receipts/${receipt?.id}/appeal`, payload),
    onSuccess: () => {
      toast.push('success', t('compliance.hsp.appealSubmitted'))
      void qc.invalidateQueries({ queryKey: ['compliance', 'hsp'] })
      setReason('')
      setContact('')
      onClose()
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof ApiError ? error.message : t('errors.generic')),
  })

  if (!receipt) return null

  return (
    <Modal
      open
      onClose={onClose}
      title={t('compliance.hsp.appealTitle')}
      footer={
        <>
          <button type="button" className="btn-secondary btn-sm" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="btn-primary btn-sm"
            disabled={reason.trim().length < 10 || appeal.isPending}
            onClick={() => appeal.mutate({ reason: reason.trim(), contact: contact.trim() })}
          >
            {appeal.isPending ? <Spinner /> : <Send className="h-4 w-4" />}
            {t('compliance.hsp.submitAppeal')}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        <p className="text-sm text-shell-700">
          {t('compliance.hsp.appealExplain', { reference: receipt.receipt_ref })}
        </p>
        {/* The appeal is a new record pointing at the receipt; the original
            decision stays exactly as it was taken. */}
        <p className="rounded-lg border border-info-200 bg-info-50 p-3 text-xs text-info-700">
          {t('compliance.hsp.appealAppendOnly')}
        </p>
        <Field
          label={t('compliance.hsp.appealReason')}
          required
          hint={t('compliance.hsp.appealReasonHint')}
        >
          <textarea
            className="input"
            rows={5}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </Field>
        <Field label={t('compliance.hsp.appealContact')} hint={t('compliance.hsp.appealContactHint')}>
          <input className="input" value={contact} onChange={(e) => setContact(e.target.value)} />
        </Field>
      </div>
    </Modal>
  )
}

/* -------------------------------------------------------------------------- */
/* Appeal path — shown on every receipt, allowed or denied                    */
/* -------------------------------------------------------------------------- */
function AppealPath({
  receipt,
  onOpenAppeal,
}: {
  receipt: HspReceiptDetail
  onOpenAppeal: () => void
}) {
  const { t } = useTranslation()
  const { can } = useAuth()
  const mayAppeal = can('compliance.dsr', 'CREATE')

  const alreadyFiled = !['NONE', 'AVAILABLE'].includes(receipt.appeal_status)
  const deadlinePassed =
    !!receipt.appeal_deadline_at && new Date(receipt.appeal_deadline_at).getTime() < Date.now()

  return (
    <div className="rounded-lg border border-brand-200 bg-brand-50/60 p-4">
      <div className="mb-2 flex items-center gap-2">
        <Gavel className="h-4 w-4 text-brand-700" />
        <p className="text-sm font-semibold text-brand-800">{t('compliance.hsp.appealPath')}</p>
      </div>
      <p className="text-xs text-shell-700">{t('compliance.hsp.appealPathBody')}</p>

      <dl className="mt-3 grid gap-2 sm:grid-cols-3">
        <div className="rounded-lg border border-shell-200 bg-white p-3">
          <dt className="text-2xs uppercase tracking-wide text-shell-400">
            {t('compliance.hsp.appealStatus')}
          </dt>
          <dd className="mt-0.5">
            <span className={APPEAL_BADGE[receipt.appeal_status] ?? 'badge-muted'}>
              {t(`compliance.appealStatus.${receipt.appeal_status}`, {
                defaultValue: receipt.appeal_status,
              })}
            </span>
          </dd>
        </div>
        <div className="rounded-lg border border-shell-200 bg-white p-3">
          <dt className="text-2xs uppercase tracking-wide text-shell-400">
            {t('compliance.hsp.appealDeadline')}
          </dt>
          <dd className="tabular mt-0.5 text-sm text-shell-800">
            {receipt.appeal_deadline_at
              ? formatDate(receipt.appeal_deadline_at, { withTime: true })
              : t('compliance.unknown')}
          </dd>
        </div>
        <div className="rounded-lg border border-shell-200 bg-white p-3">
          <dt className="text-2xs uppercase tracking-wide text-shell-400">
            {t('compliance.hsp.appealChannel')}
          </dt>
          <dd className="mt-0.5 break-words text-sm text-shell-800">
            {receipt.appeal_channel
              ? t(`compliance.appealChannel.${receipt.appeal_channel}`, {
                  defaultValue: receipt.appeal_channel,
                })
              : t('compliance.hsp.appealChannelUnknown')}
            {receipt.appeal_contact && (
              <span className="block break-all text-2xs text-shell-500">
                {receipt.appeal_contact}
              </span>
            )}
          </dd>
        </div>
      </dl>

      {alreadyFiled && (
        <div className="mt-3 rounded-lg border border-shell-200 bg-white p-3">
          <p className="text-2xs uppercase tracking-wide text-shell-400">
            {t('compliance.hsp.appealRecord')}
          </p>
          <p className="tabular mt-0.5 text-xs text-shell-600">
            {receipt.appeal_submitted_at
              ? formatDate(receipt.appeal_submitted_at, { withTime: true })
              : '—'}
            {receipt.appeal_decided_by ? ` · ${receipt.appeal_decided_by}` : ''}
            {receipt.appeal_decided_at
              ? ` · ${formatDate(receipt.appeal_decided_at, { withTime: true })}`
              : ''}
          </p>
          {receipt.appeal_reason && (
            <p className="mt-1 whitespace-pre-wrap text-xs text-shell-700">
              {receipt.appeal_reason}
            </p>
          )}
          {receipt.appeal_outcome && (
            <p className="mt-1 text-xs font-medium text-shell-800">
              {t('compliance.hsp.appealOutcome')}: {receipt.appeal_outcome}
            </p>
          )}
        </div>
      )}

      {!alreadyFiled && (
        <div className="mt-3">
          {mayAppeal && !deadlinePassed ? (
            <button type="button" className="btn-primary btn-sm" onClick={onOpenAppeal}>
              <Gavel className="h-4 w-4" />
              {t('compliance.hsp.fileAppeal')}
            </button>
          ) : (
            <p className="text-xs text-shell-600">
              {deadlinePassed
                ? t('compliance.hsp.appealDeadlinePassed')
                : t('compliance.hsp.appealOffline')}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Detail                                                                     */
/* -------------------------------------------------------------------------- */
function DetailModal({
  id,
  onClose,
  onAppeal,
}: {
  id: number | null
  onClose: () => void
  onAppeal: (receipt: HspReceiptDetail) => void
}) {
  const { t } = useTranslation()
  const lang = currentLanguage()

  const detailQuery = useQuery({
    queryKey: ['compliance', 'hsp', 'receipt', id],
    queryFn: () => api.get<HspReceiptDetail>(`/compliance/hsp/receipts/${id}`),
    enabled: id !== null,
  })

  if (id === null) return null
  const receipt = detailQuery.data

  return (
    <Modal
      open
      onClose={onClose}
      size="lg"
      title={receipt ? receipt.receipt_ref : t('compliance.hsp.receiptDetail')}
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
      ) : !receipt ? (
        <EmptyState title={t('common.noData')} />
      ) : (
        <div className="space-y-4">
          {/* 1 — what was asked */}
          <div>
            <SectionTitle>{t('compliance.hsp.whatWasAsked')}</SectionTitle>
            <div className="rounded-lg border border-shell-200 bg-shell-50 p-3">
              <p className="text-sm text-shell-800">
                {(lang === 'en' ? receipt.question_en : receipt.question_tr) ||
                  receipt.question_code}
              </p>
              <dl className="mt-2 grid gap-2 sm:grid-cols-2">
                {[
                  [t('compliance.hsp.domain'), receipt.domain],
                  [t('compliance.hsp.action'), receipt.requested_action ?? '—'],
                  [t('compliance.hsp.resource'), receipt.requested_resource ?? '—'],
                  [t('compliance.hsp.purpose'), receipt.purpose_code ?? t('compliance.unknown')],
                  [t('compliance.hsp.subject'), receipt.subject_ref ?? '—'],
                  [t('compliance.hsp.actor'), receipt.actor ?? '—'],
                ].map(([label, value]) => (
                  <div key={label}>
                    <dt className="text-2xs uppercase tracking-wide text-shell-400">{label}</dt>
                    <dd className="break-words font-mono text-xs text-shell-700">{value}</dd>
                  </div>
                ))}
              </dl>
              {receipt.input_summary && (
                <p className="mt-2 whitespace-pre-wrap text-xs text-shell-600">
                  {receipt.input_summary}
                </p>
              )}
            </div>
          </div>

          {/* 2 — verdict */}
          <div
            className={`flex items-start gap-3 rounded-lg border p-4 ${
              isDenied(receipt.verdict)
                ? 'border-danger-200 bg-danger-50 text-danger-700'
                : receipt.verdict === 'ALLOW'
                  ? 'border-ok-200 bg-ok-50 text-ok-700'
                  : 'border-warn-200 bg-warn-50 text-warn-700'
            }`}
          >
            <VerdictIcon verdict={receipt.verdict} />
            <div>
              <p className="text-sm font-semibold">
                {t(`compliance.verdict.${receipt.verdict}`, { defaultValue: receipt.verdict })}
              </p>
              {/* A timeout is spelled out so nobody reads it as "it passed". */}
              {receipt.verdict === 'DENY_TIMEOUT' && (
                <p className="mt-0.5 text-xs">
                  {t('compliance.hsp.timeoutExplain', {
                    ms: formatNumber(receipt.timeout_ms ?? 0),
                  })}
                </p>
              )}
              {receipt.verdict === 'DENY_NO_POLICY' && (
                <p className="mt-0.5 text-xs">{t('compliance.hsp.noPolicyExplain')}</p>
              )}
            </div>
          </div>

          {/* 3 — reason */}
          <div>
            <SectionTitle>{t('compliance.hsp.reason')}</SectionTitle>
            <div className="rounded-lg border border-shell-200 p-3">
              {receipt.reason_code && (
                <p className="font-mono text-2xs text-shell-500">{receipt.reason_code}</p>
              )}
              <p className="mt-0.5 whitespace-pre-wrap text-sm text-shell-700">
                {(lang === 'en' ? receipt.reason_en : receipt.reason_tr) ||
                  t('compliance.hsp.reasonMissing')}
              </p>
            </div>
          </div>

          {/* 4 — policy */}
          <div>
            <SectionTitle>{t('compliance.hsp.policy')}</SectionTitle>
            <dl className="grid gap-2 sm:grid-cols-2">
              {[
                [
                  t('compliance.hsp.policyPack'),
                  receipt.policy_pack_code
                    ? `${receipt.policy_pack_code} v${receipt.policy_pack_version ?? '?'}`
                    : t('compliance.hsp.policyMissing'),
                ],
                [t('compliance.hsp.policyRule'), receipt.policy_rule_code ?? '—'],
                [
                  t('compliance.hsp.policyCitation'),
                  receipt.policy_citation_status
                    ? t(`compliance.citationStatus.${receipt.policy_citation_status}`, {
                        defaultValue: receipt.policy_citation_status,
                      })
                    : t('compliance.unknown'),
                ],
                [
                  t('compliance.hsp.policyApprovedBy'),
                  receipt.policy_approved_by ?? t('compliance.hsp.policyUnapproved'),
                ],
              ].map(([label, value]) => (
                <div key={label} className="rounded-lg border border-shell-200 p-3">
                  <dt className="text-2xs uppercase tracking-wide text-shell-400">{label}</dt>
                  <dd className="mt-0.5 break-words text-sm text-shell-800">{value}</dd>
                </div>
              ))}
            </dl>
          </div>

          {/* 5 — time and chain position */}
          <div>
            <SectionTitle>{t('compliance.hsp.timing')}</SectionTitle>
            <dl className="grid gap-2 sm:grid-cols-3">
              {[
                [
                  t('compliance.hsp.decidedAt'),
                  formatDate(receipt.decided_at, { withTime: true }),
                ],
                [
                  t('compliance.hsp.evaluationTime'),
                  receipt.evaluation_ms !== null && receipt.evaluation_ms !== undefined
                    ? `${formatNumber(receipt.evaluation_ms)} ms`
                    : '—',
                ],
                [
                  t('compliance.hsp.expiresAt'),
                  receipt.expires_at
                    ? formatDate(receipt.expires_at, { withTime: true })
                    : t('compliance.hsp.noExpiry'),
                ],
              ].map(([label, value]) => (
                <div key={label} className="rounded-lg border border-shell-200 p-3">
                  <dt className="text-2xs uppercase tracking-wide text-shell-400">{label}</dt>
                  <dd className="tabular mt-0.5 text-sm text-shell-800">{value}</dd>
                </div>
              ))}
            </dl>
            <div className="mt-2 space-y-0.5 rounded-lg border border-shell-200 bg-shell-50 p-3">
              <p className="flex items-center gap-1.5 text-2xs uppercase tracking-wide text-shell-400">
                <Link2 className="h-3 w-3" />
                {t('compliance.hsp.chain')}
                {receipt.sequence_no !== null && receipt.sequence_no !== undefined
                  ? ` #${receipt.sequence_no}`
                  : ''}
              </p>
              <p className="break-all font-mono text-2xs text-shell-600">
                {t('compliance.hsp.entryHash')}: {receipt.entry_hash ?? '—'}
              </p>
              <p className="break-all font-mono text-2xs text-shell-400">
                {t('compliance.hsp.prevHash')}: {receipt.prev_hash ?? '—'}
              </p>
            </div>
          </div>

          {/* 6 — appeal path, always present */}
          <AppealPath receipt={receipt} onOpenAppeal={() => onAppeal(receipt)} />
        </div>
      )}
    </Modal>
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function HspReceipts() {
  const { t } = useTranslation()
  const lang = currentLanguage()

  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [verdict, setVerdict] = useState('')
  const [deniedOnly, setDeniedOnly] = useState(false)
  const [detailId, setDetailId] = useState<number | null>(null)
  const [appealing, setAppealing] = useState<HspReceiptDetail | null>(null)

  const listQuery = useQuery({
    queryKey: ['compliance', 'hsp', page, search, verdict, deniedOnly],
    queryFn: () =>
      api.get<Paged<HspReceipt>>('/compliance/hsp/receipts', {
        page,
        size: PAGE_SIZE,
        q: search || undefined,
        verdict: verdict || undefined,
        denied_only: deniedOnly ? true : undefined,
      }),
  })

  const data = listQuery.data

  return (
    <div>
      <PageHeader
        title={t('compliance.hsp.title')}
        subtitle={t('compliance.hsp.subtitle')}
        icon={<Scale className="h-5 w-5" />}
      />

      <div className="mb-4 flex items-start gap-3 rounded-lg border border-brand-200 bg-brand-50 p-4 text-brand-800">
        <Gavel className="mt-0.5 h-5 w-5 shrink-0" />
        <div>
          <p className="text-sm font-medium">{t('compliance.hsp.bannerTitle')}</p>
          <p className="mt-0.5 text-xs">{t('compliance.hsp.bannerBody')}</p>
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
                  placeholder={t('compliance.hsp.searchPlaceholder')}
                  onChange={(e) => {
                    setSearch(e.target.value)
                    setPage(1)
                  }}
                />
              </div>
            </Field>
          </div>
          <Field label={t('compliance.hsp.verdict')}>
            <select
              className="input"
              value={verdict}
              onChange={(e) => {
                setVerdict(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {VERDICTS.map((v) => (
                <option key={v} value={v}>
                  {t(`compliance.verdict.${v}`, { defaultValue: v })}
                </option>
              ))}
            </select>
          </Field>
          <label className="mb-1 flex cursor-pointer items-center gap-2 text-xs text-shell-700">
            <input
              type="checkbox"
              className="h-4 w-4 rounded border-shell-300"
              checked={deniedOnly}
              onChange={(e) => {
                setDeniedOnly(e.target.checked)
                setPage(1)
              }}
            />
            {t('compliance.hsp.deniedOnly')}
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
            title={t('compliance.hsp.empty')}
            description={t('compliance.hsp.emptyHint')}
          />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('compliance.hsp.decidedAt')}</th>
                    <th>{t('compliance.hsp.whatWasAsked')}</th>
                    <th>{t('compliance.hsp.subject')}</th>
                    <th>{t('compliance.hsp.verdict')}</th>
                    <th>{t('compliance.hsp.reason')}</th>
                    <th>{t('compliance.hsp.policy')}</th>
                    <th>{t('compliance.hsp.appealPath')}</th>
                    <th className="w-10" />
                  </tr>
                </thead>
                <tbody>
                  {data?.items.map((receipt) => (
                    <tr key={receipt.id}>
                      <td className="tabular whitespace-nowrap align-top text-xs">
                        {formatDate(receipt.decided_at, { withTime: true })}
                        <p className="font-mono text-2xs text-shell-400">{receipt.receipt_ref}</p>
                      </td>
                      <td className="align-top">
                        <p className="text-xs text-shell-800">
                          {(lang === 'en' ? receipt.question_en : receipt.question_tr) ||
                            receipt.question_code}
                        </p>
                        <p className="font-mono text-2xs text-shell-400">{receipt.domain}</p>
                      </td>
                      <td className="align-top">
                        <p className="font-mono text-xs text-shell-800">
                          {receipt.subject_ref ?? '—'}
                        </p>
                        {receipt.actor && (
                          <p className="text-2xs text-shell-400">
                            {t('compliance.hsp.actor')}: {receipt.actor}
                          </p>
                        )}
                      </td>
                      <td className="align-top">
                        <span className={VERDICT_BADGE[receipt.verdict] ?? 'badge-muted'}>
                          {t(`compliance.verdict.${receipt.verdict}`, {
                            defaultValue: receipt.verdict,
                          })}
                        </span>
                        {receipt.human_in_loop && (
                          <p className="mt-1 text-2xs text-info-600">
                            {t('compliance.hsp.humanInLoop')}
                          </p>
                        )}
                      </td>
                      <td className="align-top text-xs text-shell-600">
                        {(lang === 'en' ? receipt.reason_en : receipt.reason_tr) ||
                          receipt.reason_code || (
                            <span className="badge-warn">
                              {t('compliance.hsp.reasonMissing')}
                            </span>
                          )}
                      </td>
                      <td className="align-top text-xs">
                        {receipt.policy_pack_code ? (
                          <>
                            <span className="font-mono text-2xs text-shell-700">
                              {receipt.policy_pack_code} v{receipt.policy_pack_version ?? '?'}
                            </span>
                            <p className="font-mono text-2xs text-shell-400">
                              {receipt.policy_rule_code ?? '—'}
                            </p>
                          </>
                        ) : (
                          <span className="badge-warn">{t('compliance.hsp.policyMissing')}</span>
                        )}
                      </td>
                      <td className="align-top">
                        <span className={APPEAL_BADGE[receipt.appeal_status] ?? 'badge-muted'}>
                          {t(`compliance.appealStatus.${receipt.appeal_status}`, {
                            defaultValue: receipt.appeal_status,
                          })}
                        </span>
                        {receipt.appeal_deadline_at && (
                          <p className="tabular mt-1 flex items-center gap-1 text-2xs text-shell-400">
                            <Clock className="h-3 w-3" />
                            {formatDate(receipt.appeal_deadline_at, { short: true })}
                          </p>
                        )}
                      </td>
                      <td className="align-top">
                        <button
                          type="button"
                          className="btn-ghost btn-sm"
                          onClick={() => setDetailId(receipt.id)}
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

      <DetailModal
        id={detailId}
        onClose={() => setDetailId(null)}
        onAppeal={(receipt) => {
          // Hand the receipt over rather than stacking two dialogs; the appeal
          // form already repeats the reference the reader needs.
          setDetailId(null)
          setAppealing(receipt)
        }}
      />
      <AppealModal receipt={appealing} onClose={() => setAppealing(null)} />
    </div>
  )
}
