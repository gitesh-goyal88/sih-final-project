'use client';

import { useQuery } from '@tanstack/react-query';
import { Skeleton, StatusChip } from '@/components/ui';
import { api, type Facility } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import { ago } from '@/lib/time';

/** District facility status + capability freshness (R-01: admin freshness report). */
export default function AdminFacilities() {
  const { t, lang } = useI18n();
  const q = useQuery({ queryKey: ['admin', 'facilities', 'full'], queryFn: () => api<{ data: Facility[] }>('/facilities'), refetchInterval: 30_000 });
  if (!q.data) return <Skeleton />;
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">{t('nav.facilities')}</h1>
      <table className="w-full rounded-xl border bg-white text-left">
        <thead className="bg-primary-tint text-sm">
          <tr><th className="p-3">{t('admin.facility')}</th><th className="p-3">{t('capability.status')}</th><th className="p-3">{t('capability.beds')}</th>
            <th className="p-3">{t('capability.updated')}</th><th className="p-3">{t('capability.services')}</th></tr>
        </thead>
        <tbody>
          {q.data.data.map((f) => (
            <tr key={f.id} className="border-t align-top">
              <td className="p-3 font-semibold">{f.name}<div className="text-sm font-normal text-ink-muted">{f.level}</div></td>
              <td className="p-3"><StatusChip tone={f.status === 'open' ? 'done' : f.status === 'full' ? 'waiting' : 'offline'} label={t(`facilityStatus.${f.status}`)} /></td>
              <td className="p-3">{f.bedsAvailable}/{f.bedsTotal ?? '—'}</td>
              <td className="p-3">{f.stale ? <StatusChip tone="problem" label={`${t('capability.staleLabel')} · ${ago(f.capabilityUpdatedAt, lang)}`} /> : ago(f.capabilityUpdatedAt, lang)}</td>
              <td className="p-3 text-sm">{f.capabilities.filter((c) => c.available).map((c) => c.code).join(', ')}
                {f.capabilities.some((c) => c.flaggedForReview) && <div className="text-warning">⚑ {t('capability.flagged')}: {f.capabilities.filter((c) => c.flaggedForReview).map((c) => c.code).join(', ')}</div>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
