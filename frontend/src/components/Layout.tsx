/**
 * Application shell: sidebar, top bar, command palette, notification bell.
 *
 * The sidebar is built from the permission set the backend returned, so a
 * salesperson never even sees the administration section.  Server-side checks
 * are independent; this is purely about not showing dead ends.
 */
import clsx from 'clsx'
import {
  BadgePercent, BarChart3, Bot, Boxes, Building2, ChevronDown, ClipboardList,
  FileCheck, Scale, ScrollText,
  Cpu, CreditCard, Database, FileText, GraduationCap, Grid2X2, HeartPulse, Languages,
  LayoutDashboard, LineChart, LogOut, Map, Menu, Package, Receipt, RotateCcw, Route,
  Search, Settings, ShieldCheck, ShoppingCart, Store, Tags, Target, Truck, User,
  Users, Warehouse, X, Zap,
} from 'lucide-react'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom'

import { useAuth } from '@/lib/auth'
import { setLanguage, currentLanguage } from '@/lib/i18n'
import { initials } from '@/lib/format'
import { CommandPalette } from './CommandPalette'
import { NotificationBell } from './NotificationBell'

interface NavItem {
  to: string
  labelKey: string
  icon: ReactNode
  resource: string
  action?: string
}
interface NavGroup {
  labelKey: string | null
  items: NavItem[]
}

const ic = 'h-4 w-4 shrink-0'

