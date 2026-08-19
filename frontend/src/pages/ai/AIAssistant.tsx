/**
 * AI Plasiyer Asistanı / AI Field Assistant.
 *
 * Two tools a salesperson actually uses standing at a shop door or at the
 * depot gate: "what should I offer this customer?" and "what should I load on
 * the van?".  Both are computed server-side from real history, so when no
 * language model answers the numbers still arrive — the screen simply says so
 * instead of pretending a narrative exists.
 */
import { useMutation, useQuery } from '@tanstack/react-query'
import { AlertTriangle, Bot, PackageSearch, Sparkles, Truck } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  PageHeader,
  SectionTitle,
  Spinner,
  useToast,
} from '@/components/ui'
import { api, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatNumber, formatPercent, formatQuantity, toNumber } from '@/lib/format'
import { bilingual, currentLanguage } from '@/lib/i18n'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface Suggestion {
  suggestion_id?: number | null
  suggestion_kind: string
  agent_kind: string
  subject_type: string
  subject_id?: number | null
  payload: Record<string, unknown>
  explanation: string
  reasoning?: string | null
  confidence: number
  provider?: string | null
  model?: string | null
  error_key?: string | null
  degraded: boolean
}

/** One proposed line — analytics and the fallback model name fields differently. */
interface SuggestionLine {
  product_id: number
  sku?: string | null
  product?: string | null
  name?: string | null
  uom?: string | null
  avg_quantity?: number | string
  avg_cases?: number | string
  suggested_quantity?: number | string
  suggested_cases?: number | string
  base_quantity?: number | string
  days_since_last?: number | null
  days_since_last_purchase?: number | null
  last_purchase_date?: string | null
  depletion_probability?: number | null
  on_van_quantity?: number | string
  depot_available?: number | string
  warehouse_available?: number | string | null
  avg_daily_sales?: number | string
  volume_l?: number | string
  weight_kg?: number | string
  reason?: string | null
  reason_tr?: string | null
  reason_en?: string | null
  [key: string]: unknown
}

interface CustomerRow {
  id: number
  code: string
  name: string
  trade_name?: string | null
  city?: string | null
}

interface SalespersonRow {
  id: number
  code: string
  full_name: string
  default_vehicle_id?: number | null
}

interface VehicleRow {
  id: number
  code: string
  plate_number: string
  name?: string | null
}

type Tool = 'order' | 'vanLoad'

function linesOf(payload: Record<string, unknown>): SuggestionLine[] {
  const items = payload.items ?? payload.lines
  return Array.isArray(items) ? (items as SuggestionLine[]) : []
}

function lineName(line: SuggestionLine): string {
  return String(line.product ?? line.name ?? `#${line.product_id}`)
}

function lineReason(line: SuggestionLine): string {
  const localized = bilingual(line as Record<string, unknown>, 'reason')
  return localized || String(line.reason ?? '')
}

/* -------------------------------------------------------------------------- */
/* Provenance strip                                                           */
/* -------------------------------------------------------------------------- */
function Provenance({ result }: { result: Suggestion }) {
  const { t } = useTranslation()
  return (
    <div className="mb-3 flex flex-wrap items-center gap-2 text-2xs">
      <span className="badge-muted">
        {t('aiAssistant.answeredBy')}: {result.provider ?? '—'}
        {result.model ? ` · ${result.model}` : ''}
      </span>
      {result.confidence > 0 && (
        <span className="badge-info">
          {t('ai.confidence')} {formatPercent(result.confidence * 100)}
        </span>
      )}
      {result.degraded && (
        <span className="badge-warn flex items-center gap-1">
          <AlertTriangle className="h-3 w-3" />
          {t('aiAssistant.statisticalOnly')}
        </span>
      )}
    </div>
  )
}

