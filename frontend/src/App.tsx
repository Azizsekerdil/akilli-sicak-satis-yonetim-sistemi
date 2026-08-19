/**
 * Routing.
 *
 * Every protected route is wrapped in <Guard resource=…>, which redirects to
 * the login screen when signed out and to /forbidden when the user lacks the
 * permission.  Pages are lazily loaded so the initial bundle stays small — it
 * matters on a phone in a van with poor signal.
 *
 * <Guard> also intercepts an account that still owes a password change and
 * shows nothing but the change form.  The server enforces the same rule on
 * every route (see `app/core/deps.py`), so this redirect is for the person,
 * not for the security boundary.
 */
import { Suspense, lazy, type ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'

import { Layout } from '@/components/Layout'
import { LoadingBlock } from '@/components/ui'
import { useAuth } from '@/lib/auth'

const Login = lazy(() => import('@/pages/Login'))
const ForcePasswordChange = lazy(() => import('@/pages/ForcePasswordChange'))
const Dashboard = lazy(() => import('@/pages/Dashboard'))
const Profile = lazy(() => import('@/pages/Profile'))
const Forbidden = lazy(() => import('@/pages/Forbidden'))
const NotFound = lazy(() => import('@/pages/NotFound'))

const HotSale = lazy(() => import('@/pages/sales/HotSale'))
const Orders = lazy(() => import('@/pages/sales/Orders'))
const Invoices = lazy(() => import('@/pages/sales/Invoices'))
const Payments = lazy(() => import('@/pages/sales/Payments'))
const Returns = lazy(() => import('@/pages/sales/Returns'))

const Salespersons = lazy(() => import('@/pages/field/Salespersons'))
const RoutesPage = lazy(() => import('@/pages/field/Routes'))
const MapPage = lazy(() => import('@/pages/field/MapView'))
const Visits = lazy(() => import('@/pages/field/Visits'))
const Vehicles = lazy(() => import('@/pages/field/Vehicles'))
const DaySessions = lazy(() => import('@/pages/field/DaySessions'))

const Products = lazy(() => import('@/pages/stock/Products'))
const Warehouses = lazy(() => import('@/pages/stock/Warehouses'))
const VehicleStock = lazy(() => import('@/pages/stock/VehicleStock'))
const VanLoad = lazy(() => import('@/pages/stock/VanLoad'))
const Transfers = lazy(() => import('@/pages/stock/Transfers'))
const Counts = lazy(() => import('@/pages/stock/Counts'))
const Lots = lazy(() => import('@/pages/stock/Lots'))

const Customers = lazy(() => import('@/pages/crm/Customers'))
const CustomerDetail = lazy(() => import('@/pages/crm/CustomerDetail'))
const Ledger = lazy(() => import('@/pages/crm/Ledger'))
const RiskAnalysis = lazy(() => import('@/pages/crm/RiskAnalysis'))

const Campaigns = lazy(() => import('@/pages/marketing/Campaigns'))
const PriceLists = lazy(() => import('@/pages/marketing/PriceLists'))
const Discounts = lazy(() => import('@/pages/marketing/Discounts'))

const Reports = lazy(() => import('@/pages/analytics/Reports'))
const Statistics = lazy(() => import('@/pages/analytics/Statistics'))
const Forecasts = lazy(() => import('@/pages/analytics/Forecasts'))
const Targets = lazy(() => import('@/pages/analytics/Targets'))
const Anomalies = lazy(() => import('@/pages/analytics/Anomalies'))

const AIManager = lazy(() => import('@/pages/ai/AIManager'))
const AIAssistant = lazy(() => import('@/pages/ai/AIAssistant'))
const AITerminal = lazy(() => import('@/pages/ai/AITerminal'))
const AIProviders = lazy(() => import('@/pages/ai/AIProviders'))
const AIUsage = lazy(() => import('@/pages/ai/AIUsage'))

const CmpOverview = lazy(() => import('@/pages/compliance/Overview'))
const CmpInventory = lazy(() => import('@/pages/compliance/Inventory'))
const CmpConsents = lazy(() => import('@/pages/compliance/Consents'))
const CmpDsr = lazy(() => import('@/pages/compliance/DataSubjectRequests'))
const CmpRulePacks = lazy(() => import('@/pages/compliance/RulePacks'))
const HspReceipts = lazy(() => import('@/pages/compliance/HspReceipts'))

const Users = lazy(() => import('@/pages/system/Users'))
const Roles = lazy(() => import('@/pages/system/Roles'))
const Backup = lazy(() => import('@/pages/system/Backup'))
const AuditLog = lazy(() => import('@/pages/system/AuditLog'))
const Training = lazy(() => import('@/pages/system/Training'))
const Health = lazy(() => import('@/pages/system/Health'))
const SettingsPage = lazy(() => import('@/pages/system/Settings'))

function Guard({
  resource,
  action = 'VIEW',
  children,
}: {
  resource?: string
  action?: string
  children: ReactNode
}) {
  const { session, loading, can } = useAuth()
  const location = useLocation()

  if (loading) return <LoadingBlock />
  if (!session) return <Navigate to="/login" replace state={{ from: location.pathname }} />
  // Nothing else is reachable until the initial/reset password is replaced.
  // Rendered in place rather than redirected to a route so there is no URL an
  // impatient user can navigate away from.
  if (session.user.must_change_password) return <ForcePasswordChange />
  if (resource && !can(resource, action)) return <Navigate to="/forbidden" replace />
  return <Layout>{children}</Layout>
}

interface RouteDef {
  path: string
  element: ReactNode
  resource?: string
}

const PROTECTED: RouteDef[] = [
  { path: '/', element: <Dashboard />, resource: 'dashboard.main' },
  { path: '/profile', element: <Profile /> },

  { path: '/sales/hot-sale', element: <HotSale />, resource: 'sales.hot_sale' },
  { path: '/sales/orders', element: <Orders />, resource: 'sales.orders' },
  { path: '/sales/invoices', element: <Invoices />, resource: 'sales.invoices' },
  { path: '/sales/payments', element: <Payments />, resource: 'sales.payments' },
  { path: '/sales/returns', element: <Returns />, resource: 'sales.returns' },

  { path: '/field/salespersons', element: <Salespersons />, resource: 'field.salespersons' },
  { path: '/field/routes', element: <RoutesPage />, resource: 'field.routes' },
  { path: '/field/map', element: <MapPage />, resource: 'field.map' },
  { path: '/field/visits', element: <Visits />, resource: 'field.visits' },
  { path: '/field/vehicles', element: <Vehicles />, resource: 'field.vehicles' },
  { path: '/field/day-sessions', element: <DaySessions />, resource: 'field.day_session' },

  { path: '/stock/products', element: <Products />, resource: 'stock.products' },
  { path: '/stock/products/:id', element: <Products />, resource: 'stock.products' },
  { path: '/stock/warehouses', element: <Warehouses />, resource: 'stock.warehouses' },
  { path: '/stock/vehicle-stock', element: <VehicleStock />, resource: 'stock.vehicle_stock' },
  { path: '/stock/van-load', element: <VanLoad />, resource: 'stock.van_load' },
  { path: '/stock/transfers', element: <Transfers />, resource: 'stock.transfers' },
  { path: '/stock/counts', element: <Counts />, resource: 'stock.counts' },
  { path: '/stock/lots', element: <Lots />, resource: 'stock.lots' },

  { path: '/crm/customers', element: <Customers />, resource: 'crm.customers' },
  { path: '/crm/customers/:id', element: <CustomerDetail />, resource: 'crm.customers' },
  { path: '/crm/ledger', element: <Ledger />, resource: 'crm.ledger' },
  { path: '/crm/risk', element: <RiskAnalysis />, resource: 'crm.risk' },

  { path: '/marketing/campaigns', element: <Campaigns />, resource: 'marketing.campaigns' },
  { path: '/marketing/price-lists', element: <PriceLists />, resource: 'marketing.price_lists' },
  { path: '/marketing/discounts', element: <Discounts />, resource: 'marketing.discounts' },

  { path: '/analytics/reports', element: <Reports />, resource: 'analytics.reports' },
  { path: '/analytics/statistics', element: <Statistics />, resource: 'analytics.statistics' },
  { path: '/analytics/forecasts', element: <Forecasts />, resource: 'analytics.forecasts' },
  { path: '/analytics/targets', element: <Targets />, resource: 'analytics.targets' },
  { path: '/analytics/anomalies', element: <Anomalies />, resource: 'analytics.anomalies' },

  { path: '/ai/manager', element: <AIManager />, resource: 'ai.copilot' },
  { path: '/ai/assistant', element: <AIAssistant />, resource: 'ai.assistant' },
  { path: '/ai/terminal', element: <AITerminal />, resource: 'ai.terminal' },
  { path: '/ai/providers', element: <AIProviders />, resource: 'ai.providers' },
  { path: '/ai/usage', element: <AIUsage />, resource: 'ai.usage' },

  { path: '/compliance', element: <CmpOverview />, resource: 'compliance.overview' },
  { path: '/compliance/inventory', element: <CmpInventory />, resource: 'compliance.inventory' },
  { path: '/compliance/consents', element: <CmpConsents />, resource: 'compliance.consent' },
  { path: '/compliance/dsr', element: <CmpDsr />, resource: 'compliance.dsr' },
  { path: '/compliance/rulepacks', element: <CmpRulePacks />, resource: 'compliance.rulepacks' },
  { path: '/compliance/hsp-receipts', element: <HspReceipts />, resource: 'hsp.receipts' },

  { path: '/system/users', element: <Users />, resource: 'system.users' },
  { path: '/system/roles', element: <Roles />, resource: 'system.roles' },
  { path: '/system/backup', element: <Backup />, resource: 'system.backup' },
  { path: '/system/audit', element: <AuditLog />, resource: 'system.audit' },
  { path: '/system/training', element: <Training />, resource: 'system.training' },
  { path: '/system/health', element: <Health />, resource: 'system.health' },
  { path: '/system/settings', element: <SettingsPage />, resource: 'system.settings' },
]

export default function App() {
  return (
    <Suspense fallback={<LoadingBlock />}>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/forbidden" element={<Forbidden />} />
        {PROTECTED.map(({ path, element, resource }) => (
          <Route
            key={path}
            path={path}
            element={<Guard resource={resource}>{element}</Guard>}
          />
        ))}
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Suspense>
  )
}