export const NAV_GROUPS: NavGroup[] = [
  {
    labelKey: null,
    items: [
      { to: '/', labelKey: 'nav.dashboard', icon: <LayoutDashboard className={ic} />, resource: 'dashboard.main' },
    ],
  },
  {
    labelKey: 'nav.sales',
    items: [
      { to: '/sales/hot-sale', labelKey: 'nav.hotSale', icon: <Zap className={ic} />, resource: 'sales.hot_sale' },
      { to: '/sales/orders', labelKey: 'nav.orders', icon: <ShoppingCart className={ic} />, resource: 'sales.orders' },
      { to: '/sales/invoices', labelKey: 'nav.invoices', icon: <Receipt className={ic} />, resource: 'sales.invoices' },
      { to: '/sales/payments', labelKey: 'nav.payments', icon: <CreditCard className={ic} />, resource: 'sales.payments' },
      { to: '/sales/returns', labelKey: 'nav.returns', icon: <RotateCcw className={ic} />, resource: 'sales.returns' },
    ],
  },
  {
    labelKey: 'nav.field',
    items: [
      { to: '/field/salespersons', labelKey: 'nav.salespersons', icon: <Users className={ic} />, resource: 'field.salespersons' },
      { to: '/field/routes', labelKey: 'nav.routes', icon: <Route className={ic} />, resource: 'field.routes' },
      { to: '/field/map', labelKey: 'nav.map', icon: <Map className={ic} />, resource: 'field.map' },
      { to: '/field/visits', labelKey: 'nav.visits', icon: <ClipboardList className={ic} />, resource: 'field.visits' },
      { to: '/field/vehicles', labelKey: 'nav.vehicles', icon: <Truck className={ic} />, resource: 'field.vehicles' },
      { to: '/field/day-sessions', labelKey: 'nav.daySession', icon: <Grid2X2 className={ic} />, resource: 'field.day_session' },
    ],
  },
  {
    labelKey: 'nav.stock',
    items: [
      { to: '/stock/products', labelKey: 'nav.products', icon: <Package className={ic} />, resource: 'stock.products' },
      { to: '/stock/warehouses', labelKey: 'nav.warehouses', icon: <Warehouse className={ic} />, resource: 'stock.warehouses' },
      { to: '/stock/vehicle-stock', labelKey: 'nav.vehicleStock', icon: <Boxes className={ic} />, resource: 'stock.vehicle_stock' },
      { to: '/stock/van-load', labelKey: 'nav.vanLoad', icon: <Truck className={ic} />, resource: 'stock.van_load' },
      { to: '/stock/transfers', labelKey: 'nav.transfers', icon: <Building2 className={ic} />, resource: 'stock.transfers' },
      { to: '/stock/counts', labelKey: 'nav.counts', icon: <ClipboardList className={ic} />, resource: 'stock.counts' },
      { to: '/stock/lots', labelKey: 'nav.lots', icon: <Tags className={ic} />, resource: 'stock.lots' },
    ],
  },
  {
    labelKey: 'nav.crm',
    items: [
      { to: '/crm/customers', labelKey: 'nav.customers', icon: <Store className={ic} />, resource: 'crm.customers' },
      { to: '/crm/ledger', labelKey: 'nav.ledger', icon: <FileText className={ic} />, resource: 'crm.ledger' },
      { to: '/crm/risk', labelKey: 'nav.risk', icon: <ShieldCheck className={ic} />, resource: 'crm.risk' },
    ],
  },
  {
    labelKey: 'nav.marketing',
    items: [
      { to: '/marketing/campaigns', labelKey: 'nav.campaigns', icon: <BadgePercent className={ic} />, resource: 'marketing.campaigns' },
      { to: '/marketing/price-lists', labelKey: 'nav.priceLists', icon: <Tags className={ic} />, resource: 'marketing.price_lists' },
      { to: '/marketing/discounts', labelKey: 'nav.discounts', icon: <BadgePercent className={ic} />, resource: 'marketing.discounts' },
    ],
  },
  {
    labelKey: 'nav.analytics',
    items: [
      { to: '/analytics/reports', labelKey: 'nav.reports', icon: <FileText className={ic} />, resource: 'analytics.reports' },
      { to: '/analytics/statistics', labelKey: 'nav.statistics', icon: <BarChart3 className={ic} />, resource: 'analytics.statistics' },
      { to: '/analytics/forecasts', labelKey: 'nav.forecasts', icon: <LineChart className={ic} />, resource: 'analytics.forecasts' },
      { to: '/analytics/targets', labelKey: 'nav.targets', icon: <Target className={ic} />, resource: 'analytics.targets' },
      { to: '/analytics/anomalies', labelKey: 'nav.anomalies', icon: <HeartPulse className={ic} />, resource: 'analytics.anomalies' },
    ],
  },
  {
    labelKey: 'nav.ai',
    items: [
      { to: '/ai/manager', labelKey: 'nav.aiManager', icon: <Bot className={ic} />, resource: 'ai.copilot' },
      { to: '/ai/assistant', labelKey: 'nav.aiAssistant', icon: <Bot className={ic} />, resource: 'ai.assistant' },
      { to: '/ai/terminal', labelKey: 'nav.aiTerminal', icon: <Cpu className={ic} />, resource: 'ai.terminal' },
      { to: '/ai/providers', labelKey: 'nav.aiProviders', icon: <Settings className={ic} />, resource: 'ai.providers' },
      { to: '/ai/usage', labelKey: 'nav.aiUsage', icon: <BarChart3 className={ic} />, resource: 'ai.usage' },
    ],
  },
  {
    labelKey: 'nav.compliance',
    items: [
      { to: '/compliance', labelKey: 'nav.cmpOverview', icon: <Scale className={ic} />, resource: 'compliance.overview' },
      { to: '/compliance/inventory', labelKey: 'nav.cmpInventory', icon: <Database className={ic} />, resource: 'compliance.inventory' },
      { to: '/compliance/consents', labelKey: 'nav.cmpConsents', icon: <FileCheck className={ic} />, resource: 'compliance.consent' },
      { to: '/compliance/dsr', labelKey: 'nav.cmpDsr', icon: <ScrollText className={ic} />, resource: 'compliance.dsr' },
      { to: '/compliance/rulepacks', labelKey: 'nav.cmpRulePacks', icon: <FileText className={ic} />, resource: 'compliance.rulepacks' },
      { to: '/compliance/hsp-receipts', labelKey: 'nav.hspReceipts', icon: <ShieldCheck className={ic} />, resource: 'hsp.receipts' },
    ],
  },
  {
    labelKey: 'nav.system',
    items: [
      { to: '/system/users', labelKey: 'nav.users', icon: <Users className={ic} />, resource: 'system.users' },
      { to: '/system/roles', labelKey: 'nav.roles', icon: <ShieldCheck className={ic} />, resource: 'system.roles' },
      { to: '/system/backup', labelKey: 'nav.backup', icon: <Database className={ic} />, resource: 'system.backup' },
      { to: '/system/audit', labelKey: 'nav.audit', icon: <FileText className={ic} />, resource: 'system.audit' },
      { to: '/system/training', labelKey: 'nav.training', icon: <GraduationCap className={ic} />, resource: 'system.training' },
      { to: '/system/health', labelKey: 'nav.health', icon: <HeartPulse className={ic} />, resource: 'system.health' },
      { to: '/system/settings', labelKey: 'nav.settings', icon: <Settings className={ic} />, resource: 'system.settings' },
    ],
  },
]