function Explanation({ result }: { result: Suggestion }) {
  const { t } = useTranslation()
  if (!result.explanation) return null
  return (
    <div className="mb-4">
      <SectionTitle>{t('aiAssistant.explanation')}</SectionTitle>
      <p className="whitespace-pre-wrap rounded-lg border border-shell-200 bg-shell-50 p-3 text-sm leading-relaxed text-shell-700">
        {result.explanation}
      </p>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function AIAssistant() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const toast = useToast()

  const [tool, setTool] = useState<Tool>('order')
  const today = new Date().toISOString().slice(0, 10)

  const canRun = can('ai.assistant', 'EXECUTE')

  /* --- order suggestion ------------------------------------------------- */
  const [term, setTerm] = useState('')
  const [customerId, setCustomerId] = useState<number | null>(null)
  const [orderDate, setOrderDate] = useState(today)

  const customersQuery = useQuery({
    queryKey: ['ai-assistant', 'customers', term],
    queryFn: () =>
      api.get<Paged<CustomerRow>>('/customers', { term: term || undefined, page: 1, size: 30 }),
    enabled: tool === 'order' && can('crm.customers', 'VIEW'),
  })

  const orderMutation = useMutation({
    mutationFn: (id: number) =>
      api.post<Suggestion>(`/ai/assistant/customer/${id}`, undefined, {
        on_date: orderDate || undefined,
      }),
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  /* --- van load --------------------------------------------------------- */
  const [salespersonId, setSalespersonId] = useState<number | null>(null)
  const [vehicleId, setVehicleId] = useState<number | null>(null)
  const [loadDate, setLoadDate] = useState(today)

  const salespersonsQuery = useQuery({
    queryKey: ['ai-assistant', 'salespersons'],
    queryFn: () =>
      api.get<Paged<SalespersonRow>>('/vehicles/salespersons', { page: 1, size: 200, is_active: true }),
    enabled: tool === 'vanLoad' && can('field.salespersons', 'VIEW'),
  })

  const vehiclesQuery = useQuery({
    queryKey: ['ai-assistant', 'vehicles'],
    queryFn: () => api.get<Paged<VehicleRow>>('/vehicles', { page: 1, size: 200, is_active: true }),
    enabled: tool === 'vanLoad' && can('field.vehicles', 'VIEW'),
  })

  const vanLoadMutation = useMutation({
    mutationFn: (id: number) =>
      api.post<Suggestion>('/ai/assistant/van-load', undefined, {
        salesperson_id: id,
        vehicle_id: vehicleId ?? undefined,
        on_date: loadDate || undefined,
      }),
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  const orderLines = useMemo(
    () => (orderMutation.data ? linesOf(orderMutation.data.payload) : []),
    [orderMutation.data],
  )
  const loadLines = useMemo(
    () => (vanLoadMutation.data ? linesOf(vanLoadMutation.data.payload) : []),
    [vanLoadMutation.data],
  )

  const lang = currentLanguage()
  const customers = customersQuery.data?.items ?? []
  const salespersons = salespersonsQuery.data?.items ?? []
  const vehicles = vehiclesQuery.data?.items ?? []

  return (
    <div>
      <PageHeader
        title={t('aiAssistant.title')}
        subtitle={t('aiAssistant.subtitle')}
        icon={<Bot className="h-5 w-5" />}
      />

      <div className="mb-4 flex flex-wrap gap-2">
        <button
          type="button"
          className={tool === 'order' ? 'btn-primary btn-sm' : 'btn-secondary btn-sm'}
          onClick={() => setTool('order')}
        >
          <PackageSearch className="h-4 w-4" />
          {t('aiAssistant.orderTool')}
        </button>
        <button
          type="button"
          className={tool === 'vanLoad' ? 'btn-primary btn-sm' : 'btn-secondary btn-sm'}
          onClick={() => setTool('vanLoad')}
        >
          <Truck className="h-4 w-4" />
          {t('aiAssistant.vanLoadTool')}
        </button>
      </div>

      {tool === 'order' ? (
        <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
          <Card title={t('aiAssistant.customer')}>
            <div className="space-y-3">
              <input
                className="input"
                value={term}
                placeholder={t('aiAssistant.searchCustomer')}
                onChange={(e) => setTerm(e.target.value)}
              />
              <Field label={t('aiAssistant.date')}>
                <input
                  type="date"
                  className="input"
                  value={orderDate}
                  onChange={(e) => setOrderDate(e.target.value)}
                />
              </Field>
              <div className="max-h-72 overflow-y-auto rounded-lg border border-shell-200">
                {customersQuery.isLoading ? (
                  <LoadingBlock />
                ) : customersQuery.isError ? (
                  <ErrorState error={customersQuery.error} />
                ) : customers.length === 0 ? (
                  <p className="p-4 text-center text-xs text-shell-400">{t('common.noResults')}</p>
                ) : (
                  <ul>
                    {customers.map((c) => (
                      <li key={c.id}>
                        <button
                          type="button"
                          onClick={() => setCustomerId(c.id)}
                          className={`w-full border-b border-shell-100 px-3 py-2 text-left text-xs last:border-0 ${
                            customerId === c.id
                              ? 'bg-brand-50 font-medium text-brand-700'
                              : 'text-shell-600 hover:bg-shell-50'
                          }`}
                        >
                          <span className="block truncate">{c.trade_name || c.name}</span>
                          <span className="tabular text-2xs text-shell-400">
                            {c.code}
                            {c.city ? ` · ${c.city}` : ''}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <button
                type="button"
                className="btn-primary w-full"
                disabled={!canRun || customerId === null || orderMutation.isPending}
                onClick={() => customerId !== null && orderMutation.mutate(customerId)}
              >
                {orderMutation.isPending ? <Spinner /> : <Sparkles className="h-4 w-4" />}
                {t('aiAssistant.generate')}
              </button>
            </div>
          </Card>

          <Card title={t('aiAssistant.orderTool')}>
            {orderMutation.isPending ? (
              <LoadingBlock label={t('ai.thinking')} />
            ) : orderMutation.isError ? (
              <ErrorState error={orderMutation.error} />
            ) : !orderMutation.data ? (
              <EmptyState title={t('aiAssistant.pickCustomer')} />
            ) : (
              <>
                <Provenance result={orderMutation.data} />
                <Explanation result={orderMutation.data} />
                {orderLines.length === 0 ? (
                  <EmptyState title={t('aiAssistant.noLines')} />
                ) : (
                  <div className="table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>{t('aiAssistant.product')}</th>
                          <th className="text-right">{t('aiAssistant.avgQuantity')}</th>
                          <th className="text-right">{t('aiAssistant.daysSinceLast')}</th>
                          <th className="text-right">{t('aiAssistant.depletion')}</th>
                          <th className="text-right">{t('aiAssistant.suggestedQuantity')}</th>
                          <th>{t('aiAssistant.reason')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {orderLines.map((line) => {
                          const days = line.days_since_last ?? line.days_since_last_purchase
                          const probability = line.depletion_probability
                          return (
                            <tr key={line.product_id}>
                              <td>
                                <span className="block font-medium text-shell-800">
                                  {lineName(line)}
                                </span>
                                <span className="tabular text-2xs text-shell-400">
                                  {line.sku ?? ''}
                                  {line.last_purchase_date
                                    ? ` · ${formatDate(line.last_purchase_date, { short: true })}`
                                    : ''}
                                </span>
                              </td>
                              <td className="tabular text-right">
                                {formatQuantity(
                                  line.avg_quantity ?? line.suggested_quantity ?? 0,
                                  line.uom ?? undefined,
                                  lang,
                                )}
                              </td>
                              <td className="tabular text-right">
                                {days === null || days === undefined
                                  ? '—'
                                  : `${formatNumber(days)} ${t('aiAssistant.days')}`}
                              </td>
                              <td className="tabular text-right">
                                {probability === null || probability === undefined ? (
                                  '—'
                                ) : (
                                  <span
                                    className={
                                      probability >= 0.7
                                        ? 'badge-danger'
                                        : probability >= 0.4
                                          ? 'badge-warn'
                                          : 'badge-muted'
                                    }
                                  >
                                    {formatPercent(probability * 100)}
                                  </span>
                                )}
                              </td>
                              <td className="tabular text-right font-medium">
                                {formatQuantity(
                                  line.suggested_quantity ?? 0,
                                  line.uom ?? undefined,
                                  lang,
                                )}
                              </td>
                              <td className="max-w-md text-2xs text-shell-500">{lineReason(line)}</td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </>
            )}
          </Card>
        </div>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
          <Card title={t('aiAssistant.vanLoadTool')}>
            <div className="space-y-3">
              <Field label={t('aiAssistant.salesperson')} required>
                <select
                  className="input"
                  value={salespersonId ?? ''}
                  onChange={(e) => {
                    const id = e.target.value ? Number(e.target.value) : null
                    setSalespersonId(id)
                    const person = salespersons.find((s) => s.id === id)
                    setVehicleId(person?.default_vehicle_id ?? null)
                  }}
                >
                  <option value="">{t('common.select')}</option>
                  {salespersons.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.full_name} ({s.code})
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={t('aiAssistant.vehicle')} hint={t('aiAssistant.vehicleAuto')}>
                <select
                  className="input"
                  value={vehicleId ?? ''}
                  onChange={(e) => setVehicleId(e.target.value ? Number(e.target.value) : null)}
                >
                  <option value="">{t('common.select')}</option>
                  {vehicles.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.plate_number}
                      {v.name ? ` — ${v.name}` : ''}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={t('aiAssistant.date')}>
                <input
                  type="date"
                  className="input"
                  value={loadDate}
                  onChange={(e) => setLoadDate(e.target.value)}
                />
              </Field>
              {salespersonsQuery.isError && <ErrorState error={salespersonsQuery.error} />}
              <button
                type="button"
                className="btn-primary w-full"
                disabled={!canRun || salespersonId === null || vanLoadMutation.isPending}
                onClick={() => salespersonId !== null && vanLoadMutation.mutate(salespersonId)}
              >
                {vanLoadMutation.isPending ? <Spinner /> : <Sparkles className="h-4 w-4" />}
                {t('aiAssistant.generate')}
              </button>
            </div>
          </Card>

          <Card title={t('aiAssistant.vanLoadTool')}>
            {vanLoadMutation.isPending ? (
              <LoadingBlock label={t('ai.thinking')} />
            ) : vanLoadMutation.isError ? (
              <ErrorState error={vanLoadMutation.error} />
            ) : !vanLoadMutation.data ? (
              <EmptyState title={t('aiAssistant.pickSalesperson')} />
            ) : (
              <>
                <Provenance result={vanLoadMutation.data} />
                <Explanation result={vanLoadMutation.data} />
                {loadLines.length === 0 ? (
                  <EmptyState title={t('aiAssistant.noLines')} />
                ) : (
                  <>
                    <p className="tabular mb-2 text-xs text-shell-500">
                      {t('aiAssistant.totals', {
                        volume: formatNumber(
                          loadLines.reduce((sum, l) => sum + toNumber(l.volume_l), 0),
                          { decimals: 1 },
                        ),
                        weight: formatNumber(
                          loadLines.reduce((sum, l) => sum + toNumber(l.weight_kg), 0),
                          { decimals: 1 },
                        ),
                      })}
                    </p>
                    <div className="table-wrap">
                      <table className="table">
                        <thead>
                          <tr>
                            <th>{t('aiAssistant.product')}</th>
                            <th className="text-right">{t('aiAssistant.cases')}</th>
                            <th className="text-right">{t('aiAssistant.suggestedQuantity')}</th>
                            <th className="text-right">{t('aiAssistant.onVan')}</th>
                            <th className="text-right">{t('aiAssistant.depotAvailable')}</th>
                            <th className="text-right">{t('aiAssistant.volume')}</th>
                            <th className="text-right">{t('aiAssistant.weight')}</th>
                            <th>{t('aiAssistant.reason')}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {loadLines.map((line) => (
                            <tr key={line.product_id}>
                              <td>
                                <span className="block font-medium text-shell-800">
                                  {lineName(line)}
                                </span>
                                <span className="tabular text-2xs text-shell-400">
                                  {line.sku ?? ''}
                                </span>
                              </td>
                              <td className="tabular text-right">
                                {line.suggested_cases === undefined
                                  ? '—'
                                  : formatNumber(line.suggested_cases)}
                              </td>
                              <td className="tabular text-right font-medium">
                                {formatQuantity(
                                  line.base_quantity ?? line.suggested_quantity ?? 0,
                                  line.uom ?? undefined,
                                  lang,
                                )}
                              </td>
                              <td className="tabular text-right">
                                {line.on_van_quantity === undefined
                                  ? '—'
                                  : formatNumber(line.on_van_quantity, { decimals: 0 })}
                              </td>
                              <td className="tabular text-right">
                                {formatNumber(
                                  line.depot_available ?? line.warehouse_available ?? 0,
                                  { decimals: 0 },
                                )}
                              </td>
                              <td className="tabular text-right">
                                {formatNumber(line.volume_l ?? 0, { decimals: 1 })}
                              </td>
                              <td className="tabular text-right">
                                {formatNumber(line.weight_kg ?? 0, { decimals: 1 })}
                              </td>
                              <td className="max-w-md text-2xs text-shell-500">
                                {lineReason(line)}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                )}
              </>
            )}
          </Card>
        </div>
      )}
    </div>
  )
}
