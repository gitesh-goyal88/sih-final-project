'use client';

import Link from 'next/link';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Button, Card, CaseStatusChip, CategoryLabel, ConfirmDialog, EmptyState, ErrorNote, PersonCallCard, Skeleton, StatusChip } from '@/components/ui';
import { api, type CaseDetail, type CaseOut } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import { ago } from '@/lib/time';
import { uuidv7 } from '@/lib/uuid';

type Packet = {
  shortCode: string; category?: string; neededCapabilities: string[];
  patient?: { name?: string; ageYears?: number; sex?: string; bloodGroup?: string; shortCode?: string } | null;
  latestVitals?: Record<string, number | string> | null; riskReasons: string[]; conditions: string[]; medications: string[];
  ashaNotes?: string | null; ashaContact?: { name?: string; phone?: string } | null;
  currentLeg?: { legOrder: number; custodianFirstName?: string; etaMin?: number } | null;
};

const OUTCOMES = ['treated_discharged', 'admitted', 'referred_onward', 'left_against_advice', 'death', 'other'] as const;
const MEDICINES = ['IFA', 'Calcium', 'Paracetamol', 'Amoxicillin', 'ORS', 'Zinc', 'Metformin', 'Amlodipine'];
const VISIT_TASKS = ['pnc_visit', 'anc_visit', 'newborn_check', 'bp_check', 'sugar_check', 'referral_followup'] as const;

export default function InTransitPage() {
  const { t } = useI18n();
  const cases = useQuery({
    queryKey: ['cases', 'facility-active'],
    queryFn: () => api<{ data: CaseOut[] }>('/cases?status=accepted&status=transport_assigned&status=in_transit&status=arrived_seen'),
    refetchInterval: 20_000,
  });
  const list = cases.data?.data ?? [];
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">{t('transit.title')}</h1>
      {cases.isLoading ? <Skeleton /> : list.length === 0 ? <EmptyState title={t('transit.empty')} /> : (
        <div className="grid gap-4 xl:grid-cols-2">{list.map((c) => <TransitCard key={c.id} c={c} />)}</div>
      )}
    </div>
  );
}

