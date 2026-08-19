/**
 * AI Satış Müdürü / AI Sales Manager — the natural-language copilot.
 *
 * One question goes to POST /ai/ask, which routes it to the right agent, runs
 * real (read-only) queries and comes back with a narrative plus the data that
 * justifies it.  The screen never hides that provenance: the SQL that ran, the
 * row count and the rows themselves are one click away, and the model's own
 * reasoning is behind a second toggle.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Bot,
  Database,
  MessageSquarePlus,
  Send,
  Sparkles,
  Trash2,
  UserRound,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { EmptyState, ErrorState, LoadingBlock, PageHeader, Spinner, useToast } from '@/components/ui'
import { api, type Paged } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatNumber } from '@/lib/format'
import { currentLanguage } from '@/lib/i18n'

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */
interface DataContext {
  sql?: string | null
  columns?: string[]
  rows?: Record<string, unknown>[]
  row_count?: number
  truncated?: boolean
  [key: string]: unknown
}

interface ChatMessage {
  id: number
  conversation_id: number
  role: string
  content: string
  reasoning?: string | null
  provider?: string | null
  model?: string | null
  data_context?: DataContext | null
  input_tokens: number
  output_tokens: number
  latency_ms: number
  created_at?: string | null
}

interface Conversation {
  id: number
  title: string
  agent_kind: string
  language: string
  is_archived: boolean
  message_count: number
  total_tokens: number
  total_cost: number | string
  created_at?: string | null
  updated_at?: string | null
  messages: ChatMessage[]
}

interface AskResult {
  conversation_id: number
  message_id?: number | null
  agent_kind: string
  answer: string
  reasoning?: string | null
  provider?: string | null
  model?: string | null
  data_context: DataContext
  confidence: number
  input_tokens: number
  output_tokens: number
  latency_ms: number
  error_key?: string | null
  degraded: boolean
}

const EXAMPLE_KEYS = [
  'aiManager.example1',
  'aiManager.example2',
  'aiManager.example3',
  'aiManager.example4',
  'aiManager.example5',
  'aiManager.example6',
] as const

