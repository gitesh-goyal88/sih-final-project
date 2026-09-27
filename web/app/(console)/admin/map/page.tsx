'use client';

import dynamic from 'next/dynamic';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { Card, Skeleton, StatusChip } from '@/components/ui';
import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import { ESCALATION_REASON } from '@/lib/vocab';
import type { MapCase, MapFacility } from '@/components/DistrictMap';

const DistrictMap = dynamic(() => import('@/components/DistrictMap').then((m) => m.DistrictMap), { ssr: false });

type Dash = {
  tiles: { openEmergencies: number; referralsWaiting: number; casesClosedToday: number; facilitiesNotUpdated: number };
  stuck: { caseId: string; shortCode: string; reason: string; level: number; ageS: number }[];
  facilities: { id: string; name: string; status: string; bedsAvailable: number; stale: boolean }[];
  weekly: { medianSosToAcceptS: number | null; medianSosToArrivalMin: number | null; closedWithin72hPct: number | null };
};

/** UI-UX §7.4: top row = 4 numbers only; map of open cases; stuck cases; simple weekly numbers. Refresh ≤ 10 s (NFR-P07). */
export default function AdminMap() {
  const { t } = useI18n();
  const dash = useQuery({ queryKey: ['admin', 'dashboard'], queryFn: () => api<Dash>('/admin/dashboard'), refetchInterval: 10_000 });
  const cases = useQuery({ queryKey: ['admin', 'map'], queryFn: () => api<{ data: MapCase[] }>('/admin/map/cases'), refetchInterval: 10_000 });
  const facs = useQuery({ queryKey: ['admin', 'facilities'], queryFn: () => api<{ data: MapFacility[] }>('/facilities') });
  const d = dash.data;
  const tile = (label: string, n: number | undefined, tone: string) => (
    <Card><p className="text-sm text-ink-muted">{label}</p><p className={`text-5xl font-bold ${tone}`}>{n ?? '—'}</p></Card>
  );
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">{t('admin.title')}</h1>
      {!d ? <Skeleton lines={1} /> : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {tile(t('admin.openEmergencies'), d.tiles.openEmergencies, 'text-emergency')}
          {tile(t('admin.referralsWaiting'), d.tiles.referralsWaiting, 'text-ink')}
          {tile(t('admin.closedToday'), d.tiles.casesClosedToday, 'text-success')}
          {tile(t('admin.notUpdated'), d.tiles.facilitiesNotUpdated, 'text-ink')}
        </div>
      )}
      <DistrictMap cases={cases.data?.data ?? []} facilities={facs.data?.data ?? []} />
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <h2 className="mb-2 text-lg font-semibold">{t('admin.stuck')}</h2>
          {!d?.stuck.length ? <p className="text-ink-muted">✔ {t('admin.noStuck')}</p> : (
            <ul className="divide-y">
              {d.stuck.map((s) => (
                <li key={`${s.caseId}-${s.reason}`} className="flex items-center gap-2 py-2">
                  <span className="font-semibold">{s.shortCode}</span>
                  <StatusChip tone="problem" label={ESCALATION_REASON[s.reason] ?? s.reason} />
                  <span className="ml-auto text-sm text-ink-muted">{Math.round(s.ageS / 60)} min</span>
                </li>
              ))}
            </ul>
          )}
          <Link href="/admin/escalations" className="mt-2 inline-block font-semibold text-primary underline">{t('nav.escalations')} →</Link>
        </Card>
        <Card>
          <h2 className="mb-2 text-lg font-semibold">{t('admin.weekly')}</h2>
          <dl className="grid grid-cols-3 gap-2 text-center">
            <div><dt className="text-sm text-ink-muted">{t('admin.medianAccept')}</dt><dd className="text-2xl font-bold">{d?.weekly.medianSosToAcceptS != null ? `${Math.round(d.weekly.medianSosToAcceptS / 60)} min` : '—'}</dd></div>
            <div><dt className="text-sm text-ink-muted">{t('admin.medianArrival')}</dt><dd className="text-2xl font-bold">{d?.weekly.medianSosToArrivalMin != null ? `${d.weekly.medianSosToArrivalMin} min` : '—'}</dd></div>
            <div><dt className="text-sm text-ink-muted">{t('reports.closed72')}</dt><dd className="text-2xl font-bold">{d?.weekly.closedWithin72hPct != null ? `${d.weekly.closedWithin72hPct}%` : '—'}</dd></div>
          </dl>
        </Card>
      </div>
    </div>
  );
}
