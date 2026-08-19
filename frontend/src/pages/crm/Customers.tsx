/**
 * Müşteriler / Customers — the CRM master list.
 *
 * Search + filter grid over `GET /customers`, a full customer card modal
 * (identity, tax, address & GPS, contact, visit plan, commercial terms) and a
 * CSV export that reuses the backend's own `/customers/export` writer so the
 * file matches what the office already knows.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, MapPin, Pencil, Plus, Search, Trash2, Users } from 'lucide-react'
import { useMemo, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatMoney, formatNumber, toNumber } from '@/lib/format'
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
export interface CustomerRow {
  id: number
  code: string
  name: string
  trade_name?: string | null
  customer_type: string
  channel: string
  status: string
  city?: string | null
  district?: string | null
  phone?: string | null
  mobile?: string | null
  latitude?: number | null
  longitude?: number | null
  default_salesperson_id?: number | null
  default_route_id?: number | null
  visit_days?: string | null
  visit_sequence: number
  balance: number | string
  overdue_balance: number | string
  credit_limit: number | string
  total_sales_amount: number | string
  order_count: number
  last_order_date?: string | null
  last_visit_date?: string | null
  risk_score: number
  is_priority: boolean
}

interface CustomerCard extends CustomerRow {
  sub_channel?: string | null
  tax_office?: string | null
  tax_number?: string | null
  national_id?: string | null
  is_e_invoice: boolean
  address?: string | null
  neighbourhood?: string | null
  postal_code?: string | null
  region_id?: number | null
  email?: string | null
  contact_person?: string | null
  visit_frequency: string
  service_time_minutes: number
  opening_time?: string | null
  closing_time?: string | null
  price_list_id?: number | null
  payment_method: string
  payment_term_days: number
  risk_limit: number | string
  discount_percent: number
  currency: string
  notes?: string | null
  tags?: string | null
}

interface LookupRow {
  id: number
  code?: string | null
  name?: string | null
  full_name?: string | null
}

const TYPES = [
  'GROCERY', 'MARKET', 'SUPERMARKET', 'RESTAURANT', 'CAFE', 'HOTEL', 'KIOSK',
  'CANTEEN', 'SCHOOL', 'HOSPITAL', 'GAS_STATION', 'WHOLESALER', 'DEALER',
  'DISTRIBUTOR', 'HORECA', 'OTHER',
]
const CHANNELS = ['TRADITIONAL', 'MODERN', 'HORECA', 'WHOLESALE', 'ONLINE', 'INSTITUTIONAL']
const STATUSES = ['ACTIVE', 'PASSIVE', 'BLOCKED', 'PROSPECT', 'CHURNED']
const FREQUENCIES = ['DAILY', 'TWICE_WEEKLY', 'WEEKLY', 'BIWEEKLY', 'MONTHLY', 'ON_DEMAND']
const WEEKDAYS = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']
const METHODS = [
  'CASH', 'CREDIT_CARD', 'BANK_TRANSFER', 'CHEQUE', 'PROMISSORY_NOTE',
  'OPEN_ACCOUNT', 'MIXED',
]
const SORTS = ['name', 'code', 'balance', 'overdue', 'sales', 'last_order', 'risk']

interface FormState {
  code: string; name: string; trade_name: string; customer_type: string
  channel: string; sub_channel: string; status: string
  tax_office: string; tax_number: string; national_id: string; is_e_invoice: boolean
  address: string; city: string; district: string; neighbourhood: string
  postal_code: string; latitude: string; longitude: string; region_id: string
  phone: string; mobile: string; email: string; contact_person: string
  default_route_id: string; default_salesperson_id: string
  visit_frequency: string; visit_days: string[]; visit_sequence: string
  service_time_minutes: string; opening_time: string; closing_time: string
  is_priority: boolean
  price_list_id: string; payment_method: string; payment_term_days: string
  credit_limit: string; risk_limit: string; discount_percent: string; currency: string
  notes: string; tags: string
}

const EMPTY: FormState = {
  code: '', name: '', trade_name: '', customer_type: 'GROCERY',
  channel: 'TRADITIONAL', sub_channel: '', status: 'ACTIVE',
  tax_office: '', tax_number: '', national_id: '', is_e_invoice: false,
  address: '', city: '', district: '', neighbourhood: '', postal_code: '',
  latitude: '', longitude: '', region_id: '',
  phone: '', mobile: '', email: '', contact_person: '',
  default_route_id: '', default_salesperson_id: '',
  visit_frequency: 'WEEKLY', visit_days: [], visit_sequence: '0',
  service_time_minutes: '10', opening_time: '', closing_time: '', is_priority: false,
  price_list_id: '', payment_method: 'CASH', payment_term_days: '0',
  credit_limit: '0', risk_limit: '0', discount_percent: '0', currency: 'TRY',
  notes: '', tags: '',
}

const str = (v: string) => (v.trim() === '' ? null : v.trim())
const int = (v: string) => (v.trim() === '' ? null : Math.trunc(Number(v)))
const dec = (v: string) => (v.trim() === '' ? 0 : Number(v))

/* -------------------------------------------------------------------------- */
/* Small form primitives                                                      */
/* -------------------------------------------------------------------------- */
function TextIn({
  label, value, onChange, type = 'text', required, hint, step,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  type?: string
  required?: boolean
  hint?: string
  step?: string
}) {
  return (
    <Field label={label} required={required} hint={hint}>
      <input
        className="input"
        type={type}
        step={step}
        value={value}
        required={required}
        onChange={(e) => onChange(e.target.value)}
      />
    </Field>
  )
}

