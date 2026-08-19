/**
 * Kampanyalar / Campaigns.
 *
 * The create form is type-driven: choosing a campaign type shows only the
 * fields that type actually uses, because a "buy 10 get 1" and a "3% over
 * ₺20,000" have almost nothing in common beyond a name and a date window.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BadgePercent, Pause, Play, Plus, TrendingUp } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, ApiError, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { currentLanguage } from '@/lib/i18n'
import { formatDate, formatMoney, formatPercent } from '@/lib/format'
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
  Spinner,
  StatusBadge,
  useToast,
} from '@/components/ui'

interface Campaign {
  id: number
  code: string
  name: string
  name_en?: string | null
  campaign_type: string
  status: string
  start_date: string
  end_date: string
  scope: string
  discount_percent: number
  discount_amount: number | string
  free_quantity: number | string
  free_product_id?: number | null
  priority: number
  application_count: number
  total_discount_given: number | string
}

interface Profitability {
  campaign_id?: number
  applications?: number
  discount_given?: number | string
  free_goods_cost?: number | string
  revenue?: number | string
  incremental_margin?: number | string
  roi_percent?: number
}

const TYPES = [
  'BUY_X_GET_Y',
  'QUANTITY_DISCOUNT',
  'VALUE_DISCOUNT',
  'BASKET_MIX',
  'FIXED_PRICE',
  'PERCENT_DISCOUNT',
  'AMOUNT_DISCOUNT',
  'FREE_GOODS',
] as const
type CampaignType = (typeof TYPES)[number]

const SCOPES = [
  'ALL', 'CUSTOMER', 'CUSTOMER_TYPE', 'CHANNEL', 'REGION',
  'ROUTE', 'SALESPERSON', 'PRODUCT', 'CATEGORY', 'BRAND',
] as const

/** Which extra inputs each campaign type needs. */
const FIELDS: Record<CampaignType, { threshold?: string; percent?: boolean; amount?: boolean; free?: boolean }> = {
  BUY_X_GET_Y: { threshold: 'buyQuantity', free: true },
  QUANTITY_DISCOUNT: { threshold: 'minQuantity', percent: true },
  VALUE_DISCOUNT: { threshold: 'minAmount', percent: true },
  BASKET_MIX: { threshold: 'distinctProducts', percent: true },
  FIXED_PRICE: { amount: true },
  PERCENT_DISCOUNT: { percent: true },
  AMOUNT_DISCOUNT: { amount: true },
  FREE_GOODS: { free: true },
}

const today = () => new Date().toISOString().slice(0, 10)
const plusDays = (n: number) =>
  new Date(Date.now() + n * 864e5).toISOString().slice(0, 10)

