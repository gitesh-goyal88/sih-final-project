'use client';

import 'maplibre-gl/dist/maplibre-gl.css';
import maplibregl from 'maplibre-gl';
import { useEffect, useRef } from 'react';

export type MapCase = { caseId: string; shortCode: string; status: string; category?: string | null; verified: boolean;
                        point?: { lat: number; lng: number } | null; escalationLevel: number };
export type MapFacility = { id: string; name: string; status: string; stale: boolean; location?: { lat: number; lng: number } };

const TONE: Record<string, string> = { created: '#F9A825', matched: '#F9A825', accepted: '#1E8E3E', transport_assigned: '#F9A825',
  in_transit: '#F9A825', arrived_seen: '#1E8E3E' };
const TILE = process.env.NEXT_PUBLIC_TILE_URL || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';

/** US12 district map: open cases coloured by status (+ label, never colour alone) and facility status. */
export function DistrictMap({ cases, facilities }: { cases: MapCase[]; facilities: MapFacility[] }) {
  const el = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const markers = useRef<maplibregl.Marker[]>([]);

  useEffect(() => {
    if (!el.current || map.current) return;
    map.current = new maplibregl.Map({
      container: el.current,
      style: { version: 8, sources: { osm: { type: 'raster', tiles: [TILE], tileSize: 256,
        attribution: '© OpenStreetMap contributors' } }, layers: [{ id: 'osm', type: 'raster', source: 'osm' }] },
      center: [77.45, 27.6], zoom: 10,
    });
    map.current.addControl(new maplibregl.NavigationControl(), 'top-right');
    return () => { map.current?.remove(); map.current = null; };
  }, []);

  useEffect(() => {
    const m = map.current;
    if (!m) return;
    markers.current.forEach((x) => x.remove());
    markers.current = [];
    const pts: [number, number][] = [];
    for (const f of facilities) {
      if (!f.location) continue;
      const d = document.createElement('div');
      d.className = 'rounded-md border-2 bg-white px-1.5 text-xs font-bold';
      d.style.borderColor = f.status === 'open' ? (f.stale ? '#F9A825' : '#1E4FA3') : '#6B7280';
      d.textContent = `🏥 ${f.name}${f.status !== 'open' ? ` (${f.status})` : ''}${f.stale ? ' ⏳' : ''}`;
      markers.current.push(new maplibregl.Marker({ element: d }).setLngLat([f.location.lng, f.location.lat]).addTo(m));
      pts.push([f.location.lng, f.location.lat]);
    }
    for (const c of cases) {
      if (!c.point) continue;
      const d = document.createElement('div');
      d.setAttribute('role', 'img');
      d.setAttribute('aria-label', `${c.shortCode} ${c.status}`);
      d.className = 'rounded-full px-2 py-0.5 text-xs font-bold text-ink shadow';
      d.style.background = c.escalationLevel > 0 ? '#D32F2F' : TONE[c.status] ?? '#1E4FA3';
      if (c.escalationLevel > 0) d.style.color = '#fff';
      d.textContent = `${c.shortCode} · ${c.status.replace('_', ' ')}${c.verified ? '' : ' ?'}`;
      markers.current.push(new maplibregl.Marker({ element: d }).setLngLat([c.point.lng, c.point.lat]).addTo(m));
      pts.push([c.point.lng, c.point.lat]);
    }
    if (pts.length > 1) {
      const b = pts.reduce((acc, p) => acc.extend(p), new maplibregl.LngLatBounds(pts[0], pts[0]));
      m.fitBounds(b, { padding: 60, maxZoom: 12, duration: 0 });
    }
  }, [cases, facilities]);

  return <div ref={el} className="h-[480px] w-full rounded-xl border" aria-label="District map" />;
}
