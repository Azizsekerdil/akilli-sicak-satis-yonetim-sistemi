/**
 * Report centre.
 *
 * The catalogue, the columns and the filter list all come from the backend
 * (`GET /reports/groups`), so this screen renders whatever the report engine
 * declares — no report is hard-coded here.  Running is a POST because the
 * parameter set is open-ended; exports stream back as a blob.
 */
import { useMutation, useQuery } from '@tanstack/react-query'
import { Download, FileText, Play, Search } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  PageHeader,
  SkeletonRows,
  Spinner,
  useToast,
} from '@/components/ui'
import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { currentLanguage } from '@/lib/i18n'
import { formatDate, formatMoney, formatNumber, formatPercent } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Types (mirror app/schemas/report.py)                                       */
/* -------------------------------------------------------------------------- */
interface ReportColumn {
  key: string
  label?: string | null
  label_tr: string
  label_en: string
  type: string
  width: number
  align: string
}
interface ReportFilter {
  key: string
  label?: string | null
  label_tr: string
  label_en: string
  type: string
  required: boolean
  default: unknown
  source: string | null
}
interface ReportDef {
  key: string
  title: string
  title_tr: string
  title_en: string
  description?: string | null
  module: string
  columns: ReportColumn[]
  filters: ReportFilter[]
  group_by: string | null
  totals: string[]
  permission: { resource: string; action: string }
  formats: string[]
}
interface ReportGroup {
  module: string
  reports: ReportDef[]
}
interface ReportResult {
  columns: ReportColumn[]
  rows: Record<string, unknown>[]
  totals: Record<string, unknown>
  meta: {
    key: string
    title: string
    module: string
    row_count: number
    generated_at: string
    currency: string
    start?: string | null
    end?: string | null
    restricted: boolean
  }
}

type Params = Record<string, string>

/* Option sources the backend names in FilterDef.source. */
const ENTITY_SOURCES: Record<
  string,
  { path: string; params?: Record<string, unknown>; label: (r: Record<string, string>) => string }
> = {
  salespersons: { path: '/vehicles/salespersons', params: { size: 200, is_active: true }, label: (r) => r.full_name },
  customers: { path: '/customers', params: { size: 200 }, label: (r) => `${r.code} — ${r.name}` },
  products: { path: '/products', params: { size: 200 }, label: (r) => `${r.sku} — ${r.name}` },
  warehouses: { path: '/warehouses', params: { size: 200 }, label: (r) => `${r.code} — ${r.name}` },
  vehicles: { path: '/vehicles', params: { size: 200 }, label: (r) => r.plate_number },
  routes: { path: '/routes', params: { size: 200 }, label: (r) => `${r.code} — ${r.name}` },
  campaigns: { path: '/campaigns', params: { size: 200 }, label: (r) => r.name },
}
const TREE_SOURCES: Record<string, string> = {
  categories: '/products/categories',
  brands: '/products/brands',
}
const STATIC_SOURCES: Record<string, string[]> = {
  payment_methods: ['CASH', 'CREDIT_CARD', 'BANK_TRANSFER', 'CHEQUE', 'OPEN_ACCOUNT'],
  target_subjects: ['COMPANY', 'REGION', 'ROUTE', 'SALESPERSON', 'PRODUCT', 'CATEGORY', 'BRAND', 'CUSTOMER'],
  target_metrics: ['REVENUE', 'VOLUME', 'MARGIN', 'COLLECTION', 'VISITS', 'NEW_CUSTOMERS'],
}

function isoDate(offsetDays = 0): string {
  const d = new Date()
  d.setDate(d.getDate() + offsetDays)
  const z = new Date(d.getTime() - d.getTimezoneOffset() * 60_000)
  return z.toISOString().slice(0, 10)
}