export default function Campaigns() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const { push } = useToast()
  const qc = useQueryClient()
  const lang = currentLanguage()

  const [page, setPage] = useState(1)
  const [status, setStatus] = useState('')
  const [open, setOpen] = useState(false)
  const [roiFor, setRoiFor] = useState<Campaign | null>(null)

  const [form, setForm] = useState({
    code: '',
    name: '',
    name_en: '',
    campaign_type: 'QUANTITY_DISCOUNT' as CampaignType,
    scope: 'ALL' as (typeof SCOPES)[number],
    scope_values: '',
    start_date: today(),
    end_date: plusDays(90),
    threshold: '5',
    discount_percent: '5',
    discount_amount: '0',
    free_product_id: '',
    free_quantity: '1',
    priority: '100',
  })

  const list = useQuery({
    queryKey: ['campaigns', page, status],
    queryFn: () =>
      api.get<Paged<Campaign>>('/campaigns', { page, size: 25, status: status || undefined }),
  })

  const roi = useQuery({
    queryKey: ['campaign-roi', roiFor?.id],
    queryFn: () => api.get<Profitability>(`/campaigns/${roiFor!.id}/profitability`),
    enabled: !!roiFor,
    retry: false,
    throwOnError: false,
  })

  const create = useMutation({
    mutationFn: () => {
      const spec = FIELDS[form.campaign_type]
      return api.post('/campaigns', {
        code: form.code.trim(),
        name: form.name.trim(),
        name_en: form.name_en.trim() || null,
        campaign_type: form.campaign_type,
        scope: form.scope,
        scope_values: form.scope_values.trim() || null,
        start_date: form.start_date,
        end_date: form.end_date,
        discount_percent: spec.percent ? Number(form.discount_percent) : 0,
        discount_amount: spec.amount ? Number(form.discount_amount) : 0,
        free_product_id: spec.free && form.free_product_id ? Number(form.free_product_id) : null,
        free_quantity: spec.free ? Number(form.free_quantity) : 0,
        priority: Number(form.priority),
        conditions: spec.threshold
          ? [
              {
                subject: 'ORDER',
                metric:
                  form.campaign_type === 'VALUE_DISCOUNT'
                    ? 'AMOUNT'
                    : form.campaign_type === 'BASKET_MIX'
                      ? 'DISTINCT_PRODUCTS'
                      : 'QUANTITY',
                min_value: Number(form.threshold),
                step_value:
                  form.campaign_type === 'BUY_X_GET_Y' ? Number(form.threshold) : null,
              },
            ]
          : [],
      })
    },
    onSuccess: () => {
      push('success', t('common.created'))
      setOpen(false)
      void qc.invalidateQueries({ queryKey: ['campaigns'] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const toggle = useMutation({
    mutationFn: ({ id, action }: { id: number; action: 'activate' | 'pause' }) =>
      api.post(`/campaigns/${id}/${action}`),
    onSuccess: () => {
      push('success', t('common.updated'))
      void qc.invalidateQueries({ queryKey: ['campaigns'] })
    },
    onError: (e) => push('error', e instanceof ApiError ? e.message : t('errors.generic')),
  })

  const spec = FIELDS[form.campaign_type]
  const data = list.data

  return (
    <>
      <PageHeader
        title={t('nav.campaigns')}
        subtitle={t('nav.marketing')}
        icon={<BadgePercent className="h-5 w-5" />}
        actions={
          can('marketing.campaigns', 'CREATE') && (
            <button type="button" className="btn-primary btn-sm" onClick={() => setOpen(true)}>
              <Plus className="h-3.5 w-3.5" />
              {t('common.new')}
            </button>
          )
        }
      />

      <Card bodyClassName="p-0">
        <div className="flex flex-wrap items-center gap-2 border-b border-shell-200 p-3">
          {['', 'ACTIVE', 'DRAFT', 'PAUSED', 'EXPIRED'].map((s) => (
            <button
              key={s || 'all'}
              type="button"
              onClick={() => {
                setStatus(s)
                setPage(1)
              }}
              className={status === s ? 'btn-primary btn-sm' : 'btn-secondary btn-sm'}
            >
              {s || t('common.all')}
            </button>
          ))}
        </div>

        {list.isLoading ? (
          <LoadingBlock />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : (data?.items ?? []).length === 0 ? (
          <EmptyState />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('common.code')}</th>
                    <th>{t('common.name')}</th>
                    <th>{t('common.details')}</th>
                    <th>{t('common.dateRange')}</th>
                    <th>{t('common.status')}</th>
                    <th className="text-right">{t('common.discount')}</th>
                    <th className="text-right">#</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {(data?.items ?? []).map((c) => (
                    <tr key={c.id}>
                      <td className="font-mono text-xs">{c.code}</td>
                      <td className="max-w-xs truncate">
                        {lang === 'en' && c.name_en ? c.name_en : c.name}
                      </td>
                      <td>
                        <span className="badge-muted">{c.campaign_type}</span>
                      </td>
                      <td className="whitespace-nowrap text-xs">
                        {formatDate(c.start_date, { short: true })} –{' '}
                        {formatDate(c.end_date, { short: true })}
                      </td>
                      <td>
                        <StatusBadge status={c.status} />
                      </td>
                      <td className="tabular text-right">
                        {c.discount_percent
                          ? formatPercent(c.discount_percent)
                          : formatMoney(c.total_discount_given)}
                      </td>
                      <td className="tabular text-right">{c.application_count}</td>
                      <td className="whitespace-nowrap text-right">
                        <button
                          type="button"
                          className="btn-ghost btn-sm"
                          onClick={() => setRoiFor(c)}
                          title={t('nav.reports')}
                        >
                          <TrendingUp className="h-3.5 w-3.5" />
                        </button>
                        {can('marketing.campaigns', 'UPDATE') && (
                          <button
                            type="button"
                            className="btn-ghost btn-sm"
                            onClick={() =>
                              toggle.mutate({
                                id: c.id,
                                action: c.status === 'ACTIVE' ? 'pause' : 'activate',
                              })
                            }
                          >
                            {c.status === 'ACTIVE' ? (
                              <Pause className="h-3.5 w-3.5" />
                            ) : (
                              <Play className="h-3.5 w-3.5" />
                            )}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination
              page={data?.page ?? 1}
              pages={data?.pages ?? 1}
              total={data?.total ?? 0}
              size={data?.size ?? 25}
              onPage={setPage}
            />
          </>
        )}
      </Card>

      {/* --- create --- */}
      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title={t('common.new')}
        size="lg"
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setOpen(false)}>
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={create.isPending || !form.code || !form.name}
              onClick={() => create.mutate()}
            >
              {create.isPending && <Spinner />}
              {t('common.save')}
            </button>
          </>
        }
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t('common.code')} required>
            <input
              className="input"
              value={form.code}
              onChange={(e) => setForm({ ...form, code: e.target.value.toUpperCase() })}
            />
          </Field>
          <Field label={t('common.name')} required>
            <input
              className="input"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </Field>

          <Field label="Type / Tip" required>
            <select
              className="input"
              value={form.campaign_type}
              onChange={(e) =>
                setForm({ ...form, campaign_type: e.target.value as CampaignType })
              }
            >
              {TYPES.map((tp) => (
                <option key={tp} value={tp}>
                  {tp}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Scope / Kapsam">
            <select
              className="input"
              value={form.scope}
              onChange={(e) =>
                setForm({ ...form, scope: e.target.value as (typeof SCOPES)[number] })
              }
            >
              {SCOPES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>

          {form.scope !== 'ALL' && (
            <Field
              label="Scope values / Kapsam değerleri"
              hint="12,44,91"
              required
            >
              <input
                className="input"
                value={form.scope_values}
                onChange={(e) => setForm({ ...form, scope_values: e.target.value })}
              />
            </Field>
          )}

          <Field label={t('common.from')} required>
            <input
              type="date"
              className="input"
              value={form.start_date}
              onChange={(e) => setForm({ ...form, start_date: e.target.value })}
            />
          </Field>
          <Field label={t('common.to')} required>
            <input
              type="date"
              className="input"
              value={form.end_date}
              onChange={(e) => setForm({ ...form, end_date: e.target.value })}
            />
          </Field>

          <div className="sm:col-span-2">
            <SectionTitle>{form.campaign_type}</SectionTitle>
          </div>

          {spec.threshold && (
            <Field label={spec.threshold} required>
              <input
                type="number"
                min={1}
                className="input tabular"
                value={form.threshold}
                onChange={(e) => setForm({ ...form, threshold: e.target.value })}
              />
            </Field>
          )}
          {spec.percent && (
            <Field label={`${t('common.discount')} %`} required>
              <input
                type="number"
                min={0}
                max={100}
                step="0.1"
                className="input tabular"
                value={form.discount_percent}
                onChange={(e) => setForm({ ...form, discount_percent: e.target.value })}
              />
            </Field>
          )}
          {spec.amount && (
            <Field label={t('common.amount')} required>
              <input
                type="number"
                min={0}
                step="0.01"
                className="input tabular"
                value={form.discount_amount}
                onChange={(e) => setForm({ ...form, discount_amount: e.target.value })}
              />
            </Field>
          )}
          {spec.free && (
            <>
              <Field label="Free product id / Bedava ürün id" required>
                <input
                  type="number"
                  className="input tabular"
                  value={form.free_product_id}
                  onChange={(e) => setForm({ ...form, free_product_id: e.target.value })}
                />
              </Field>
              <Field label="Free quantity / Bedava miktar" required>
                <input
                  type="number"
                  min={1}
                  className="input tabular"
                  value={form.free_quantity}
                  onChange={(e) => setForm({ ...form, free_quantity: e.target.value })}
                />
              </Field>
            </>
          )}
          <Field label="Priority / Öncelik" hint="0 = highest">
            <input
              type="number"
              className="input tabular"
              value={form.priority}
              onChange={(e) => setForm({ ...form, priority: e.target.value })}
            />
          </Field>
        </div>
      </Modal>

      {/* --- ROI --- */}
      <Modal
        open={!!roiFor}
        onClose={() => setRoiFor(null)}
        title={`${roiFor?.name ?? ''} — ROI`}
      >
        {roi.isLoading ? (
          <LoadingBlock />
        ) : !roi.data ? (
          <EmptyState />
        ) : (
          <dl className="space-y-2 text-sm">
            {[
              ['Applications / Kullanım', roi.data.applications ?? 0],
              ['Discount given / Verilen iskonto', formatMoney(roi.data.discount_given)],
              ['Free goods cost / Bedava mal maliyeti', formatMoney(roi.data.free_goods_cost)],
              ['Revenue / Ciro', formatMoney(roi.data.revenue)],
              ['Margin / Marj', formatMoney(roi.data.incremental_margin)],
              ['ROI', formatPercent(roi.data.roi_percent ?? 0)],
            ].map(([k, v]) => (
              <div
                key={String(k)}
                className="flex justify-between border-b border-shell-100 pb-2"
              >
                <dt className="text-shell-500">{k}</dt>
                <dd className="tabular font-medium">{String(v)}</dd>
              </div>
            ))}
          </dl>
        )}
      </Modal>
    </>
  )
}
