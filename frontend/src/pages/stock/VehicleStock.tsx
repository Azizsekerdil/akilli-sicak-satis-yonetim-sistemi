/**
 * Araç Stokları / Vehicle stock.
 *
 * One van at a time: what is on board in cases *and* base units, which lots and
 * when they expire, and how full the van is against its declared volume and
 * weight capacity — the two numbers that decide whether the next load fits.
 */
import { useQuery } from '@tanstack/react-query'
import { RefreshCw, Search, Truck } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { daysUntil, formatDate, formatMoney, formatNumber, formatQuantity } from '@/lib/format'
import {
  Card,
  EmptyState,
  ErrorState,
  ExpiryBadge,
  PageHeader,
  SkeletonRows,
  useToast,
} from '@/components/ui'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface VehicleRow {
  id: number
  code: string
  plate_number: string
  name?: string | null
  status: string
  is_active: boolean
  warehouse_id?: number | null
  default_salesperson_id?: number | null
  capacity_volume_l: number
  capacity_weight_kg: number
  capacity_cases?: number | null
}

interface LotRow {
  lot_id?: number | null
  lot_number?: string | null
  expiry_date?: string | null
  qty: number | string
}

interface StockRow {
  product_id: number
  sku: string
  name: string
  uom: string
  base_qty: number | string
  case_qty: number | string
  value: number | string
  lots: LotRow[]
}

interface Capacity {
  vehicle_id: number
  plate_number: string
  warehouse_id: number
  volume_l: number
  weight_kg: number
  capacity_volume_l: number
  capacity_weight_kg: number
  volume_percent: number
  weight_percent: number
  base_quantity: number | string
  product_count: number
}

const num = (v: unknown): number => {
  const n = typeof v === 'number' ? v : Number(v ?? 0)
  return Number.isFinite(n) ? n : 0
}

