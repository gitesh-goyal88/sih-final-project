'use client';

import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'next/navigation';
import { Suspense, useState, type FormEvent } from 'react';
import { ReferralBuilder } from '@/components/ReferralBuilder';
import { Button, Card, CaseStatusChip, CategoryLabel, ErrorNote } from '@/components/ui';
import { api, type CaseOut } from '@/lib/api';
import { useI18n } from '@/lib/i18n';

function Referrals() {
  const { t } = useI18n();
  const params = useSearchParams();
  const [patientId, setPatientId] = useState<string | null>(params.get('patientId'));
  const [code, setCode] = useState('');
  const [err, setErr] = useState<unknown>(null);
  const [created, setCreated] = useState<string | null>(null);
  const mine = useQuery({ queryKey: ['cases', 'doctor'], queryFn: () => api<{ data: CaseOut[] }>('/cases?status=open') });

  async function find(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    try {
      setPatientId((await api<{ id: string }>(`/patients/by-code/${code.trim().toUpperCase()}`)).id);
    } catch (x) {
      setErr(x);
    }
  }
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">{t('nav.referrals')}</h1>
      {!patientId ? (
        <Card>
          <form onSubmit={find} className="flex gap-2">
            <input value={code} onChange={(e) => setCode(e.target.value)} maxLength={6} placeholder="K7M2QX" aria-label={t('patients.code')}
              className="min-h-[48px] flex-1 rounded-lg border-2 px-3 uppercase tracking-widest" />
            <Button type="submit">{t('patients.find')}</Button>
          </form>
          <ErrorNote error={err} />
        </Card>
      ) : (
        <ReferralBuilder patientId={patientId} teleconsultSessionId={params.get('teleconsultSessionId')}
          onCreated={(_, c) => { setCreated(c); setPatientId(null); void mine.refetch(); }} />
      )}
      {created && <p role="status" className="text-success">✔ {t('ref.created', { code: created })}</p>}
      <section>
        <h2 className="mb-2 text-lg font-semibold">{t('ref.mine')}</h2>
        <ul className="divide-y rounded-xl border bg-white">
          {(mine.data?.data ?? []).map((c) => (
            <li key={c.id} className="flex items-center gap-3 p-3">
              <span className="font-semibold">{c.shortCode}</span><CategoryLabel code={c.emergencyCategory} />
              <span className="text-ink-muted">{c.currentFacility?.name ?? ''}</span>
              <span className="ml-auto"><CaseStatusChip status={c.status} /></span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

export default function ReferralsPage() {
  return <Suspense><Referrals /></Suspense>;
}