function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 2000)
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function Reports() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const lang = currentLanguage()

  const [term, setTerm] = useState('')
  const [activeKey, setActiveKey] = useState<string | null>(null)
  const [params, setParams] = useState<Params>({})
  const [result, setResult] = useState<ReportResult | null>(null)

  const catalogue = useQuery({
    queryKey: ['report-groups'],
    queryFn: () => api.get<ReportGroup[]>('/reports/groups'),
  })

  const groups = useMemo(() => {
    const q = term.trim().toLocaleLowerCase('tr-TR')
    return (catalogue.data ?? [])
      .map((g) => ({
        ...g,
        reports: g.reports.filter((r) => !q || r.title.toLocaleLowerCase('tr-TR').includes(q)),
      }))
      .filter((g) => g.reports.length > 0)
  }, [catalogue.data, term])

  const definition = useMemo(() => {
    for (const g of catalogue.data ?? []) {
      const hit = g.reports.find((r) => r.key === activeKey)
      if (hit) return hit
    }
    return null
  }, [catalogue.data, activeKey])

  /* Seed the form whenever a different report is picked. */
  useEffect(() => {
    if (!definition) return
    const seed: Params = {}
    for (const f of definition.filters) {
      if (f.key === 'start') seed[f.key] = isoDate(-29)
      else if (f.key === 'end' || f.key === 'as_of') seed[f.key] = isoDate()
      else if (f.default !== null && f.default !== undefined) seed[f.key] = String(f.default)
      else seed[f.key] = ''
    }
    setParams(seed)
    setResult(null)
  }, [definition])

  const cleanParams = () =>
    Object.fromEntries(Object.entries(params).filter(([, v]) => v !== '' && v !== undefined))

  const run = useMutation({
    mutationFn: () => api.post<ReportResult>(`/reports/${activeKey}/run`, { params: cleanParams() }),
    onSuccess: setResult,
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const exportReport = useMutation({
    mutationFn: async (format: string) => {
      const { blob, filename } = await api.download(`/reports/${activeKey}/export`, {
        format,
        params: cleanParams(),
      })
      saveBlob(blob, filename)
    },
    onSuccess: () => push('success', t('reports.exported')),
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const missingRequired = (definition?.filters ?? []).some((f) => f.required && !params[f.key])

  if (catalogue.isError) {
    return (
      <>
        <PageHeader title={t('reports.title')} icon={<FileText className="h-5 w-5" />} />
        <Card><ErrorState error={catalogue.error} onRetry={() => void catalogue.refetch()} /></Card>
      </>
    )
  }

  return (
    <>
      <PageHeader title={t('reports.title')} icon={<FileText className="h-5 w-5" />} />

      <div className="grid gap-5 lg:grid-cols-4">
        {/* ------------------------- Catalogue ------------------------- */}
        <Card className="lg:col-span-1" title={t('reports.catalogue')} bodyClassName="p-3">
          <div className="relative mb-3">
            <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
            <input className="input pl-9" placeholder={t('reports.searchReport')} value={term}
              onChange={(e) => setTerm(e.target.value)} />
          </div>
          {catalogue.isLoading ? (
            <LoadingBlock />
          ) : groups.length === 0 ? (
            <EmptyState title={t('reports.noReports')} />
          ) : (
            <div className="max-h-[32rem] space-y-4 overflow-y-auto">
              {groups.map((g) => (
                <div key={g.module}>
                  <p className="mb-1 px-1 text-2xs font-semibold uppercase tracking-wide text-shell-500">
                    {t(`reports.modules.${g.module}`, { defaultValue: g.module })}
                  </p>
                  <ul className="space-y-0.5">
                    {g.reports.map((r) => (
                      <li key={r.key}>
                        <button
                          type="button"
                          onClick={() => setActiveKey(r.key)}
                          className={`w-full rounded-lg px-2 py-1.5 text-left text-sm ${
                            activeKey === r.key
                              ? 'bg-brand-50 font-medium text-brand-700'
                              : 'text-shell-700 hover:bg-shell-50'
                          }`}
                        >
                          {r.title}
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
        </Card>

        {/* ------------------------- Runner ------------------------- */}
        <div className="space-y-5 lg:col-span-3">
          {!definition ? (
            <Card><EmptyState title={t('reports.selectReport')} /></Card>
          ) : (
            <>
              <Card title={definition.title} bodyClassName="p-4">
                {definition.description && (
                  <p className="mb-3 text-sm text-shell-500">{definition.description}</p>
                )}
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  {definition.filters.map((f) => (
                    <FilterInput
                      key={f.key}
                      filter={f}
                      lang={lang}
                      value={params[f.key] ?? ''}
                      onChange={(v) => setParams((p) => ({ ...p, [f.key]: v }))}
                    />
                  ))}
                </div>
                <div className="mt-4 flex flex-wrap gap-2">
                  <button type="button" className="btn-primary btn-sm"
                    disabled={run.isPending || missingRequired} onClick={() => run.mutate()}>
                    {run.isPending ? <Spinner className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                    {t('reports.run')}
                  </button>
                  {can('analytics.reports', 'EXPORT') &&
                    ['pdf', 'excel', 'csv'].map((fmt) => (
                      <button
                        key={fmt}
                        type="button"
                        className="btn-secondary btn-sm"
                        disabled={exportReport.isPending || missingRequired}
                        onClick={() => exportReport.mutate(fmt)}
                      >
                        <Download className="h-3.5 w-3.5" />
                        {fmt.toUpperCase()}
                      </button>
                    ))}
                </div>
              </Card>

              {run.isPending ? (
                <Card bodyClassName="p-0"><SkeletonRows rows={8} cols={6} /></Card>
              ) : result ? (
                <ResultTable result={result} lang={lang} />
              ) : null}
            </>
          )}
        </div>
      </div>
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Filter input                                                               */
/* -------------------------------------------------------------------------- */
function FilterInput({
  filter,
  lang,
  value,
  onChange,
}: {
  filter: ReportFilter
  lang: string
  value: string
  onChange: (v: string) => void
}) {
  const { t } = useTranslation()
  const label = filter.label ?? (lang === 'en' ? filter.label_en : filter.label_tr)
  const source = filter.source ?? ''
  const entity = ENTITY_SOURCES[source]
  const tree = TREE_SOURCES[source]
  const statics = STATIC_SOURCES[source]

  const options = useQuery({
    queryKey: ['report-options', source],
    queryFn: async () => {
      if (entity) {
        const r = await api.get<Paged<Record<string, string>>>(entity.path, entity.params)
        return (r.items ?? []).map((row) => ({ value: String(row.id), label: entity.label(row) }))
      }
      const rows = await api.get<Record<string, unknown>[]>(tree!)
      const out: { value: string; label: string }[] = []
      const walk = (items: Record<string, unknown>[], depth: number) => {
        for (const it of items) {
          out.push({ value: String(it.id), label: `${'— '.repeat(depth)}${String(it.name ?? it.code)}` })
          walk((it.children as Record<string, unknown>[]) ?? [], depth + 1)
        }
      }
      walk(rows, 0)
      return out
    },
    enabled: Boolean(entity || tree),
    throwOnError: false,
    staleTime: 5 * 60_000,
  })

  if (filter.type === 'date') {
    return (
      <Field label={label} required={filter.required}>
        <input type="date" className="input" value={value} onChange={(e) => onChange(e.target.value)} />
      </Field>
    )
  }
  if (filter.type === 'bool') {
    return (
      <Field label={label}>
        <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">{t('common.no')}</option>
          <option value="true">{t('common.yes')}</option>
        </select>
      </Field>
    )
  }
  if (filter.type === 'int' && !filter.source) {
    return (
      <Field label={label} required={filter.required}>
        <input type="number" className="input tabular" value={value} onChange={(e) => onChange(e.target.value)} />
      </Field>
    )
  }
  if (statics) {
    return (
      <Field label={label} required={filter.required}>
        <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">{t('common.all')}</option>
          {statics.map((o) => (
            <option key={o} value={o}>{o}</option>
          ))}
        </select>
      </Field>
    )
  }
  if ((entity || tree) && (options.data ?? []).length > 0) {
    return (
      <Field label={label} required={filter.required}>
        <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">{t('common.all')}</option>
          {(options.data ?? []).map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </Field>
    )
  }
  /* Unknown or unreachable option source — accept the raw id instead of
     pretending we have a list to choose from. */
  return (
    <Field label={label} required={filter.required} hint={filter.source ? t('reports.idPlaceholder') : undefined}>
      <input
        type={filter.source ? 'number' : 'text'}
        className={filter.source ? 'input tabular' : 'input'}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </Field>
  )
}

/* -------------------------------------------------------------------------- */
/* Result table                                                               */
/* -------------------------------------------------------------------------- */
function cell(value: unknown, type: string): string {
  if (value === null || value === undefined || value === '') return '—'
  switch (type) {
    case 'money':
      return formatMoney(value as number)
    case 'percent':
      return formatPercent(value as number)
    case 'integer':
      return formatNumber(value as number)
    case 'number':
    case 'decimal':
      return formatNumber(value as number, { decimals: 2 })
    case 'date':
      return formatDate(value as string, { short: true })
    case 'datetime':
      return formatDate(value as string, { withTime: true })
    case 'bool':
      return value ? '✓' : '—'
    default:
      return String(value)
  }
}

function ResultTable({ result, lang }: { result: ReportResult; lang: string }) {
  const { t } = useTranslation()
  const columns = result.columns
  const hasTotals = Object.keys(result.totals ?? {}).length > 0

  return (
    <Card
      bodyClassName="p-0"
      title={result.meta.title}
      actions={
        <span className="text-2xs text-shell-400">
          {t('reports.rows', { count: result.meta.row_count })} ·{' '}
          {t('reports.generatedAt')} {formatDate(result.meta.generated_at, { withTime: true })}
        </span>
      }
    >
      {result.meta.restricted && (
        <p className="border-b border-warn-500/20 bg-warn-50 px-4 py-2 text-xs text-warn-700">
          {t('reports.restricted')}
        </p>
      )}
      {result.rows.length === 0 ? (
        <EmptyState title={t('reports.noResultRows')} />
      ) : (
        <div className="table-wrap max-h-[34rem] overflow-y-auto">
          <table className="table">
            <thead>
              <tr>
                {columns.map((c) => (
                  <th key={c.key} className={c.align === 'right' ? 'text-right' : c.align === 'center' ? 'text-center' : ''}>
                    {c.label ?? (lang === 'en' ? c.label_en : c.label_tr)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.rows.map((row, i) => (
                <tr key={i}>
                  {columns.map((c) => (
                    <td
                      key={c.key}
                      className={`${c.align === 'right' ? 'tabular text-right' : c.align === 'center' ? 'text-center' : ''}`}
                    >
                      {cell(row[c.key], c.type)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
            {hasTotals && (
              <tfoot>
                <tr className="bg-shell-50 font-semibold">
                  {columns.map((c, i) => (
                    <td key={c.key} className={`border-t border-shell-300 px-4 py-2.5 ${
                      c.align === 'right' ? 'tabular text-right' : ''
                    }`}>
                      {i === 0
                        ? t('reports.totalsRow')
                        : c.key in (result.totals ?? {})
                          ? cell(result.totals[c.key], c.type)
                          : ''}
                    </td>
                  ))}
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      )}
    </Card>
  )
}
