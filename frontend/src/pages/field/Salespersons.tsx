/**
 * Plasiyerler / Salespeople.
 *
 * The record that ties a login to a van, a warehouse and a discount ceiling.
 * The performance strip is best-effort: if the analytics module is not licensed
 * for this user the page still works, it just says so.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Pencil, Plus, Search, Trash2, Users } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatMoney, formatNumber, formatPercent } from '@/lib/format'
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
  StatusBadge,
  useToast,
} from '@/components/ui'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface SalespersonRow {
  id: number
  code: string
  user_id?: number | null
  full_name: string
  phone?: string | null
  email?: string | null
  region_id?: number | null
  supervisor_id?: number | null
  default_vehicle_id?: number | null
  default_warehouse_id?: number | null
  hire_date?: string | null
  is_active: boolean
  commission_percent: number
  max_discount_percent: number
  can_sell_on_credit: boolean
  cash_limit: number | string
  notes?: string | null
}

interface UserRow {
  id: number
  username: string
  full_name: string
}

interface VehicleRow {
  id: number
  plate_number: string
}

interface WarehouseRow {
  id: number
  code: string
  name: string
  warehouse_type: string
}

interface PerformanceRow {
  rank: number
  key: string
  code?: string | null
  label: string
  sales_amount: number | string
  margin_amount: number | string
  margin_percent: number
  order_count: number
  customer_count: number
}

const SIZE = 20
const daysAgo = (n: number) => {
  const d = new Date()
  d.setDate(d.getDate() - n)
  return d.toISOString().slice(0, 10)
}

export default function Salespersons() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()

  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [editing, setEditing] = useState<SalespersonRow | null | undefined>(undefined)

  const listParams = { page, size: SIZE, search: search || undefined }
  const list = useQuery({
    queryKey: ['salespersons', listParams],
    queryFn: () => api.get<Paged<SalespersonRow>>('/vehicles/salespersons', listParams),
  })

  const users = useQuery({
    queryKey: ['users', 'picker'],
    queryFn: () => api.get<Paged<UserRow>>('/system/users', { size: 200 }),
    enabled: can('system.users', 'VIEW'),
    retry: false,
    throwOnError: false,
  })

  const vehicles = useQuery({
    queryKey: ['vehicles', 'picker'],
    queryFn: () => api.get<Paged<VehicleRow>>('/vehicles', { size: 200, is_active: true }),
    enabled: can('field.vehicles', 'VIEW'),
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

  const performance = useQuery({
    queryKey: ['salesperson-performance'],
    queryFn: () =>
      api.get<PerformanceRow[]>('/analytics/salespersons', { start: daysAgo(30), limit: 100 }),
    enabled: can('analytics.reports', 'VIEW'),
    retry: false,
    throwOnError: false,
  })

  const perfOf = (row: SalespersonRow) =>
    (performance.data ?? []).find((p) => p.key === String(row.id) || p.code === row.code) ?? null

  const remove = useMutation({
    mutationFn: (id: number) => api.delete<{ message: string }>(`/vehicles/salespersons/${id}`),
    onSuccess: () => {
      push('success', t('stockCommon.removed'))
      void qc.invalidateQueries({ queryKey: ['salespersons'] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const rows = list.data?.items ?? []
  const vehicleLabel = (id?: number | null) =>
    (vehicles.data?.items ?? []).find((v) => v.id === id)?.plate_number ?? (id ? `#${id}` : '—')
  const warehouseLabel = (id?: number | null) =>
    (warehouses.data?.items ?? []).find((w) => w.id === id)?.code ?? (id ? `#${id}` : '—')
  const userLabel = (id?: number | null) =>
    (users.data?.items ?? []).find((u) => u.id === id)?.username ?? (id ? `#${id}` : '—')

  return (
    <>
      <PageHeader
        title={t('salespersons.title')}
        subtitle={t('salespersons.subtitle')}
        icon={<Users className="h-5 w-5" />}
        actions={
          can('field.salespersons', 'CREATE') && (
            <button type="button" className="btn-primary btn-sm" onClick={() => setEditing(null)}>
              <Plus className="h-4 w-4" />
              {t('salespersons.new')}
            </button>
          )
        }
      />

      {can('analytics.reports', 'VIEW') && (
        <Card className="mb-4" title={t('salespersons.performance')} bodyClassName="p-3">
          {performance.isLoading ? (
            <SkeletonRows rows={1} cols={4} />
          ) : (performance.data ?? []).length === 0 ? (
            <p className="text-xs text-shell-500">{t('salespersons.performanceUnavailable')}</p>
          ) : (
            <ul className="flex gap-3 overflow-x-auto pb-1">
              {(performance.data ?? []).slice(0, 8).map((p) => (
                <li key={p.key} className="min-w-[12rem] rounded-lg border border-shell-200 p-3">
                  <p className="truncate text-xs font-medium text-shell-700">
                    #{p.rank} {p.label}
                  </p>
                  <p className="tabular mt-1 text-base font-semibold text-shell-900">
                    {formatMoney(p.sales_amount, { compact: true })}
                  </p>
                  <p className="tabular text-2xs text-shell-500">
                    {t('salespersons.margin')} {formatPercent(p.margin_percent)} ·{' '}
                    {t('salespersons.orders')} {formatNumber(p.order_count)} ·{' '}
                    {t('salespersons.customers')} {formatNumber(p.customer_count)}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}

      <Card bodyClassName="p-0">
        <div className="border-b border-shell-200 p-3">
          <div className="relative max-w-sm">
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
                  <th>{t('common.code')}</th>
                  <th>{t('salespersons.fullName')}</th>
                  <th>{t('salespersons.linkedUser')}</th>
                  <th>{t('salespersons.phone')}</th>
                  <th>{t('salespersons.defaultVehicle')}</th>
                  <th>{t('salespersons.defaultWarehouse')}</th>
                  <th className="text-right">{t('salespersons.commission')}</th>
                  <th className="text-right">{t('salespersons.maxDiscount')}</th>
                  <th className="text-right">{t('salespersons.sales')}</th>
                  <th>{t('common.status')}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((s) => {
                  const perf = perfOf(s)
                  return (
                    <tr key={s.id}>
                      <td className="tabular whitespace-nowrap">{s.code}</td>
                      <td className="min-w-[10rem] font-medium">{s.full_name}</td>
                      <td className="text-xs text-shell-500">
                        {s.user_id ? userLabel(s.user_id) : t('salespersons.noUser')}
                      </td>
                      <td className="tabular text-xs">{s.phone ?? '—'}</td>
                      <td className="text-xs">{vehicleLabel(s.default_vehicle_id)}</td>
                      <td className="text-xs">{warehouseLabel(s.default_warehouse_id)}</td>
                      <td className="tabular text-right">{formatPercent(s.commission_percent)}</td>
                      <td className="tabular text-right">{formatPercent(s.max_discount_percent)}</td>
                      <td className="tabular text-right">
                        {perf ? formatMoney(perf.sales_amount, { compact: true }) : '—'}
                      </td>
                      <td>
                        <StatusBadge status={s.is_active ? 'ACTIVE' : 'PASSIVE'} />
                      </td>
                      <td className="whitespace-nowrap text-right">
                        {can('field.salespersons', 'UPDATE') && (
                          <button
                            type="button"
                            className="btn-ghost btn-sm"
                            onClick={() => setEditing(s)}
                            aria-label={t('common.edit')}
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </button>
                        )}
                        {can('field.salespersons', 'DELETE') && (
                          <button
                            type="button"
                            className="btn-ghost btn-sm text-danger-600"
                            onClick={() => {
                              if (window.confirm(t('stockCommon.confirmDelete'))) remove.mutate(s.id)
                            }}
                            aria-label={t('common.delete')}
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
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

      {editing !== undefined && (
        <SalespersonEditor
          person={editing}
          users={users.data?.items ?? []}
          vehicles={vehicles.data?.items ?? []}
          warehouses={warehouses.data?.items ?? []}
          people={rows}
          onClose={() => setEditing(undefined)}
          onSaved={() => {
            setEditing(undefined)
            void qc.invalidateQueries({ queryKey: ['salespersons'] })
          }}
        />
      )}
    </>
  )
}

/* -------------------------------------------------------------------------- */
/* Editor                                                                     */
/* -------------------------------------------------------------------------- */
function SalespersonEditor({
  person,
  users,
  vehicles,
  warehouses,
  people,
  onClose,
  onSaved,
}: {
  person: SalespersonRow | null
  users: UserRow[]
  vehicles: VehicleRow[]
  warehouses: WarehouseRow[]
  people: SalespersonRow[]
  onClose: () => void
  onSaved: () => void
}) {
  const { t } = useTranslation()
  const { push } = useToast()
  const [f, setF] = useState({
    full_name: person?.full_name ?? '',
    code: person?.code ?? '',
    user_id: person?.user_id != null ? String(person.user_id) : '',
    phone: person?.phone ?? '',
    email: person?.email ?? '',
    region_id: person?.region_id != null ? String(person.region_id) : '',
    supervisor_id: person?.supervisor_id != null ? String(person.supervisor_id) : '',
    default_vehicle_id: person?.default_vehicle_id != null ? String(person.default_vehicle_id) : '',
    default_warehouse_id: person?.default_warehouse_id != null ? String(person.default_warehouse_id) : '',
    hire_date: person?.hire_date ?? '',
    commission_percent: String(person?.commission_percent ?? 0),
    max_discount_percent: String(person?.max_discount_percent ?? 10),
    can_sell_on_credit: person?.can_sell_on_credit ?? true,
    cash_limit: String(person?.cash_limit ?? 0),
    is_active: person?.is_active ?? true,
    notes: person?.notes ?? '',
  })
  const set = (k: keyof typeof f, v: string | boolean) => setF((p) => ({ ...p, [k]: v }))

  const save = useMutation({
    mutationFn: () => {
      const n = (v: string) => (v.trim() === '' ? null : Number(v))
      const body: Record<string, unknown> = {
        full_name: f.full_name.trim(),
        user_id: n(f.user_id),
        phone: f.phone.trim() || null,
        email: f.email.trim() || null,
        region_id: n(f.region_id),
        supervisor_id: n(f.supervisor_id),
        default_vehicle_id: n(f.default_vehicle_id),
        default_warehouse_id: n(f.default_warehouse_id),
        hire_date: f.hire_date || null,
        commission_percent: Number(f.commission_percent || 0),
        max_discount_percent: Number(f.max_discount_percent || 0),
        can_sell_on_credit: f.can_sell_on_credit,
        cash_limit: Number(f.cash_limit || 0),
        notes: f.notes.trim() || null,
      }
      if (person) return api.put<SalespersonRow>(`/vehicles/salespersons/${person.id}`, { ...body, is_active: f.is_active })
      return api.post<SalespersonRow>('/vehicles/salespersons', {
        ...body,
        code: f.code.trim() || null,
      })
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
      title={person ? t('salespersons.edit') : t('salespersons.new')}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={save.isPending || f.full_name.trim().length < 2}
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
            <Field label={t('salespersons.fullName')} required>
              <input className="input" value={f.full_name} onChange={(e) => set('full_name', e.target.value)} />
            </Field>
            {!person && (
              <Field label={t('common.code')}>
                <input className="input" value={f.code} onChange={(e) => set('code', e.target.value)} />
              </Field>
            )}
            <Field label={t('salespersons.linkedUser')}>
              <select className="input" value={f.user_id} onChange={(e) => set('user_id', e.target.value)}>
                <option value="">{t('salespersons.noUser')}</option>
                {users.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.full_name} ({u.username})
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t('salespersons.phone')}>
              <input className="input tabular" value={f.phone} onChange={(e) => set('phone', e.target.value)} />
            </Field>
            <Field label={t('salespersons.email')}>
              <input className="input" type="email" value={f.email} onChange={(e) => set('email', e.target.value)} />
            </Field>
            <Field label={t('salespersons.hireDate')}>
              <input
                type="date"
                className="input"
                value={f.hire_date}
                onChange={(e) => set('hire_date', e.target.value)}
              />
            </Field>
          </div>
        </div>

        <div>
          <SectionTitle>{t('nav.field')}</SectionTitle>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Field label={t('salespersons.regionId')}>
              <input
                type="number"
                className="input tabular"
                value={f.region_id}
                onChange={(e) => set('region_id', e.target.value)}
              />
            </Field>
            <Field label={t('salespersons.supervisor')}>
              <select
                className="input"
                value={f.supervisor_id}
                onChange={(e) => set('supervisor_id', e.target.value)}
              >
                <option value="">{t('common.select')}</option>
                {people
                  .filter((p) => p.id !== person?.id)
                  .map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.full_name}
                    </option>
                  ))}
              </select>
            </Field>
            <Field label={t('salespersons.defaultVehicle')}>
              <select
                className="input"
                value={f.default_vehicle_id}
                onChange={(e) => set('default_vehicle_id', e.target.value)}
              >
                <option value="">{t('common.select')}</option>
                {vehicles.map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.plate_number}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t('salespersons.defaultWarehouse')}>
              <select
                className="input"
                value={f.default_warehouse_id}
                onChange={(e) => set('default_warehouse_id', e.target.value)}
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
          </div>
        </div>

        <div>
          <SectionTitle>{t('salespersons.commission')}</SectionTitle>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Field label={t('salespersons.commission')}>
              <input
                type="number"
                step="0.1"
                min={0}
                max={100}
                className="input tabular text-right"
                value={f.commission_percent}
                onChange={(e) => set('commission_percent', e.target.value)}
              />
            </Field>
            <Field label={t('salespersons.maxDiscount')}>
              <input
                type="number"
                step="0.1"
                min={0}
                max={100}
                className="input tabular text-right"
                value={f.max_discount_percent}
                onChange={(e) => set('max_discount_percent', e.target.value)}
              />
            </Field>
            <Field label={t('salespersons.cashLimit')}>
              <input
                type="number"
                step="0.01"
                min={0}
                className="input tabular text-right"
                value={f.cash_limit}
                onChange={(e) => set('cash_limit', e.target.value)}
              />
            </Field>
            <label className="flex items-center gap-2 pt-5 text-sm text-shell-700">
              <input
                type="checkbox"
                checked={f.can_sell_on_credit}
                onChange={(e) => set('can_sell_on_credit', e.target.checked)}
              />
              {t('salespersons.canSellOnCredit')}
            </label>
            {person && (
              <label className="flex items-center gap-2 pt-5 text-sm text-shell-700">
                <input type="checkbox" checked={f.is_active} onChange={(e) => set('is_active', e.target.checked)} />
                {t('common.active')}
              </label>
            )}
          </div>
        </div>

        <Field label={t('common.notes')}>
          <textarea className="input" rows={2} value={f.notes} onChange={(e) => set('notes', e.target.value)} />
        </Field>
        {person?.hire_date && (
          <p className="text-2xs text-shell-400">
            {t('salespersons.hireDate')}: {formatDate(person.hire_date, { short: true })}
          </p>
        )}
      </div>
    </Modal>
  )
}