function Bar({ label, used, capacity, percent, unit }: {
  label: string
  used: number
  capacity: number
  percent: number
  unit: string
}) {
  const pct = Math.max(0, Math.min(100, percent))
  const tone = pct >= 95 ? 'bg-danger-500' : pct >= 80 ? 'bg-warn-500' : 'bg-brand-500'
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between text-xs">
        <span className="font-medium text-shell-600">{label}</span>
        <span className="tabular text-shell-500">
          {formatNumber(used, { decimals: 1 })} / {formatNumber(capacity, { decimals: 0 })} {unit}
          {' · '}
          {formatNumber(percent, { decimals: 1 })}%
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-shell-200">
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function VehicleStock() {
  const { t } = useTranslation()
  const { session } = useAuth()
  const { push } = useToast()

  const [vehicleId, setVehicleId] = useState<number | null>(null)
  const [term, setTerm] = useState('')

  const vehicles = useQuery({
    queryKey: ['vehicles', 'picker'],
    queryFn: () => api.get<Paged<VehicleRow>>('/vehicles', { size: 200, is_active: true }),
  })

  /* Default to the signed-in salesperson's own van. */
  useEffect(() => {
    if (vehicleId !== null) return
    const items = vehicles.data?.items ?? []
    if (items.length === 0) return
    const mine = items.find((v) => v.default_salesperson_id === session?.salesperson_id)
    setVehicleId((mine ?? items[0]).id)
  }, [vehicles.data, session?.salesperson_id, vehicleId])

  const stock = useQuery({
    queryKey: ['vehicle-stock', vehicleId],
    queryFn: () => api.get<StockRow[]>(`/warehouses/vehicles/${vehicleId}/stock`),
    enabled: !!vehicleId,
  })

  const capacity = useQuery({
    queryKey: ['vehicle-capacity', vehicleId],
    queryFn: () => api.get<Capacity>(`/vehicles/${vehicleId}/capacity`),
    enabled: !!vehicleId,
    retry: false,
    throwOnError: false,
  })

  const rows = useMemo(() => {
    const list = stock.data ?? []
    const q = term.trim().toLocaleLowerCase('tr-TR')
    if (!q) return list
    return list.filter(
      (r) =>
        r.name.toLocaleLowerCase('tr-TR').includes(q) || r.sku.toLocaleLowerCase('tr-TR').includes(q),
    )
  }, [stock.data, term])

  const totalValue = rows.reduce((sum, r) => sum + num(r.value), 0)
  const vehicle = (vehicles.data?.items ?? []).find((v) => v.id === vehicleId) ?? null
  const cap = capacity.data

  return (
    <>
      <PageHeader
        title={t('vehicleStock.title')}
        subtitle={t('vehicleStock.subtitle')}
        icon={<Truck className="h-5 w-5" />}
        actions={
          <button
            type="button"
            className="btn-secondary btn-sm"
            disabled={!vehicleId}
            onClick={() => {
              void stock.refetch()
              void capacity.refetch()
              push('info', t('common.refresh'))
            }}
          >
            <RefreshCw className="h-4 w-4" />
            {t('common.refresh')}
          </button>
        }
      />

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <div className="min-w-[14rem]">
          <label className="label">{t('stockCommon.vehicle')}</label>
          <select
            className="input"
            value={vehicleId ?? ''}
            onChange={(e) => setVehicleId(e.target.value ? Number(e.target.value) : null)}
          >
            <option value="">{t('stockCommon.selectVehicle')}</option>
            {(vehicles.data?.items ?? []).map((v) => (
              <option key={v.id} value={v.id}>
                {v.plate_number}
                {v.name ? ` — ${v.name}` : ''}
              </option>
            ))}
          </select>
        </div>
        <div className="relative min-w-[12rem] flex-1">
          <label className="label">{t('common.search')}</label>
          <Search className="absolute left-2.5 top-[2.15rem] h-4 w-4 text-shell-400" />
          <input
            className="input pl-9"
            placeholder={t('common.search')}
            value={term}
            onChange={(e) => setTerm(e.target.value)}
          />
        </div>
      </div>

      {vehicles.isLoading ? (
        <Card>
          <SkeletonRows rows={3} cols={3} />
        </Card>
      ) : (vehicles.data?.items ?? []).length === 0 ? (
        <Card>
          <EmptyState title={t('vehicleStock.noVehicle')} />
        </Card>
      ) : !vehicleId ? (
        <Card>
          <EmptyState title={t('vehicleStock.selectHint')} />
        </Card>
      ) : (
        <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_20rem]">
          <Card
            title={vehicle ? `${vehicle.plate_number} — ${t('hotSale.vanStock')}` : t('hotSale.vanStock')}
            bodyClassName="p-0"
            actions={<span className="tabular text-2xs text-shell-400">{formatMoney(totalValue)}</span>}
          >
            {stock.isLoading ? (
              <SkeletonRows rows={8} cols={6} />
            ) : stock.isError ? (
              <ErrorState error={stock.error} onRetry={() => void stock.refetch()} />
            ) : rows.length === 0 ? (
              <EmptyState />
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>{t('stockCommon.sku')}</th>
                      <th>{t('stockCommon.product')}</th>
                      <th className="text-right">{t('stockCommon.cases')}</th>
                      <th className="text-right">{t('stockCommon.baseUnits')}</th>
                      <th>{t('stockCommon.lot')}</th>
                      <th>{t('stockCommon.expiry')}</th>
                      <th className="text-right">{t('stockCommon.value')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.product_id}>
                        <td className="tabular whitespace-nowrap">{r.sku}</td>
                        <td className="min-w-[10rem]">{r.name}</td>
                        <td className="tabular text-right font-medium">
                          {formatNumber(r.case_qty, { decimals: 2 })}
                        </td>
                        <td className="tabular text-right">{formatQuantity(r.base_qty, r.uom)}</td>
                        <td className="text-xs">
                          {r.lots.length === 0 ? (
                            '—'
                          ) : (
                            <ul className="space-y-0.5">
                              {r.lots.map((l, i) => (
                                <li key={`${r.product_id}-${l.lot_id ?? i}`} className="whitespace-nowrap">
                                  {l.lot_number ?? '—'}
                                  <span className="tabular ml-1 text-shell-400">
                                    {formatNumber(l.qty, { decimals: 2 })}
                                  </span>
                                </li>
                              ))}
                            </ul>
                          )}
                        </td>
                        <td className="text-xs">
                          {r.lots.length === 0 ? (
                            '—'
                          ) : (
                            <ul className="space-y-0.5">
                              {r.lots.map((l, i) => (
                                <li
                                  key={`${r.product_id}-exp-${l.lot_id ?? i}`}
                                  className="flex items-center gap-1.5 whitespace-nowrap"
                                >
                                  {l.expiry_date ? formatDate(l.expiry_date, { short: true }) : '—'}
                                  <ExpiryBadge days={daysUntil(l.expiry_date)} />
                                </li>
                              ))}
                            </ul>
                          )}
                        </td>
                        <td className="tabular text-right">{formatMoney(r.value)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card title={t('vehicleStock.capacity')}>
            {capacity.isLoading ? (
              <SkeletonRows rows={2} cols={1} />
            ) : !cap ? (
              <EmptyState title={t('stockCommon.moduleUnavailable')} />
            ) : (
              <div className="space-y-5">
                <Bar
                  label={t('vehicleStock.volumeUsage')}
                  used={cap.volume_l}
                  capacity={cap.capacity_volume_l}
                  percent={cap.volume_percent}
                  unit="L"
                />
                <Bar
                  label={t('vehicleStock.weightUsage')}
                  used={cap.weight_kg}
                  capacity={cap.capacity_weight_kg}
                  percent={cap.weight_percent}
                  unit="kg"
                />
                <dl className="space-y-2 border-t border-shell-200 pt-4 text-sm">
                  <div className="flex justify-between">
                    <dt className="text-shell-500">{t('vehicleStock.productCount')}</dt>
                    <dd className="tabular font-medium">{formatNumber(cap.product_count)}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-shell-500">{t('vehicleStock.totalBase')}</dt>
                    <dd className="tabular font-medium">{formatNumber(cap.base_quantity, { decimals: 2 })}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-shell-500">{t('stockCommon.value')}</dt>
                    <dd className="tabular font-medium">{formatMoney(totalValue)}</dd>
                  </div>
                  {vehicle?.capacity_cases != null && (
                    <div className="flex justify-between">
                      <dt className="text-shell-500">{t('vehicles.capacityCases')}</dt>
                      <dd className="tabular font-medium">{formatNumber(vehicle.capacity_cases)}</dd>
                    </div>
                  )}
                </dl>
              </div>
            )}
          </Card>
        </div>
      )}
    </>
  )
}
