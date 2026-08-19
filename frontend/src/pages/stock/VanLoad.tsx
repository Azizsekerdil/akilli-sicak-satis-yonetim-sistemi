/**
 * Araç Yükleme / Van loading — the AI-assisted load-out screen.
 *
 * The forecast endpoint proposes what the day's route will actually sell, minus
 * what is already on board, capped by depot stock and the van's capacity.  The
 * loader can override every line; capacity usage recalculates as they type, so
 * an over-load is visible before the pallet jack moves.
 */
import { useMutation, useQuery } from '@tanstack/react-query'
import { Plus, Sparkles, Trash2, Truck, Upload } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { currentLanguage } from '@/lib/i18n'
import { formatDate, formatMoney, formatNumber } from '@/lib/format'
import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  PageHeader,
  SectionTitle,
  Spinner,
  StatusBadge,
  useToast,
} from '@/components/ui'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface VehicleRow {
  id: number
  plate_number: string
  name?: string | null
  capacity_volume_l: number
  capacity_weight_kg: number
  default_salesperson_id?: number | null
  home_warehouse_id?: number | null
}

interface SalespersonRow {
  id: number
  code: string
  full_name: string
  default_vehicle_id?: number | null
  default_warehouse_id?: number | null
}

interface WarehouseRow {
  id: number
  code: string
  name: string
  warehouse_type: string
}

interface Suggestion {
  product_id: number
  sku: string
  name: string
  suggested_cases: number
  base_quantity: number | string
  uom: string
  volume_l: number
  weight_kg: number
  confidence: number
  on_van_quantity: number | string
  depot_available: number | string
  reason_tr: string
  reason_en: string
}

interface ProductLite {
  id: number
  sku: string
  name: string
  sales_uom: string
  units_per_case: number | string
}

/** The grid row carries no dimensions — the detail endpoint does. */
interface ProductDetail extends ProductLite {
  unit_volume_l?: number | null
  unit_weight_kg?: number | null
  case_volume_l?: number | null
  case_weight_kg?: number | null
}

interface Capacity {
  volume_l: number
  weight_kg: number
  capacity_volume_l: number
  capacity_weight_kg: number
}

interface LoadRow {
  id: number
  document_no: string
  load_date: string
  vehicle_id: number
  salesperson_id?: number | null
  source_warehouse_id: number
  is_posted: boolean
  is_reload: boolean
  total_volume_l: number
  total_weight_kg: number
  total_cost: number | string
}

interface Line {
  product_id: number
  sku: string
  name: string
  uom: string
  cases: string
  suggested_cases: number
  base_per_case: number
  volume_per_case: number
  weight_per_case: number
  confidence: number
  reason_tr: string
  reason_en: string
  on_van: number
  depot_available: number
}

const num = (v: unknown): number => {
  const n = typeof v === 'number' ? v : Number(v ?? 0)
  return Number.isFinite(n) ? n : 0
}

