/**
 * Depolar / Warehouses.
 *
 * Master data on the left, the selected warehouse's stock on the right.  A
 * stock row drills into its stock card — the movement ledger with a running
 * balance, which is the document a warehouse manager actually argues with.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Pencil, Plus, Search, Trash2, Warehouse as WarehouseIcon } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { currentLanguage } from '@/lib/i18n'
import { formatDate, formatMoney, formatNumber, formatQuantity } from '@/lib/format'
import {
  Card,
  EmptyState,
  ErrorState,
  ExpiryBadge,
  Field,
  Modal,
  PageHeader,
  Pagination,
  SectionTitle,
  SkeletonRows,
  Spinner,
  StatusBadge,
  useToast,
} from '@/components/ui'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface WarehouseRow {
  id: number
  code: string
  name: string
  name_en?: string | null
  description?: string | null
  warehouse_type: string
  is_active: boolean
  region_id?: number | null
  manager_id?: number | null
  address?: string | null
  city?: string | null
  latitude?: number | null
  longitude?: number | null
  capacity_volume_l?: number | null
  capacity_weight_kg?: number | null
  allows_negative_stock: boolean
  allocation_strategy: string
  stock_value?: number | string | null
  product_count?: number | null
}

interface BalanceRow {
  id: number
  warehouse_id: number
  product_id: number
  lot_id: number
  status: string
  quantity: number | string
  reserved_quantity: number | string
  available: number | string
  average_cost: number | string
  value: number | string
  product_sku?: string | null
  product_name?: string | null
  uom?: string | null
  case_qty?: number | string | null
  lot_number?: string | null
  expiry_date?: string | null
  days_to_expiry?: number | null
  is_blocked: boolean
}

interface Valuation {
  warehouse_id: number
  warehouse_code: string
  warehouse_name: string
  currency: string
  total_value: number | string
  total_quantity: number | string
  product_count: number
  lot_count: number
}

interface CardRow {
  movement_id: number
  moved_at?: string | null
  movement_type: string
  lot_number?: string | null
  expiry_date?: string | null
  quantity_in: number | string
  quantity_out: number | string
  unit_cost: number | string
  balance: number | string
  reference_type?: string | null
  reference_no?: string | null
}

interface StockCard {
  product_id: number
  sku: string
  product_name: string
  uom: string
  warehouse_id: number
  warehouse_code: string
  opening_balance: number | string
  closing_balance: number | string
  total_in: number | string
  total_out: number | string
  rows: CardRow[]
}

const TYPES = ['CENTRAL', 'REGIONAL', 'TRANSIT', 'VEHICLE', 'QUARANTINE']
const STRATEGIES = ['FEFO', 'FIFO', 'LIFO']
const SIZE = 25

export default function Warehouses() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()
  const lang = currentLanguage()

  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [type, setType] = useState('')
  const [selected, setSelected] = useState<WarehouseRow | null>(null)
  const [editing, setEditing] = useState<WarehouseRow | null | undefined>(undefined)

  const [stockPage, setStockPage] = useState(1)
  const [stockTerm, setStockTerm] = useState('')
  const [includeZero, setIncludeZero] = useState(false)
  const [cardOf, setCardOf] = useState<BalanceRow | null>(null)

  const name = (w: { name: string; name_en?: string | null }) =>
    lang === 'en' && w.name_en ? w.name_en : w.name

  const listParams = { page, size: SIZE, search: search || undefined, warehouse_type: type || undefined }
  const list = useQuery({
    queryKey: ['warehouses', listParams],
    queryFn: () => api.get<Paged<WarehouseRow>>('/warehouses', listParams),
  })

  const stockParams = {
    page: stockPage,
    size: SIZE,
    search: stockTerm || undefined,
    include_zero: includeZero || undefined,
  }
  const stock = useQuery({
    queryKey: ['warehouse-stock', selected?.id, stockParams],
    queryFn: () => api.get<Paged<BalanceRow>>(`/warehouses/${selected!.id}/stock`, stockParams),
    enabled: !!selected,
  })

  const valuation = useQuery({
    queryKey: ['warehouse-valuation', selected?.id],
    queryFn: () => api.get<Valuation>(`/warehouses/${selected!.id}/valuation`),
    enabled: !!selected,
  })

  const card = useQuery({
    queryKey: ['stock-card', selected?.id, cardOf?.product_id],
    queryFn: () =>
      api.get<StockCard>('/warehouses/stock/card', {
        product_id: cardOf!.product_id,
        warehouse_id: selected!.id,
      }),
    enabled: !!cardOf && !!selected,
  })

  const remove = useMutation({
    mutationFn: (id: number) => api.delete<{ message: string }>(`/warehouses/${id}`),
    onSuccess: (_d, id) => {
      push('success', t('stockCommon.removed'))
      if (selected?.id === id) setSelected(null)
      void qc.invalidateQueries({ queryKey: ['warehouses'] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const rows = list.data?.items ?? []

  return (
    <>
      <PageHeader
        title={t('warehouses.title')}
        subtitle={t('warehouses.subtitle')}
        icon={<WarehouseIcon className="h-5 w-5" />}
        actions={
          can('stock.warehouses', 'CREATE') && (
            <button type="button" className="btn-primary btn-sm" onClick={() => setEditing(null)}>
              <Plus className="h-4 w-4" />
              {t('warehouses.new')}
            </button>
          )
        }
      />

      <div className="grid gap-5 xl:grid-cols-[22rem_minmax(0,1fr)]">
        {/* --------------------------- Warehouse list --------------------------- */}
        <Card bodyClassName="p-0">
          <div className="flex flex-wrap gap-2 border-b border-shell-200 p-3">
            <div className="relative min-w-[9rem] flex-1">
              <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
              <input
                className="input pl-9"
                placeholder={t('common.search')}
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value)
                  setPage(1)
                }}
              />
            </div>
            <select
              className="input w-auto"
              value={type}
              onChange={(e) => {
                setType(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {TYPES.map((ty) => (
                <option key={ty} value={ty}>
                  {ty}
                </option>
              ))}
            </select>
          </div>

          {list.isLoading ? (
            <SkeletonRows rows={6} cols={2} />
          ) : list.isError ? (
            <ErrorState error={list.error} onRetry={() => void list.refetch()} />
          ) : rows.length === 0 ? (
            <EmptyState />
          ) : (
            <ul className="divide-y divide-shell-100">
              {rows.map((w) => (
                <li
                  key={w.id}
                  className={selected?.id === w.id ? 'bg-brand-50/60' : 'hover:bg-shell-50'}
                >
                  <div className="flex items-center gap-2 px-3 py-2.5">
                    <button
                      type="button"
                      className="min-w-0 flex-1 text-left"
                      onClick={() => {
                        setSelected(w)
                        setStockPage(1)
                      }}
                    >
                      <p className="truncate text-sm font-medium text-shell-800">{name(w)}</p>
                      <p className="text-2xs text-shell-500">
                        {w.code} · {w.warehouse_type}
                        {w.city ? ` · ${w.city}` : ''}
                      </p>
                    </button>
                    <StatusBadge status={w.is_active ? 'ACTIVE' : 'PASSIVE'} />
                    {can('stock.warehouses', 'UPDATE') && (
                      <button
                        type="button"
                        className="btn-ghost btn-sm"
                        onClick={() => setEditing(w)}
                        aria-label={t('common.edit')}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </button>
                    )}
                    {can('stock.warehouses', 'DELETE') && (
                      <button
                        type="button"
                        className="btn-ghost btn-sm text-danger-600"
                        onClick={() => {
                          if (window.confirm(t('stockCommon.confirmDelete'))) remove.mutate(w.id)
                        }}
                        aria-label={t('common.delete')}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}

          {list.data && (
            <Pagination
              page={list.data.page}
              pages={list.data.pages}
              total={list.data.total}
              size={list.data.size}
              onPage={setPage}
            />
          )}
        </Card>

        {/* ------------------------------ Stock -------------------------------- */}
        <div className="space-y-4">
          {!selected ? (
            <Card>
              <EmptyState title={t('warehouses.selectHint')} />
            </Card>
          ) : (
            <>
              <div className="grid gap-3 sm:grid-cols-4">
                {[
                  { label: t('warehouses.valuation'), value: formatMoney(valuation.data?.total_value ?? 0) },
                  { label: t('warehouses.totalQuantity'), value: formatNumber(valuation.data?.total_quantity ?? 0) },
                  { label: t('warehouses.productCount'), value: formatNumber(valuation.data?.product_count ?? 0) },
                  { label: t('warehouses.lotCount'), value: formatNumber(valuation.data?.lot_count ?? 0) },
                ].map((k) => (
                  <div key={k.label} className="card p-4">
                    <p className="truncate text-xs font-medium uppercase tracking-wide text-shell-500">
                      {k.label}
                    </p>
                    <p className="tabular mt-1.5 text-xl font-semibold text-shell-900">{k.value}</p>
                  </div>
                ))}
              </div>

              <Card title={`${name(selected)} — ${t('warehouses.stockTab')}`} bodyClassName="p-0">
                <div className="flex flex-wrap items-center gap-3 border-b border-shell-200 p-3">
                  <div className="relative min-w-[10rem] flex-1">
                    <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
                    <input
                      className="input pl-9"
                      placeholder={t('common.search')}
                      value={stockTerm}
                      onChange={(e) => {
                        setStockTerm(e.target.value)
                        setStockPage(1)
                      }}
                    />
                  </div>
                  <label className="flex items-center gap-1.5 text-xs text-shell-600">
                    <input
                      type="checkbox"
                      checked={includeZero}
                      onChange={(e) => {
                        setIncludeZero(e.target.checked)
                        setStockPage(1)
                      }}
                    />
                    {t('warehouses.includeZero')}
                  </label>
                </div>

                {stock.isLoading ? (
                  <SkeletonRows rows={8} cols={6} />
                ) : stock.isError ? (
                  <ErrorState error={stock.error} onRetry={() => void stock.refetch()} />
                ) : (stock.data?.items ?? []).length === 0 ? (
                  <EmptyState />
                ) : (
                  <div className="table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>{t('stockCommon.sku')}</th>
                          <th>{t('stockCommon.product')}</th>
                          <th>{t('stockCommon.lot')}</th>
                          <th>{t('stockCommon.expiry')}</th>
                          <th className="text-right">{t('stockCommon.quantity')}</th>
                          <th className="text-right">{t('stockCommon.cases')}</th>
                          <th className="text-right">{t('stockCommon.value')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(stock.data?.items ?? []).map((b) => (
                          <tr
                            key={b.id}
                            className="cursor-pointer"
                            onClick={() => setCardOf(b)}
                            title={t('warehouses.stockCard')}
                          >
                            <td className="tabular whitespace-nowrap">{b.product_sku ?? '—'}</td>
                            <td className="min-w-[10rem]">{b.product_name ?? `#${b.product_id}`}</td>
                            <td className="text-xs">
                              {b.lot_number ?? '—'}
                              {b.is_blocked && <span className="badge-danger ml-1">{t('lots.blocked')}</span>}
                            </td>
                            <td className="whitespace-nowrap text-xs">
                              {b.expiry_date ? (
                                <span className="flex items-center gap-1.5">
                                  {formatDate(b.expiry_date, { short: true })}
                                  <ExpiryBadge days={b.days_to_expiry ?? null} />
                                </span>
                              ) : (
                                '—'
                              )}
                            </td>
                            <td className="tabular text-right">
                              {formatQuantity(b.quantity, b.uom ?? undefined)}
                            </td>
                            <td className="tabular text-right">{formatNumber(b.case_qty ?? 0, { decimals: 2 })}</td>
                            <td className="tabular text-right">{formatMoney(b.value)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                {stock.data && (
                  <Pagination
                    page={stock.data.page}
                    pages={stock.data.pages}
                    total={stock.data.total}
                    size={stock.data.size}
                    onPage={setStockPage}
                  />
                )}
              </Card>
            </>
          )}
        </div>
      </div>

      {/* ------------------------------ Stock card ------------------------------ */}
      {cardOf && (
        <Modal
          open
          size="xl"
          onClose={() => setCardOf(null)}
          title={`${t('warehouses.stockCard')} — ${cardOf.product_name ?? cardOf.product_sku ?? ''}`}
        >
          {card.isLoading ? (
            <SkeletonRows rows={8} cols={6} />
          ) : card.isError ? (
            <ErrorState error={card.error} onRetry={() => void card.refetch()} />
          ) : (
            <>
              <div className="mb-4 grid gap-3 sm:grid-cols-4">
                {[
                  [t('warehouses.opening'), card.data?.opening_balance],
                  [t('warehouses.totalIn'), card.data?.total_in],
                  [t('warehouses.totalOut'), card.data?.total_out],
                  [t('warehouses.closing'), card.data?.closing_balance],
                ].map(([label, value]) => (
                  <div key={String(label)} className="rounded-lg border border-shell-200 p-3">
                    <p className="text-2xs uppercase tracking-wide text-shell-500">{label}</p>
                    <p className="tabular mt-1 text-sm font-semibold">{formatNumber(value ?? 0, { decimals: 2 })}</p>
                  </div>
                ))}
              </div>
              {(card.data?.rows ?? []).length === 0 ? (
                <EmptyState />
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>{t('common.date')}</th>
                        <th>{t('warehouses.movement')}</th>
                        <th>{t('stockCommon.lot')}</th>
                        <th>{t('warehouses.reference')}</th>
                        <th className="text-right">{t('warehouses.inQty')}</th>
                        <th className="text-right">{t('warehouses.outQty')}</th>
                        <th className="text-right">{t('warehouses.balance')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(card.data?.rows ?? []).map((r) => (
                        <tr key={r.movement_id}>
                          <td className="whitespace-nowrap text-xs">
                            {formatDate(r.moved_at, { withTime: true, short: true })}
                          </td>
                          <td className="text-xs">{r.movement_type}</td>
                          <td className="text-xs">{r.lot_number ?? '—'}</td>
                          <td className="text-xs text-shell-500">{r.reference_no ?? '—'}</td>
                          <td className="tabular text-right text-ok-700">
                            {Number(r.quantity_in) ? formatNumber(r.quantity_in, { decimals: 2 }) : '—'}
                          </td>
                          <td className="tabular text-right text-danger-700">
                            {Number(r.quantity_out) ? formatNumber(r.quantity_out, { decimals: 2 }) : '—'}
                          </td>
                          <td className="tabular text-right font-medium">
                            {formatNumber(r.balance, { decimals: 2 })}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </Modal>
      )}

      {editing !== undefined && (
        <WarehouseEditor
          warehouse={editing}
          onClose={() => setEditing(undefined)}
          onSaved={() => {
            setEditing(undefined)
            void qc.invalidateQueries({ queryKey: ['warehouses'] })
          }}
        />
      )}
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Editor                                                                     */
/* -------------------------------------------------------------------------- */
function WarehouseEditor({
  warehouse,
  onClose,
  onSaved,
}: {
  warehouse: WarehouseRow | null
  onClose: () => void
  onSaved: () => void
}) {
  const { t } = useTranslation()
  const { push } = useToast()
  const [f, setF] = useState({
    code: warehouse?.code ?? '',
    name: warehouse?.name ?? '',
    name_en: warehouse?.name_en ?? '',
    warehouse_type: warehouse?.warehouse_type ?? 'CENTRAL',
    city: warehouse?.city ?? '',
    address: warehouse?.address ?? '',
    region_id: warehouse?.region_id ? String(warehouse.region_id) : '',
    latitude: warehouse?.latitude != null ? String(warehouse.latitude) : '',
    longitude: warehouse?.longitude != null ? String(warehouse.longitude) : '',
    capacity_volume_l: warehouse?.capacity_volume_l != null ? String(warehouse.capacity_volume_l) : '',
    capacity_weight_kg: warehouse?.capacity_weight_kg != null ? String(warehouse.capacity_weight_kg) : '',
    allocation_strategy: warehouse?.allocation_strategy ?? 'FEFO',
    allows_negative_stock: warehouse?.allows_negative_stock ?? false,
    is_active: warehouse?.is_active ?? true,
    description: warehouse?.description ?? '',
  })
  const set = (k: keyof typeof f, v: string | boolean) => setF((p) => ({ ...p, [k]: v }))

  const save = useMutation({
    mutationFn: () => {
      const numOrNull = (v: string) => (v.trim() === '' ? null : Number(v))
      const body: Record<string, unknown> = {
        name: f.name.trim(),
        name_en: f.name_en.trim() || null,
        warehouse_type: f.warehouse_type,
        city: f.city.trim() || null,
        address: f.address.trim() || null,
        region_id: numOrNull(f.region_id),
        latitude: numOrNull(f.latitude),
        longitude: numOrNull(f.longitude),
        capacity_volume_l: numOrNull(f.capacity_volume_l),
        capacity_weight_kg: numOrNull(f.capacity_weight_kg),
        allocation_strategy: f.allocation_strategy,
        allows_negative_stock: f.allows_negative_stock,
        is_active: f.is_active,
        description: f.description.trim() || null,
      }
      if (warehouse) return api.put<WarehouseRow>(`/warehouses/${warehouse.id}`, body)
      return api.post<WarehouseRow>('/warehouses', { ...body, code: f.code.trim() })
    },
    onSuccess: () => {
      push('success', t('stockCommon.saved'))
      onSaved()
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title={warehouse ? t('warehouses.edit') : t('warehouses.new')}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={save.isPending || !f.name.trim() || (!warehouse && !f.code.trim())}
            onClick={() => save.mutate()}
          >
            {save.isPending && <Spinner />}
            {t('common.save')}
          </button>
        </>
      }
    >
      <div className="space-y-5">
        <div>
          <SectionTitle>{t('common.details')}</SectionTitle>
          <div className="grid gap-3 sm:grid-cols-2">
            {!warehouse && (
              <Field label={t('common.code')} required>
                <input className="input" value={f.code} onChange={(e) => set('code', e.target.value)} />
              </Field>
            )}
            <Field label={t('common.name')} required>
              <input className="input" value={f.name} onChange={(e) => set('name', e.target.value)} />
            </Field>
            <Field label={t('products.nameEn')}>
              <input className="input" value={f.name_en} onChange={(e) => set('name_en', e.target.value)} />
            </Field>
            <Field label={t('warehouses.type')}>
              <select
                className="input"
                value={f.warehouse_type}
                onChange={(e) => set('warehouse_type', e.target.value)}
              >
                {TYPES.map((ty) => (
                  <option key={ty} value={ty}>
                    {ty}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t('warehouses.allocation')}>
              <select
                className="input"
                value={f.allocation_strategy}
                onChange={(e) => set('allocation_strategy', e.target.value)}
              >
                {STRATEGIES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t('warehouses.regionId')}>
              <input
                className="input tabular"
                type="number"
                value={f.region_id}
                onChange={(e) => set('region_id', e.target.value)}
              />
            </Field>
          </div>
        </div>

        <div>
          <SectionTitle>{t('warehouses.address')}</SectionTitle>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t('warehouses.city')}>
              <input className="input" value={f.city} onChange={(e) => set('city', e.target.value)} />
            </Field>
            <Field label={t('warehouses.address')}>
              <input className="input" value={f.address} onChange={(e) => set('address', e.target.value)} />
            </Field>
            <Field label={t('warehouses.latitude')}>
              <input
                className="input tabular"
                type="number"
                step="0.000001"
                value={f.latitude}
                onChange={(e) => set('latitude', e.target.value)}
              />
            </Field>
            <Field label={t('warehouses.longitude')}>
              <input
                className="input tabular"
                type="number"
                step="0.000001"
                value={f.longitude}
                onChange={(e) => set('longitude', e.target.value)}
              />
            </Field>
          </div>
        </div>

        <div>
          <SectionTitle>{t('warehouses.capacityVolume')}</SectionTitle>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t('warehouses.capacityVolume')}>
              <input
                className="input tabular"
                type="number"
                step="0.01"
                value={f.capacity_volume_l}
                onChange={(e) => set('capacity_volume_l', e.target.value)}
              />
            </Field>
            <Field label={t('warehouses.capacityWeight')}>
              <input
                className="input tabular"
                type="number"
                step="0.01"
                value={f.capacity_weight_kg}
                onChange={(e) => set('capacity_weight_kg', e.target.value)}
              />
            </Field>
            <label className="flex items-center gap-2 pt-5 text-sm text-shell-700">
              <input
                type="checkbox"
                checked={f.allows_negative_stock}
                onChange={(e) => set('allows_negative_stock', e.target.checked)}
              />
              {t('warehouses.allowsNegative')}
            </label>
            <label className="flex items-center gap-2 pt-5 text-sm text-shell-700">
              <input type="checkbox" checked={f.is_active} onChange={(e) => set('is_active', e.target.checked)} />
              {t('common.active')}
            </label>
          </div>
        </div>

        <Field label={t('common.description')}>
          <textarea
            className="input"
            rows={2}
            value={f.description}
            onChange={(e) => set('description', e.target.value)}
          />
        </Field>
      </div>
    </Modal>
  )
}
