/**
 * Live field map.
 *
 * Built directly on Leaflet (BSD-2-Clause) rather than react-leaflet.
 * react-leaflet 4.x and @react-leaflet/core are published under the
 * Hippocratic Licence 2.1, which is not OSI-approved and carries
 * field-of-use restrictions; it cannot travel inside a permissively licensed
 * public release. Leaflet itself has always been BSD-2-Clause, and the handful
 * of components this page used (MapContainer, TileLayer, Marker, CircleMarker,
 * Polyline, Popup) are thin wrappers over the imperative API used here.
 *
 * OpenStreetMap tiles (attribution is a licence condition — do not remove).
 * Leaflet's default marker icons are image files that break under bundlers,
 * so every marker here is an `L.divIcon` built from inline SVG/HTML.
 */
import 'leaflet/dist/leaflet.css'

import { useQuery } from '@tanstack/react-query'
import * as L from 'leaflet'
import { Map as MapIcon, Truck, Warehouse } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Card, ErrorState, Field, LoadingBlock, PageHeader } from '@/components/ui'
import { api } from '@/lib/api'
import { formatDate, formatMoney, formatNumber } from '@/lib/format'

/* -------------------------------------------------------------------------- */
/* Types (mirror MapSnapshotOut in app/schemas/route.py)                      */
/* -------------------------------------------------------------------------- */
interface MapVehicle {
  id: number
  code: string
  plate_number: string
  status: string
  latitude: number | null
  longitude: number | null
  position_at: string | null
  salesperson_id: number | null
  salesperson_name: string | null
  is_refrigerated: boolean
}
interface MapCustomer {
  id: number
  code: string
  name: string
  latitude: number | null
  longitude: number | null
  status: string
  customer_type: string
  is_priority: boolean
  balance: number | string
  last_visit_date: string | null
}
interface MapWarehouse {
  id: number
  code: string
  name: string
  warehouse_type: string
  latitude: number | null
  longitude: number | null
}
interface MapRoutePoint {
  sequence: number
  customer_id: number
  name: string | null
  latitude: number | null
  longitude: number | null
  status: string
  planned_arrival: string | null
}
interface MapRoute {
  id: number
  code: string
  name: string
  status: string
  salesperson_id: number | null
  salesperson_name: string | null
  vehicle_id: number | null
  planned_distance_km: number
  planned_stops: number
  completed_stops: number
  points: MapRoutePoint[]
}
interface MapSnapshot {
  on_date: string
  vehicles: MapVehicle[]
  customers: MapCustomer[]
  warehouses: MapWarehouse[]
  routes: MapRoute[]
}

type LatLng = [number, number]

const FALLBACK_CENTER: LatLng = [39.0, 35.0] // Türkiye
const ROUTE_COLOURS = ['#4f46e5', '#0891b2', '#c026d3', '#ea580c', '#0d9488', '#7c3aed']

const STOP_COLOUR: Record<string, string> = {
  COMPLETED: '#059669',
  ARRIVED: '#2563eb',
  PENDING: '#d97706',
  SKIPPED: '#dc2626',
  FAILED: '#dc2626',
}
const UNPLANNED_COLOUR = '#94a3b8'

function isoDate(d: Date = new Date()): string {
  const z = new Date(d.getTime() - d.getTimezoneOffset() * 60_000)
  return z.toISOString().slice(0, 10)
}

const esc = (s: string): string =>
  s.replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] ?? c,
  )

/** Truck pin with the plate rendered beside it. Pure HTML — no image assets. */
function vehicleIcon(label: string, colour: string): L.DivIcon {
  return L.divIcon({
    className: '',
    iconSize: [30, 30],
    iconAnchor: [15, 15],
    html: `<div style="display:flex;align-items:center;gap:4px;transform:translateX(-9px)">
      <span style="display:flex;width:26px;height:26px;align-items:center;justify-content:center;
        border-radius:9999px;background:${colour};box-shadow:0 1px 6px rgba(15,23,42,.35);color:#fff">
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M10 17h4V5H2v12h3"/><path d="M20 17h2v-3.34a4 4 0 0 0-1.17-2.83L19 9h-5v8h1"/>
          <circle cx="7.5" cy="17.5" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/>
        </svg>
      </span>
      <span style="white-space:nowrap;border-radius:4px;background:rgba(255,255,255,.92);
        padding:1px 5px;font-size:10px;font-weight:600;color:#1e293b;
        box-shadow:0 1px 3px rgba(15,23,42,.2)">${esc(label)}</span>
    </div>`,
  })
}

