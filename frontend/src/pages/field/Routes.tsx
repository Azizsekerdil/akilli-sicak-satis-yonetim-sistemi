/**
 * Route planning, optimisation and execution.
 *
 * Left: the day's routes.  Right: the selected route's ordered stops, the
 * optimiser and the plan-vs-actual reconciliation.  Every write action is
 * gated on `field.routes:EXECUTE` (or CREATE for generation) — the server
 * enforces the same rules independently.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  CalendarPlus,
  CheckCircle2,
  Flag,
  MapPin,
  Play,
  Route as RouteIcon,
  SkipForward,
  Sparkles,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  Modal,
  PageHeader,
  Pagination,
  SectionTitle,
  SkeletonRows,
  Spinner,
  StatusBadge,
  useToast,
} from '@/components/ui'
import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatNumber, formatPercent, toNumber } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Types (mirror app/schemas/route.py)                                        */
/* -------------------------------------------------------------------------- */
interface RouteListItem {
  id: number
  code: string
  name: string
  is_template: boolean
  route_date: string | null
  weekday: string | null
  status: string
  salesperson_id: number | null
  vehicle_id: number | null
  region_id: number | null
  planned_stops: number
  completed_stops: number
  skipped_stops: number
  planned_distance_km: number
  actual_distance_km: number
  planned_duration_min: number
  actual_duration_min: number
  total_sales_amount: number | string
  is_optimized: boolean
  optimizer: string | null
  started_at: string | null
  completed_at: string | null
  is_active: boolean
}

interface RouteStopOut {
  id: number
  customer_id: number
  customer_code: string | null
  customer_name: string | null
  latitude: number | null
  longitude: number | null
  address: string | null
  phone: string | null
  sequence: number
  status: string
  planned_arrival: string | null
  planned_departure: string | null
  service_time_minutes: number
  distance_from_previous_km: number
  travel_time_from_previous_min: number
  arrived_at: string | null
  departed_at: string | null
  geofence_distance_m: number | null
  delay_minutes: number
  skip_reason: string | null
  is_priority: boolean
}

interface RouteOut extends RouteListItem {
  description: string | null
  planned_start_time: string | null
  completion_rate: number
  salesperson_name: string | null
  vehicle_plate: string | null
  optimization_seconds: number | null
  optimization_note: string | null
  stops: RouteStopOut[]
}

interface OptimizeOut {
  route_id: number
  code: string
  solver: string
  seconds: number
  stops: number
  distance_km: number
  duration_min: number
  objective: number
  unassigned_customer_ids: number[]
  message: string
}

interface GenerateDailyOut {
  on_date: string
  weekday: string
  created: number
  updated: number
  skipped: number
  customers_planned: number
  message: string
}

interface PlanVsActualOut {
  route_id: number
  code: string
  route_date: string | null
  status: string
  planned_stops: number
  completed: number
  skipped: number
  planned_km: number
  actual_km: number
  planned_minutes: number
  actual_minutes: number
  deviation_percent: number
  time_deviation_percent: number
  completion_rate: number
  delayed_stops: { customer_id: number; name: string | null; sequence: number; planned_arrival: string | null; delay_minutes: number }[]
  unvisited_customers: { customer_id: number; code: string | null; name: string | null; sequence: number; status: string; skip_reason: string | null }[]
}

interface ArriveOut {
  stop_id: number
  status: string
  geofence_distance_m: number | null
  in_geofence: boolean | null
  delay_minutes: number
}

interface SalespersonRow { id: number; full_name: string; code: string }
interface VehicleRow { id: number; plate_number: string; code: string }

const ROUTE_STATUSES = ['PLANNED', 'OPTIMIZED', 'ASSIGNED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED']
const PAGE_SIZE = 25