function TransitCard({ c }: { c: CaseOut }) {
  const { t, lang } = useI18n();
  const qc = useQueryClient();
  const [showPacket, setShowPacket] = useState(false);
  const [closing, setClosing] = useState(false);
  const [regNo, setRegNo] = useState('');
  const detail = useQuery({ queryKey: ['case', c.id], queryFn: () => api<CaseDetail>(`/cases/${c.id}`) });
  const packet = useQuery({ queryKey: ['case', c.id, 'packet'], queryFn: () => api<Packet>(`/cases/${c.id}/handoff-packet`), enabled: showPacket });
  const act = useMutation({
    mutationFn: (body: Record<string, unknown>) => api(`/cases/${c.id}/status`, { body, idem: uuidv7() }),
    onSuccess: () => {
      setClosing(false);
      void qc.invalidateQueries({ queryKey: ['cases'] });
      void qc.invalidateQueries({ queryKey: ['case', c.id] });
    },
  });
  const preReg = useMutation({
    mutationFn: () => api(`/cases/${c.id}/pre-register`, { body: { facilityRegNo: regNo || null } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['case', c.id] }),
  });
  const d = detail.data;
  const leg = d?.legs.find((l) => l.id === c.currentLegId) ?? d?.legs.find((l) => l.status === 'accepted');
  const adm = d?.admission;
  return (
    <Card>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="text-lg font-bold">{c.shortCode}</span>
        <CategoryLabel code={c.emergencyCategory} />
        <span className="ml-auto"><CaseStatusChip status={c.status} /></span>
      </div>
      {c.legProgress && <p className="mb-2 text-sm">{t('transit.leg', { x: c.legProgress.current, y: c.legProgress.total })}</p>}
      {leg?.custodian && <PersonCallCard name={leg.custodian.firstName} role={t('transit.driver')} phone={leg.custodian.phone ?? leg.custodian.phoneMasked} />}
      <p className="mt-2 text-lg">{c.etaMin ? t('transit.eta', { n: c.etaMin }) : t('transit.etaUnknown')}</p>
      {adm?.preRegisteredAt
        ? <p className="mt-2"><StatusChip tone="done" label={`${t('transit.preRegistered')} ${adm.facilityRegNo ?? ''}`} /></p>
        : c.status !== 'arrived_seen' && (
          <div className="mt-3 flex gap-2">
            <input value={regNo} onChange={(e) => setRegNo(e.target.value)} placeholder={t('transit.regNo')} aria-label={t('transit.regNo')}
              className="min-h-[48px] flex-1 rounded-lg border-2 px-3" />
            <Button variant="secondary" onClick={() => preReg.mutate()} disabled={preReg.isPending}>{t('transit.preRegister')}</Button>
          </div>
        )}
      <button onClick={() => setShowPacket((s) => !s)} aria-expanded={showPacket} className="mt-3 font-semibold text-primary underline">
        {showPacket ? '▾' : '▸'} {t('transit.handoffPacket')}
      </button>
      {showPacket && packet.data && (
        <div className="mt-2 space-y-2 rounded-lg bg-primary-tint p-3 text-sm">
          {packet.data.patient && (
            <p className="font-semibold">
              {packet.data.patient.name}, {packet.data.patient.ageYears ?? '?'} {packet.data.patient.sex} · {t('transit.blood')} {packet.data.patient.bloodGroup ?? '—'}
              {' · '}<Link className="underline" href={`/facility/patient/${d?.case.patientId}`}>{t('transit.fullRecord')}</Link>
            </p>
          )}
          {packet.data.latestVitals && (
            <p>{t('transit.vitals')}: {Object.entries(packet.data.latestVitals).filter(([k]) => k !== 'recordedAt').map(([k, v]) => `${k} ${v}`).join(', ')}
              {' '}({ago(String(packet.data.latestVitals.recordedAt ?? ''), lang)})</p>
          )}
          {packet.data.riskReasons.length > 0 && <p className="text-emergency">⚠ {packet.data.riskReasons.join(', ')}</p>}
          <p>{t('transit.conditions')}: {packet.data.conditions.join(', ') || '—'} · {t('transit.medicines')}: {packet.data.medications.join(', ') || '—'}</p>
          {packet.data.ashaNotes && <p>{t('transit.ashaNotes')}: {packet.data.ashaNotes}</p>}
          {packet.data.ashaContact && <PersonCallCard name={packet.data.ashaContact.name} role="ASHA" phone={packet.data.ashaContact.phone} />}
        </div>
      )}
      <div className="mt-4 flex flex-wrap gap-2">
        {c.status !== 'arrived_seen' && (
          <Button variant="success" onClick={() => act.mutate({ action: 'arrived' })} disabled={act.isPending}>✔ {t('transit.arrived')}</Button>
        )}
        {c.status === 'arrived_seen' && !adm?.seenAt && (
          <Button onClick={() => act.mutate({ action: 'seen' })} disabled={act.isPending}>{t('transit.seen')}</Button>
        )}
        {c.status === 'arrived_seen' && adm?.seenAt && (
          <Button onClick={() => setClosing(true)}>{t('transit.close')}</Button>
        )}
      </div>
      <ErrorNote error={act.error ?? preReg.error} />
      <CloseDialog open={closing} onClose={() => setClosing(false)} busy={act.isPending} onSubmit={(body) => act.mutate(body)} />
    </Card>
  );
}

