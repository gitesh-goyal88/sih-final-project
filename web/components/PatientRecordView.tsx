'use client';

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Button, Card, CaseStatusChip, ConfirmDialog, ErrorNote, Skeleton, StatusChip } from '@/components/ui';
import { api, ProblemError } from '@/lib/api';
import { useI18n } from '@/lib/i18n';

type Entry = {
  id: string; kind: string; authorRole: string; authorName?: string | null; recordedAt: string; highRisk: boolean;
  vitals?: Record<string, number | string | null> | null; symptoms?: { code: string; present: boolean }[] | null;
  notes?: string | null; riskFlags: { ruleCode: string; evaluatedBy: string }[]; superseded: boolean; enteredInError: boolean;
};
type Record_ = {
  patient: { id: string; name?: string; shortCode?: string; sex: string; ageYears?: number; bloodGroup?: string;
             cohorts: { cohort: string; eddDate?: string | null }[]; highRisk: boolean };
  summary: { highRisk: boolean; riskReasons: string[]; latestVitals?: Record<string, number> | null;
             conditions: { code: string }[]; medications: { medicineName: string; frequencyText?: string }[];
             openCases: { id: string; shortCode: string; status: string }[]; activeCarePlan?: { nextVisitOn?: string } | null };
  entries: Entry[];
};

const VITAL_LABEL: Record<string, string> = {
  bpSystolic: 'BP sys', bpDiastolic: 'BP dia', pulseBpm: 'Pulse', respRate: 'Resp', spo2Pct: 'SpO₂ %', tempC: 'Temp °C',
  weightKg: 'Weight kg', hbGDl: 'Hb g/dL', rbsMgDl: 'RBS', gestationWeeks: 'Weeks', fetalHrBpm: 'Fetal HR', heightCm: 'Height', muacCm: 'MUAC',
};

/** PRD PatientRecordView: longitudinal history, screening data, referral trail. Every read is audited server-side. */
export function PatientRecordView({ patientId }: { patientId: string }) {
  const { t } = useI18n();
  const [grant, setGrant] = useState<string | null>(null);
  const [askGlass, setAskGlass] = useState(false);
  const rec = useQuery({
    queryKey: ['record', patientId, grant],
    queryFn: () => api<Record_>(`/patients/${patientId}/record`, { headers: grant ? { 'X-Break-Glass': grant } : {} }),
    retry: false,
  });
  if (rec.isLoading) return <Skeleton />;
  if (rec.error) {
    const notFound = rec.error instanceof ProblemError && rec.error.code === 'NOT_FOUND';
    return (
      <div className="space-y-3">
        <ErrorNote error={notFound ? { title: t('record.noAccess') } : rec.error} />
        {notFound && <Button variant="secondary" onClick={() => setAskGlass(true)}>{t('record.breakGlass')}</Button>}
        <BreakGlass open={askGlass} patientId={patientId} onClose={() => setAskGlass(false)} onGranted={(g) => { setGrant(g); setAskGlass(false); }} />
      </div>
    );
  }
  const r = rec.data!;
  return (
    <div className="space-y-4">
      <Card>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-bold">{r.patient.name}</h1>
          <span className="text-ink-muted">{r.patient.ageYears ?? '?'} · {r.patient.sex} · {r.patient.shortCode}</span>
          {r.summary.highRisk && <StatusChip tone="problem" label={t('record.highRisk')} />}
          {r.patient.cohorts.map((c) => <StatusChip key={c.cohort} tone="info" label={t(`cohort.${c.cohort}`)} />)}
          {grant && <StatusChip tone="waiting" label={t('record.breakGlassActive')} />}
        </div>
        <dl className="mt-3 grid gap-2 sm:grid-cols-3">
          <div><dt className="text-sm text-ink-muted">{t('transit.blood')}</dt><dd>{r.patient.bloodGroup ?? '—'}</dd></div>
          <div><dt className="text-sm text-ink-muted">{t('transit.conditions')}</dt><dd>{r.summary.conditions.map((c) => c.code).join(', ') || '—'}</dd></div>
          <div><dt className="text-sm text-ink-muted">{t('transit.medicines')}</dt><dd>{r.summary.medications.map((m) => m.medicineName).join(', ') || '—'}</dd></div>
        </dl>
        {r.summary.riskReasons.length > 0 && <p className="mt-2 text-emergency">⚠ {r.summary.riskReasons.join(', ')}</p>}
      </Card>
      <Card>
        <h2 className="mb-2 text-lg font-semibold">{t('record.referrals')}</h2>
        {r.summary.openCases.length === 0 ? <p className="text-ink-muted">—</p> : (
          <ul className="space-y-1">{r.summary.openCases.map((c) => <li key={c.id} className="flex gap-2">{c.shortCode} <CaseStatusChip status={c.status} /></li>)}</ul>
        )}
      </Card>
      <Card>
        <h2 className="mb-2 text-lg font-semibold">{t('record.timeline')}</h2>
        <ol className="space-y-3">
          {r.entries.map((e) => (
            <li key={e.id} className={`rounded-lg border p-3 ${e.superseded || e.enteredInError ? 'opacity-60' : ''}`}>
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="font-semibold">{t(`entry.${e.kind}`)}</span>
                <time>{new Date(e.recordedAt).toLocaleString()}</time>
                <span className="text-ink-muted">· {e.authorName ?? e.authorRole}</span>
                {e.highRisk && <StatusChip tone="problem" label={t('record.highRisk')} />}
                {e.superseded && <StatusChip tone="offline" label={t('record.corrected')} />}
                {e.enteredInError && <StatusChip tone="offline" label={t('record.enteredInError')} />}
              </div>
              {e.vitals && (
                <p className="mt-1">{Object.entries(e.vitals).filter(([, v]) => v != null && v !== '').map(([k, v]) => `${VITAL_LABEL[k] ?? k} ${v}`).join(' · ')}</p>
              )}
              {e.symptoms && e.symptoms.length > 0 && (
                <p className="mt-1 text-sm">{e.symptoms.map((s) => `${s.present ? '✔' : '✖'} ${s.code}`).join('  ')}</p>
              )}
              {e.notes && <p className="mt-1 text-sm">{e.notes}</p>}
            </li>
          ))}
        </ol>
      </Card>
    </div>
  );
}

function BreakGlass({ open, patientId, onClose, onGranted }: { open: boolean; patientId: string; onClose: () => void; onGranted: (g: string) => void }) {
  const { t } = useI18n();
  const [reason, setReason] = useState('');
  const [err, setErr] = useState<unknown>(null);
  async function submit() {
    try {
      const r = await api<{ grantId: string }>('/break-glass', { body: { patientId, reason } });
      onGranted(r.grantId);
    } catch (e) {
      setErr(e);
    }
  }
  return (
    <ConfirmDialog open={open} title={t('record.breakGlass')} onClose={onClose}>
      <p className="mb-3 text-sm">{t('record.breakGlassWarn')}</p>
      <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3} className="w-full rounded-lg border-2 p-2"
        aria-label={t('record.breakGlassReason')} placeholder={t('record.breakGlassReason')} />
      <ErrorNote error={err} />
      <div className="mt-3 flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose}>{t('cancel')}</Button>
        <Button variant="danger" disabled={reason.trim().length < 10} onClick={() => void submit()}>{t('record.breakGlassConfirm')}</Button>
      </div>
    </ConfirmDialog>
  );
}
