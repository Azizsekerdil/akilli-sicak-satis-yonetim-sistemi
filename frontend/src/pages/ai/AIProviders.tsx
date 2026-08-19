/**
 * AI Sağlayıcıları / AI Providers.
 *
 * One card per provider (LM Studio, NVIDIA, Claude) with its connection
 * settings, task→model routing, failover priority and live health.
 *
 * Credentials: the API returns only a masked hint (e.g. "nvap****6f2a") and a
 * boolean.  The key field is a password input that starts empty — submitting it
 * empty leaves the stored key untouched, because the backend drops unset
 * fields.  A full key is never rendered anywhere on this screen.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Cpu, KeyRound, Plug, Plus, Save, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  Card,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  PageHeader,
  SectionTitle,
  Spinner,
  useToast,
} from '@/components/ui'
import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatNumber, formatPercent } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface ProviderStatus {
  provider: string
  display_name: string
  enabled: boolean
  configured: boolean
  healthy: boolean
  base_url: string
  model?: string | null
  masked_key: string
  latency_ms?: number | null
  avg_latency_ms: number
  error_rate: number
  requests: number
  errors: number
  last_ok_at?: string | null
  last_error_at?: string | null
  last_error?: string | null
  failover_priority: number
  supports_vision: boolean
  supports_embeddings: boolean
  input_cost_per_1k: number | string
  output_cost_per_1k: number | string
  task_model_map: Record<string, string>
}

interface ModelList {
  provider: string
  models: string[]
  default_model?: string | null
  count: number
}

interface TestResult {
  provider: string
  ok: boolean
  latency_ms: number
  models: string[]
  error?: string | null
  message: string
}

interface AiHealth {
  healthy: boolean
  active_provider?: string | null
  providers: ProviderStatus[]
  checked_at?: string | null
}

const TASK_TYPES = [
  'GENERAL',
  'ANALYSIS',
  'VISION',
  'MATH',
  'CODING',
  'REPORTING',
  'LONG_CONTEXT',
  'EMBEDDING',
  'SQL',
] as const

/** Only these keys exist on ProviderUpdateIn — the backend forbids extras. */
interface ProviderPatch {
  is_enabled?: boolean
  base_url?: string
  default_model?: string
  api_key?: string
  timeout_seconds?: number
  max_tokens?: number
  temperature?: number
  failover_priority?: number
  task_model_map?: Record<string, string>
  input_cost_per_1k?: number
  output_cost_per_1k?: number
}

