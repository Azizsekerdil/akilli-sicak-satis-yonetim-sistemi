/**
 * Gün Yönetimi / Day sessions.
 *
 * Opening a day locks a salesperson to a van; closing it reconciles the van
 * against the ledger:
 *   opening + loaded + reloaded − sold + returned − wastage = theoretical
 *   variance = theoretical − counted   (positive means stock is missing)
 * The cash side is reconciled the same way, against declared cash.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarDays, ClipboardList, LockKeyhole, Play } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatMoney, formatNumber } from '@/lib/format'
import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  Modal,
  PageHeader,
  Pagination,
  SkeletonRows,
  Spinner,
  StatusBadge,
  useToast,
} from '@/components/ui'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface SessionRow {
  id: number
  session_date: string
  salesperson_id: number
  vehicle_id: number
  route_id?: number | null
  warehouse_id?: number | null
  status: string
  opened_at?: string | null
  closed_at?: string | null
  start_odometer_km?: number | null
  end_odometer_km?: number | null
  loaded_qty: number | string
  reloaded_qty: number | string
  sold_qty: number | string
  returned_qty: number | string
  wastage_qty: number | string
  theoretical_qty: number | string
  counted_qty: number | string
  variance_qty: number | string
  variance_value: number | string
  total_sales_amount: number | string
  total_collected_cash: number | string
  total_collected_other: number | string
  declared_cash: number | string
  cash_variance: number | string
  visits_planned: number
  visits_done: number
  invoices_count: number
  has_variance: boolean
  notes?: string | null
}

interface ReconRow {
  product_id: number
  sku?: string | null
  product_name?: string | null
  base_uom?: string | null
  opening: number | string
  loaded: number | string
  reloaded: number | string
  sold: number | string
  returned: number | string
  wastage: number | string
  other: number | string
  on_hand: number | string
  theoretical: number | string
  counted?: number | string | null
  variance?: number | string | null
  unit_cost: number | string
  variance_value?: number | string | null
}

interface Reconciliation {
  session: SessionRow
  rows: ReconRow[]
  total_variance_qty: number | string
  total_variance_value: number | string
  cash_expected: number | string
  cash_declared: number | string
  cash_variance: number | string
}

interface SalespersonRow {
  id: number
  full_name: string
  default_vehicle_id?: number | null
}

interface VehicleRow {
  id: number
  plate_number: string
}

const STATUSES = ['OPEN', 'RECONCILING', 'CLOSED', 'DISPUTED']
const SIZE = 20
const today = () => new Date().toISOString().slice(0, 10)
const num = (v: unknown): number => {
  const n = typeof v === 'number' ? v : Number(v ?? 0)
  return Number.isFinite(n) ? n : 0
}

export default function DaySessions() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()

  const [page, setPage] = useState(1)
  const [status, setStatus] = useState('')
  const [salespersonId, setSalespersonId] = useState('')
  const [vehicleId, setVehicleId] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [onlyVariance, setOnlyVariance] = useState(false)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [opening, setOpening] = useState(false)
  const [closing, setClosing] = useState(false)

  const salespersons = useQuery({
    queryKey: ['salespersons', 'picker'],
    queryFn: () => api.get<Paged<SalespersonRow>>('/vehicles/salespersons', { size: 200, is_active: true }),
    retry: false,
    throwOnError: false,
  })
  const vehicles = useQuery({
    queryKey: ['vehicles', 'picker'],
    queryFn: () => api.get<Paged<VehicleRow>>('/vehicles', { size: 200, is_active: true }),
    retry: false,
    throwOnError: false,
  })

  const listParams = {
    page,
    size: SIZE,
    status: status || undefined,
    salesperson_id: salespersonId || undefined,
    vehicle_id: vehicleId || undefined,
    date_from: dateFrom || undefined,
    date_to: dateTo || undefined,
    has_variance: onlyVariance ? true : undefined,
  }
  const list = useQuery({
    queryKey: ['day-sessions', listParams],
    queryFn: () => api.get<Paged<SessionRow>>('/vehicles/day-sessions', listParams),
  })

  const recon = useQuery({
    queryKey: ['day-session-recon', selectedId],
    queryFn: () => api.get<Reconciliation>(`/vehicles/day-sessions/${selectedId}/reconciliation`),
    enabled: selectedId !== null,
  })

  const personName = (id: number) =>
    (salespersons.data?.items ?? []).find((s) => s.id === id)?.full_name ?? `#${id}`
  const plate = (id: number) =>
    (vehicles.data?.items ?? []).find((v) => v.id === id)?.plate_number ?? `#${id}`

  const rows = list.data?.items ?? []
  const session = recon.data?.session ?? null
  const canClose = session ? session.status === 'OPEN' || session.status === 'RECONCILING' : false

  return (
    <>
      <PageHeader
        title={t('daySessions.title')}
        subtitle={t('daySessions.subtitle')}
        icon={<CalendarDays className="h-5 w-5" />}
        actions={
          can('field.day_session', 'CREATE') && (
            <button type="button" className="btn-primary btn-sm" onClick={() => setOpening(true)}>
              <Play className="h-4 w-4" />
              {t('daySessions.openDay')}
            </button>
          )
        }
      />

      <Card className="mb-4" bodyClassName="p-3">
        <div className="flex flex-wrap items-end gap-2">
          <div>
            <label className="label">{t('common.from')}</label>
            <input
              type="date"
              className="input w-auto"
              value={dateFrom}
              onChange={(e) => {
                setDateFrom(e.target.value)
                setPage(1)
              }}
            />
          </div>
          <div>
            <label className="label">{t('common.to')}</label>
            <input
              type="date"
              className="input w-auto"
              value={dateTo}
              onChange={(e) => {
                setDateTo(e.target.value)
                setPage(1)
              }}
            />
          </div>
          <div>
            <label className="label">{t('stockCommon.salesperson')}</label>
            <select
              className="input w-auto"
              value={salespersonId}
              onChange={(e) => {
                setSalespersonId(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {(salespersons.data?.items ?? []).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.full_name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label">{t('stockCommon.vehicle')}</label>
            <select
              className="input w-auto"
              value={vehicleId}
              onChange={(e) => {
                setVehicleId(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {(vehicles.data?.items ?? []).map((v) => (
                <option key={v.id} value={v.id}>
                  {v.plate_number}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label">{t('common.status')}</label>
            <select
              className="input w-auto"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{t('common.all')}</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <label className="flex items-center gap-1.5 pb-2 text-xs text-shell-600">
            <input
              type="checkbox"
              checked={onlyVariance}
              onChange={(e) => {
                setOnlyVariance(e.target.checked)
                setPage(1)
              }}
            />
            {t('daySessions.onlyVariance')}
          </label>
        </div>
      </Card>

      <div className="grid gap-5 xl:grid-cols-[22rem_minmax(0,1fr)]">
        <Card bodyClassName="p-0">
          {list.isLoading ? (
            <SkeletonRows rows={6} cols={2} />
          ) : list.isError ? (
            <ErrorState error={list.error} onRetry={() => void list.refetch()} />
          ) : rows.length === 0 ? (
            <EmptyState />
          ) : (
            <ul className="divide-y divide-shell-100">
              {rows.map((s) => (
                <li key={s.id} className={selectedId === s.id ? 'bg-brand-50/60' : 'hover:bg-shell-50'}>
                  <button
                    type="button"
                    className="w-full px-3 py-2.5 text-left"
                    onClick={() => setSelectedId(s.id)}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-medium text-shell-800">
                        {formatDate(s.session_date, { short: true })}
                      </span>
                      <StatusBadge status={s.status} />
                    </div>
                    <p className="mt-0.5 truncate text-2xs text-shell-500">
                      {personName(s.salesperson_id)} · {plate(s.vehicle_id)}
                    </p>
                    <div className="mt-1 flex items-center justify-between text-2xs">
                      <span className="tabular text-shell-500">{formatMoney(s.total_sales_amount)}</span>
                      {s.has_variance && (
                        <span className="badge-danger">{t('daySessions.hasVariance')}</span>
                      )}
                    </div>
                  </button>
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

        <div className="space-y-4">
          {!selectedId ? (
            <Card>
              <EmptyState title={t('daySessions.selectHint')} />
            </Card>
          ) : recon.isLoading ? (
            <Card>
              <SkeletonRows rows={8} cols={8} />
            </Card>
          ) : recon.isError ? (
            <Card>
              <ErrorState error={recon.error} onRetry={() => void recon.refetch()} />
            </Card>
          ) : session && recon.data ? (
            <>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {[
                  { label: t('daySessions.totalSales'), value: formatMoney(session.total_sales_amount) },
                  { label: t('daySessions.expectedCash'), value: formatMoney(recon.data.cash_expected) },
                  { label: t('daySessions.declaredCash'), value: formatMoney(recon.data.cash_declared) },
                  {
                    label: t('daySessions.cashVariance'),
                    value: formatMoney(recon.data.cash_variance),
                    danger: num(recon.data.cash_variance) !== 0,
                  },
                ].map((k) => (
                  <div key={k.label} className="card p-4">
                    <p className="truncate text-xs font-medium uppercase tracking-wide text-shell-500">
                      {k.label}
                    </p>
                    <p
                      className={`tabular mt-1.5 text-lg font-semibold ${
                        k.danger ? 'text-danger-600' : 'text-shell-900'
                      }`}
                    >
                      {k.value}
                    </p>
                  </div>
                ))}
              </div>

              <Card
                title={t('daySessions.reconciliation')}
                bodyClassName="p-0"
                actions={
                  <div className="flex items-center gap-2">
                    <span className="tabular text-2xs text-shell-500">
                      {t('stockCommon.variance')}:{' '}
                      {formatNumber(recon.data.total_variance_qty, { decimals: 2 })} ·{' '}
                      {formatMoney(recon.data.total_variance_value)}
                    </span>
                    {canClose && can('field.day_session', 'UPDATE') && (
                      <button type="button" className="btn-primary btn-sm" onClick={() => setClosing(true)}>
                        <LockKeyhole className="h-3.5 w-3.5" />
                        {t('daySessions.closeDay')}
                      </button>
                    )}
                  </div>
                }
              >
                {recon.data.rows.length === 0 ? (
                  <EmptyState />
                ) : (
                  <div className="table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>{t('stockCommon.sku')}</th>
                          <th>{t('stockCommon.product')}</th>
                          <th className="text-right">{t('daySessions.opening')}</th>
                          <th className="text-right">{t('daySessions.loaded')}</th>
                          <th className="text-right">{t('daySessions.reloaded')}</th>
                          <th className="text-right">{t('daySessions.sold')}</th>
                          <th className="text-right">{t('daySessions.returned')}</th>
                          <th className="text-right">{t('daySessions.wastage')}</th>
                          <th className="text-right">{t('daySessions.theoretical')}</th>
                          <th className="text-right">{t('daySessions.counted')}</th>
                          <th className="text-right">{t('stockCommon.variance')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {recon.data.rows.map((r) => {
                          const variance = num(r.variance)
                          return (
                            <tr key={r.product_id}>
                              <td className="tabular whitespace-nowrap text-xs">{r.sku ?? '—'}</td>
                              <td className="min-w-[10rem]">{r.product_name ?? `#${r.product_id}`}</td>
                              <td className="tabular text-right">{formatNumber(r.opening, { decimals: 2 })}</td>
                              <td className="tabular text-right">{formatNumber(r.loaded, { decimals: 2 })}</td>
                              <td className="tabular text-right">{formatNumber(r.reloaded, { decimals: 2 })}</td>
                              <td className="tabular text-right">{formatNumber(r.sold, { decimals: 2 })}</td>
                              <td className="tabular text-right">{formatNumber(r.returned, { decimals: 2 })}</td>
                              <td className="tabular text-right">{formatNumber(r.wastage, { decimals: 2 })}</td>
                              <td className="tabular text-right font-medium">
                                {formatNumber(r.theoretical, { decimals: 2 })}
                              </td>
                              <td className="tabular text-right">
                                {r.counted == null ? '—' : formatNumber(r.counted, { decimals: 2 })}
                              </td>
                              <td
                                className={`tabular text-right font-medium ${
                                  variance > 0
                                    ? 'text-danger-600'
                                    : variance < 0
                                      ? 'text-warn-600'
                                      : 'text-shell-400'
                                }`}
                              >
                                {r.variance == null ? '—' : formatNumber(variance, { decimals: 2 })}
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>
            </>
          ) : null}
        </div>
      </div>

      {opening && (
        <OpenDayModal
          salespersons={salespersons.data?.items ?? []}
          vehicles={vehicles.data?.items ?? []}
          onClose={() => setOpening(false)}
          onOpened={(id) => {
            setOpening(false)
            setSelectedId(id)
            push('success', t('daySessions.opened'))
            void qc.invalidateQueries({ queryKey: ['day-sessions'] })
          }}
        />
      )}

      {closing && session && recon.data && (
        <CloseDayModal
          session={session}
          rows={recon.data.rows}
          expectedCash={num(recon.data.cash_expected)}
          onClose={() => setClosing(false)}
          onClosed={() => {
            setClosing(false)
            push('success', t('daySessions.closed'))
            void recon.refetch()
            void qc.invalidateQueries({ queryKey: ['day-sessions'] })
          }}
        />
      )}
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Open day                                                                   */
/* -------------------------------------------------------------------------- */
function OpenDayModal({
  salespersons,
  vehicles,
  onClose,
  onOpened,
}: {
  salespersons: SalespersonRow[]
  vehicles: VehicleRow[]
  onClose: () => void
  onOpened: (id: number) => void
}) {
  const { t } = useTranslation()
  const { push } = useToast()
  const { session } = useAuth()
  const [salespersonId, setSalespersonId] = useState(
    session?.salesperson_id != null ? String(session.salesperson_id) : '',
  )
  const [vehicleId, setVehicleId] = useState('')
  const [date, setDate] = useState(today())
  const [routeId, setRouteId] = useState('')
  const [odometer, setOdometer] = useState('')
  const [notes, setNotes] = useState('')

  /* Follow the salesperson's default van unless the user overrides it. */
  useEffect(() => {
    const sp = salespersons.find((s) => String(s.id) === salespersonId)
    if (sp?.default_vehicle_id) setVehicleId(String(sp.default_vehicle_id))
  }, [salespersonId, salespersons])

  const open = useMutation({
    mutationFn: () =>
      api.post<SessionRow>('/vehicles/day-sessions/open', {
        salesperson_id: Number(salespersonId),
        vehicle_id: Number(vehicleId),
        route_id: routeId ? Number(routeId) : null,
        start_odometer: odometer ? Number(odometer) : null,
        session_date: date,
        notes: notes || null,
      }),
    onSuccess: (s) => onOpened(s.id),
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title={t('daySessions.openDay')}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={!salespersonId || !vehicleId || open.isPending}
            onClick={() => open.mutate()}
          >
            {open.isPending && <Spinner />}
            {t('daySessions.openDay')}
          </button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t('stockCommon.salesperson')} required>
          <select className="input" value={salespersonId} onChange={(e) => setSalespersonId(e.target.value)}>
            <option value="">{t('stockCommon.selectSalesperson')}</option>
            {salespersons.map((s) => (
              <option key={s.id} value={s.id}>
                {s.full_name}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('stockCommon.vehicle')} required>
          <select className="input" value={vehicleId} onChange={(e) => setVehicleId(e.target.value)}>
            <option value="">{t('stockCommon.selectVehicle')}</option>
            {vehicles.map((v) => (
              <option key={v.id} value={v.id}>
                {v.plate_number}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('daySessions.sessionDate')} required>
          <input type="date" className="input" value={date} onChange={(e) => setDate(e.target.value)} />
        </Field>
        <Field label={t('daySessions.route')}>
          <input
            type="number"
            className="input tabular"
            value={routeId}
            onChange={(e) => setRouteId(e.target.value)}
          />
        </Field>
        <Field label={t('daySessions.startOdometer')}>
          <input
            type="number"
            step="0.1"
            className="input tabular text-right"
            value={odometer}
            onChange={(e) => setOdometer(e.target.value)}
          />
        </Field>
        <div className="sm:col-span-2">
          <Field label={t('common.notes')}>
            <textarea className="input" rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
          </Field>
        </div>
      </div>
    </Modal>
  )
}

/* -------------------------------------------------------------------------- */
/* Close day                                                                  */
/* -------------------------------------------------------------------------- */
function CloseDayModal({
  session,
  rows,
  expectedCash,
  onClose,
  onClosed,
}: {
  session: SessionRow
  rows: ReconRow[]
  expectedCash: number
  onClose: () => void
  onClosed: () => void
}) {
  const { t } = useTranslation()
  const { push } = useToast()
  const [counted, setCounted] = useState<Record<number, string>>(() =>
    Object.fromEntries(rows.map((r) => [r.product_id, String(num(r.counted ?? r.on_hand))])),
  )
  const [declaredCash, setDeclaredCash] = useState(String(num(session.declared_cash) || expectedCash || 0))
  const [endOdometer, setEndOdometer] = useState(
    session.end_odometer_km != null ? String(session.end_odometer_km) : '',
  )
  const [notes, setNotes] = useState('')

  const close = useMutation({
    mutationFn: () =>
      api.post<SessionRow>(`/vehicles/day-sessions/${session.id}/close`, {
        counted: rows.map((r) => ({
          product_id: r.product_id,
          quantity: num(counted[r.product_id]),
          uom: r.base_uom ?? null,
        })),
        declared_cash: num(declaredCash),
        end_odometer: endOdometer ? Number(endOdometer) : null,
        notes: notes || null,
      }),
    onSuccess: onClosed,
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const cashVariance = num(declaredCash) - expectedCash

  return (
    <Modal
      open
      size="xl"
      onClose={onClose}
      title={`${t('daySessions.closeDay')} — ${formatDate(session.session_date, { short: true })}`}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="button" className="btn-primary" disabled={close.isPending} onClick={() => close.mutate()}>
            {close.isPending && <Spinner />}
            {t('daySessions.closeDay')}
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label={t('daySessions.declaredCash')} required>
            <input
              type="number"
              step="0.01"
              min={0}
              className="input tabular text-right"
              value={declaredCash}
              onChange={(e) => setDeclaredCash(e.target.value)}
            />
          </Field>
          <Field label={t('daySessions.expectedCash')}>
            <input className="input tabular text-right" value={formatMoney(expectedCash)} readOnly disabled />
          </Field>
          <Field label={t('daySessions.endOdometer')}>
            <input
              type="number"
              step="0.1"
              className="input tabular text-right"
              value={endOdometer}
              onChange={(e) => setEndOdometer(e.target.value)}
            />
          </Field>
        </div>

        <p
          className={`rounded-lg px-3 py-2 text-xs ${
            cashVariance === 0 ? 'bg-shell-100 text-shell-600' : 'bg-danger-50 text-danger-700'
          }`}
        >
          {t('daySessions.cashVariance')}: <span className="tabular">{formatMoney(cashVariance)}</span>
        </p>

        <div className="flex items-center justify-between">
          <p className="text-xs font-semibold uppercase tracking-wide text-shell-500">
            {t('daySessions.physicalCount')}
          </p>
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={() =>
              setCounted(Object.fromEntries(rows.map((r) => [r.product_id, String(num(r.theoretical))])))
            }
          >
            <ClipboardList className="h-3.5 w-3.5" />
            {t('daySessions.useTheoretical')}
          </button>
        </div>

        {rows.length === 0 ? (
          <EmptyState title={t('stockCommon.noLines')} />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>{t('stockCommon.sku')}</th>
                  <th>{t('stockCommon.product')}</th>
                  <th className="text-right">{t('daySessions.theoretical')}</th>
                  <th className="text-right">{t('daySessions.counted')}</th>
                  <th className="text-right">{t('stockCommon.variance')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const variance = num(r.theoretical) - num(counted[r.product_id])
                  return (
                    <tr key={r.product_id}>
                      <td className="tabular whitespace-nowrap text-xs">{r.sku ?? '—'}</td>
                      <td className="min-w-[10rem]">{r.product_name ?? `#${r.product_id}`}</td>
                      <td className="tabular text-right">{formatNumber(r.theoretical, { decimals: 2 })}</td>
                      <td className="text-right">
                        <input
                          type="number"
                          min={0}
                          step="0.001"
                          className="input tabular w-24 py-1 text-right"
                          value={counted[r.product_id] ?? ''}
                          onChange={(e) =>
                            setCounted((p) => ({ ...p, [r.product_id]: e.target.value }))
                          }
                        />
                      </td>
                      <td
                        className={`tabular text-right font-medium ${
                          variance > 0 ? 'text-danger-600' : variance < 0 ? 'text-warn-600' : 'text-shell-400'
                        }`}
                      >
                        {formatNumber(variance, { decimals: 2 })}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        <Field label={t('common.notes')}>
          <textarea className="input" rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
        </Field>
      </div>
    </Modal>
  )
}
