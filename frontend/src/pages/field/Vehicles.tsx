/**
 * Araçlar / Vehicles.
 *
 * Each van is also a warehouse (the backend provisions one on create), so the
 * capacity numbers here are what the loading screen enforces.  Insurance and
 * inspection dates are surfaced as warnings, not buried in a form — a van with
 * an expired inspection cannot legally leave the yard.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, MapPin, Pencil, Plus, Search, Trash2, Truck, UserCheck } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { daysUntil, formatDate, formatNumber, formatRelative } from '@/lib/format'
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
interface VehicleRow {
  id: number
  code: string
  plate_number: string
  name?: string | null
  warehouse_id?: number | null
  home_warehouse_id?: number | null
  region_id?: number | null
  vehicle_type: string
  status: string
  is_active: boolean
  brand?: string | null
  model?: string | null
  model_year?: number | null
  is_refrigerated: boolean
  capacity_volume_l: number
  capacity_weight_kg: number
  capacity_cases?: number | null
  default_salesperson_id?: number | null
  fuel_type?: string | null
  avg_consumption_l_100km?: number | null
  odometer_km: number
  insurance_expiry?: string | null
  inspection_expiry?: string | null
  last_maintenance_at?: string | null
  last_lat?: number | null
  last_lng?: number | null
  last_position_at?: string | null
  notes?: string | null
}

interface SalespersonRow {
  id: number
  full_name: string
  code: string
}

interface WarehouseRow {
  id: number
  code: string
  name: string
  warehouse_type: string
}

interface Warning {
  vehicle_id: number
  plate_number: string
  kind: string
  expiry_date: string
  days_left: number
  is_expired: boolean
  severity: string
}

const TYPES = ['VAN', 'TRUCK', 'PICKUP', 'REFRIGERATED', 'MOTORCYCLE']
const STATUSES = ['ACTIVE', 'MAINTENANCE', 'INACTIVE']
const SIZE = 20

export default function Vehicles() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()

  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('')
  const [editing, setEditing] = useState<VehicleRow | null | undefined>(undefined)
  const [assigning, setAssigning] = useState<VehicleRow | null>(null)

  const listParams = { page, size: SIZE, search: search || undefined, status: status || undefined }
  const list = useQuery({
    queryKey: ['vehicles', listParams],
    queryFn: () => api.get<Paged<VehicleRow>>('/vehicles', listParams),
  })

  const warnings = useQuery({
    queryKey: ['vehicle-warnings'],
    queryFn: () => api.get<Warning[]>('/vehicles/maintenance-warnings', { within_days: 30 }),
    retry: false,
    throwOnError: false,
  })

  const salespersons = useQuery({
    queryKey: ['salespersons', 'picker'],
    queryFn: () => api.get<Paged<SalespersonRow>>('/vehicles/salespersons', { size: 200, is_active: true }),
    enabled: can('field.salespersons', 'VIEW'),
    retry: false,
    throwOnError: false,
  })

  const warehouses = useQuery({
    queryKey: ['warehouses', 'picker'],
    queryFn: () => api.get<Paged<WarehouseRow>>('/warehouses', { size: 200, is_active: true }),
    enabled: can('stock.warehouses', 'VIEW'),
    retry: false,
    throwOnError: false,
  })

  const personName = (id?: number | null) =>
    (salespersons.data?.items ?? []).find((s) => s.id === id)?.full_name ?? (id ? `#${id}` : '—')

  const remove = useMutation({
    mutationFn: (id: number) => api.delete<{ message: string }>(`/vehicles/${id}`),
    onSuccess: () => {
      push('success', t('stockCommon.removed'))
      void qc.invalidateQueries({ queryKey: ['vehicles'] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const assign = useMutation({
    mutationFn: (payload: { id: number; salesperson_id: number }) =>
      api.post<VehicleRow>(`/vehicles/${payload.id}/assign`, { salesperson_id: payload.salesperson_id }),
    onSuccess: () => {
      push('success', t('vehicles.assigned'))
      setAssigning(null)
      void qc.invalidateQueries({ queryKey: ['vehicles'] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const rows = list.data?.items ?? []
  const warnRows = warnings.data ?? []

  return (
    <>
      <PageHeader
        title={t('vehicles.title')}
        subtitle={t('vehicles.subtitle')}
        icon={<Truck className="h-5 w-5" />}
        actions={
          can('field.vehicles', 'CREATE') && (
            <button type="button" className="btn-primary btn-sm" onClick={() => setEditing(null)}>
              <Plus className="h-4 w-4" />
              {t('vehicles.new')}
            </button>
          )
        }
      />

      {warnRows.length > 0 && (
        <Card
          className="mb-4 border-warn-200"
          title={t('vehicles.warnings')}
          actions={<AlertTriangle className="h-4 w-4 text-warn-600" />}
          bodyClassName="p-3"
        >
          <ul className="flex flex-wrap gap-2">
            {warnRows.map((w) => (
              <li
                key={`${w.vehicle_id}-${w.kind}`}
                className={`rounded-lg border px-3 py-2 text-xs ${
                  w.is_expired ? 'border-danger-200 bg-danger-50' : 'border-warn-200 bg-warn-50'
                }`}
              >
                <span className="font-medium">{w.plate_number}</span>{' '}
                <span className="text-shell-600">
                  {w.kind === 'INSURANCE' ? t('vehicles.insuranceExpiry') : t('vehicles.inspectionExpiry')}
                </span>{' '}
                <span className="tabular">{formatDate(w.expiry_date, { short: true })}</span>{' '}
                <span className={w.is_expired ? 'text-danger-700' : 'text-warn-700'}>
                  {w.is_expired ? t('vehicles.expiredDoc') : t('vehicles.daysLeft', { count: w.days_left })}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card bodyClassName="p-0">
        <div className="flex flex-wrap gap-2 border-b border-shell-200 p-3">
          <div className="relative min-w-[12rem] flex-1">
            <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
            <input
              className="input pl-9"
              placeholder={t('vehicles.plate')}
              value={search}
              onChange={(e) => {
                setSearch(e.target.value)
                setPage(1)
              }}
            />
          </div>
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

        {list.isLoading ? (
          <SkeletonRows rows={8} cols={7} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>{t('vehicles.plate')}</th>
                  <th>{t('vehicles.type')}</th>
                  <th>{t('vehicles.brandModel')}</th>
                  <th className="text-right">{t('vehicles.capacityVolume')}</th>
                  <th className="text-right">{t('vehicles.capacityWeight')}</th>
                  <th>{t('vehicles.insuranceExpiry')}</th>
                  <th>{t('vehicles.inspectionExpiry')}</th>
                  <th>{t('vehicles.assignedTo')}</th>
                  <th>{t('vehicles.lastPosition')}</th>
                  <th>{t('common.status')}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((v) => (
                  <tr key={v.id}>
                    <td className="whitespace-nowrap font-medium">
                      {v.plate_number}
                      {v.is_refrigerated && (
                        <span className="badge-info ml-1.5">{t('vehicles.refrigerated')}</span>
                      )}
                    </td>
                    <td className="text-xs">{v.vehicle_type}</td>
                    <td className="text-xs text-shell-500">
                      {[v.brand, v.model, v.model_year].filter(Boolean).join(' ') || '—'}
                    </td>
                    <td className="tabular text-right">{formatNumber(v.capacity_volume_l)}</td>
                    <td className="tabular text-right">{formatNumber(v.capacity_weight_kg)}</td>
                    <td className="whitespace-nowrap text-xs">
                      {v.insurance_expiry ? (
                        <span className="flex items-center gap-1.5">
                          {formatDate(v.insurance_expiry, { short: true })}
                          <ExpiryBadge days={daysUntil(v.insurance_expiry)} />
                        </span>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td className="whitespace-nowrap text-xs">
                      {v.inspection_expiry ? (
                        <span className="flex items-center gap-1.5">
                          {formatDate(v.inspection_expiry, { short: true })}
                          <ExpiryBadge days={daysUntil(v.inspection_expiry)} />
                        </span>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td className="text-xs">{personName(v.default_salesperson_id)}</td>
                    <td className="text-xs text-shell-500">
                      {v.last_lat != null && v.last_lng != null ? (
                        <span className="flex items-center gap-1">
                          <MapPin className="h-3 w-3 text-shell-400" />
                          <span className="tabular">
                            {v.last_lat.toFixed(4)}, {v.last_lng.toFixed(4)}
                          </span>
                          <span className="text-2xs text-shell-400">{formatRelative(v.last_position_at)}</span>
                        </span>
                      ) : (
                        t('vehicles.noPosition')
                      )}
                    </td>
                    <td>
                      <StatusBadge status={v.is_active ? v.status : 'INACTIVE'} />
                    </td>
                    <td className="whitespace-nowrap text-right">
                      {can('field.vehicles', 'UPDATE') && (
                        <>
                          <button
                            type="button"
                            className="btn-ghost btn-sm"
                            onClick={() => setAssigning(v)}
                            aria-label={t('vehicles.assign')}
                          >
                            <UserCheck className="h-3.5 w-3.5" />
                          </button>
                          <button
                            type="button"
                            className="btn-ghost btn-sm"
                            onClick={() => setEditing(v)}
                            aria-label={t('common.edit')}
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </button>
                        </>
                      )}
                      {can('field.vehicles', 'DELETE') && (
                        <button
                          type="button"
                          className="btn-ghost btn-sm text-danger-600"
                          onClick={() => {
                            if (window.confirm(t('stockCommon.confirmDelete'))) remove.mutate(v.id)
                          }}
                          aria-label={t('vehicles.retire')}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
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

      {assigning && (
        <Modal
          open
          size="sm"
          onClose={() => setAssigning(null)}
          title={`${t('vehicles.assign')} — ${assigning.plate_number}`}
        >
          {(salespersons.data?.items ?? []).length === 0 ? (
            <EmptyState title={t('stockCommon.moduleUnavailable')} />
          ) : (
            <ul className="max-h-72 divide-y divide-shell-100 overflow-y-auto">
              {(salespersons.data?.items ?? []).map((s) => (
                <li key={s.id}>
                  <button
                    type="button"
                    className="flex w-full items-center justify-between px-2 py-2 text-left text-sm hover:bg-shell-50"
                    disabled={assign.isPending}
                    onClick={() => assign.mutate({ id: assigning.id, salesperson_id: s.id })}
                  >
                    <span className="truncate">{s.full_name}</span>
                    <span className="tabular text-2xs text-shell-400">{s.code}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Modal>
      )}

      {editing !== undefined && (
        <VehicleEditor
          vehicle={editing}
          salespersons={salespersons.data?.items ?? []}
          warehouses={warehouses.data?.items ?? []}
          onClose={() => setEditing(undefined)}
          onSaved={() => {
            setEditing(undefined)
            void qc.invalidateQueries({ queryKey: ['vehicles'] })
          }}
        />
      )}
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Editor                                                                     */
/* -------------------------------------------------------------------------- */
function VehicleEditor({
  vehicle,
  salespersons,
  warehouses,
  onClose,
  onSaved,
}: {
  vehicle: VehicleRow | null
  salespersons: SalespersonRow[]
  warehouses: WarehouseRow[]
  onClose: () => void
  onSaved: () => void
}) {
  const { t } = useTranslation()
  const { push } = useToast()
  const [f, setF] = useState({
    plate_number: vehicle?.plate_number ?? '',
    name: vehicle?.name ?? '',
    vehicle_type: vehicle?.vehicle_type ?? 'VAN',
    status: vehicle?.status ?? 'ACTIVE',
    brand: vehicle?.brand ?? '',
    model: vehicle?.model ?? '',
    model_year: vehicle?.model_year != null ? String(vehicle.model_year) : '',
    is_refrigerated: vehicle?.is_refrigerated ?? false,
    capacity_volume_l: String(vehicle?.capacity_volume_l ?? 8000),
    capacity_weight_kg: String(vehicle?.capacity_weight_kg ?? 3500),
    capacity_cases: vehicle?.capacity_cases != null ? String(vehicle.capacity_cases) : '',
    odometer_km: String(vehicle?.odometer_km ?? 0),
    fuel_type: vehicle?.fuel_type ?? '',
    avg_consumption_l_100km:
      vehicle?.avg_consumption_l_100km != null ? String(vehicle.avg_consumption_l_100km) : '',
    insurance_expiry: vehicle?.insurance_expiry ?? '',
    inspection_expiry: vehicle?.inspection_expiry ?? '',
    last_maintenance_at: vehicle?.last_maintenance_at ?? '',
    home_warehouse_id: vehicle?.home_warehouse_id != null ? String(vehicle.home_warehouse_id) : '',
    default_salesperson_id:
      vehicle?.default_salesperson_id != null ? String(vehicle.default_salesperson_id) : '',
    region_id: vehicle?.region_id != null ? String(vehicle.region_id) : '',
    is_active: vehicle?.is_active ?? true,
    notes: vehicle?.notes ?? '',
  })
  const set = (k: keyof typeof f, v: string | boolean) => setF((p) => ({ ...p, [k]: v }))

  const save = useMutation({
    mutationFn: () => {
      const n = (v: string) => (v.trim() === '' ? null : Number(v))
      const body: Record<string, unknown> = {
        plate_number: f.plate_number.trim(),
        name: f.name.trim() || null,
        vehicle_type: f.vehicle_type,
        brand: f.brand.trim() || null,
        model: f.model.trim() || null,
        model_year: n(f.model_year),
        is_refrigerated: f.is_refrigerated,
        capacity_volume_l: Number(f.capacity_volume_l || 0),
        capacity_weight_kg: Number(f.capacity_weight_kg || 0),
        capacity_cases: n(f.capacity_cases),
        odometer_km: Number(f.odometer_km || 0),
        fuel_type: f.fuel_type.trim() || null,
        avg_consumption_l_100km: n(f.avg_consumption_l_100km),
        insurance_expiry: f.insurance_expiry || null,
        inspection_expiry: f.inspection_expiry || null,
        last_maintenance_at: f.last_maintenance_at || null,
        home_warehouse_id: n(f.home_warehouse_id),
        default_salesperson_id: n(f.default_salesperson_id),
        region_id: n(f.region_id),
        notes: f.notes.trim() || null,
      }
      if (vehicle) {
        return api.put<VehicleRow>(`/vehicles/${vehicle.id}`, {
          ...body,
          status: f.status,
          is_active: f.is_active,
        })
      }
      return api.post<VehicleRow>('/vehicles', body)
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
      size="lg"
      onClose={onClose}
      title={vehicle ? t('vehicles.edit') : t('vehicles.new')}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={save.isPending || f.plate_number.trim().length < 2}
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
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Field label={t('vehicles.plate')} required>
              <input
                className="input"
                value={f.plate_number}
                onChange={(e) => set('plate_number', e.target.value)}
              />
            </Field>
            <Field label={t('common.name')}>
              <input className="input" value={f.name} onChange={(e) => set('name', e.target.value)} />
            </Field>
            <Field label={t('vehicles.type')}>
              <select
                className="input"
                value={f.vehicle_type}
                onChange={(e) => set('vehicle_type', e.target.value)}
              >
                {TYPES.map((ty) => (
                  <option key={ty} value={ty}>
                    {ty}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t('vehicles.brand')}>
              <input className="input" value={f.brand} onChange={(e) => set('brand', e.target.value)} />
            </Field>
            <Field label={t('vehicles.model')}>
              <input className="input" value={f.model} onChange={(e) => set('model', e.target.value)} />
            </Field>
            <Field label={t('vehicles.modelYear')}>
              <input
                type="number"
                className="input tabular"
                value={f.model_year}
                onChange={(e) => set('model_year', e.target.value)}
              />
            </Field>
            {vehicle && (
              <Field label={t('common.status')}>
                <select className="input" value={f.status} onChange={(e) => set('status', e.target.value)}>
                  {STATUSES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </Field>
            )}
            <label className="flex items-center gap-2 pt-5 text-sm text-shell-700">
              <input
                type="checkbox"
                checked={f.is_refrigerated}
                onChange={(e) => set('is_refrigerated', e.target.checked)}
              />
              {t('vehicles.refrigerated')}
            </label>
            {vehicle && (
              <label className="flex items-center gap-2 pt-5 text-sm text-shell-700">
                <input type="checkbox" checked={f.is_active} onChange={(e) => set('is_active', e.target.checked)} />
                {t('common.active')}
              </label>
            )}
          </div>
        </div>

        <div>
          <SectionTitle>{t('vehicleStock.capacity')}</SectionTitle>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Field label={t('vehicles.capacityVolume')}>
              <input
                type="number"
                step="0.01"
                className="input tabular text-right"
                value={f.capacity_volume_l}
                onChange={(e) => set('capacity_volume_l', e.target.value)}
              />
            </Field>
            <Field label={t('vehicles.capacityWeight')}>
              <input
                type="number"
                step="0.01"
                className="input tabular text-right"
                value={f.capacity_weight_kg}
                onChange={(e) => set('capacity_weight_kg', e.target.value)}
              />
            </Field>
            <Field label={t('vehicles.capacityCases')}>
              <input
                type="number"
                className="input tabular text-right"
                value={f.capacity_cases}
                onChange={(e) => set('capacity_cases', e.target.value)}
              />
            </Field>
            <Field label={t('vehicles.odometer')}>
              <input
                type="number"
                step="0.1"
                className="input tabular text-right"
                value={f.odometer_km}
                onChange={(e) => set('odometer_km', e.target.value)}
              />
            </Field>
            <Field label={t('vehicles.fuelType')}>
              <input className="input" value={f.fuel_type} onChange={(e) => set('fuel_type', e.target.value)} />
            </Field>
            <Field label={t('vehicles.consumption')}>
              <input
                type="number"
                step="0.1"
                className="input tabular text-right"
                value={f.avg_consumption_l_100km}
                onChange={(e) => set('avg_consumption_l_100km', e.target.value)}
              />
            </Field>
          </div>
        </div>

        <div>
          <SectionTitle>{t('vehicles.warnings')}</SectionTitle>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Field label={t('vehicles.insuranceExpiry')}>
              <input
                type="date"
                className="input"
                value={f.insurance_expiry}
                onChange={(e) => set('insurance_expiry', e.target.value)}
              />
            </Field>
            <Field label={t('vehicles.inspectionExpiry')}>
              <input
                type="date"
                className="input"
                value={f.inspection_expiry}
                onChange={(e) => set('inspection_expiry', e.target.value)}
              />
            </Field>
            <Field label={t('vehicles.lastMaintenance')}>
              <input
                type="date"
                className="input"
                value={f.last_maintenance_at}
                onChange={(e) => set('last_maintenance_at', e.target.value)}
              />
            </Field>
          </div>
        </div>

        <div>
          <SectionTitle>{t('common.details')}</SectionTitle>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Field label={t('vehicles.homeWarehouse')}>
              <select
                className="input"
                value={f.home_warehouse_id}
                onChange={(e) => set('home_warehouse_id', e.target.value)}
              >
                <option value="">{t('common.select')}</option>
                {warehouses
                  .filter((w) => w.warehouse_type !== 'VEHICLE')
                  .map((w) => (
                    <option key={w.id} value={w.id}>
                      {w.code} — {w.name}
                    </option>
                  ))}
              </select>
            </Field>
            <Field label={t('vehicles.assignedTo')}>
              <select
                className="input"
                value={f.default_salesperson_id}
                onChange={(e) => set('default_salesperson_id', e.target.value)}
              >
                <option value="">{t('common.select')}</option>
                {salespersons.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.full_name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t('warehouses.regionId')}>
              <input
                type="number"
                className="input tabular"
                value={f.region_id}
                onChange={(e) => set('region_id', e.target.value)}
              />
            </Field>
          </div>
        </div>

        <Field label={t('common.notes')}>
          <textarea className="input" rows={2} value={f.notes} onChange={(e) => set('notes', e.target.value)} />
        </Field>
      </div>
    </Modal>
  )
}
