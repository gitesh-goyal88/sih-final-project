'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Button, Card, ErrorNote, Skeleton, StatusChip } from '@/components/ui';
import { api, ProblemError, type Facility } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import { useSession } from '@/lib/session';
import { ago } from '@/lib/time';
import { uuidv7 } from '@/lib/uuid';

/**
 * Capability panel (UI-UX §7.2, API-Guide §6.7): must be quick to update — matching is only as good as this data.
 * Toggles for declared capabilities, beds stepper, prominent "Last updated", 6-hour reminder,
 * and the one-tap "Everything is still correct" (confirmAll). Online only; If-Match version guard.
 */
export default function CapabilityPage() {
  const { t, lang } = useI18n();
  const { user } = useSession();
  const qc = useQueryClient();
  const fid = user?.facilityIds?.[0];
  const facility = useQuery({ queryKey: ['facility', fid, 'full'], queryFn: () => api<Facility>(`/facilities/${fid}`), enabled: !!fid });
  const catalog = useQuery({ queryKey: ['catalog'], queryFn: () => api<{ capabilities: { code: string; labelEn: string; labelHi: string; group: string }[] }>('/reference/catalog'), staleTime: 3600_000 });
  const [caps, setCaps] = useState<Record<string, boolean>>({});
  const [beds, setBeds] = useState(0);
  const [status, setStatus] = useState<'open' | 'full' | 'closed'>('open');
  useEffect(() => {
    if (!facility.data) return;
    setCaps(Object.fromEntries(facility.data.capabilities.map((c) => [c.code, c.available])));
    setBeds(facility.data.bedsAvailable);
    setStatus(facility.data.status as typeof status);
  }, [facility.data]);

  const save = useMutation({
    mutationFn: (confirmAll: boolean) => api<Facility>(`/facilities/${fid}`, {
      method: 'PATCH', idem: uuidv7(), headers: { 'If-Match': String(facility.data?.version) },
      body: confirmAll ? { confirmAll: true } : { bedsAvailable: beds, status, capabilities: caps, confirmAll: false },
    }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['facility'] }),
  });

  if (!fid) return null;
  if (facility.isLoading || !facility.data) return <Skeleton />;
  const f = facility.data;
  const hours = (Date.now() - new Date(f.capabilityUpdatedAt).getTime()) / 3600_000;
  const label = (code: string) => {
    const c = catalog.data?.capabilities.find((x) => x.code === code);
    return c ? (lang === 'hi' ? c.labelHi : c.labelEn) : code;
  };
  return (
    <div className="max-w-3xl space-y-6">
      <h1 className="text-2xl font-bold">{t('capability.title')}</h1>
      <Card className={hours > 6 ? 'border-warning' : ''}>
        <p className="text-xl font-semibold">{t('capability.updated')} {ago(f.capabilityUpdatedAt, lang)}</p>
        {f.stale && <StatusChip tone="problem" label={t('capability.staleLabel')} />}
        {hours > 6 && <p className="mt-2">⏳ {t('capability.reminder')}</p>}
        <Button variant="success" className="mt-3" onClick={() => save.mutate(true)} disabled={save.isPending}>✔ {t('capability.allCorrect')}</Button>
      </Card>
      <Card>
        <h2 className="mb-3 text-lg font-semibold">{t('capability.beds')}</h2>
        <div className="flex items-center gap-3">
          <Button variant="secondary" ariaLabel={t('capability.fewer')} onClick={() => setBeds((b) => Math.max(0, b - 1))}>−</Button>
          <output aria-live="polite" className="w-16 text-center text-4xl font-bold">{beds}</output>
          <Button variant="secondary" ariaLabel={t('capability.more')} onClick={() => setBeds((b) => b + 1)}>+</Button>
          <span className="text-ink-muted">/ {f.bedsTotal ?? '—'}</span>
        </div>
        <fieldset className="mt-4 flex gap-2">
          <legend className="mb-2 font-semibold">{t('capability.status')}</legend>
          {(['open', 'full', 'closed'] as const).map((s) => (
            <label key={s} className={`flex min-h-[48px] items-center gap-2 rounded-lg border px-4 ${status === s ? 'border-primary bg-primary-tint' : ''}`}>
              <input type="radio" name="status" checked={status === s} onChange={() => setStatus(s)} />{t(`facilityStatus.${s}`)}
            </label>
          ))}
        </fieldset>
      </Card>
      <Card>
        <h2 className="mb-3 text-lg font-semibold">{t('capability.services')}</h2>
        <ul className="grid gap-2 sm:grid-cols-2">
          {f.capabilities.map((c) => (
            <li key={c.code}>
              <label className="flex min-h-[56px] cursor-pointer items-center justify-between gap-3 rounded-lg border px-4">
                <span className="font-semibold">{label(c.code)}{c.flaggedForReview && <span className="ml-2 text-sm text-warning">⚑ {t('capability.flagged')}</span>}</span>
                <span className="flex items-center gap-2">
                  <span className={caps[c.code] ? 'text-success' : 'text-offline'}>{caps[c.code] ? `✔ ${t('available')}` : `✖ ${t('notAvailable')}`}</span>
                  <input type="checkbox" role="switch" aria-checked={!!caps[c.code]} checked={!!caps[c.code]}
                    onChange={(e) => setCaps((x) => ({ ...x, [c.code]: e.target.checked }))} className="h-6 w-6" />
                </span>
              </label>
            </li>
          ))}
        </ul>
        <p className="mt-3 text-sm text-ink-muted">{t('capability.declareNote')}</p>
      </Card>
      <Button onClick={() => save.mutate(false)} disabled={save.isPending} className="w-full">{t('save')}</Button>
      {save.isSuccess && <p role="status" className="text-success">✔ {t('capability.saved')}</p>}
      <ErrorNote error={save.error instanceof ProblemError && save.error.code === 'VERSION_MISMATCH' ? { title: t('capability.conflict') } : save.error} />
    </div>
  );
}