/* -------------------------------------------------------------------------- */
/* Source-data block                                                          */
/* -------------------------------------------------------------------------- */
function cellText(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function SourceData({ context }: { context: DataContext }) {
  const { t } = useTranslation()
  const rows = Array.isArray(context.rows) ? context.rows : []
  const columns = useMemo(() => {
    if (Array.isArray(context.columns) && context.columns.length) return context.columns
    return rows.length ? Object.keys(rows[0]) : []
  }, [context.columns, rows])

  const count = typeof context.row_count === 'number' ? context.row_count : rows.length
  const hasTable = columns.length > 0 && rows.length > 0
  const hasSql = Boolean(context.sql)
  if (!hasTable && !hasSql) return null

  return (
    <details className="mt-3 rounded-lg border border-shell-200 bg-shell-50/60">
      <summary className="flex cursor-pointer select-none items-center gap-2 px-3 py-2 text-xs font-medium text-shell-600">
        <Database className="h-3.5 w-3.5" />
        {t('aiManager.sourceData')}
        <span className="tabular text-2xs text-shell-400">
          {t('aiManager.sourceRows', { count })}
        </span>
      </summary>
      <div className="space-y-3 border-t border-shell-200 px-3 py-3">
        {hasSql && (
          <div>
            <p className="mb-1 text-2xs font-semibold uppercase tracking-wide text-shell-500">
              {t('aiManager.sql')}
            </p>
            <pre className="table-wrap max-h-48 overflow-auto rounded-md bg-shell-900 p-3 text-2xs leading-relaxed text-shell-100">
              {String(context.sql)}
            </pre>
          </div>
        )}
        {hasTable && (
          <div className="table-wrap max-h-72 overflow-auto rounded-md border border-shell-200 bg-white">
            <table className="table">
              <thead>
                <tr>
                  {columns.map((c) => (
                    <th key={c}>{c}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, 100).map((row, i) => (
                  <tr key={i}>
                    {columns.map((c) => (
                      <td key={c} className="tabular whitespace-nowrap">
                        {cellText(row[c])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </details>
  )
}

function Reasoning({ text }: { text: string }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  return (
    <div className="mt-2">
      <button type="button" className="btn-ghost btn-sm" onClick={() => setOpen((v) => !v)}>
        <Sparkles className="h-3.5 w-3.5" />
        {open ? t('aiManager.hideReasoning') : t('aiManager.showReasoning')}
      </button>
      {open && (
        <p className="mt-2 whitespace-pre-wrap rounded-lg border border-info-200 bg-info-50 p-3 text-xs leading-relaxed text-info-700">
          {text}
        </p>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function AIManager() {
  const { t } = useTranslation()
  const { can } = useAuth()
  const toast = useToast()
  const queryClient = useQueryClient()

  const [activeId, setActiveId] = useState<number | null>(null)
  const [question, setQuestion] = useState('')
  const [pending, setPending] = useState<string | null>(null)
  const endRef = useRef<HTMLDivElement>(null)

  const canAsk = can('ai.copilot', 'EXECUTE')

  const listQuery = useQuery({
    queryKey: ['ai', 'conversations'],
    queryFn: () => api.get<Paged<Conversation>>('/ai/conversations', { page: 1, size: 50 }),
  })

  const detailQuery = useQuery({
    queryKey: ['ai', 'conversation', activeId],
    queryFn: () => api.get<Conversation>(`/ai/conversations/${activeId}`),
    enabled: activeId !== null,
  })

  const ask = useMutation({
    mutationFn: (text: string) =>
      api.post<AskResult>('/ai/ask', {
        question: text,
        conversation_id: activeId ?? undefined,
        language: currentLanguage(),
      }),
    onSuccess: async (result) => {
      setPending(null)
      setActiveId(result.conversation_id)
      await queryClient.invalidateQueries({ queryKey: ['ai', 'conversations'] })
      await queryClient.invalidateQueries({
        queryKey: ['ai', 'conversation', result.conversation_id],
      })
      if (result.degraded) toast.push('warning', t('aiManager.degraded'))
    },
    onError: (error: unknown) => {
      setPending(null)
      toast.push('error', error instanceof Error ? error.message : t('errors.generic'))
    },
  })

  const remove = useMutation({
    mutationFn: (id: number) => api.delete<{ success: boolean }>(`/ai/conversations/${id}`),
    onSuccess: async (_data, id) => {
      if (activeId === id) setActiveId(null)
      await queryClient.invalidateQueries({ queryKey: ['ai', 'conversations'] })
      toast.push('success', t('aiManager.deleted'))
    },
    onError: (error: unknown) =>
      toast.push('error', error instanceof Error ? error.message : t('errors.generic')),
  })

  const messages = detailQuery.data?.messages ?? []

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages.length, pending])

  const submit = () => {
    const text = question.trim()
    if (!text || ask.isPending || !canAsk) return
    setPending(text)
    setQuestion('')
    ask.mutate(text)
  }

  const conversations = listQuery.data?.items ?? []

  return (
    <div>
      <PageHeader
        title={t('aiManager.title')}
        subtitle={t('aiManager.subtitle')}
        icon={<Bot className="h-5 w-5" />}
        actions={
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={() => {
              setActiveId(null)
              setPending(null)
            }}
          >
            <MessageSquarePlus className="h-4 w-4" />
            {t('aiManager.newConversation')}
          </button>
        }
      />

      <div className="grid gap-4 lg:grid-cols-[260px_minmax(0,1fr)]">
        {/* Conversation rail */}
        <aside className="card flex max-h-[70vh] flex-col overflow-hidden">
          <header className="card-header">
            <h2 className="card-title">{t('aiManager.conversations')}</h2>
          </header>
          <div className="flex-1 overflow-y-auto p-2">
            {listQuery.isLoading ? (
              <LoadingBlock />
            ) : listQuery.isError ? (
              <ErrorState error={listQuery.error} onRetry={() => void listQuery.refetch()} />
            ) : conversations.length === 0 ? (
              <p className="px-2 py-6 text-center text-xs text-shell-400">
                {t('aiManager.noConversations')}
              </p>
            ) : (
              <ul className="space-y-1">
                {conversations.map((c) => (
                  <li key={c.id} className="group flex items-center gap-1">
                    <button
                      type="button"
                      onClick={() => {
                        setActiveId(c.id)
                        setPending(null)
                      }}
                      className={`flex-1 truncate rounded-lg px-2.5 py-2 text-left text-xs ${
                        activeId === c.id
                          ? 'bg-brand-50 font-medium text-brand-700'
                          : 'text-shell-600 hover:bg-shell-100'
                      }`}
                      title={c.title}
                    >
                      <span className="block truncate">{c.title || `#${c.id}`}</span>
                      <span className="tabular text-2xs text-shell-400">
                        {formatDate(c.updated_at, { short: true, withTime: true })}
                      </span>
                    </button>
                    {canAsk && (
                      <button
                        type="button"
                        className="btn-ghost btn-sm opacity-0 transition-opacity group-hover:opacity-100"
                        aria-label={t('aiManager.deleteConversation')}
                        onClick={() => {
                          if (window.confirm(t('aiManager.deleteConfirm'))) remove.mutate(c.id)
                        }}
                      >
                        <Trash2 className="h-3.5 w-3.5 text-danger-500" />
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </aside>

        {/* Chat */}
        <section className="card flex max-h-[70vh] min-h-[520px] flex-col overflow-hidden">
          <div className="flex-1 space-y-4 overflow-y-auto p-4">
            {activeId !== null && detailQuery.isLoading ? (
              <LoadingBlock />
            ) : activeId !== null && detailQuery.isError ? (
              <ErrorState error={detailQuery.error} onRetry={() => void detailQuery.refetch()} />
            ) : messages.length === 0 && !pending ? (
              <EmptyState
                title={t('aiManager.emptyChat')}
                description={t('ai.askPlaceholder')}
                icon={<Sparkles className="h-6 w-6" />}
              />
            ) : (
              messages.map((m) =>
                m.role === 'user' ? (
                  <div key={m.id} className="flex justify-end gap-2">
                    <div className="max-w-[80%] rounded-xl rounded-br-sm bg-brand-600 px-3.5 py-2.5 text-sm text-white">
                      <p className="whitespace-pre-wrap">{m.content}</p>
                    </div>
                    <div className="mt-0.5 rounded-full bg-brand-100 p-1.5 text-brand-700">
                      <UserRound className="h-3.5 w-3.5" />
                    </div>
                  </div>
                ) : (
                  <div key={m.id} className="flex gap-2">
                    <div className="mt-0.5 rounded-full bg-shell-100 p-1.5 text-shell-600">
                      <Bot className="h-3.5 w-3.5" />
                    </div>
                    <div className="max-w-[85%] flex-1">
                      <div className="rounded-xl rounded-tl-sm border border-shell-200 bg-white px-3.5 py-2.5">
                        {m.content ? (
                          <p className="whitespace-pre-wrap text-sm leading-relaxed text-shell-700">
                            {m.content}
                          </p>
                        ) : (
                          <p className="flex items-center gap-2 text-sm text-warn-700">
                            <AlertTriangle className="h-4 w-4" />
                            {t('aiManager.degraded')}
                          </p>
                        )}
                        {m.data_context && <SourceData context={m.data_context} />}
                        {m.reasoning && <Reasoning text={m.reasoning} />}
                      </div>
                      <p className="tabular mt-1 px-1 text-2xs text-shell-400">
                        {[m.provider, m.model].filter(Boolean).join(' · ')}
                        {m.provider ? ' — ' : ''}
                        {t('aiManager.tokenLine', {
                          input: formatNumber(m.input_tokens),
                          output: formatNumber(m.output_tokens),
                          latency: formatNumber(m.latency_ms),
                        })}
                      </p>
                    </div>
                  </div>
                ),
              )
            )}

            {pending && (
              <>
                <div className="flex justify-end gap-2">
                  <div className="max-w-[80%] rounded-xl rounded-br-sm bg-brand-600 px-3.5 py-2.5 text-sm text-white">
                    <p className="whitespace-pre-wrap">{pending}</p>
                  </div>
                  <div className="mt-0.5 rounded-full bg-brand-100 p-1.5 text-brand-700">
                    <UserRound className="h-3.5 w-3.5" />
                  </div>
                </div>
                <div className="flex items-center gap-2 text-sm text-shell-400">
                  <div className="rounded-full bg-shell-100 p-1.5 text-shell-600">
                    <Bot className="h-3.5 w-3.5" />
                  </div>
                  <Spinner />
                  {t('ai.thinking')}
                </div>
              </>
            )}
            <div ref={endRef} />
          </div>

          {/* Examples + composer */}
          <div className="border-t border-shell-200 p-3">
            <div className="mb-2 flex flex-wrap gap-1.5">
              {EXAMPLE_KEYS.map((key) => (
                <button
                  key={key}
                  type="button"
                  className="rounded-full border border-shell-200 bg-shell-50 px-2.5 py-1 text-2xs text-shell-600 hover:border-brand-300 hover:text-brand-700"
                  onClick={() => setQuestion(t(key))}
                >
                  {t(key)}
                </button>
              ))}
            </div>
            <div className="flex items-end gap-2">
              <textarea
                className="input min-h-[44px] resize-y"
                rows={2}
                value={question}
                disabled={!canAsk}
                placeholder={t('ai.askPlaceholder')}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    submit()
                  }
                }}
              />
              <button
                type="button"
                className="btn-primary"
                onClick={submit}
                disabled={!canAsk || ask.isPending || !question.trim()}
              >
                {ask.isPending ? <Spinner /> : <Send className="h-4 w-4" />}
                <span className="hidden sm:inline">{t('aiManager.send')}</span>
              </button>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}