/* -------------------------------------------------------------------------- */
/* One provider card                                                          */
/* -------------------------------------------------------------------------- */
function ProviderCard({
  provider,
  isActive,
  canEdit,
  canTest,
}: {
  provider: ProviderStatus
  isActive: boolean
  canEdit: boolean
  canTest: boolean
}) {
  const { t } = useTranslation()
  const toast = useToast()
  const queryClient = useQueryClient()

  const [enabled, setEnabled] = useState(provider.enabled)
  const [baseUrl, setBaseUrl] = useState(provider.base_url)
  const [model, setModel] = useState(provider.model ?? '')
  const [apiKey, setApiKey] = useState('')
  const [timeout, setTimeoutSeconds] = useState('')
  const [maxTokens, setMaxTokens] = useState('')
  const [temperature, setTemperature] = useState('')
  const [priority, setPriority] = useState(String(provider.failover_priority))
  const [taskMap, setTaskMap] = useState<Record<string, string>>(provider.task_model_map ?? {})
  const [newTask, setNewTask] = useState<string>(TASK_TYPES[0])
  const [test, setTest] = useState<TestResult | null>(null)

  useEffect(() => {
    setEnabled(provider.enabled)
    setBaseUrl(provider.base_url)
    setModel(provider.model ?? '')
    setPriority(String(provider.failover_priority))
    setTaskMap(provider.task_model_map ?? {})
  }, [provider])

  const modelsQuery = useQuery({
    queryKey: ['ai', 'provider-models', provider.provider],
    queryFn: () => api.get<ModelList>(`/ai/providers/${provider.provider}/models`),
    enabled: false,
    retry: false,
  })

  const save = useMutation({
    mutationFn: (patch: ProviderPatch) =>
      api.put<ProviderStatus>(`/ai/providers/${provider.provider}`, patch),
    onSuccess: async () => {
      setApiKey('')
      await queryClient.invalidateQueries({ queryKey: ['ai', 'providers'] })
      await queryClient.invalidateQueries({ queryKey: ['ai', 'health'] })
      toast.push('success', t('aiProviders.saved'))
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  const testConnection = useMutation({
    mutationFn: () => api.post<TestResult>(`/ai/providers/${provider.provider}/test`),
    onSuccess: async (result) => {
      setTest(result)
      await queryClient.invalidateQueries({ queryKey: ['ai', 'providers'] })
      toast.push(result.ok ? 'success' : 'error', result.message)
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  const submit = () => {
    const patch: ProviderPatch = {
      is_enabled: enabled,
      base_url: baseUrl,
      failover_priority: Number(priority) || provider.failover_priority,
      task_model_map: taskMap,
    }
    if (model) patch.default_model = model
    // An empty key means "keep whatever is stored" — never send a blank over it.
    if (apiKey.trim()) patch.api_key = apiKey.trim()
    if (timeout.trim()) patch.timeout_seconds = Number(timeout)
    if (maxTokens.trim()) patch.max_tokens = Number(maxTokens)
    if (temperature.trim()) patch.temperature = Number(temperature)
    save.mutate(patch)
  }

  const models = modelsQuery.data?.models ?? []

  return (
    <Card
      title={provider.display_name || provider.provider}
      actions={
        <div className="flex flex-wrap items-center gap-1.5">
          {isActive && <span className="badge-info">{t('aiProviders.activeProvider')}</span>}
          <span className={provider.configured ? 'badge-ok' : 'badge-muted'}>
            {provider.configured ? t('aiProviders.configured') : t('ai.notConfigured')}
          </span>
          <span className={provider.healthy ? 'badge-ok' : 'badge-danger'}>
            {provider.healthy ? t('aiProviders.healthy') : t('aiProviders.unhealthy')}
          </span>
        </div>
      }
    >
      {/* Live health */}
      <SectionTitle>{t('aiProviders.health')}</SectionTitle>
      <dl className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div>
          <dt className="text-2xs uppercase tracking-wide text-shell-400">{t('ai.latency')}</dt>
          <dd className="tabular text-sm font-medium text-shell-800">
            {provider.latency_ms === null || provider.latency_ms === undefined
              ? '—'
              : `${formatNumber(provider.latency_ms)} ms`}
          </dd>
        </div>
        <div>
          <dt className="text-2xs uppercase tracking-wide text-shell-400">
            {t('aiProviders.errorRate')}
          </dt>
          <dd className="tabular text-sm font-medium text-shell-800">
            {formatPercent(provider.error_rate)}
          </dd>
        </div>
        <div>
          <dt className="text-2xs uppercase tracking-wide text-shell-400">
            {t('aiProviders.requests')}
          </dt>
          <dd className="tabular text-sm font-medium text-shell-800">
            {formatNumber(provider.requests)} / {formatNumber(provider.errors)}
          </dd>
        </div>
        <div>
          <dt className="text-2xs uppercase tracking-wide text-shell-400">
            {t('aiProviders.lastOk')}
          </dt>
          <dd className="tabular text-sm font-medium text-shell-800">
            {formatDate(provider.last_ok_at, { short: true, withTime: true })}
          </dd>
        </div>
      </dl>
      {provider.last_error && (
        <p className="mb-4 rounded-lg border border-danger-200 bg-danger-50 p-2.5 text-2xs text-danger-700">
          <strong>{t('aiProviders.lastError')}:</strong> {provider.last_error}
        </p>
      )}

      {/* Settings */}
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t('aiProviders.baseUrl')}>
          <input
            className="input"
            value={baseUrl}
            disabled={!canEdit}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
        </Field>
        <Field label={t('ai.model')}>
          <div className="flex gap-2">
            <select
              className="input"
              value={model}
              disabled={!canEdit}
              onChange={(e) => setModel(e.target.value)}
            >
              <option value="">{t('common.select')}</option>
              {(models.length ? models : provider.model ? [provider.model] : []).map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn-secondary btn-sm shrink-0"
              onClick={() => void modelsQuery.refetch()}
              disabled={modelsQuery.isFetching}
            >
              {modelsQuery.isFetching ? <Spinner /> : <Cpu className="h-3.5 w-3.5" />}
              {t('aiProviders.loadModels')}
            </button>
          </div>
          {modelsQuery.isError && (
            <p className="mt-1 text-2xs text-danger-600">{t('aiProviders.noModels')}</p>
          )}
        </Field>

        <Field
          label={t('aiProviders.apiKey')}
          hint={provider.masked_key ? `${provider.masked_key} — ${t('aiProviders.apiKeyHint')}` : t('aiProviders.apiKeyHint')}
        >
          <input
            type="password"
            className="input"
            autoComplete="new-password"
            value={apiKey}
            disabled={!canEdit}
            placeholder={provider.masked_key || '—'}
            onChange={(e) => setApiKey(e.target.value)}
          />
        </Field>
        <Field label={t('aiProviders.failoverPriority')}>
          <input
            type="number"
            className="input tabular"
            value={priority}
            disabled={!canEdit}
            onChange={(e) => setPriority(e.target.value)}
          />
        </Field>

        <Field label={t('aiProviders.timeout')} hint={t('aiProviders.apiKeyHint')}>
          <input
            type="number"
            className="input tabular"
            value={timeout}
            disabled={!canEdit}
            placeholder="120"
            onChange={(e) => setTimeoutSeconds(e.target.value)}
          />
        </Field>
        <Field label={t('aiProviders.maxTokens')} hint={t('aiProviders.apiKeyHint')}>
          <input
            type="number"
            className="input tabular"
            value={maxTokens}
            disabled={!canEdit}
            placeholder="2048"
            onChange={(e) => setMaxTokens(e.target.value)}
          />
        </Field>
        <Field label={t('aiProviders.temperature')} hint={t('aiProviders.apiKeyHint')}>
          <input
            type="number"
            step="0.1"
            min="0"
            max="2"
            className="input tabular"
            value={temperature}
            disabled={!canEdit}
            placeholder="0.3"
            onChange={(e) => setTemperature(e.target.value)}
          />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label={t('aiProviders.contextLength')} hint={t('aiProviders.notSupported')}>
            <input className="input" disabled value="" placeholder="—" />
          </Field>
          <Field label={t('aiProviders.streaming')} hint={t('aiProviders.notSupported')}>
            <input className="input" disabled value="" placeholder="—" />
          </Field>
        </div>
      </div>

      <label className="mt-3 flex items-center gap-2 text-sm text-shell-700">
        <input
          type="checkbox"
          checked={enabled}
          disabled={!canEdit}
          onChange={(e) => setEnabled(e.target.checked)}
        />
        {t('aiProviders.enabled')}
      </label>

      {/* Task → model map */}
      <div className="mt-5">
        <SectionTitle>{t('aiProviders.taskModels')}</SectionTitle>
        <div className="space-y-2">
          {Object.entries(taskMap).map(([task, value]) => (
            <div key={task} className="flex items-center gap-2">
              <span className="badge-muted w-36 shrink-0 justify-center">{task}</span>
              <input
                className="input"
                value={value}
                disabled={!canEdit}
                onChange={(e) => setTaskMap((prev) => ({ ...prev, [task]: e.target.value }))}
              />
              {canEdit && (
                <button
                  type="button"
                  className="btn-ghost btn-sm"
                  onClick={() =>
                    setTaskMap((prev) => {
                      const next = { ...prev }
                      delete next[task]
                      return next
                    })
                  }
                >
                  <Trash2 className="h-3.5 w-3.5 text-danger-500" />
                </button>
              )}
            </div>
          ))}
          {canEdit && (
            <div className="flex items-center gap-2">
              <select
                className="input w-36 shrink-0"
                value={newTask}
                onChange={(e) => setNewTask(e.target.value)}
              >
                {TASK_TYPES.map((task) => (
                  <option key={task} value={task}>
                    {task}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn-secondary btn-sm"
                onClick={() =>
                  setTaskMap((prev) => ({ ...prev, [newTask]: prev[newTask] ?? model ?? '' }))
                }
              >
                <Plus className="h-3.5 w-3.5" />
                {t('aiProviders.addMapping')}
              </button>
            </div>
          )}
        </div>
      </div>

      <p className="mt-4 flex items-start gap-2 rounded-lg border border-warn-200 bg-warn-50 p-2.5 text-2xs text-warn-800">
        <KeyRound className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        {t('aiProviders.apiKeyNote')}
      </p>

      {test && (
        <p
          className={`mt-3 rounded-lg border p-2.5 text-2xs ${
            test.ok
              ? 'border-ok-200 bg-ok-50 text-ok-700'
              : 'border-danger-200 bg-danger-50 text-danger-700'
          }`}
        >
          {test.ok
            ? `${t('ai.latency')}: ${formatNumber(test.latency_ms)} ms · ${formatNumber(test.models.length)} model`
            : (test.error ?? test.message)}
        </p>
      )}

      <div className="mt-4 flex flex-wrap gap-2">
        {canEdit && (
          <button type="button" className="btn-primary btn-sm" onClick={submit} disabled={save.isPending}>
            {save.isPending ? <Spinner /> : <Save className="h-3.5 w-3.5" />}
            {t('common.save')}
          </button>
        )}
        {canTest && (
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={() => testConnection.mutate()}
            disabled={testConnection.isPending}
          >
            {testConnection.isPending ? <Spinner /> : <Plug className="h-3.5 w-3.5" />}
            {testConnection.isPending ? t('aiProviders.testing') : t('ai.testConnection')}
          </button>
        )}
      </div>
    </Card>
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function AIProviders() {
  const { t } = useTranslation()
  const { can } = useAuth()

  const providersQuery = useQuery({
    queryKey: ['ai', 'providers'],
    queryFn: () => api.get<ProviderStatus[]>('/ai/providers'),
  })

  const healthQuery = useQuery({
    queryKey: ['ai', 'health'],
    queryFn: () => api.get<AiHealth>('/ai/health'),
    refetchInterval: 60_000,
  })

  const providers = providersQuery.data ?? []
  const activeProvider = healthQuery.data?.active_provider ?? null

  return (
    <div>
      <PageHeader
        title={t('aiProviders.title')}
        subtitle={t('aiProviders.subtitle')}
        icon={<Activity className="h-5 w-5" />}
        actions={
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={() => void providersQuery.refetch()}
          >
            {t('common.refresh')}
          </button>
        }
      />

      {providersQuery.isLoading ? (
        <LoadingBlock />
      ) : providersQuery.isError ? (
        <ErrorState error={providersQuery.error} onRetry={() => void providersQuery.refetch()} />
      ) : providers.length === 0 ? (
        <EmptyState title={t('aiProviders.noProviders')} />
      ) : (
        <div className="grid gap-4 xl:grid-cols-2">
          {providers.map((provider) => (
            <ProviderCard
              key={provider.provider}
              provider={provider}
              isActive={activeProvider === provider.provider}
              canEdit={can('ai.providers', 'UPDATE')}
              canTest={can('ai.providers', 'EXECUTE')}
            />
          ))}
        </div>
      )}
    </div>
  )
}
