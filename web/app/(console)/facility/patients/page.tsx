'use client';

import { useRouter } from 'next/navigation';
import { useState, type FormEvent } from 'react';
import { Button, Card, ErrorNote } from '@/components/ui';
import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';

/** Web finds patients by short code or through a case — names are encrypted on the server (schema R8, D14). */
export default function PatientsPage() {
  const { t } = useI18n();
  const router = useRouter();
  const [code, setCode] = useState('');
  const [err, setErr] = useState<unknown>(null);
  async function find(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    try {
      const p = await api<{ id: string }>(`/patients/by-code/${encodeURIComponent(code.trim().toUpperCase())}`);
      router.push(`/facility/patient/${p.id}`);
    } catch (x) {
      setErr(x);
    }
  }
  return (
    <div className="max-w-xl space-y-4">
      <h1 className="text-2xl font-bold">{t('patients.title')}</h1>
      <Card>
        <form onSubmit={find} className="flex gap-2">
          <input value={code} onChange={(e) => setCode(e.target.value)} maxLength={6} aria-label={t('patients.code')}
            placeholder="K7M2QX" className="min-h-[48px] flex-1 rounded-lg border-2 px-3 text-lg uppercase tracking-widest" />
          <Button type="submit" disabled={code.trim().length !== 6}>{t('patients.find')}</Button>
        </form>
        <p className="mt-3 text-sm text-ink-muted">{t('patients.hint')}</p>
      </Card>
      <ErrorNote error={err} />
    </div>
  );
}