function isoDate(d: Date = new Date()): string {
  const z = new Date(d.getTime() - d.getTimezoneOffset() * 60_000)
  return z.toISOString().slice(0, 10)
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function Routes() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()

  const [dateFrom, setDateFrom] = useState(isoDate())
  const [dateTo, setDateTo] = useState(isoDate())
  const [status, setStatus] = useState('')
  const [salespersonId, setSalespersonId] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<number | null>(null)

  const [genOpen, setGenOpen] = useState(false)
  const [genDate, setGenDate] = useState(isoDate())
  const [genRegion, setGenRegion] = useState('')
  const [skipStop, setSkipStop] = useState<RouteStopOut | null>(null)
  const [skipReason, setSkipReason] = useState('')
  const [optResult, setOptResult] = useState<{ before: number; out: OptimizeOut } | null>(null)

  const mayExecute = can('field.routes', 'EXECUTE')
  const mayCreate = can('field.routes', 'CREATE')

  const people = useQuery({
    queryKey: ['route-salespersons'],
    queryFn: () => api.get<Paged<SalespersonRow>>('/vehicles/salespersons', { size: 200, is_active: true }),
    enabled: can('field.salespersons'),
    throwOnError: false,
  })
  const vehicles = useQuery({
    queryKey: ['route-vehicles'],
    queryFn: () => api.get<Paged<VehicleRow>>('/vehicles', { size: 200 }),
    enabled: can('field.vehicles'),
    throwOnError: false,
  })

  const personName = useMemo(() => {
    const m = new Map<number, string>()
    for (const p of people.data?.items ?? []) m.set(p.id, p.full_name)
    return m
  }, [people.data])
  const plateOf = useMemo(() => {
    const m = new Map<number, string>()
    for (const v of vehicles.data?.items ?? []) m.set(v.id, v.plate_number)
    return m
  }, [vehicles.data])

  const listParams = {
    page,
    size: PAGE_SIZE,
    date_from: dateFrom || undefined,
    date_to: dateTo || undefined,
    status: status || undefined,
    salesperson_id: salespersonId || undefined,
    search: search || undefined,
    is_template: false,
  }

  const list = useQuery({
    queryKey: ['routes', listParams],
    queryFn: () => api.get<Paged<RouteListItem>>('/routes', listParams),
  })

  const detail = useQuery({
    queryKey: ['route', selected],
    queryFn: () => api.get<RouteOut>(`/routes/${selected}`),
    enabled: selected !== null,
  })

  const pva = useQuery({
    queryKey: ['route-pva', selected],
    queryFn: () => api.get<PlanVsActualOut>(`/routes/${selected}/plan-vs-actual`),
    enabled: selected !== null,
    throwOnError: false,
  })

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['routes'] })
    void qc.invalidateQueries({ queryKey: ['route', selected] })
    void qc.invalidateQueries({ queryKey: ['route-pva', selected] })
  }
  const fail = (e: unknown) => push('error', e instanceof ApiError ? e.message : t('errors.generic'))

  const optimize = useMutation({
    mutationFn: async () => {
      const before = toNumber(detail.data?.planned_distance_km)
      const out = await api.post<OptimizeOut>(`/routes/${selected}/optimize`, {
        prefer_exact: true,
        time_limit_s: 10,
      })
      return { before, out }
    },
    onSuccess: (r) => {
      setOptResult(r)
      invalidate()
    },
    onError: fail,
  })

  const generate = useMutation({
    mutationFn: () =>
      api.post<GenerateDailyOut>('/routes/generate-daily', {
        on_date: genDate,
        region_id: genRegion ? Number(genRegion) : null,
      }),
    onSuccess: (r) => {
      setGenOpen(false)
      push('success', t('routes.generateDailyResult', { created: r.created, updated: r.updated, skipped: r.skipped }))
      invalidate()
    },
    onError: fail,
  })

  const startRoute = useMutation({
    mutationFn: () => api.post<RouteOut>(`/routes/${selected}/start`, {}),
    onSuccess: () => { push('success', t('routes.startedMsg')); invalidate() },
    onError: fail,
  })

  const completeRoute = useMutation({
    mutationFn: () => api.post<RouteOut>(`/routes/${selected}/complete`, {}),
    onSuccess: () => { push('success', t('routes.completedMsg')); invalidate() },
    onError: fail,
  })

  const arrive = useMutation({
    mutationFn: async (stopId: number) => {
      const pos = await currentPosition()
      return api.post<ArriveOut>(`/routes/${selected}/stops/${stopId}/arrive`, pos)
    },
    onSuccess: (r) => {
      push(
        r.in_geofence === false ? 'warning' : 'success',
        r.in_geofence === false ? t('routes.outGeofence') : t('routes.arrivedMsg'),
      )
      invalidate()
    },
    onError: fail,
  })

  const skip = useMutation({
    mutationFn: () =>
      api.post(`/routes/${selected}/stops/${skipStop!.id}/skip`, { reason: skipReason }),
    onSuccess: () => {
      setSkipStop(null)
      setSkipReason('')
      push('success', t('routes.skippedMsg'))
      invalidate()
    },
    onError: fail,
  })

  const rows = list.data?.items ?? []
  const route = detail.data

  return (
    <>
      <PageHeader
        title={t('routes.title')}
        subtitle={t('routes.subtitle')}
        icon={<RouteIcon className="h-5 w-5" />}
        actions={
          mayCreate && (
            <button type="button" className="btn-primary btn-sm" onClick={() => setGenOpen(true)}>
              <CalendarPlus className="h-4 w-4" />
              {t('routes.generateDaily')}
            </button>
          )
        }
      />

      <Card className="mb-4" bodyClassName="p-3">
        <div className="flex flex-wrap items-end gap-2">
          <Field label={t('common.from')}>
            <input type="date" className="input" value={dateFrom}
              onChange={(e) => { setDateFrom(e.target.value); setPage(1) }} />
          </Field>
          <Field label={t('common.to')}>
            <input type="date" className="input" value={dateTo}
              onChange={(e) => { setDateTo(e.target.value); setPage(1) }} />
          </Field>
          <Field label={t('common.status')}>
            <select className="input" value={status}
              onChange={(e) => { setStatus(e.target.value); setPage(1) }}>
              <option value="">{t('routes.allStatuses')}</option>
              {ROUTE_STATUSES.map((s) => (
                <option key={s} value={s}>{t(`routes.status.${s}`)}</option>
              ))}
            </select>
          </Field>
          {(people.data?.items ?? []).length > 0 && (
            <Field label={t('routes.salesperson')}>
              <select className="input" value={salespersonId}
                onChange={(e) => { setSalespersonId(e.target.value); setPage(1) }}>
                <option value="">{t('common.all')}</option>
                {(people.data?.items ?? []).map((p) => (
                  <option key={p.id} value={p.id}>{p.full_name}</option>
                ))}
              </select>
            </Field>
          )}
          <Field label={t('common.search')}>
            <input className="input" value={search} placeholder={t('common.search')}
              onChange={(e) => { setSearch(e.target.value); setPage(1) }} />
          </Field>
        </div>
      </Card>

      <div className="grid gap-5 xl:grid-cols-5">
        {/* ------------------------- List ------------------------- */}
        <Card className="xl:col-span-3" bodyClassName="p-0">
          {list.isLoading ? (
            <SkeletonRows rows={6} cols={6} />
          ) : list.isError ? (
            <ErrorState error={list.error} onRetry={() => void list.refetch()} />
          ) : rows.length === 0 ? (
            <EmptyState />
          ) : (
            <>
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>{t('common.date')}</th>
                      <th>{t('common.code')}</th>
                      <th>{t('routes.salesperson')}</th>
                      <th>{t('routes.vehicle')}</th>
                      <th className="text-right">{t('routes.stops')}</th>
                      <th className="text-right">{t('routes.plannedKm')}</th>
                      <th>{t('common.status')}</th>
                      <th className="text-right">{t('routes.completion')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => {
                      const pct = r.planned_stops > 0 ? (r.completed_stops / r.planned_stops) * 100 : 0
                      return (
                        <tr
                          key={r.id}
                          onClick={() => setSelected(r.id)}
                          className={`cursor-pointer ${selected === r.id ? 'bg-brand-50' : ''}`}
                        >
                          <td className="whitespace-nowrap">{formatDate(r.route_date, { short: true })}</td>
                          <td>
                            <span className="font-medium text-shell-900">{r.code}</span>
                            <span className="block text-2xs text-shell-400">{r.name}</span>
                          </td>
                          <td>{r.salesperson_id ? personName.get(r.salesperson_id) ?? `#${r.salesperson_id}` : '—'}</td>
                          <td>{r.vehicle_id ? plateOf.get(r.vehicle_id) ?? `#${r.vehicle_id}` : '—'}</td>
                          <td className="tabular text-right">
                            {formatNumber(r.completed_stops)} / {formatNumber(r.planned_stops)}
                          </td>
                          <td className="tabular text-right">{formatNumber(r.planned_distance_km, { decimals: 1 })}</td>
                          <td>
                            <StatusBadge status={r.status} label={t(`routes.status.${r.status}`, { defaultValue: r.status })} />
                          </td>
                          <td className="tabular text-right">{formatPercent(pct)}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              <Pagination
                page={list.data?.page ?? 1}
                pages={list.data?.pages ?? 1}
                total={list.data?.total ?? 0}
                size={list.data?.size ?? PAGE_SIZE}
                onPage={setPage}
              />
            </>
          )}
        </Card>

        {/* ------------------------- Detail ------------------------- */}
        <div className="space-y-5 xl:col-span-2">
          {selected === null ? (
            <Card><EmptyState title={t('routes.selectRoute')} icon={<MapPin className="h-6 w-6" />} /></Card>
          ) : detail.isLoading ? (
            <Card><LoadingBlock /></Card>
          ) : detail.isError ? (
            <Card><ErrorState error={detail.error} onRetry={() => void detail.refetch()} /></Card>
          ) : route ? (
            <>
              <Card
                title={`${route.code} — ${route.name}`}
                bodyClassName="p-4"
                actions={
                  <StatusBadge status={route.status} label={t(`routes.status.${route.status}`, { defaultValue: route.status })} />
                }
              >
                <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
                  <Row label={t('routes.onDate')} value={formatDate(route.route_date)} />
                  <Row label={t('routes.salesperson')} value={route.salesperson_name ?? '—'} />
                  <Row label={t('routes.vehicle')} value={route.vehicle_plate ?? '—'} />
                  <Row label={t('routes.plannedKm')} value={formatNumber(route.planned_distance_km, { decimals: 1 })} />
                  <Row label={t('routes.actualKm')} value={formatNumber(route.actual_distance_km, { decimals: 1 })} />
                  <Row label={t('routes.completion')} value={formatPercent(route.completion_rate)} />
                  <Row
                    label={t('routes.optimized')}
                    value={route.is_optimized ? `${route.optimizer ?? '—'} · ${formatNumber(route.optimization_seconds ?? 0, { decimals: 2 })}s` : t('common.no')}
                  />
                </dl>

                {mayExecute && (
                  <div className="mt-4 flex flex-wrap gap-2">
                    <button type="button" className="btn-secondary btn-sm"
                      disabled={optimize.isPending} onClick={() => optimize.mutate()}>
                      {optimize.isPending ? <Spinner className="h-3.5 w-3.5" /> : <Sparkles className="h-3.5 w-3.5" />}
                      {t('routes.optimize')}
                    </button>
                    <button type="button" className="btn-secondary btn-sm"
                      disabled={startRoute.isPending || route.status === 'IN_PROGRESS' || route.status === 'COMPLETED'}
                      onClick={() => startRoute.mutate()}>
                      <Play className="h-3.5 w-3.5" />
                      {t('routes.start')}
                    </button>
                    <button type="button" className="btn-secondary btn-sm"
                      disabled={completeRoute.isPending || route.status === 'COMPLETED'}
                      onClick={() => completeRoute.mutate()}>
                      <Flag className="h-3.5 w-3.5" />
                      {t('routes.completeRoute')}
                    </button>
                  </div>
                )}
              </Card>

              <Card title={t('routes.stops')} bodyClassName="p-0">
                {route.stops.length === 0 ? (
                  <EmptyState title={t('routes.noStops')} />
                ) : (
                  <ul className="divide-y divide-shell-100">
                    {route.stops.map((s) => (
                      <StopRow
                        key={s.id}
                        stop={s}
                        canExecute={mayExecute}
                        busy={arrive.isPending}
                        onArrive={() => arrive.mutate(s.id)}
                        onSkip={() => { setSkipStop(s); setSkipReason('') }}
                      />
                    ))}
                  </ul>
                )}
              </Card>

              {pva.data && <PlanVsActual data={pva.data} />}
            </>
          ) : null}
        </div>
      </div>

      {/* ------------------------- Modals ------------------------- */}
      <Modal
        open={genOpen}
        onClose={() => setGenOpen(false)}
        title={t('routes.generateDailyTitle')}
        footer={
          <>
            <button type="button" className="btn-secondary btn-sm" onClick={() => setGenOpen(false)}>
              {t('common.cancel')}
            </button>
            <button type="button" className="btn-primary btn-sm"
              disabled={generate.isPending || !genDate} onClick={() => generate.mutate()}>
              {generate.isPending && <Spinner className="h-3.5 w-3.5" />}
              {t('routes.generateDaily')}
            </button>
          </>
        }
      >
        <div className="space-y-3">
          <Field label={t('routes.onDate')} required>
            <input type="date" className="input" value={genDate} onChange={(e) => setGenDate(e.target.value)} />
          </Field>
          <Field label="Region ID" hint={t('common.optional')}>
            <input type="number" className="input tabular" value={genRegion}
              onChange={(e) => setGenRegion(e.target.value)} />
          </Field>
        </div>
      </Modal>

      <Modal
        open={skipStop !== null}
        onClose={() => setSkipStop(null)}
        title={t('routes.skipTitle')}
        size="sm"
        footer={
          <>
            <button type="button" className="btn-secondary btn-sm" onClick={() => setSkipStop(null)}>
              {t('common.cancel')}
            </button>
            <button type="button" className="btn-danger btn-sm"
              disabled={!skipReason.trim() || skip.isPending} onClick={() => skip.mutate()}>
              {skip.isPending && <Spinner className="h-3.5 w-3.5" />}
              {t('routes.skip')}
            </button>
          </>
        }
      >
        <Field label={t('routes.skipReason')} required>
          <input className="input" value={skipReason} maxLength={255}
            onChange={(e) => setSkipReason(e.target.value)} />
        </Field>
      </Modal>

      <Modal
        open={optResult !== null}
        onClose={() => setOptResult(null)}
        title={t('routes.optimizeResult')}
        size="sm"
      >
        {optResult && (
          <dl className="space-y-1.5 text-sm">
            <Row label={t('routes.solver')} value={optResult.out.solver} />
            <Row label={t('routes.before')} value={`${formatNumber(optResult.before, { decimals: 1 })} km`} />
            <Row label={t('routes.after')} value={`${formatNumber(optResult.out.distance_km, { decimals: 1 })} km`} />
            <Row
              label={t('routes.gain')}
              value={`${formatNumber(optResult.before - optResult.out.distance_km, { decimals: 1 })} km`}
            />
            <Row label={t('routes.stops')} value={formatNumber(optResult.out.stops)} />
            <Row label={t('routes.objective')} value={formatNumber(optResult.out.objective, { decimals: 2 })} />
            <Row label={t('routes.elapsed')} value={formatNumber(optResult.out.seconds, { decimals: 2 })} />
            {optResult.out.unassigned_customer_ids.length > 0 && (
              <Row label={t('routes.unassigned')} value={formatNumber(optResult.out.unassigned_customer_ids.length)} />
            )}
          </dl>
        )}
      </Modal>
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Sub-components                                                             */
/* -------------------------------------------------------------------------- */
function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="shrink-0 text-shell-500">{label}</dt>
      <dd className="tabular truncate text-right font-medium text-shell-800">{value}</dd>
    </div>
  )
}

function StopRow({
  stop, canExecute, busy, onArrive, onSkip,
}: {
  stop: RouteStopOut
  canExecute: boolean
  busy: boolean
  onArrive: () => void
  onSkip: () => void
}) {
  const { t } = useTranslation()
  const open = stop.status === 'PENDING' || stop.status === 'ARRIVED'
  return (
    <li className="px-4 py-2.5">
      <div className="flex items-start gap-3">
        <span className="tabular mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-shell-100 text-2xs font-semibold text-shell-600">
          {stop.sequence}
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-shell-900">
            {stop.customer_name ?? `#${stop.customer_id}`}
            {stop.is_priority && <span className="badge-info ml-1.5">★</span>}
          </p>
          <p className="text-2xs text-shell-400">
            {stop.customer_code} · {stop.planned_arrival ?? '—'} ·{' '}
            {formatNumber(stop.distance_from_previous_km, { decimals: 1 })} km
          </p>
          {stop.skip_reason && <p className="text-2xs text-danger-600">{stop.skip_reason}</p>}
          {stop.geofence_distance_m !== null && (
            <p className="text-2xs text-shell-400">
              {t('routes.geofenceDistance')}: {formatNumber(stop.geofence_distance_m, { decimals: 0 })} m
            </p>
          )}
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <StatusBadge status={stop.status} label={t(`routes.stopStatus.${stop.status}`, { defaultValue: stop.status })} />
          {canExecute && open && (
            <div className="flex gap-1">
              <button type="button" className="btn-ghost btn-sm" disabled={busy} onClick={onArrive}
                title={t('routes.arrive')}>
                <CheckCircle2 className="h-3.5 w-3.5" />
              </button>
              <button type="button" className="btn-ghost btn-sm" onClick={onSkip} title={t('routes.skip')}>
                <SkipForward className="h-3.5 w-3.5" />
              </button>
            </div>
          )}
        </div>
      </div>
    </li>
  )
}

function PlanVsActual({ data }: { data: PlanVsActualOut }) {
  const { t } = useTranslation()
  const tone = (v: number) => (Math.abs(v) <= 10 ? 'text-ok-600' : Math.abs(v) <= 25 ? 'text-warn-600' : 'text-danger-600')
  return (
    <Card title={t('routes.planVsActual')} bodyClassName="p-4">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
        <Row label={t('routes.plannedKm')} value={formatNumber(data.planned_km, { decimals: 1 })} />
        <Row label={t('routes.actualKm')} value={formatNumber(data.actual_km, { decimals: 1 })} />
        <Row label={t('routes.plannedMin')} value={formatNumber(data.planned_minutes)} />
        <Row label={t('routes.actualMin')} value={formatNumber(data.actual_minutes)} />
      </dl>
      <div className="mt-3 grid grid-cols-3 gap-2 text-center">
        {[
          [t('routes.distanceDeviation'), data.deviation_percent],
          [t('routes.timeDeviation'), data.time_deviation_percent],
          [t('routes.completion'), data.completion_rate],
        ].map(([label, value], i) => (
          <div key={String(label)} className="rounded-lg border border-shell-200 p-2">
            <p className="text-2xs uppercase tracking-wide text-shell-500">{label}</p>
            <p className={`tabular text-sm font-semibold ${i === 2 ? 'text-shell-800' : tone(Number(value))}`}>
              {formatPercent(Number(value), { sign: i < 2 })}
            </p>
          </div>
        ))}
      </div>

      {data.delayed_stops.length > 0 && (
        <div className="mt-4">
          <SectionTitle>{t('routes.delayedStops')}</SectionTitle>
          <ul className="space-y-1 text-xs">
            {data.delayed_stops.map((d) => (
              <li key={d.customer_id} className="flex justify-between gap-2 text-shell-600">
                <span className="truncate">{d.sequence}. {d.name ?? `#${d.customer_id}`}</span>
                <span className="tabular shrink-0 text-warn-700">
                  +{formatNumber(d.delay_minutes)} {t('visits.minutes')}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {data.unvisited_customers.length > 0 && (
        <div className="mt-4">
          <SectionTitle>{t('routes.unvisited')}</SectionTitle>
          <ul className="space-y-1 text-xs">
            {data.unvisited_customers.map((u) => (
              <li key={u.customer_id} className="flex justify-between gap-2 text-shell-600">
                <span className="truncate">{u.name ?? u.code ?? `#${u.customer_id}`}</span>
                <span className="shrink-0 text-shell-400">{u.skip_reason ?? t(`routes.stopStatus.${u.status}`, { defaultValue: u.status })}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  )
}

/** Best-effort GPS fix; the backend accepts a body with no coordinates. */
function currentPosition(): Promise<{ latitude?: number; longitude?: number }> {
  if (!('geolocation' in navigator)) return Promise.resolve({})
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (p) => resolve({ latitude: p.coords.latitude, longitude: p.coords.longitude }),
      () => resolve({}),
      { timeout: 5000, maximumAge: 30_000 },
    )
  })
}