export function Layout({ children }: { children: ReactNode }) {
  const { t } = useTranslation()
  const { session, logout, can } = useAuth()
  const [mobileOpen, setMobileOpen] = useState(false)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const location = useLocation()
  const navigate = useNavigate()

  useEffect(() => setMobileOpen(false), [location.pathname])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen(true)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const groups = useMemo(
    () =>
      NAV_GROUPS.map((g) => ({
        ...g,
        items: g.items.filter((i) => can(i.resource, i.action ?? 'VIEW')),
      })).filter((g) => g.items.length > 0),
    [can],
  )

  const lang = currentLanguage()

  return (
    <div className="flex h-full bg-shell-50">
      {/* ---------------- Sidebar ---------------- */}
      <aside
        className={clsx(
          'fixed inset-y-0 left-0 z-40 flex w-64 flex-col bg-shell-900 transition-transform lg:static lg:translate-x-0',
          mobileOpen ? 'translate-x-0' : '-translate-x-full',
        )}
      >
        <div className="flex h-14 items-center gap-2.5 border-b border-shell-800 px-4">
          <div className="rounded-lg bg-brand-600 p-1.5">
            <Truck className="h-4 w-4 text-white" />
          </div>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold text-white">
              {t('app.shortName')}
            </p>
            <p className="truncate text-2xs text-shell-400">v1.0.0</p>
          </div>
          <button
            type="button"
            className="text-shell-400 hover:text-white lg:hidden"
            onClick={() => setMobileOpen(false)}
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <nav className="flex-1 space-y-4 overflow-y-auto px-3 py-4">
          {groups.map((group, gi) => (
            <div key={group.labelKey ?? `g${gi}`}>
              {group.labelKey && (
                <p className="mb-1.5 px-2 text-2xs font-semibold uppercase tracking-wider text-shell-500">
                  {t(group.labelKey)}
                </p>
              )}
              <ul className="space-y-0.5">
                {group.items.map((item) => (
                  <li key={item.to}>
                    <NavLink
                      to={item.to}
                      end={item.to === '/'}
                      className={({ isActive }) =>
                        clsx(
                          'flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors',
                          isActive
                            ? 'bg-brand-600 font-medium text-white'
                            : 'text-shell-300 hover:bg-shell-800 hover:text-white',
                        )
                      }
                    >
                      {item.icon}
                      <span className="truncate">{t(item.labelKey)}</span>
                    </NavLink>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>

        <div className="border-t border-shell-800 p-3">
          <div className="flex items-center gap-2.5 rounded-lg px-2 py-1.5">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-600 text-xs font-semibold text-white">
              {initials(session?.user.full_name ?? '?')}
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs font-medium text-white">
                {session?.user.full_name}
              </p>
              <p className="truncate text-2xs text-shell-400">
                {lang === 'en' ? session?.user.role?.name_en : session?.user.role?.name_tr}
              </p>
            </div>
          </div>
        </div>
      </aside>

      {mobileOpen && (
        <div
          className="fixed inset-0 z-30 bg-shell-950/50 lg:hidden"
          onClick={() => setMobileOpen(false)}
        />
      )}

      {/* ---------------- Main ---------------- */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="no-print sticky top-0 z-20 flex h-14 items-center gap-3 border-b border-shell-200 bg-white/90 px-4 backdrop-blur">
          <button
            type="button"
            className="btn-ghost btn-sm lg:hidden"
            onClick={() => setMobileOpen(true)}
            aria-label="menu"
          >
            <Menu className="h-4 w-4" />
          </button>

          <button
            type="button"
            onClick={() => setPaletteOpen(true)}
            className="flex flex-1 items-center gap-2 rounded-lg border border-shell-200 bg-shell-50 px-3 py-1.5 text-sm text-shell-400 hover:bg-shell-100 sm:max-w-md"
          >
            <Search className="h-4 w-4" />
            <span className="flex-1 text-left">{t('common.searchPlaceholder')}</span>
            <kbd className="hidden rounded border border-shell-300 bg-white px-1.5 text-2xs text-shell-500 sm:inline">
              Ctrl K
            </kbd>
          </button>

          <div className="ml-auto flex items-center gap-1">
            <button
              type="button"
              className="btn-ghost btn-sm gap-1.5"
              onClick={() => setLanguage(lang === 'tr' ? 'en' : 'tr')}
              title={lang === 'tr' ? 'Switch to English' : "Türkçe'ye geç"}
            >
              <Languages className="h-4 w-4" />
              <span className="text-xs font-semibold uppercase">{lang}</span>
            </button>

            <NotificationBell />

            <div className="relative">
              <button
                type="button"
                className="btn-ghost btn-sm gap-1.5"
                onClick={() => setMenuOpen((v) => !v)}
              >
                <div className="flex h-6 w-6 items-center justify-center rounded-full bg-brand-600 text-2xs font-semibold text-white">
                  {initials(session?.user.full_name ?? '?')}
                </div>
                <ChevronDown className="h-3.5 w-3.5" />
              </button>
              {menuOpen && (
                <>
                  <div className="fixed inset-0 z-40" onClick={() => setMenuOpen(false)} />
                  <div className="absolute right-0 z-50 mt-1 w-56 animate-slide-up rounded-lg border border-shell-200 bg-white py-1 shadow-pop">
                    <div className="border-b border-shell-100 px-3 py-2">
                      <p className="truncate text-sm font-medium text-shell-800">
                        {session?.user.full_name}
                      </p>
                      <p className="truncate text-xs text-shell-500">
                        @{session?.user.username}
                      </p>
                    </div>
                    <Link
                      to="/profile"
                      className="flex items-center gap-2 px-3 py-2 text-sm text-shell-700 hover:bg-shell-50"
                      onClick={() => setMenuOpen(false)}
                    >
                      <User className="h-4 w-4" />
                      {t('auth.profile')}
                    </Link>
                    <button
                      type="button"
                      className="flex w-full items-center gap-2 px-3 py-2 text-sm text-danger-600 hover:bg-danger-50"
                      onClick={async () => {
                        setMenuOpen(false)
                        await logout()
                        navigate('/login')
                      }}
                    >
                      <LogOut className="h-4 w-4" />
                      {t('auth.logout')}
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        </header>

        <main className="flex-1 overflow-y-auto p-4 sm:p-6">{children}</main>
      </div>

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  )
}
