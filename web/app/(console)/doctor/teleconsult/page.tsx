'use client';

import { useMutation, useQuery } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { Button, EmptyState, ErrorNote, Skeleton, StatusChip } from '@/components/ui';
import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import { mmss } from '@/lib/time';

type Session = { id: string; status: string; reasonCode: string; requestedAt: string; waitingS: number;
                 patient: { firstName: string; ageYears?: number; sex: string; shortCode?: string }; ashaName?: string };

/** UI-UX §7.3 teleconsult queue: patient, ASHA, reason, waiting time, "Start call". */
export default function TeleconsultQueue() {
  const { t } = useI18n();
  const router = useRouter();
  const q = useQuery({
    queryKey: ['teleconsults', 'queue'],
    queryFn: () => api<{ data: Session[] }>('/teleconsults?status=requested,accepted,in_call,async'),
    refetchInterval: 15_000,
  });
  const accept = useMutation({
    mutationFn: async (s: Session) => {
      if (s.status === 'requested') await api(`/teleconsults/${s.id}/commands`, { body: { command: 'Accept' } });
      return s.id;
    },
    onSuccess: (id) => router.push(`/doctor/teleconsult/${id}`),
  });
  const list = q.data?.data ?? [];
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">{t('tele.queue')}</h1>
      {q.isLoading ? <Skeleton /> : list.length === 0 ? <EmptyState title={t('tele.empty')} /> : (
        <table className="w-full overflow-hidden rounded-xl border bg-white text-left">
          <thead className="bg-primary-tint text-sm">
            <tr><th className="p-3">{t('inbox.patient')}</th><th className="p-3">ASHA</th><th className="p-3">{t('tele.reason')}</th>
              <th className="p-3">{t('inbox.waiting')}</th><th className="p-3">{t('inbox.action')}</th></tr>
          </thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id} className="border-t">
                <td className="p-3 font-semibold">{s.patient.firstName}, {s.patient.ageYears ?? '?'} {s.patient.sex}
                  <div className="text-sm font-normal text-ink-muted">{s.patient.shortCode}</div></td>
                <td className="p-3">{s.ashaName}</td>
                <td className="p-3">{t(`teleReason.${s.reasonCode}`)}</td>
                <td className="p-3"><StatusChip tone={s.waitingS > 600 ? 'problem' : 'waiting'} label={mmss(s.waitingS)} /></td>
                <td className="p-3"><Button onClick={() => accept.mutate(s)} disabled={accept.isPending}>🎧 {s.status === 'requested' ? t('tele.start') : t('tele.open')}</Button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <ErrorNote error={accept.error} />
    </div>
  );
}
