/**
 * Sistem Sağlığı / System Health.
 *
 * One probe per component (backend, database, redis, the three AI providers,
 * disk, backup, queue) with its state, latency and message, plus the version
 * and record counts from /system/info so a support call can start with facts.
 */
import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  CheckCircle2,
  HeartPulse,
  HelpCircle,
  RefreshCw,
  XCircle,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'

import {
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
  PageHeader,
  Spinner,
} from '@/components/ui'
import { api } from '@/lib/api'
import { formatDate, formatNumber } from '@/lib/format'
import { currentLanguage } from '@/lib/i18n'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface HealthComponent {
  component: string
  label_tr?: string | null
  label_en?: string | null
  state: string
  message: string
  latency_ms?: number | null
  details: Record<string, unknown>
  checked_at?: string | null
}

interface HealthSummary {
  state: string
  checked_at: string
  app_version: string
  environment: string
  components: HealthComponent[]
}

interface SystemInfo {
  app_name: string
  app_version: string
  environment: string
  default_language: string
  default_currency: string
  timezone: string
  database_engine: string
  api_prefix: string
  counts: Record<string, number>
  modules: string[]
  ai_providers: {
    provider: string
    enabled: boolean
    configured: boolean
    healthy: boolean
    model?: string | null
  }[]
}

const STATE_STYLE: Record<string, { badge: string; ring: string }> = {
  OK: { badge: 'badge-ok', ring: 'border-ok-200' },
  WARNING: { badge: 'badge-warn', ring: 'border-warn-200' },
  ERROR: { badge: 'badge-danger', ring: 'border-danger-200' },
  UNKNOWN: { badge: 'badge-muted', ring: 'border-shell-200' },
}

