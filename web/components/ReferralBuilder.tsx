'use client';

import { useMutation, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Button, Card, ErrorNote, FacilityCapabilityChips, Skeleton, StatusChip } from '@/components/ui';
import { api, type MatchResponse } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import { uuidv7 } from '@/lib/uuid';

type Catalog = { capabilities: { code: string; labelEn: string; labelHi: string; group: string }[] };

/**
 * PRD ReferralBuilder + FacilityMatchList (UI-UX §6.3 / §7.3). Card order: name → capability ✔ → distance/ETA → Send
 * (UI §11). Ranking is capability first, then ETA; stale data is labelled; picking a lower-ranked facility needs a
 * reason (FR-M03). The server re-runs matching when the referral is created (FR-M02).
 */
export function ReferralBuilder({ patientId, teleconsultSessionId, onCreated }: {
  patientId: string; teleconsultSessionId?: string | null; onCreated: (caseId: string, code: string) => void;
}) {
  const { t, lang } = useI18n();
  const [needs, setNeeds] = useState<string[]>([]);
  const [reason, setReason] = useState('');
  const [override, setOverride] = useState('');
  const [chosen, setChosen] = useState<string | null>(null);
  const catalog = useQuery({ queryKey: ['catalog'], queryFn: () => api<Catalog>('/reference/catalog'), staleTime: 3600_000 });
  const labels = Object.fromEntries((catalog.data?.capabilities ?? []).map((c) => [c.code, lang === 'hi' ? c.labelHi : c.labelEn]));
  const match = useQuery({
    queryKey: ['match', patientId, needs.join(',')],
    queryFn: () => api<MatchResponse>(`/facilities/match?patientId=${patientId}&needs=${needs.join(',')}`),
    enabled: needs.length > 0,
  });
  const create = useMutation({
    mutationFn: (facilityId: string) => api<{ case: { id: string; shortCode: string } }>('/cases', { idem: uuidv7(), body: {
      id: uuidv7(), type: 'referral', patientId, neededCapabilities: needs, referralReason: reason || null,
      teleconsultSessionId: teleconsultSessionId ?? null, preferredFacilityId: facilityId,
      overrideReason: override.trim() || null, needsTransport: true,
    } }),
    onSuccess: (r) => onCreated(r.case.id, r.case.shortCode),
  });
  const rows = match.data?.data ?? [];
  const chosenRow = rows.find((r) => r.facility.id === chosen);
  return (
    <div className="space-y-4">
      <Card>
        <h2 className="mb-2 font-semibold">{t('ref.need')}</h2>
        <div className="flex flex-wrap gap-2">
          {(catalog.data?.capabilities ?? []).filter((c) => c.group !== 'staff' || c.code === 'doctor_on_duty').map((c) => (
            <button key={c.code} type="button" aria-pressed={needs.includes(c.code)}
              onClick={() => setNeeds((n) => n.includes(c.code) ? n.filter((x) => x !== c.code) : [...n, c.code])}
              className={`min-h-[44px] rounded-full border px-3 ${needs.includes(c.code) ? 'border-primary bg-primary text-white' : ''}`}>
              {needs.includes(c.code) ? '✔ ' : ''}{lang === 'hi' ? c.labelHi : c.labelEn}
            </button>
          ))}
        </div>
        <label className="mt-3 block">
          <span className="font-semibold">{t('ref.reason')}</span>
          <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={1000} className="mt-1 min-h-[48px] w-full rounded-lg border-2 px-3" />
        </label>
      </Card>
      {needs.length > 0 && (match.isLoading ? <Skeleton /> : (
        <div className="space-y-3">
          {match.data?.noCapableFacility && (
            <p role="alert" className="rounded-lg border border-warning bg-warning/10 p-3">⚠ {t('ref.noCapable')}</p>
          )}
          {rows.slice(0, 3).map((m) => (
            <Card key={m.facility.id} className={chosen === m.facility.id ? 'ring-2 ring-primary' : ''}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-lg font-bold">{m.facility.name}</span>
                <span className="text-ink-muted">#{m.rank}</span>
                <span className="ml-auto text-lg">{m.reasons.distanceKm} km · {m.reasons.etaMin} min{m.reasons.etaEstimated ? ' ~' : ''}</span>
              </div>
              <div className="mt-2"><FacilityCapabilityChips matched={m.reasons.capabilitiesMatched} missing={m.reasons.capabilitiesMissing} labels={labels} /></div>
              <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
                <span>🛏 {m.reasons.beds}</span>
                {m.reasons.stale && <StatusChip tone="waiting" label={t('ref.stale')} />}
                {m.reasons.capabilityUnconfirmed && <StatusChip tone="waiting" label={t('inbox.capabilityUnconfirmed')} />}
                <Button className="ml-auto" onClick={() => (m.rank === 1 ? create.mutate(m.facility.id) : setChosen(m.facility.id))}
                  disabled={create.isPending}>{t('ref.send')}</Button>
              </div>
            </Card>
          ))}
          {chosenRow && chosenRow.rank !== 1 && (
            <Card>
              <label className="block">
                <span className="font-semibold">{t('ref.overrideReason')}</span>
                <input value={override} onChange={(e) => setOverride(e.target.value)} maxLength={300} className="mt-1 min-h-[48px] w-full rounded-lg border-2 px-3" />
              </label>
              <Button className="mt-2" disabled={!override.trim() || create.isPending} onClick={() => create.mutate(chosenRow.facility.id)}>{t('ref.sendAnyway')}</Button>
            </Card>
          )}
          <p className="text-sm text-ink-muted">{t('ref.computed')} {match.data ? new Date(match.data.computedAt).toLocaleTimeString() : ''}</p>
        </div>
      ))}
      <ErrorNote error={create.error} />
    </div>
  );
}