function SelectIn({
  label, value, onChange, options, blank,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  options: { value: string; label: string }[]
  blank?: string
}) {
  return (
    <Field label={label}>
      <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
        {blank !== undefined && <option value="">{blank}</option>}
        {options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </Field>
  )
}

function CheckIn({
  label, checked, onChange,
}: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <label className="mt-6 flex items-center gap-2 text-sm text-shell-700">
      <input
        type="checkbox"
        className="h-4 w-4 rounded border-shell-300"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      {label}
    </label>
  )
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="border-t border-shell-100 pt-4 first:border-0 first:pt-0">
      <SectionTitle>{title}</SectionTitle>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{children}</div>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function Customers() {
  const { t } = useTranslation()
  const { can, canAny } = useAuth()
  const { push } = useToast()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const [term, setTerm] = useState('')
  const [type, setType] = useState('')
  const [channel, setChannel] = useState('')
  const [status, setStatus] = useState('')
  const [regionId, setRegionId] = useState('')
  const [city, setCity] = useState('')
  const [hasDebt, setHasDebt] = useState('')
  const [orderBy, setOrderBy] = useState('name')
  const [page, setPage] = useState(1)
  const size = 25

  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<CustomerCard | null>(null)
  const [form, setForm] = useState<FormState>(EMPTY)
  const [exporting, setExporting] = useState(false)

  const patch = (p: Partial<FormState>) => setForm((f) => ({ ...f, ...p }))

  const filters = useMemo(
    () => ({
      term: term || undefined,
      customer_type: type || undefined,
      channel: channel || undefined,
      status: status || undefined,
      region_id: regionId || undefined,
      city: city || undefined,
      has_debt: hasDebt === '' ? undefined : hasDebt === 'yes',
      order_by: orderBy,
    }),
    [term, type, channel, status, regionId, city, hasDebt, orderBy],
  )

  const list = useQuery({
    queryKey: ['customers', filters, page],
    queryFn: () => api.get<Paged<CustomerRow>>('/customers', { ...filters, page, size }),
  })

  /* ---- lookups used by the card ------------------------------------------ */
  const priceLists = useQuery({
    queryKey: ['price-lists-lookup'],
    queryFn: () => api.get<LookupRow[]>('/products/price-lists', { is_active: true }),
    enabled: open && canAny(['marketing.price_lists', 'VIEW'], ['stock.products', 'VIEW']),
    retry: false,
    throwOnError: false,
  })
  const salespersons = useQuery({
    queryKey: ['salespersons-lookup'],
    queryFn: () =>
      api.get<Paged<LookupRow>>('/vehicles/salespersons', { size: 200, is_active: true }),
    enabled: open && can('field.salespersons'),
    retry: false,
    throwOnError: false,
  })
  const routes = useQuery({
    queryKey: ['routes-lookup'],
    queryFn: () => api.get<Paged<LookupRow>>('/routes', { size: 200 }),
    enabled: open && can('field.routes'),
    retry: false,
    throwOnError: false,
  })

  const opt = (rows: LookupRow[] | undefined) =>
    (rows ?? []).map((r) => ({
      value: String(r.id),
      label: [r.code, r.name ?? r.full_name].filter(Boolean).join(' · ') || String(r.id),
    }))

  /* ---- open / close ------------------------------------------------------- */
  const openCreate = () => {
    setEditing(null)
    setForm(EMPTY)
    setOpen(true)
  }

  const openEdit = async (id: number) => {
    try {
      const c = await api.get<CustomerCard>(`/customers/${id}`)
      setEditing(c)
      setForm({
        code: c.code ?? '',
        name: c.name ?? '',
        trade_name: c.trade_name ?? '',
        customer_type: c.customer_type,
        channel: c.channel,
        sub_channel: c.sub_channel ?? '',
        status: c.status,
        tax_office: c.tax_office ?? '',
        tax_number: c.tax_number ?? '',
        national_id: c.national_id ?? '',
        is_e_invoice: !!c.is_e_invoice,
        address: c.address ?? '',
        city: c.city ?? '',
        district: c.district ?? '',
        neighbourhood: c.neighbourhood ?? '',
        postal_code: c.postal_code ?? '',
        latitude: c.latitude === null || c.latitude === undefined ? '' : String(c.latitude),
        longitude: c.longitude === null || c.longitude === undefined ? '' : String(c.longitude),
        region_id: c.region_id ? String(c.region_id) : '',
        phone: c.phone ?? '',
        mobile: c.mobile ?? '',
        email: c.email ?? '',
        contact_person: c.contact_person ?? '',
        default_route_id: c.default_route_id ? String(c.default_route_id) : '',
        default_salesperson_id: c.default_salesperson_id ? String(c.default_salesperson_id) : '',
        visit_frequency: c.visit_frequency ?? 'WEEKLY',
        visit_days: (c.visit_days ?? '').split(',').map((d) => d.trim()).filter(Boolean),
        visit_sequence: String(c.visit_sequence ?? 0),
        service_time_minutes: String(c.service_time_minutes ?? 10),
        opening_time: c.opening_time ?? '',
        closing_time: c.closing_time ?? '',
        is_priority: !!c.is_priority,
        price_list_id: c.price_list_id ? String(c.price_list_id) : '',
        payment_method: c.payment_method ?? 'CASH',
        payment_term_days: String(c.payment_term_days ?? 0),
        credit_limit: String(toNumber(c.credit_limit)),
        risk_limit: String(toNumber(c.risk_limit)),
        discount_percent: String(c.discount_percent ?? 0),
        currency: c.currency ?? 'TRY',
        notes: c.notes ?? '',
        tags: c.tags ?? '',
      })
      setOpen(true)
    } catch (e) {
      push('error', e instanceof ApiError ? e.message : t('errors.generic'))
    }
  }

  /* ---- save --------------------------------------------------------------- */
  const body = () => ({
    name: form.name.trim(),
    trade_name: str(form.trade_name),
    customer_type: form.customer_type,
    channel: form.channel,
    sub_channel: str(form.sub_channel),
    status: form.status,
    tax_office: str(form.tax_office),
    tax_number: str(form.tax_number),
    national_id: str(form.national_id),
    is_e_invoice: form.is_e_invoice,
    address: str(form.address),
    city: str(form.city),
    district: str(form.district),
    neighbourhood: str(form.neighbourhood),
    postal_code: str(form.postal_code),
    latitude: form.latitude.trim() === '' ? null : Number(form.latitude),
    longitude: form.longitude.trim() === '' ? null : Number(form.longitude),
    region_id: int(form.region_id),
    phone: str(form.phone),
    mobile: str(form.mobile),
    email: str(form.email),
    contact_person: str(form.contact_person),
    default_route_id: int(form.default_route_id),
    default_salesperson_id: int(form.default_salesperson_id),
    visit_frequency: form.visit_frequency,
    visit_days: form.visit_days.length ? form.visit_days.join(',') : null,
    visit_sequence: Math.max(0, Math.trunc(dec(form.visit_sequence))),
    service_time_minutes: Math.max(0, Math.trunc(dec(form.service_time_minutes))),
    opening_time: str(form.opening_time),
    closing_time: str(form.closing_time),
    is_priority: form.is_priority,
    price_list_id: int(form.price_list_id),
    payment_method: form.payment_method,
    payment_term_days: Math.max(0, Math.trunc(dec(form.payment_term_days))),
    risk_limit: dec(form.risk_limit),
    discount_percent: dec(form.discount_percent),
    currency: form.currency || 'TRY',
    notes: str(form.notes),
    tags: str(form.tags),
  })

  const save = useMutation({
    mutationFn: async () => {
      const mayCredit = can('crm.credit_limit', 'UPDATE')
      if (editing) {
        await api.put<CustomerCard>(`/customers/${editing.id}`, body())
        if (mayCredit && dec(form.credit_limit) !== toNumber(editing.credit_limit)) {
          await api.put(`/customers/${editing.id}/credit-limit`, {
            credit_limit: dec(form.credit_limit),
            risk_limit: dec(form.risk_limit),
          })
        }
        return
      }
      await api.post<CustomerCard>('/customers', {
        ...body(),
        code: str(form.code),
        credit_limit: mayCredit ? dec(form.credit_limit) : 0,
      })
    },
    onSuccess: () => {
      push('success', t('common.success'))
      setOpen(false)
      void qc.invalidateQueries({ queryKey: ['customers'] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const remove = useMutation({
    mutationFn: (id: number) => api.delete(`/customers/${id}`),
    onSuccess: () => {
      push('success', t('common.success'))
      void qc.invalidateQueries({ queryKey: ['customers'] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const exportCsv = async () => {
    setExporting(true)
    try {
      const { blob, filename } = await api.download('/customers/export', undefined, filters)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename || 'customers.csv'
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      push('error', e instanceof ApiError ? e.message : t('errors.generic'))
    } finally {
      setExporting(false)
    }
  }

  const locate = () => {
    if (!navigator.geolocation) {
      push('error', t('crm.locationFailed'))
      return
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        patch({
          latitude: String(pos.coords.latitude),
          longitude: String(pos.coords.longitude),
        })
        push('success', t('crm.locationCaptured'))
      },
      () => push('error', t('crm.locationFailed')),
    )
  }

  const rows = list.data?.items ?? []

  /* ---- render ------------------------------------------------------------- */
  return (
    <>
      <PageHeader
        title={t('crm.customers')}
        subtitle={t('crm.customersSubtitle')}
        icon={<Users className="h-5 w-5" />}
        actions={
          <>
            {can('crm.customers', 'EXPORT') && (
              <button type="button" className="btn-secondary" onClick={() => void exportCsv()}>
                {exporting ? <Spinner /> : <Download className="h-4 w-4" />}
                {t('common.export')}
              </button>
            )}
            {can('crm.customers', 'CREATE') && (
              <button type="button" className="btn-primary" onClick={openCreate}>
                <Plus className="h-4 w-4" />
                {t('crm.newCustomer')}
              </button>
            )}
          </>
        }
      />

      <Card bodyClassName="p-4">
        <div className="flex flex-wrap items-end gap-2">
          <div className="relative min-w-[14rem] flex-1">
            <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-shell-400" />
            <input
              className="input pl-9"
              placeholder={t('crm.searchPlaceholder')}
              value={term}
              onChange={(e) => {
                setTerm(e.target.value)
                setPage(1)
              }}
            />
          </div>
          {[
            { v: type, set: setType, opts: TYPES, ns: 'crm.types', blank: t('crm.type') },
            { v: channel, set: setChannel, opts: CHANNELS, ns: 'crm.channels', blank: t('crm.channel') },
            { v: status, set: setStatus, opts: STATUSES, ns: 'crm.statuses', blank: t('crm.status') },
          ].map((f) => (
            <select
              key={f.blank}
              className="input w-auto"
              value={f.v}
              onChange={(e) => {
                f.set(e.target.value)
                setPage(1)
              }}
            >
              <option value="">{f.blank}</option>
              {f.opts.map((o) => (
                <option key={o} value={o}>{t(`${f.ns}.${o}`)}</option>
              ))}
            </select>
          ))}
          <input
            className="input w-28"
            placeholder={t('crm.city')}
            value={city}
            onChange={(e) => {
              setCity(e.target.value)
              setPage(1)
            }}
          />
          <input
            className="input tabular w-24"
            type="number"
            placeholder={t('crm.regionId')}
            value={regionId}
            onChange={(e) => {
              setRegionId(e.target.value)
              setPage(1)
            }}
          />
          <select
            className="input w-auto"
            value={hasDebt}
            onChange={(e) => {
              setHasDebt(e.target.value)
              setPage(1)
            }}
          >
            <option value="">{t('crm.debtFilter')}</option>
            <option value="yes">{t('crm.withDebt')}</option>
            <option value="no">{t('crm.noDebt')}</option>
          </select>
          <select
            className="input w-auto"
            value={orderBy}
            onChange={(e) => setOrderBy(e.target.value)}
          >
            {SORTS.map((s) => (
              <option key={s} value={s}>
                {t(`crm.sort${s.split('_').map((p) => p[0].toUpperCase() + p.slice(1)).join('')}`)}
              </option>
            ))}
          </select>
        </div>
      </Card>

      <Card className="mt-4" bodyClassName="p-0">
        {list.isLoading ? (
          <SkeletonRows rows={8} cols={7} />
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
                    <th>{t('crm.code')}</th>
                    <th>{t('crm.name')}</th>
                    <th>{t('crm.type')}</th>
                    <th>{t('crm.status')}</th>
                    <th>{t('crm.phone')}</th>
                    <th className="text-right">{t('crm.balance')}</th>
                    <th className="text-right">{t('crm.creditLimit')}</th>
                    <th>{t('crm.lastOrder')}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((c) => (
                    <tr
                      key={c.id}
                      className="cursor-pointer"
                      onClick={() => navigate(`/crm/customers/${c.id}`)}
                    >
                      <td className="tabular whitespace-nowrap font-medium">{c.code}</td>
                      <td>
                        <span className="block max-w-[16rem] truncate font-medium text-shell-900">
                          {c.trade_name || c.name}
                        </span>
                        <span className="block text-2xs text-shell-400">
                          {[c.city, c.district].filter(Boolean).join(' / ')}
                        </span>
                      </td>
                      <td className="whitespace-nowrap text-xs">{t(`crm.types.${c.customer_type}`)}</td>
                      <td><StatusBadge status={c.status} label={t(`crm.statuses.${c.status}`)} /></td>
                      <td className="tabular whitespace-nowrap text-xs">{c.phone || c.mobile || '—'}</td>
                      <td
                        className={`tabular text-right ${
                          toNumber(c.overdue_balance) > 0 ? 'font-semibold text-danger-600' : ''
                        }`}
                      >
                        {formatMoney(c.balance)}
                      </td>
                      <td className="tabular text-right">{formatMoney(c.credit_limit)}</td>
                      <td className="whitespace-nowrap text-xs">{formatDate(c.last_order_date)}</td>
                      <td className="whitespace-nowrap text-right">
                        {can('crm.customers', 'UPDATE') && (
                          <button
                            type="button"
                            className="btn-ghost btn-sm"
                            onClick={(e) => {
                              e.stopPropagation()
                              void openEdit(c.id)
                            }}
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </button>
                        )}
                        {can('crm.customers', 'DELETE') && (
                          <button
                            type="button"
                            className="btn-ghost btn-sm text-danger-600"
                            onClick={(e) => {
                              e.stopPropagation()
                              if (window.confirm(t('crm.confirmDelete'))) remove.mutate(c.id)
                            }}
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
            <Pagination
              page={list.data?.page ?? 1}
              pages={list.data?.pages ?? 1}
              total={list.data?.total ?? 0}
              size={size}
              onPage={setPage}
            />
          </>
        )}
      </Card>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        size="xl"
        title={editing ? t('crm.editCustomer') : t('crm.newCustomer')}
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setOpen(false)}>
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={save.isPending || form.name.trim() === ''}
              onClick={() => save.mutate()}
            >
              {save.isPending && <Spinner />}
              {t('common.save')}
            </button>
          </>
        }
      >
        <div className="space-y-5">
          <Group title={t('crm.identity')}>
            <TextIn
              label={t('crm.code')}
              value={form.code}
              onChange={(v) => patch({ code: v })}
              hint={editing ? undefined : t('common.optional')}
            />
            <TextIn label={t('crm.name')} required value={form.name} onChange={(v) => patch({ name: v })} />
            <TextIn label={t('crm.tradeName')} value={form.trade_name} onChange={(v) => patch({ trade_name: v })} />
            <SelectIn
              label={t('crm.type')}
              value={form.customer_type}
              onChange={(v) => patch({ customer_type: v })}
              options={TYPES.map((o) => ({ value: o, label: t(`crm.types.${o}`) }))}
            />
            <SelectIn
              label={t('crm.channel')}
              value={form.channel}
              onChange={(v) => patch({ channel: v })}
              options={CHANNELS.map((o) => ({ value: o, label: t(`crm.channels.${o}`) }))}
            />
            <TextIn label={t('crm.subChannel')} value={form.sub_channel} onChange={(v) => patch({ sub_channel: v })} />
            <SelectIn
              label={t('crm.status')}
              value={form.status}
              onChange={(v) => patch({ status: v })}
              options={STATUSES.map((o) => ({ value: o, label: t(`crm.statuses.${o}`) }))}
            />
            <TextIn label={t('crm.tags')} value={form.tags} onChange={(v) => patch({ tags: v })} />
            <CheckIn
              label={t('crm.isPriority')}
              checked={form.is_priority}
              onChange={(v) => patch({ is_priority: v })}
            />
          </Group>

          <Group title={t('crm.taxInfo')}>
            <TextIn label={t('crm.taxOffice')} value={form.tax_office} onChange={(v) => patch({ tax_office: v })} />
            <TextIn label={t('crm.taxNumber')} value={form.tax_number} onChange={(v) => patch({ tax_number: v })} />
            <TextIn label={t('crm.nationalId')} value={form.national_id} onChange={(v) => patch({ national_id: v })} />
            <CheckIn
              label={t('crm.eInvoice')}
              checked={form.is_e_invoice}
              onChange={(v) => patch({ is_e_invoice: v })}
            />
          </Group>

          <Group title={t('crm.addressInfo')}>
            <div className="sm:col-span-2 lg:col-span-3">
              <Field label={t('crm.address')}>
                <textarea
                  className="input"
                  rows={2}
                  value={form.address}
                  onChange={(e) => patch({ address: e.target.value })}
                />
              </Field>
            </div>
            <TextIn label={t('crm.city')} value={form.city} onChange={(v) => patch({ city: v })} />
            <TextIn label={t('crm.district')} value={form.district} onChange={(v) => patch({ district: v })} />
            <TextIn label={t('crm.neighbourhood')} value={form.neighbourhood} onChange={(v) => patch({ neighbourhood: v })} />
            <TextIn label={t('crm.postalCode')} value={form.postal_code} onChange={(v) => patch({ postal_code: v })} />
            <TextIn label={t('crm.regionId')} type="number" value={form.region_id} onChange={(v) => patch({ region_id: v })} />
            <TextIn label={t('crm.latitude')} type="number" step="any" value={form.latitude} onChange={(v) => patch({ latitude: v })} />
            <TextIn label={t('crm.longitude')} type="number" step="any" value={form.longitude} onChange={(v) => patch({ longitude: v })} />
            <div className="flex items-end">
              <button type="button" className="btn-secondary w-full" onClick={locate}>
                <MapPin className="h-4 w-4" />
                {t('crm.useMyLocation')}
              </button>
            </div>
          </Group>

          <Group title={t('crm.contactInfo')}>
            <TextIn label={t('crm.phone')} value={form.phone} onChange={(v) => patch({ phone: v })} />
            <TextIn label={t('crm.mobile')} value={form.mobile} onChange={(v) => patch({ mobile: v })} />
            <TextIn label={t('crm.email')} type="email" value={form.email} onChange={(v) => patch({ email: v })} />
            <TextIn label={t('crm.contactPerson')} value={form.contact_person} onChange={(v) => patch({ contact_person: v })} />
          </Group>

          <Group title={t('crm.visitPlan')}>
            <SelectIn
              label={t('crm.visitFrequency')}
              value={form.visit_frequency}
              onChange={(v) => patch({ visit_frequency: v })}
              options={FREQUENCIES.map((o) => ({ value: o, label: t(`crm.frequencies.${o}`) }))}
            />
            <div className="sm:col-span-2">
              <Field label={t('crm.visitDays')}>
                <div className="flex flex-wrap gap-1.5">
                  {WEEKDAYS.map((d) => {
                    const on = form.visit_days.includes(d)
                    return (
                      <button
                        key={d}
                        type="button"
                        className={on ? 'btn-primary btn-sm' : 'btn-secondary btn-sm'}
                        onClick={() =>
                          patch({
                            visit_days: on
                              ? form.visit_days.filter((x) => x !== d)
                              : [...form.visit_days, d],
                          })
                        }
                      >
                        {t(`crm.weekdays.${d}`)}
                      </button>
                    )
                  })}
                </div>
              </Field>
            </div>
            <TextIn label={t('crm.visitSequence')} type="number" value={form.visit_sequence} onChange={(v) => patch({ visit_sequence: v })} />
            <TextIn label={t('crm.serviceMinutes')} type="number" value={form.service_time_minutes} onChange={(v) => patch({ service_time_minutes: v })} />
            <TextIn label={t('crm.openingTime')} type="time" value={form.opening_time} onChange={(v) => patch({ opening_time: v })} />
            <TextIn label={t('crm.closingTime')} type="time" value={form.closing_time} onChange={(v) => patch({ closing_time: v })} />
            {salespersons.data ? (
              <SelectIn
                label={t('crm.salesperson')}
                value={form.default_salesperson_id}
                onChange={(v) => patch({ default_salesperson_id: v })}
                options={opt(salespersons.data.items)}
                blank={t('common.select')}
              />
            ) : (
              <TextIn label={t('crm.salesperson')} type="number" value={form.default_salesperson_id} onChange={(v) => patch({ default_salesperson_id: v })} />
            )}
            {routes.data ? (
              <SelectIn
                label={t('crm.route')}
                value={form.default_route_id}
                onChange={(v) => patch({ default_route_id: v })}
                options={opt(routes.data.items)}
                blank={t('common.select')}
              />
            ) : (
              <TextIn label={t('crm.route')} type="number" value={form.default_route_id} onChange={(v) => patch({ default_route_id: v })} />
            )}
          </Group>

          <Group title={t('crm.commercialTerms')}>
            {priceLists.data ? (
              <SelectIn
                label={t('crm.priceList')}
                value={form.price_list_id}
                onChange={(v) => patch({ price_list_id: v })}
                options={opt(priceLists.data)}
                blank={t('common.select')}
              />
            ) : (
              <TextIn label={t('crm.priceList')} type="number" value={form.price_list_id} onChange={(v) => patch({ price_list_id: v })} />
            )}
            <SelectIn
              label={t('crm.paymentMethod')}
              value={form.payment_method}
              onChange={(v) => patch({ payment_method: v })}
              options={METHODS.map((o) => ({ value: o, label: t(`crm.paymentMethods.${o}`) }))}
            />
            <TextIn label={t('crm.paymentTermDays')} type="number" value={form.payment_term_days} onChange={(v) => patch({ payment_term_days: v })} />
            {can('crm.credit_limit', 'UPDATE') && (
              <TextIn label={t('crm.creditLimit')} type="number" step="0.01" value={form.credit_limit} onChange={(v) => patch({ credit_limit: v })} />
            )}
            <TextIn label={t('crm.riskLimit')} type="number" step="0.01" value={form.risk_limit} onChange={(v) => patch({ risk_limit: v })} />
            <TextIn label={t('crm.discountPercent')} type="number" step="0.01" value={form.discount_percent} onChange={(v) => patch({ discount_percent: v })} />
            <TextIn label={t('crm.currency')} value={form.currency} onChange={(v) => patch({ currency: v })} />
            <div className="sm:col-span-2 lg:col-span-3">
              <Field label={t('common.notes')}>
                <textarea
                  className="input"
                  rows={2}
                  value={form.notes}
                  onChange={(e) => patch({ notes: e.target.value })}
                />
              </Field>
            </div>
          </Group>

          {editing && (
            <p className="text-2xs text-shell-400">
              {t('crm.orderCount')}: {formatNumber(editing.order_count)} ·{' '}
              {t('crm.totalSales')}: {formatMoney(editing.total_sales_amount)}
            </p>
          )}
        </div>
      </Modal>
    </>
  )
}