function StateIcon({ state }: { state: string }) {
  if (state === 'OK') return <CheckCircle2 className="h-5 w-5 text-ok-600" />
  if (state === 'WARNING') return <AlertTriangle className="h-5 w-5 text-warn-600" />
  if (state === 'ERROR') return <XCircle className="h-5 w-5 text-danger-600" />
  return <HelpCircle className="h-5 w-5 text-shell-400" />
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function Health() {
  const { t } = useTranslation()
  const lang = currentLanguage()

  const healthQuery = useQuery({
    queryKey: ['system', 'health'],
    queryFn: () => api.get<HealthSummary>('/system/health', { refresh: true }),
    refetchInterval: 120_000,
  })

  const infoQuery = useQuery({
    queryKey: ['system', 'info'],
    queryFn: () => api.get<SystemInfo>('/system/info'),
  })

  const health = healthQuery.data
  const info = infoQuery.data

  return (
    <div>
      <PageHeader
        title={t('sysHealth.title')}
        subtitle={t('sysHealth.subtitle')}
        icon={<HeartPulse className="h-5 w-5" />}
        actions={
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={() => void healthQuery.refetch()}
            disabled={healthQuery.isFetching}
          >
            {healthQuery.isFetching ? <Spinner /> : <RefreshCw className="h-4 w-4" />}
            {t('common.refresh')}
          </button>
        }
      />

      {healthQuery.isLoading ? (
        <LoadingBlock />
      ) : healthQuery.isError ? (
        <ErrorState error={healthQuery.error} onRetry={() => void healthQuery.refetch()} />
      ) : (
        <>
          <div className="card mb-4 flex flex-wrap items-center justify-between gap-3 p-4">
            <div className="flex items-center gap-3">
              <StateIcon state={health?.state ?? 'UNKNOWN'} />
              <div>
                <p className="text-xs uppercase tracking-wide text-shell-500">
                  {t('sysHealth.overall')}
                </p>
                <p className="text-lg font-semibold text-shell-900">{health?.state ?? '—'}</p>
              </div>
            </div>
            <dl className="flex flex-wrap gap-6 text-xs">
              <div>
                <dt className="uppercase tracking-wide text-shell-400">{t('sysHealth.version')}</dt>
                <dd className="tabular font-medium text-shell-800">{health?.app_version ?? '—'}</dd>
              </div>
              <div>
                <dt className="uppercase tracking-wide text-shell-400">
                  {t('sysHealth.environment')}
                </dt>
                <dd className="font-medium text-shell-800">{health?.environment ?? '—'}</dd>
              </div>
              <div>
                <dt className="uppercase tracking-wide text-shell-400">
                  {t('sysHealth.checkedAt')}
                </dt>
                <dd className="tabular font-medium text-shell-800">
                  {formatDate(health?.checked_at, { withTime: true })}
                </dd>
              </div>
            </dl>
          </div>

          {(health?.components.length ?? 0) === 0 ? (
            <EmptyState title={t('sysHealth.noComponents')} />
          ) : (
            <div className="mb-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {health?.components.map((component) => {
                const style = STATE_STYLE[component.state] ?? STATE_STYLE.UNKNOWN
                const label =
                  (lang === 'en' ? component.label_en : component.label_tr) || component.component
                return (
                  <div key={component.component} className={`card border ${style.ring} p-4`}>
                    <div className="mb-2 flex items-start justify-between gap-2">
                      <div className="flex items-center gap-2">
                        <StateIcon state={component.state} />
                        <p className="text-sm font-semibold text-shell-800">{label}</p>
                      </div>
                      <span className={style.badge}>{component.state}</span>
                    </div>
                    <p className="text-xs text-shell-600">{component.message || '—'}</p>
                    <p className="tabular mt-2 text-2xs text-shell-400">
                      {component.latency_ms === null || component.latency_ms === undefined
                        ? '—'
                        : `${t('sysHealth.latency')}: ${formatNumber(component.latency_ms)} ms`}
                      {component.checked_at
                        ? ` · ${formatDate(component.checked_at, { short: true, withTime: true })}`
                        : ''}
                    </p>
                    {Object.keys(component.details ?? {}).length > 0 && (
                      <details className="mt-2">
                        <summary className="cursor-pointer text-2xs text-shell-500">
                          {t('common.details')}
                        </summary>
                        <dl className="mt-1 space-y-0.5">
                          {Object.entries(component.details).map(([key, value]) => (
                            <div key={key} className="flex justify-between gap-2 text-2xs">
                              <dt className="text-shell-400">{key}</dt>
                              <dd className="tabular break-all text-shell-600">
                                {typeof value === 'object' ? JSON.stringify(value) : String(value)}
                              </dd>
                            </div>
                          ))}
                        </dl>
                      </details>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title={t('sysHealth.recordCounts')}>
          {infoQuery.isLoading ? (
            <LoadingBlock />
          ) : infoQuery.isError ? (
            <EmptyState title={t('sysHealth.infoUnavailable')} />
          ) : (
            <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              {Object.entries(info?.counts ?? {}).map(([key, value]) => (
                <div key={key} className="rounded-lg border border-shell-200 p-3">
                  <dt className="truncate text-2xs uppercase tracking-wide text-shell-400">
                    {key}
                  </dt>
                  <dd className="tabular text-lg font-semibold text-shell-900">
                    {formatNumber(value)}
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </Card>

        <Card title={t('common.details')}>
          {infoQuery.isLoading ? (
            <LoadingBlock />
          ) : infoQuery.isError || !info ? (
            <EmptyState title={t('sysHealth.infoUnavailable')} />
          ) : (
            <div className="space-y-4">
              <dl className="grid grid-cols-2 gap-3 text-xs">
                {[
                  [t('common.name'), info.app_name],
                  [t('sysHealth.version'), info.app_version],
                  [t('sysHealth.environment'), info.environment],
                  [t('sysHealth.database'), info.database_engine],
                  [t('sysHealth.timezone'), info.timezone],
                  [t('sysSettings.language'), info.default_language],
                ].map(([label, value]) => (
                  <div key={label}>
                    <dt className="uppercase tracking-wide text-shell-400">{label}</dt>
                    <dd className="font-medium text-shell-800">{value}</dd>
                  </div>
                ))}
              </dl>
              <div>
                <p className="mb-1.5 text-2xs uppercase tracking-wide text-shell-400">
                  {t('sysHealth.modules')}
                </p>
                <div className="flex flex-wrap gap-1">
                  {info.modules.map((module) => (
                    <span key={module} className="badge-muted">
                      {module}
                    </span>
                  ))}
                </div>
              </div>
              <div>
                <p className="mb-1.5 text-2xs uppercase tracking-wide text-shell-400">
                  {t('nav.aiProviders')}
                </p>
                <div className="flex flex-wrap gap-1">
                  {info.ai_providers.map((provider) => (
                    <span
                      key={provider.provider}
                      className={provider.healthy && provider.enabled ? 'badge-ok' : 'badge-muted'}
                    >
                      {provider.provider}
                      {provider.model ? ` · ${provider.model}` : ''}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}