function warehouseIcon(): L.DivIcon {
  return L.divIcon({
    className: '',
    iconSize: [24, 24],
    iconAnchor: [12, 12],
    html: `<span style="display:flex;width:24px;height:24px;align-items:center;justify-content:center;
      border-radius:6px;background:#334155;color:#fff;box-shadow:0 1px 5px rgba(15,23,42,.35)">
      <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M22 8.35V20a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8.35A2 2 0 0 1 3.26 6.5l8-3.2a2 2 0 0 1 1.48 0l8 3.2A2 2 0 0 1 22 8.35Z"/>
        <path d="M6 18h12"/><path d="M6 14h12"/><rect width="12" height="12" x="6" y="10"/>
      </svg>
    </span>`,
  })
}

/* -------------------------------------------------------------------------- */
/* Popup markup                                                               */
/* -------------------------------------------------------------------------- */
/**
 * Leaflet popups take an HTML string, so the popup body is markup rather than
 * a React subtree. Every interpolated value goes through `esc` — these strings
 * carry customer and salesperson names, which are user data and must never
 * reach `innerHTML` unescaped.
 */
function popupHtml(
  title: string,
  subtitle: string | undefined,
  rows: [string, string][],
): string {
  const head =
    `<p style="margin:0;font-size:13px;font-weight:600;color:#0f172a">${esc(title)}</p>` +
    (subtitle
      ? `<p style="margin:0 0 6px;font-size:10px;color:#94a3b8">${esc(subtitle)}</p>`
      : '')
  const body = rows
    .map(
      ([k, v]) =>
        `<div style="display:flex;justify-content:space-between;gap:12px">` +
        `<dt style="color:#64748b">${esc(k)}</dt>` +
        `<dd style="margin:0;font-variant-numeric:tabular-nums;font-weight:500;color:#1e293b">${esc(v)}</dd>` +
        `</div>`,
    )
    .join('')
  return (
    `<div style="min-width:10rem">${head}` +
    `<dl style="margin:0;font-size:11px;display:grid;gap:2px">${body}</dl></div>`
  )
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function MapView() {
  const { t } = useTranslation()
  const [onDate, setOnDate] = useState(isoDate())
  const [routeId, setRouteId] = useState('')
  const [show, setShow] = useState({ vehicles: true, customers: true, warehouses: true, routes: true })

  const containerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<L.Map | null>(null)
  /** Every layer this component owns, so a redraw removes exactly its own. */
  const overlayRef = useRef<L.LayerGroup | null>(null)
  /** Guards the one-shot fit: refit only when the snapshot itself changes. */
  const fittedRef = useRef<string>('')

  const snapshot = useQuery({
    queryKey: ['map-snapshot', onDate],
    queryFn: () => api.get<MapSnapshot>('/routes/map', { on_date: onDate, include_customers: true }),
    refetchInterval: 60_000,
  })

  const data = snapshot.data
  const routes = useMemo(
    () => (data?.routes ?? []).filter((r) => !routeId || String(r.id) === routeId),
    [data, routeId],
  )

  const stopStatus = useMemo(() => {
    const m = new Map<number, string>()
    for (const r of routes) for (const p of r.points) m.set(p.customer_id, p.status)
    return m
  }, [routes])

  const polylines = useMemo(
    () =>
      routes.map((r, i) => ({
        id: r.id,
        colour: ROUTE_COLOURS[i % ROUTE_COLOURS.length],
        positions: r.points
          .slice()
          .sort((a, b) => a.sequence - b.sequence)
          .filter((p): p is MapRoutePoint & { latitude: number; longitude: number } =>
            p.latitude !== null && p.longitude !== null)
          .map((p) => [p.latitude, p.longitude] as LatLng),
      })),
    [routes],
  )

  const allPoints = useMemo<LatLng[]>(() => {
    const out: LatLng[] = []
    for (const v of data?.vehicles ?? []) if (v.latitude !== null && v.longitude !== null) out.push([v.latitude, v.longitude])
    for (const w of data?.warehouses ?? []) if (w.latitude !== null && w.longitude !== null) out.push([w.latitude, w.longitude])
    for (const line of polylines) out.push(...line.positions)
    if (out.length === 0) {
      for (const c of data?.customers ?? []) {
        if (c.latitude !== null && c.longitude !== null) out.push([c.latitude, c.longitude])
      }
    }
    return out
  }, [data, polylines])

  const ready = !snapshot.isLoading && !snapshot.isError

  /* --- create the map once, tear it down on unmount ---------------------- */
  useEffect(() => {
    if (!ready) return
    const host = containerRef.current
    if (!host || mapRef.current) return

    const map = L.map(host, { scrollWheelZoom: true }).setView(FALLBACK_CENTER, 6)
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
    }).addTo(map)
    overlayRef.current = L.layerGroup().addTo(map)
    mapRef.current = map

    return () => {
      map.remove()
      mapRef.current = null
      overlayRef.current = null
      fittedRef.current = ''
    }
  }, [ready])

  /* --- redraw the overlay whenever data or the toggles change ------------ */
  useEffect(() => {
    const map = mapRef.current
    const overlay = overlayRef.current
    if (!map || !overlay) return
    overlay.clearLayers()

    if (show.routes) {
      for (const line of polylines) {
        if (line.positions.length > 1) {
          L.polyline(line.positions, { color: line.colour, weight: 3, opacity: 0.75 }).addTo(overlay)
        }
      }
    }

    if (show.customers) {
      for (const c of data?.customers ?? []) {
        if (c.latitude === null || c.longitude === null) continue
        const extra: [string, string][] = []
        if (stopStatus.has(c.id)) {
          const s = stopStatus.get(c.id)!
          extra.push([t('routes.stops'), t(`routes.stopStatus.${s}`, { defaultValue: s })])
        }
        if (c.is_priority) extra.push([t('mapView.priority'), t('common.yes')])
        L.circleMarker([c.latitude, c.longitude], {
          radius: c.is_priority ? 8 : 6,
          color: '#ffffff',
          weight: 1.5,
          fillColor: STOP_COLOUR[stopStatus.get(c.id) ?? ''] ?? UNPLANNED_COLOUR,
          fillOpacity: 0.95,
        })
          .bindPopup(
            popupHtml(c.name, c.code, [
              [t('common.status'), c.status],
              [t('mapView.balance'), formatMoney(c.balance)],
              [t('mapView.lastVisit'), formatDate(c.last_visit_date)],
              ...extra,
            ]),
          )
          .addTo(overlay)
      }
    }

    if (show.warehouses) {
      for (const w of data?.warehouses ?? []) {
        if (w.latitude === null || w.longitude === null) continue
        L.marker([w.latitude, w.longitude], { icon: warehouseIcon() })
          .bindPopup(popupHtml(w.name, w.code, [[t('common.details'), w.warehouse_type]]))
          .addTo(overlay)
      }
    }

    if (show.vehicles) {
      for (const v of data?.vehicles ?? []) {
        if (v.latitude === null || v.longitude === null) continue
        L.marker([v.latitude, v.longitude], {
          icon: vehicleIcon(v.plate_number, v.status === 'IN_ROUTE' ? '#4f46e5' : '#0f172a'),
        })
          .bindPopup(
            popupHtml(v.plate_number, v.code, [
              [t('common.status'), v.status],
              [t('routes.salesperson'), v.salesperson_name ?? '—'],
              [t('mapView.lastPosition'), formatDate(v.position_at, { withTime: true })],
              [t('mapView.refrigerated'), v.is_refrigerated ? t('common.yes') : t('common.no')],
            ]),
          )
          .addTo(overlay)
      }
    }
  }, [data, polylines, stopStatus, show, t])

  /* --- frame the map on the data the first time a snapshot arrives ------- */
  useEffect(() => {
    const map = mapRef.current
    if (!map || allPoints.length === 0) return
    const signature = `${onDate}|${routeId}|${allPoints.length}`
    if (fittedRef.current === signature) return
    fittedRef.current = signature
    map.fitBounds(L.latLngBounds(allPoints), { padding: [40, 40], maxZoom: 14 })
  }, [allPoints, onDate, routeId])

  const toggle = (k: keyof typeof show) => setShow((s) => ({ ...s, [k]: !s[k] }))

  return (
    <>
      <PageHeader
        title={t('mapView.title')}
        subtitle={data ? formatDate(data.on_date) : undefined}
        icon={<MapIcon className="h-5 w-5" />}
        actions={
          <div className="flex flex-wrap items-end gap-2">
            <Field label={t('common.date')}>
              <input type="date" className="input" value={onDate} onChange={(e) => setOnDate(e.target.value)} />
            </Field>
            <Field label={t('mapView.route')}>
              <select className="input" value={routeId} onChange={(e) => setRouteId(e.target.value)}>
                <option value="">{t('mapView.allRoutes')}</option>
                {(data?.routes ?? []).map((r) => (
                  <option key={r.id} value={r.id}>{r.code} — {r.name}</option>
                ))}
              </select>
            </Field>
          </div>
        }
      />

      {snapshot.isLoading ? (
        <Card><LoadingBlock /></Card>
      ) : snapshot.isError ? (
        <Card><ErrorState error={snapshot.error} onRetry={() => void snapshot.refetch()} /></Card>
      ) : (
        <div className="relative h-[calc(100vh-15rem)] min-h-[420px] overflow-hidden rounded-xl border border-shell-200 shadow-card">
          <div ref={containerRef} className="h-full w-full" />

          {/* --------- overlay: layer toggles + legend --------- */}
          <div className="pointer-events-none absolute right-3 top-3 z-[1000] w-56 max-w-[calc(100%-1.5rem)]">
            <div className="pointer-events-auto rounded-lg border border-shell-200 bg-white/95 p-3 shadow-pop backdrop-blur">
              <p className="mb-2 text-2xs font-semibold uppercase tracking-wide text-shell-500">
                {t('mapView.layers')}
              </p>
              <div className="space-y-1.5 text-xs">
                {([
                  ['vehicles', t('mapView.vehicles'), (data?.vehicles ?? []).length],
                  ['customers', t('mapView.customers'), (data?.customers ?? []).length],
                  ['warehouses', t('mapView.warehouses'), (data?.warehouses ?? []).length],
                  ['routes', t('mapView.routeLines'), polylines.length],
                ] as [keyof typeof show, string, number][]).map(([key, label, count]) => (
                  <label key={key} className="flex cursor-pointer items-center gap-2 text-shell-700">
                    <input type="checkbox" checked={show[key]} onChange={() => toggle(key)}
                      className="h-3.5 w-3.5 rounded border-shell-300 text-brand-600" />
                    <span className="flex-1">{label}</span>
                    <span className="tabular text-2xs text-shell-400">{formatNumber(count)}</span>
                  </label>
                ))}
              </div>

              <p className="mb-2 mt-3 border-t border-shell-200 pt-2 text-2xs font-semibold uppercase tracking-wide text-shell-500">
                {t('mapView.legend')}
              </p>
              <ul className="space-y-1 text-2xs text-shell-600">
                {[
                  [STOP_COLOUR.COMPLETED, t('mapView.completedStop')],
                  [STOP_COLOUR.PENDING, t('mapView.pendingStop')],
                  [STOP_COLOUR.SKIPPED, t('mapView.skippedStop')],
                  [UNPLANNED_COLOUR, t('mapView.unplannedCustomer')],
                ].map(([colour, label]) => (
                  <li key={label} className="flex items-center gap-2">
                    <span className="h-2.5 w-2.5 rounded-full" style={{ background: colour }} />
                    {label}
                  </li>
                ))}
                <li className="flex items-center gap-2"><Truck className="h-3 w-3 text-shell-700" />{t('mapView.vehicles')}</li>
                <li className="flex items-center gap-2"><Warehouse className="h-3 w-3 text-shell-700" />{t('mapView.warehouses')}</li>
              </ul>
            </div>
          </div>

          {allPoints.length === 0 && (
            <div className="pointer-events-none absolute inset-x-0 bottom-4 z-[1000] flex justify-center">
              <span className="rounded-lg bg-white/95 px-3 py-1.5 text-xs text-shell-600 shadow-pop">
                {t('mapView.noCoordinates')}
              </span>
            </div>
          )}
        </div>
      )}

      {/* Route summary strip */}
      {routes.length > 0 && (
        <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {routes.slice(0, 8).map((r, i) => (
            <div key={r.id} className="card p-3">
              <div className="flex items-center gap-2">
                <span className="h-2.5 w-2.5 shrink-0 rounded-full"
                  style={{ background: ROUTE_COLOURS[i % ROUTE_COLOURS.length] }} />
                <p className="truncate text-sm font-medium text-shell-900">{r.code}</p>
              </div>
              <p className="truncate text-2xs text-shell-500">{r.salesperson_name ?? r.name}</p>
              <p className="tabular mt-1 text-xs text-shell-600">
                {t('mapView.stopsProgress', { completed: r.completed_stops, planned: r.planned_stops })} ·{' '}
                {formatNumber(r.planned_distance_km, { decimals: 1 })} km
              </p>
            </div>
          ))}
        </div>
      )}
    </>
  )
}