function CloseDialog({ open, onClose, onSubmit, busy }: { open: boolean; onClose: () => void; busy: boolean; onSubmit: (b: Record<string, unknown>) => void }) {
  const { t } = useI18n();
  const [outcome, setOutcome] = useState<(typeof OUTCOMES)[number]>('treated_discharged');
  const [notes, setNotes] = useState('');
  const [meds, setMeds] = useState<string[]>([]);
  const [visitDays, setVisitDays] = useState<number>(3);
  const [visitTask, setVisitTask] = useState<(typeof VISIT_TASKS)[number]>('pnc_visit');
  const [onwardNeed, setOnwardNeed] = useState('');
  function submit() {
    const items = [
      ...meds.map((m, i) => ({ id: uuidv7(), kind: 'medicine', medicineName: m, frequencyText: '1 daily', durationDays: 30, sortOrder: i + 1 })),
      { id: uuidv7(), kind: 'visit', dueOffsetDays: visitDays, taskType: visitTask, sortOrder: 90 },
    ];
    const next = new Date(Date.now() + visitDays * 86400_000).toISOString().slice(0, 10);
    onSubmit({
      action: 'close', outcome,
      outcomeEntry: { id: uuidv7(), kind: 'referral_outcome', notes: notes || null },
      carePlan: { id: uuidv7(), summary: notes || null, nextVisitOn: next, items },
      ...(outcome === 'referred_onward' ? { onwardReferral: { id: uuidv7(), neededCapabilities: onwardNeed.split(',').map((s) => s.trim()).filter(Boolean),
        referralReason: notes || 'Onward referral', needsTransport: true } } : {}),
    });
  }
  return (
    <ConfirmDialog open={open} title={t('close.title')} onClose={onClose}>
      <div className="space-y-4">
        <label className="block">
          <span className="font-semibold">{t('close.outcome')}</span>
          <select value={outcome} onChange={(e) => setOutcome(e.target.value as typeof outcome)} className="mt-1 min-h-[48px] w-full rounded-lg border-2 px-3">
            {OUTCOMES.map((o) => <option key={o} value={o}>{t(`outcome.${o}`)}</option>)}
          </select>
        </label>
        {outcome === 'referred_onward' && (
          <label className="block">
            <span className="font-semibold">{t('close.onwardNeeds')}</span>
            <input value={onwardNeed} onChange={(e) => setOnwardNeed(e.target.value)} placeholder="icu, ventilator" className="mt-1 min-h-[48px] w-full rounded-lg border-2 px-3" />
          </label>
        )}
        <label className="block">
          <span className="font-semibold">{t('close.notes')} <span className="font-normal text-ink-muted">({t('optional')})</span></span>
          <textarea value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={1000} className="mt-1 w-full rounded-lg border-2 p-2" rows={3} />
        </label>
        <fieldset>
          <legend className="font-semibold">{t('close.medicines')}</legend>
          <div className="mt-1 flex flex-wrap gap-2">
            {MEDICINES.map((m) => (
              <label key={m} className={`flex min-h-[44px] items-center gap-2 rounded-lg border px-3 ${meds.includes(m) ? 'border-primary bg-primary-tint' : ''}`}>
                <input type="checkbox" checked={meds.includes(m)} onChange={(e) => setMeds((x) => e.target.checked ? [...x, m] : x.filter((y) => y !== m))} />{m}
              </label>
            ))}
          </div>
        </fieldset>
        <fieldset>
          <legend className="font-semibold">{t('close.nextVisit')}</legend>
          <div className="mt-1 flex flex-wrap gap-2">
            {[3, 7, 14, 30].map((d) => (
              <button key={d} type="button" onClick={() => setVisitDays(d)} aria-pressed={visitDays === d}
                className={`min-h-[44px] rounded-lg border px-3 ${visitDays === d ? 'border-primary bg-primary text-white' : ''}`}>{t('close.inDays', { n: d })}</button>
            ))}
          </div>
          <select value={visitTask} onChange={(e) => setVisitTask(e.target.value as typeof visitTask)} aria-label={t('close.visitType')}
            className="mt-2 min-h-[48px] w-full rounded-lg border-2 px-3">
            {VISIT_TASKS.map((v) => <option key={v} value={v}>{t(`task.${v}`)}</option>)}
          </select>
        </fieldset>
        <p className="text-sm text-ink-muted">{t('close.next')}</p>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>{t('cancel')}</Button>
          <Button disabled={busy} onClick={submit}>{t('close.confirm')}</Button>
        </div>
      </div>
    </ConfirmDialog>
  );
}
