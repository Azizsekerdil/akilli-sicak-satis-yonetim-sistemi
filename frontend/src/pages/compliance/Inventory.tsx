/**
 * Kişisel Veri Envanteri / Personal Data Inventory.
 *
 * One row per (table, column) the discovery scanner classified as personal
 * data, plus whatever a reviewer has since recorded against it.  Two rules
 * shape the whole screen:
 *
 *   1. A field whose lawful basis, retention or cross-border status is UNKNOWN
 *      is rendered as UNKNOWN — never folded into "fine".  The default filter
 *      is therefore biased towards what still needs a human.
 *   2. Nothing here is a legal conclusion.  The scanner produces *candidates*;
 *      classification is a person's decision and stays visible as such.
 *
 * Backend contract: GET /compliance/inventory  ->  Paged<InventoryField>
 */
import { useQuery } from '@tanstack/react-query'
import {
  Database,
  Download,
  Eye,
  Fingerprint,
  HelpCircle,
  MapPin,
  Search,
  ShieldAlert,
} from 'lucide-react'
import { useMemo, useState } from 'react'
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
} from '@/components/ui'
import { api, type Paged } from '@/lib/api'
import { formatDate, formatNumber } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
export interface InventoryField {
  id: number
  table_name: string
  column_name: string
  data_category: string
  identifiability: string
  purpose_code?: string | null
  lawful_basis: string
  retention_rule?: string | null
  retention_days?: number | null
  masking: string
  cross_border: string
  review_status: string
  source: string
  evidence_ref?: string | null
  detected_at?: string | null
  reviewed_at?: string | null
  reviewed_by?: string | null
  note?: string | null
}

interface InventorySummary {
  tables: number
  fields: number
  direct_identifiers: number
  location_fields: number
  special_category_candidates: number
  unknown_lawful_basis: number
  unknown_retention: number
  review_required: number
}

const PAGE_SIZE = 50

/** Kept in sync with the scanner's vocabulary.  An unmapped value still renders
 *  (raw code + muted badge) rather than disappearing from the filter. */
const DATA_CATEGORIES = [
  'DIRECT_IDENTIFIER',
  'PERSONAL',
  'SPECIAL_CATEGORY_CANDIDATE',
  'LOCATION',
  'CREDENTIAL',
  'UNKNOWN',
]
const LAWFUL_BASES = [
  'CONSENT',
  'CONTRACT',
  'LEGAL_OBLIGATION',
  'VITAL_INTERESTS',
  'PUBLIC_TASK',
  'LEGITIMATE_INTERESTS',
  'UNKNOWN',
]
const REVIEW_STATUSES = ['REVIEW_REQUIRED', 'IN_REVIEW', 'ACCEPTED', 'REJECTED', 'UNKNOWN']

const CATEGORY_BADGE: Record<string, string> = {
  DIRECT_IDENTIFIER: 'badge-danger',
  SPECIAL_CATEGORY_CANDIDATE: 'badge-danger',
  LOCATION: 'badge-warn',
  CREDENTIAL: 'badge-danger',
  PERSONAL: 'badge-info',
  UNKNOWN: 'badge-muted',
}

const REVIEW_BADGE: Record<string, string> = {
  ACCEPTED: 'badge-ok',
  IN_REVIEW: 'badge-info',
  REVIEW_REQUIRED: 'badge-warn',
  REJECTED: 'badge-danger',
  UNKNOWN: 'badge-muted',
}

/** UNKNOWN must read as a gap, not as a neutral blank. */
function unknownTone(value: string | null | undefined): string {
  return !value || value === 'UNKNOWN' ? 'badge-warn' : 'badge-muted'
}

function CategoryIcon({ category }: { category: string }) {
  if (category === 'LOCATION') return <MapPin className="h-3.5 w-3.5" />
  if (category === 'DIRECT_IDENTIFIER' || category === 'CREDENTIAL')
    return <Fingerprint className="h-3.5 w-3.5" />
  if (category === 'UNKNOWN') return <HelpCircle className="h-3.5 w-3.5" />
  return <Database className="h-3.5 w-3.5" />
}