const today = () => new Date().toISOString().slice(0, 10)

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function VanLoad() {
  const { t } = useTranslation()
  const { can, session } = useAuth()
  const { push } = useToast()
  const lang = currentLanguage()

  const [salespersonId, setSalespersonId] = useState<number | null>(session?.salesperson_id ?? null)
  const [vehicleId, setVehicleId] = useState<number | null>(null)
  const [warehouseId, setWarehouseId] = useState<number | null>(null)
  const [onDate, setOnDate] = useState(today())
  const [isReload, setIsReload] = useState(false)
  const [notes, setNotes] = useState('')
  const [lines, setLines] = useState<Line[]>([])
  const [createdId, setCreatedId] = useState<number | null>(null)
  const [productTerm, setProductTerm] = useState('')

  const salespersons = useQuery({
    queryKey: ['salespersons', 'picker'],
    queryFn: () => api.get<Paged<SalespersonRow>>('/vehicles/salespersons', { size: 200, is_active: true }),
  })
  const vehicles = useQuery({
    queryKey: ['vehicles', 'picker'],
    queryFn: () => api.get<Paged<VehicleRow>>('/vehicles', { size: 200, is_active: true }),
  })
  const warehouses = useQuery({
    queryKey: ['warehouses', 'picker'],
    queryFn: () => api.get<Paged<WarehouseRow>>('/warehouses', { size: 200, is_active: true }),
  })

  const capacity = useQuery({
    queryKey: ['vehicle-capacity', vehicleId],
    queryFn: () => api.get<Capacity>(`/vehicles/${vehicleId}/capacity`),
    enabled: !!vehicleId,
    retry: false,
    throwOnError: false,
  })

  const recent = useQuery({
    queryKey: ['van-loads', vehicleId, salespersonId],
    queryFn: () =>
      api.get<Paged<LoadRow>>('/vehicles/loads', {
        size: 8,
        vehicle_id: vehicleId ?? undefined,
        salesperson_id: salespersonId ?? undefined,
      }),
  })

  const vehicle = (vehicles.data?.items ?? []).find((v) => v.id === vehicleId) ?? null
  const depots = (warehouses.data?.items ?? []).filter((w) => w.warehouse_type !== 'VEHICLE')

  /* When a salesperson is picked, follow their defaults. */
  const pickSalesperson = (id: number | null) => {
    setSalespersonId(id)
    const sp = (salespersons.data?.items ?? []).find((s) => s.id === id)
    if (sp?.default_vehicle_id) setVehicleId(sp.default_vehicle_id)
    if (sp?.default_warehouse_id) setWarehouseId(sp.default_warehouse_id)
  }

  /* ---- AI suggestion ------------------------------------------------------ */
  const suggest = useMutation({
    mutationFn: () =>
      api.get<Suggestion[]>('/analytics/forecast/van-load', {
        salesperson_id: salespersonId,
        vehicle_id: vehicleId ?? undefined,
        on_date: onDate,
      }),
    onSuccess: (rows) => {
      if (rows.length === 0) {
        push('info', t('vanLoad.noSuggestion'))
        return
      }
      setCreatedId(null)
      setLines(
        rows.map((s) => {
          const cases = Math.max(1, s.suggested_cases || 1)
          return {
            product_id: s.product_id,
            sku: s.sku,
            name: s.name,
            uom: s.uom || 'CASE',
            cases: String(s.suggested_cases),
            suggested_cases: s.suggested_cases,
            base_per_case: num(s.base_quantity) / cases,
            volume_per_case: num(s.volume_l) / cases,
            weight_per_case: num(s.weight_kg) / cases,
            confidence: s.confidence,
            reason_tr: s.reason_tr,
            reason_en: s.reason_en,
            on_van: num(s.on_van_quantity),
            depot_available: num(s.depot_available),
          }
        }),
      )
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  /* ---- manual add --------------------------------------------------------- */
  const products = useQuery({
    queryKey: ['vanload-products', productTerm],
    queryFn: () => api.get<Paged<ProductLite>>('/products', { q: productTerm, size: 8, only_sellable: true }),
    enabled: productTerm.trim().length >= 2,
  })

  const addProduct = async (p: ProductLite) => {
    setProductTerm('')
    if (lines.some((l) => l.product_id === p.id)) return
    let detail: ProductDetail = p
    try {
      detail = await api.get<ProductDetail>(`/products/${p.id}`)
    } catch {
      // Dimensions are a nicety here: without them the line still loads, the
      // capacity bar just under-counts it.
    }
    const perCase = num(detail.units_per_case) || 1
    setLines((prev) => [
      ...prev,
      {
        product_id: p.id,
        sku: p.sku,
        name: p.name,
        uom: detail.sales_uom || 'CASE',
        cases: '1',
        suggested_cases: 0,
        base_per_case: perCase,
        volume_per_case: num(detail.case_volume_l) || num(detail.unit_volume_l) * perCase,
        weight_per_case: num(detail.case_weight_kg) || num(detail.unit_weight_kg) * perCase,
        confidence: 0,
        reason_tr: '',
        reason_en: '',
        on_van: 0,
        depot_available: 0,
      },
    ])
  }

  /* ---- running capacity --------------------------------------------------- */
  const usage = useMemo(() => {
    const added = lines.reduce(
      (acc, l) => {
        const c = num(l.cases)
        acc.volume += c * l.volume_per_case
        acc.weight += c * l.weight_per_case
        acc.cases += c
        return acc
      },
      { volume: 0, weight: 0, cases: 0 },
    )
    const baseVolume = capacity.data?.volume_l ?? 0
    const baseWeight = capacity.data?.weight_kg ?? 0
    const capVolume = capacity.data?.capacity_volume_l || vehicle?.capacity_volume_l || 0
    const capWeight = capacity.data?.capacity_weight_kg || vehicle?.capacity_weight_kg || 0
    return {
      ...added,
      volumeTotal: baseVolume + added.volume,
      weightTotal: baseWeight + added.weight,
      capVolume,
      capWeight,
      volumePct: capVolume > 0 ? ((baseVolume + added.volume) / capVolume) * 100 : 0,
      weightPct: capWeight > 0 ? ((baseWeight + added.weight) / capWeight) * 100 : 0,
    }
  }, [lines, capacity.data, vehicle])

  const overloaded = usage.volumePct > 100 || usage.weightPct > 100
  const payloadLines = lines.filter((l) => num(l.cases) > 0)

  /* ---- create & post ------------------------------------------------------ */
  const create = useMutation({
    mutationFn: () =>
      api.post<LoadRow>('/vehicles/loads', {
        vehicle_id: vehicleId,
        source_warehouse_id: warehouseId,
        salesperson_id: salespersonId,
        load_date: onDate,
        is_reload: isReload,
        notes: notes || null,
        lines: payloadLines.map((l) => ({
          product_id: l.product_id,
          quantity: num(l.cases),
          uom: l.uom,
          planned_quantity: l.suggested_cases || undefined,
        })),
      }),
    onSuccess: (load) => {
      setCreatedId(load.id)
      push('success', t('vanLoad.created'))
      void recent.refetch()
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const post = useMutation({
    mutationFn: (id: number) => api.post<LoadRow>(`/vehicles/loads/${id}/post`, {}),
    onSuccess: () => {
      push('success', t('vanLoad.posted'))
      setCreatedId(null)
      setLines([])
      void recent.refetch()
      void capacity.refetch()
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const ready = !!vehicleId && !!warehouseId && payloadLines.length > 0

  return (
    <>
      <PageHeader
        title={t('vanLoad.title')}
        subtitle={t('vanLoad.subtitle')}
        icon={<Truck className="h-5 w-5" />}
      />

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="space-y-4">
          {/* --------------------------- Selectors --------------------------- */}
          <Card>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Field label={t('stockCommon.salesperson')} required>
                <select
                  className="input"
                  value={salespersonId ?? ''}
                  onChange={(e) => pickSalesperson(e.target.value ? Number(e.target.value) : null)}
                >
                  <option value="">{t('stockCommon.selectSalesperson')}</option>
                  {(salespersons.data?.items ?? []).map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.full_name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={t('stockCommon.vehicle')} required>
                <select
                  className="input"
                  value={vehicleId ?? ''}
                  onChange={(e) => setVehicleId(e.target.value ? Number(e.target.value) : null)}
                >
                  <option value="">{t('stockCommon.selectVehicle')}</option>
                  {(vehicles.data?.items ?? []).map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.plate_number}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={t('vanLoad.sourceWarehouse')} required>
                <select
                  className="input"
                  value={warehouseId ?? ''}
                  onChange={(e) => setWarehouseId(e.target.value ? Number(e.target.value) : null)}
                >
                  <option value="">{t('stockCommon.selectWarehouse')}</option>
                  {depots.map((w) => (
                    <option key={w.id} value={w.id}>
                      {w.code} — {w.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={t('vanLoad.loadDate')} required>
                <input
                  type="date"
                  className="input"
                  value={onDate}
                  onChange={(e) => setOnDate(e.target.value)}
                />
              </Field>
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-4">
              <label className="flex items-center gap-2 text-sm text-shell-700">
                <input type="checkbox" checked={isReload} onChange={(e) => setIsReload(e.target.checked)} />
                {t('vanLoad.isReload')}
              </label>
              {can('analytics.forecasts', 'VIEW') && (
                <button
                  type="button"
                  className="btn-primary btn-sm"
                  disabled={!salespersonId || suggest.isPending}
                  onClick={() => suggest.mutate()}
                >
                  {suggest.isPending ? <Spinner /> : <Sparkles className="h-4 w-4" />}
                  {t('vanLoad.getSuggestion')}
                </button>
              )}
            </div>
          </Card>

          {/* ---------------------------- Lines ------------------------------ */}
          <Card
            title={t('vanLoad.suggestions')}
            bodyClassName="p-0"
            actions={<span className="text-2xs text-shell-400">{t('vanLoad.selectAll')}</span>}
          >
            <div className="border-b border-shell-200 p-3">
              <div className="relative">
                <Plus className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
                <input
                  className="input pl-9"
                  placeholder={t('stockCommon.selectProduct')}
                  value={productTerm}
                  onChange={(e) => setProductTerm(e.target.value)}
                />
              </div>
              {productTerm.trim().length >= 2 && (
                <ul className="mt-2 max-h-48 divide-y divide-shell-100 overflow-y-auto rounded-lg border border-shell-200">
                  {products.isLoading && <LoadingBlock />}
                  {(products.data?.items ?? []).map((p) => (
                    <li key={p.id}>
                      <button
                        type="button"
                        className="flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-shell-50"
                        onClick={() => void addProduct(p)}
                      >
                        <span className="truncate">{p.name}</span>
                        <span className="tabular text-2xs text-shell-400">{p.sku}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {suggest.isPending ? (
              <LoadingBlock />
            ) : lines.length === 0 ? (
              <EmptyState title={t('stockCommon.noLines')} />
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>{t('stockCommon.sku')}</th>
                      <th>{t('stockCommon.product')}</th>
                      <th className="text-right">{t('vanLoad.onVan')}</th>
                      <th className="text-right">{t('vanLoad.depotAvailable')}</th>
                      <th className="text-right">{t('vanLoad.suggestedCases')}</th>
                      <th className="text-right">{t('vanLoad.loadCases')}</th>
                      <th className="min-w-[14rem]">{t('vanLoad.reason')}</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {lines.map((l, i) => (
                      <tr key={l.product_id}>
                        <td className="tabular whitespace-nowrap">{l.sku}</td>
                        <td className="min-w-[10rem]">{l.name}</td>
                        <td className="tabular text-right text-xs text-shell-500">
                          {formatNumber(l.on_van, { decimals: 0 })}
                        </td>
                        <td className="tabular text-right text-xs text-shell-500">
                          {formatNumber(l.depot_available, { decimals: 0 })}
                        </td>
                        <td className="tabular text-right">{formatNumber(l.suggested_cases)}</td>
                        <td className="text-right">
                          <input
                            type="number"
                            min={0}
                            step="1"
                            className="input tabular w-20 py-1 text-right"
                            value={l.cases}
                            onChange={(e) =>
                              setLines((prev) =>
                                prev.map((x, xi) => (xi === i ? { ...x, cases: e.target.value } : x)),
                              )
                            }
                          />
                        </td>
                        <td className="text-xs text-shell-600">
                          <p className="mb-1 line-clamp-2">{lang === 'en' ? l.reason_en : l.reason_tr}</p>
                          {l.confidence > 0 && (
                            <div className="flex items-center gap-2">
                              <div className="h-1.5 w-20 overflow-hidden rounded-full bg-shell-200">
                                <div
                                  className="h-full rounded-full bg-brand-500"
                                  style={{ width: `${Math.round(Math.min(1, l.confidence) * 100)}%` }}
                                />
                              </div>
                              <span className="tabular text-2xs text-shell-400">
                                {formatNumber(l.confidence * 100, { decimals: 0 })}%
                              </span>
                            </div>
                          )}
                        </td>
                        <td className="text-right">
                          <button
                            type="button"
                            className="btn-ghost btn-sm text-danger-600"
                            onClick={() => setLines((prev) => prev.filter((_, xi) => xi !== i))}
                            aria-label={t('common.delete')}
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          {/* -------------------------- Recent loads -------------------------- */}
          <Card title={t('vanLoad.recentLoads')} bodyClassName="p-0">
            {recent.isLoading ? (
              <LoadingBlock />
            ) : recent.isError ? (
              <ErrorState error={recent.error} onRetry={() => void recent.refetch()} />
            ) : (recent.data?.items ?? []).length === 0 ? (
              <EmptyState />
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>{t('stockCommon.documentNo')}</th>
                      <th>{t('common.date')}</th>
                      <th className="text-right">{t('vehicleStock.volumeUsage')}</th>
                      <th className="text-right">{t('vehicleStock.weightUsage')}</th>
                      <th className="text-right">{t('common.total')}</th>
                      <th>{t('common.status')}</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {(recent.data?.items ?? []).map((l) => (
                      <tr key={l.id}>
                        <td className="tabular whitespace-nowrap">{l.document_no}</td>
                        <td className="whitespace-nowrap text-xs">{formatDate(l.load_date, { short: true })}</td>
                        <td className="tabular text-right">{formatNumber(l.total_volume_l, { decimals: 1 })}</td>
                        <td className="tabular text-right">{formatNumber(l.total_weight_kg, { decimals: 1 })}</td>
                        <td className="tabular text-right">{formatMoney(l.total_cost)}</td>
                        <td>
                          <StatusBadge
                            status={l.is_posted ? 'POSTED' : 'DRAFT'}
                            label={l.is_posted ? t('vanLoad.posted_') : t('vanLoad.draft')}
                          />
                        </td>
                        <td className="text-right">
                          {!l.is_posted && can('stock.van_load', 'EXECUTE') && (
                            <button
                              type="button"
                              className="btn-secondary btn-sm"
                              disabled={post.isPending}
                              onClick={() => post.mutate(l.id)}
                            >
                              <Upload className="h-3.5 w-3.5" />
                              {t('vanLoad.postLoad')}
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>

        {/* ---------------------------- Side panel ---------------------------- */}
        <div className="space-y-4">
          <Card title={t('vanLoad.capacityUsage')}>
            <div className="space-y-4">
              <UsageBar
                label={t('vehicleStock.volumeUsage')}
                used={usage.volumeTotal}
                capacity={usage.capVolume}
                percent={usage.volumePct}
                unit="L"
              />
              <UsageBar
                label={t('vehicleStock.weightUsage')}
                used={usage.weightTotal}
                capacity={usage.capWeight}
                percent={usage.weightPct}
                unit="kg"
              />
              <dl className="space-y-2 border-t border-shell-200 pt-3 text-sm">
                <div className="flex justify-between">
                  <dt className="text-shell-500">{t('stockCommon.cases')}</dt>
                  <dd className="tabular font-medium">{formatNumber(usage.cases)}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-shell-500">{t('vanLoad.suggestions')}</dt>
                  <dd className="tabular font-medium">{formatNumber(payloadLines.length)}</dd>
                </div>
              </dl>
              {overloaded && (
                <p className="rounded-lg bg-danger-50 px-3 py-2 text-xs text-danger-700">
                  {t('common.warning')}: {formatNumber(Math.max(usage.volumePct, usage.weightPct), { decimals: 0 })}%
                </p>
              )}
            </div>
          </Card>

          <Card title={t('vanLoad.createLoad')}>
            <div className="space-y-3">
              <Field label={t('common.notes')}>
                <textarea
                  className="input"
                  rows={2}
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                />
              </Field>
              {can('stock.van_load', 'CREATE') && (
                <button
                  type="button"
                  className="btn-primary w-full"
                  disabled={!ready || create.isPending || createdId !== null}
                  onClick={() => create.mutate()}
                >
                  {create.isPending ? <Spinner /> : <Plus className="h-4 w-4" />}
                  {t('vanLoad.createLoad')}
                </button>
              )}
              {createdId !== null && can('stock.van_load', 'EXECUTE') && (
                <button
                  type="button"
                  className="btn-secondary w-full"
                  disabled={post.isPending}
                  onClick={() => post.mutate(createdId)}
                >
                  {post.isPending ? <Spinner /> : <Upload className="h-4 w-4" />}
                  {t('vanLoad.postLoad')}
                </button>
              )}
              <SectionTitle>{t('common.summary')}</SectionTitle>
              <p className="text-xs text-shell-500">
                {vehicle ? vehicle.plate_number : '—'} · {formatDate(onDate, { short: true })}
              </p>
            </div>
          </Card>
        </div>
      </div>
    </>
  )
}

function UsageBar({ label, used, capacity, percent, unit }: {
  label: string
  used: number
  capacity: number
  percent: number
  unit: string
}) {
  const pct = Math.max(0, Math.min(100, percent))
  const tone = percent > 100 ? 'bg-danger-500' : percent >= 80 ? 'bg-warn-500' : 'bg-brand-500'
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between text-xs">
        <span className="font-medium text-shell-600">{label}</span>
        <span className="tabular text-shell-500">
          {formatNumber(used, { decimals: 1 })} / {formatNumber(capacity, { decimals: 0 })} {unit}
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-shell-200">
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}
