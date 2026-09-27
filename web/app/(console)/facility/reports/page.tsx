'use client';

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Card, Skeleton } from '@/components/ui';
import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import { useSession } from '@/lib/session';
import { DECLINE_REASONS } from '@/lib/vocab';

type Stats = { offersReceived: number; accepted: number; timedOut: number; declinedByReason: Record<string, number>;
               medianResponseS: number | null; arrivals: number; closedWithin72hPct: number | null };

export default function ReportsPage() {
  const { t, lang } = useI18n();
  const { user } = useSession();
  const fid = user?.facilityIds?.[0];
  const [period, setPeriod] = useState<'week' | 'month'>('week');
  const q = useQuery({ queryKey: ['stats', fid, period], queryFn: () => api<Stats>(`/facilities/${fid}/stats?period=${period}`), enabled: !!fid });
  const s = q.data;
  const tile = (label: string, value: string | number | null) => (
    <Card><p className="text-sm text-ink-muted">{label}</p><p className="text-4xl font-bold">{value ?? '—'}</p></Card>
  );
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-2xl font-bold">{t('nav.reports')}</h1>
        <select value={period} onChange={(e) => setPeriod(e.target.value as 'week' | 'month')} aria-label={t('reports.period')}
          className="ml-auto min-h-[44px] rounded-lg border-2 px-3">
          <option value="week">{t('reports.week')}</option><option value="month">{t('reports.month')}</option>
        </select>
      </div>
      {!s ? <Skeleton /> : (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {tile(t('reports.offers'), s.offersReceived)}
            {tile(t('reports.accepted'), s.accepted)}
            {tile(t('reports.medianResponse'), s.medianResponseS != null ? `${Math.round(s.medianResponseS / 60)} min` : null)}
            {tile(t('reports.closed72'), s.closedWithin72hPct != null ? `${s.closedWithin72hPct}%` : null)}
          </div>
          <Card>
            <h2 className="mb-2 font-semibold">{t('reports.declines')}</h2>
            <ul className="space-y-1">
              {DECLINE_REASONS.map((r) => (
                <li key={r.code} className="flex justify-between"><span>{lang === 'hi' ? r.hi : r.en}</span><span className="font-bold">{s.declinedByReason[r.code] ?? 0}</span></li>
              ))}
              <li className="flex justify-between"><span>{t('reports.timedOut')}</span><span className="font-bold">{s.timedOut}</span></li>
            </ul>
          </Card>
        </>
      )}
    </div>
  );
}