/* -------------------------------------------------------------------------- */
/* Detail                                                                     */
/* -------------------------------------------------------------------------- */
function DetailModal({ row, onClose }: { row: InventoryField | null; onClose: () => void }) {
  const { t } = useTranslation()
  if (!row) return null

  const rows: [string, string][] = [
    [t('compliance.inventory.table'), row.table_name],
    [t('compliance.inventory.column'), row.column_name],
    [
      t('compliance.inventory.category'),
      t(`compliance.dataCategory.${row.data_category}`, { defaultValue: row.data_category }),
    ],
    [
      t('compliance.inventory.identifiability'),
      t(`compliance.identifiability.${row.identifiability}`, {
        defaultValue: row.identifiability,
      }),
    ],
    [t('compliance.inventory.purpose'), row.purpose_code ?? t('compliance.unknown')],
    [
      t('compliance.inventory.lawfulBasis'),
      t(`compliance.lawfulBasis.${row.lawful_basis}`, { defaultValue: row.lawful_basis }),
    ],
    [
      t('compliance.inventory.retention'),
      row.retention_rule
        ? row.retention_days
          ? `${row.retention_rule} · ${formatNumber(row.retention_days)} ${t('compliance.days')}`
          : row.retention_rule
        : t('compliance.unknown'),
    ],
    [
      t('compliance.inventory.masking'),
      t(`compliance.masking.${row.masking}`, { defaultValue: row.masking }),
    ],
    [
      t('compliance.inventory.crossBorder'),
      t(`compliance.crossBorder.${row.cross_border}`, { defaultValue: row.cross_border }),
    ],
    [
      t('compliance.inventory.source'),
      t(`compliance.inventorySource.${row.source}`, { defaultValue: row.source }),
    ],
    [t('compliance.inventory.evidenceRef'), row.evidence_ref ?? '—'],
    [
      t('compliance.inventory.detectedAt'),
      row.detected_at ? formatDate(row.detected_at, { withTime: true }) : '—',
    ],
    [
      t('compliance.inventory.reviewedAt'),
      row.reviewed_at
        ? `${formatDate(row.reviewed_at, { withTime: true })}${
            row.reviewed_by ? ` · ${row.reviewed_by}` : ''
          }`
        : t('compliance.never'),
    ],
  ]

  return (
    <Modal
      open
      onClose={onClose}
      title={`${row.table_name}.${row.column_name}`}
      footer={
        <button type="button" className="btn-secondary btn-sm" onClick={onClose}>
          {t('common.close')}
        </button>
      }
    >
      <div className="space-y-4">
        <SectionTitle>{t('compliance.inventory.classification')}</SectionTitle>
        <dl className="grid gap-2 sm:grid-cols-2">
          {rows.map(([label, value]) => (
            <div key={label} className="rounded-lg border border-shell-200 p-3">
              <dt className="text-2xs uppercase tracking-wide text-shell-400">{label}</dt>
              <dd className="mt-0.5 break-words text-sm text-shell-800">{value}</dd>
            </div>
          ))}
        </dl>

        {row.note && (
          <div>
            <SectionTitle>{t('common.notes')}</SectionTitle>
            <p className="rounded-lg border border-shell-200 bg-shell-50 p-3 text-sm text-shell-700">
              {row.note}
            </p>
          </div>
        )}

        <div className="flex items-start gap-2.5 rounded-lg border border-info-200 bg-info-50 p-3 text-info-700">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
          <p className="text-xs">{t('compliance.inventory.candidateNotice')}</p>
        </div>
      </div>
    </Modal>
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function ComplianceInventory() {
  const { t } = useTranslation()

  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [category, setCategory] = useState('')
  const [lawfulBasis, setLawfulBasis] = useState('')
  const [reviewStatus, setReviewStatus] = useState('')
  const [unknownOnly, setUnknownOnly] = useState(false)
  const [detail, setDetail] = useState<InventoryField | null>(null)

  const listQuery = useQuery({
    queryKey: [
      'compliance',
      'inventory',
      page,
      search,
      category,
      lawfulBasis,
      reviewStatus,
      unknownOnly,
    ],
    queryFn: () =>
      api.get<Paged<InventoryField>>('/compliance/inventory/fields', {
        page,
        size: PAGE_SIZE,
        q: search || undefined,
        data_category: category || undefined,
        lawful_basis: lawfulBasis || undefined,
        review_status: reviewStatus || undefined,
        unknown_only: unknownOnly ? true : undefined,
      }),
  })

  const summaryQuery = useQuery({
    queryKey: ['compliance', 'inventory', 'summary'],
    queryFn: () => api.get<InventorySummary>('/compliance/inventory/summary'),
  })

  const data = listQuery.data
  const summary = summaryQuery.data

  const tiles = useMemo(
    () =>
      summary
        ? [
            { label: t('compliance.inventory.tables'), value: summary.tables, tone: 'text-shell-900' },
            { label: t('compliance.inventory.fields'), value: summary.fields, tone: 'text-shell-900' },
            {
              label: t('compliance.inventory.directIdentifiers'),
              value: summary.direct_identifiers,
              tone: 'text-danger-700',
            },
            {
              label: t('compliance.inventory.locationFields'),
              value: summary.location_fields,
              tone: 'text-warn-700',
            },
            {
              label: t('compliance.inventory.specialCandidates'),
              value: summary.special_category_candidates,
              tone: 'text-danger-700',
            },
            {
              label: t('compliance.inventory.unknownBasis'),
              value: summary.unknown_lawful_basis,
              tone: 'text-warn-700',
            },
            {
              label: t('compliance.inventory.unknownRetention'),
              value: summary.unknown_retention,
              tone: 'text-warn-700',
            },
            {
              label: t('compliance.inventory.reviewRequired'),
              value: summary.review_required,
              tone: 'text-info-700',
            },
          ]
        : [],
    [summary, t],
  )

  /** Export writes exactly what is on screen — the current filter, current
   *  page — so the file can be reconciled against the UI it came from. */
  const exportCsv = () => {
    const rows = data?.items ?? []
    if (rows.length === 0) return
    const columns: (keyof InventoryField)[] = [
      'table_name',
      'column_name',
      'data_category',
      'identifiability',
      'purpose_code',
      'lawful_basis',
      'retention_rule',
      'retention_days',
      'masking',
      'cross_border',
      'review_status',
      'source',
      'evidence_ref',
    ]
    const cell = (v: unknown) =>
      v === null || v === undefined ? '' : `"${String(v).replace(/"/g, '""')}"`
    const body = rows.map((row) => columns.map((c) => cell(row[c])).join(';')).join('\n')
    // Leading BOM so Excel reads the Turkish characters as UTF-8.
    const blob = new Blob([`\uFEFF${columns.join(';')}\n${body}`], {
      type: 'text/csv;charset=utf-8',
    })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `veri-envanteri-${new Date().toISOString().slice(0, 10)}.csv`
    link.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div>
      <PageHeader
        title={t('compliance.inventory.title')}
        subtitle={t('compliance.inventory.subtitle')}
        icon={<Database className="h-5 w-5" />}
        actions={
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={exportCsv}
            disabled={(data?.items.length ?? 0) === 0}
          >
            <Download className="h-4 w-4" />
            {t('common.export')}
          </button>
        }
      />

      {tiles.length > 0 && (
        <div className="mb-4 grid gap-3 grid-cols-2 sm:grid-cols-4 xl:grid-cols-8">
          {tiles.map((tile) => (
            <div key={tile.label} className="card p-3">
              <p className="truncate text-2xs font-medium uppercase tracking-wide text-shell-500">
                {tile.label}
              </p>
              <p className={`tabular mt-1 text-xl font-semibold ${tile.tone}`}>
                {formatNumber(tile.value)}
              </p>
            </div>
          ))}
        </div>
      )}

      <Card className="mb-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[16rem] flex-1">
            <Field label={t('common.search')}>
              <div className="relative">
                <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-shell-400" />
                <input
                  className="input pl-9"
                  value={search}
                  placeholder={t('compliance.inventory.searchPlaceholder')}
                  onChange={(e) => {
                    setSearch(e.target.value)
                    setPage(1)
                  }}
                />
              </div>
            </Field>
          </div>
          <Field label={t('compliance.inventory.category')}>
            <select
              className="input"
              value={category}
              onChange={(e) => {
                setCategory(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {DATA_CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {t(`compliance.dataCategory.${c}`, { defaultValue: c })}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('compliance.inventory.lawfulBasis')}>
            <select
              className="input"
              value={lawfulBasis}
              onChange={(e) => {
                setLawfulBasis(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {LAWFUL_BASES.map((b) => (
                <option key={b} value={b}>
                  {t(`compliance.lawfulBasis.${b}`, { defaultValue: b })}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('compliance.inventory.reviewStatus')}>
            <select
              className="input"
              value={reviewStatus}
              onChange={(e) => {
                setReviewStatus(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {REVIEW_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {t(`compliance.reviewStatus.${s}`, { defaultValue: s })}
                </option>
              ))}
            </select>
          </Field>
          <label className="mb-1 flex cursor-pointer items-center gap-2 text-xs text-shell-700">
            <input
              type="checkbox"
              className="h-4 w-4 rounded border-shell-300"
              checked={unknownOnly}
              onChange={(e) => {
                setUnknownOnly(e.target.checked)
                setPage(1)
              }}
            />
            {t('compliance.inventory.unknownOnly')}
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
            title={t('compliance.inventory.empty')}
            description={t('compliance.inventory.emptyHint')}
          />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('compliance.inventory.field')}</th>
                    <th>{t('compliance.inventory.category')}</th>
                    <th>{t('compliance.inventory.lawfulBasis')}</th>
                    <th>{t('compliance.inventory.retention')}</th>
                    <th>{t('compliance.inventory.masking')}</th>
                    <th>{t('compliance.inventory.crossBorder')}</th>
                    <th>{t('compliance.inventory.reviewStatus')}</th>
                    <th className="w-10" />
                  </tr>
                </thead>
                <tbody>
                  {data?.items.map((row) => (
                    <tr key={row.id}>
                      <td>
                        <p className="font-mono text-xs text-shell-800">
                          {row.table_name}.{row.column_name}
                        </p>
                        <p className="text-2xs text-shell-400">
                          {t(`compliance.identifiability.${row.identifiability}`, {
                            defaultValue: row.identifiability,
                          })}
                        </p>
                      </td>
                      <td>
                        <span className={CATEGORY_BADGE[row.data_category] ?? 'badge-muted'}>
                          <CategoryIcon category={row.data_category} />
                          {t(`compliance.dataCategory.${row.data_category}`, {
                            defaultValue: row.data_category,
                          })}
                        </span>
                      </td>
                      <td>
                        <span className={unknownTone(row.lawful_basis)}>
                          {t(`compliance.lawfulBasis.${row.lawful_basis}`, {
                            defaultValue: row.lawful_basis,
                          })}
                        </span>
                      </td>
                      <td className="tabular whitespace-nowrap text-xs">
                        {row.retention_rule ? (
                          <>
                            {row.retention_rule}
                            {row.retention_days ? (
                              <span className="text-shell-400">
                                {' '}
                                · {formatNumber(row.retention_days)} {t('compliance.days')}
                              </span>
                            ) : null}
                          </>
                        ) : (
                          <span className="badge-warn">{t('compliance.unknown')}</span>
                        )}
                      </td>
                      <td>
                        <span className={unknownTone(row.masking)}>
                          {t(`compliance.masking.${row.masking}`, { defaultValue: row.masking })}
                        </span>
                      </td>
                      <td>
                        <span
                          className={
                            row.cross_border === 'YES'
                              ? 'badge-danger'
                              : row.cross_border === 'NO'
                                ? 'badge-ok'
                                : 'badge-warn'
                          }
                        >
                          {t(`compliance.crossBorder.${row.cross_border}`, {
                            defaultValue: row.cross_border,
                          })}
                        </span>
                      </td>
                      <td>
                        <span className={REVIEW_BADGE[row.review_status] ?? 'badge-muted'}>
                          {t(`compliance.reviewStatus.${row.review_status}`, {
                            defaultValue: row.review_status,
                          })}
                        </span>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn-ghost btn-sm"
                          onClick={() => setDetail(row)}
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

      <DetailModal row={detail} onClose={() => setDetail(null)} />
    </div>
  )
}
