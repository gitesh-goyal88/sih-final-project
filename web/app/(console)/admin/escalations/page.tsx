'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Button, Card, CaseStatusChip, CategoryLabel, ConfirmDialog, EmptyState, ErrorNote, Skeleton, StatusChip } from '@/components/ui';
import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import { ESCALATION_REASON } from '@/lib/vocab';

type Esc = { id: string; caseId: string; level: number; reason: string; raisedAt: string; acknowledgedAt?: string | null; note?: string | null;
             case: { shortCode: string; status: string; emergencyCategory?: string | null; verified: boolean; currentFacility?: { name: string } | null } };
const RESOLUTIONS = ['facility_reassigned', 'transport_arranged', 'called_family', 'false_alarm', 'other'] as const;

/** EscalationQueue (US12): stuck cases with the responsible party and actions — acknowledge, resolve, reassign, verify. */
export default function EscalationsPage() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ['admin', 'escalations'], queryFn: () => api<{ data: Esc[] }>('/admin/escalations?status=open'), refetchInterval: 10_000 });
  const facs = useQuery({ queryKey: ['admin', 'facilities'], queryFn: () => api<{ data: { id: string; name: string; status: string }[] }>('/facilities') });
  const [resolveFor, setResolveFor] = useState<Esc | null>(null);
  const [reassignFor, setReassignFor] = useState<Esc | null>(null);
  const done = () => void qc.invalidateQueries({ queryKey: ['admin'] });
  const ack = useMutation({ mutationFn: (e: Esc) => api(`/admin/escalations/${e.id}/acknowledge`, { method: 'POST' }), onSuccess: done });
  const verify = useMutation({
    mutationFn: (e: Esc) => api(`/cases/${e.caseId}/commands`, { body: { command: 'VerifyCase', args: { method: 'callback' } } }), onSuccess: done });
  const dispatch = useMutation({
    mutationFn: (e: Esc) => api(`/cases/${e.caseId}/commands`, { body: { command: 'DispatchUnverified', args: { note: 'Could not reach caller; dispatch anyway' } } }), onSuccess: done });
  const resolve = useMutation({
    mutationFn: (v: { e: Esc; resolution: string; note: string }) => api(`/admin/escalations/${v.e.id}/resolve`, { body: { resolution: v.resolution, note: v.note || null } }),
    onSuccess: () => { setResolveFor(null); done(); } });
  const reassign = useMutation({
    mutationFn: (v: { e: Esc; facilityId: string; reason: string }) => api(`/admin/cases/${v.e.caseId}/reassign`, { body: { facilityId: v.facilityId, reason: v.reason } }),
    onSuccess: () => { setReassignFor(null); done(); } });

  const list = q.data?.data ?? [];
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">{t('nav.escalations')}</h1>
      {q.isLoading ? <Skeleton /> : list.length === 0 ? <EmptyState title={t('admin.noStuck')} /> : list.map((e) => (
        <Card key={e.id}>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-lg font-bold">{e.case.shortCode}</span>
            <CategoryLabel code={e.case.emergencyCategory} />
            <CaseStatusChip status={e.case.status} />
            <StatusChip tone="problem" label={`L${e.level} · ${ESCALATION_REASON[e.reason] ?? e.reason}`} />
            {e.acknowledgedAt && <StatusChip tone="done" label={t('esc.acknowledged')} />}
            <span className="ml-auto text-sm text-ink-muted">{Math.round((Date.now() - new Date(e.raisedAt).getTime()) / 60000)} min</span>
          </div>
          {e.note && <p className="mt-1 text-sm">{e.note}</p>}
          {e.case.currentFacility && <p className="mt-1 text-sm">{e.case.currentFacility.name}</p>}
          <div className="mt-3 flex flex-wrap gap-2">
            {!e.acknowledgedAt && <Button variant="secondary" onClick={() => ack.mutate(e)}>{t('esc.acknowledge')}</Button>}
            {e.reason === 'unverified_sms' && <>
              <Button onClick={() => verify.mutate(e)}>{t('esc.verified')}</Button>
              <Button variant="secondary" onClick={() => dispatch.mutate(e)}>{t('esc.dispatchAnyway')}</Button>
            </>}
            {['cascade_exhausted', 'no_capable_facility'].includes(e.reason) && ['created', 'matched'].includes(e.case.status) &&
              <Button onClick={() => setReassignFor(e)}>{t('esc.reassign')}</Button>}
            <Button variant="secondary" onClick={() => setResolveFor(e)}>{t('esc.resolve')}</Button>
          </div>
        </Card>
      ))}
      <ErrorNote error={ack.error ?? verify.error ?? dispatch.error ?? resolve.error ?? reassign.error} />
      <ResolveDialog e={resolveFor} onClose={() => setResolveFor(null)} onSubmit={(resolution, note) => resolveFor && resolve.mutate({ e: resolveFor, resolution, note })} />
      <ReassignDialog e={reassignFor} facilities={facs.data?.data ?? []} onClose={() => setReassignFor(null)}
        onSubmit={(facilityId, reason) => reassignFor && reassign.mutate({ e: reassignFor, facilityId, reason })} />
    </div>
  );
}

function ResolveDialog({ e, onClose, onSubmit }: { e: Esc | null; onClose: () => void; onSubmit: (r: string, n: string) => void }) {
  const { t } = useI18n();
  const [r, setR] = useState<string>('called_family');
  const [n, setN] = useState('');
  return (
    <ConfirmDialog open={!!e} title={t('esc.resolve')} onClose={onClose}>
      <select value={r} onChange={(x) => setR(x.target.value)} className="min-h-[48px] w-full rounded-lg border-2 px-3" aria-label={t('esc.resolution')}>
        {RESOLUTIONS.map((x) => <option key={x} value={x}>{t(`resolution.${x}`)}</option>)}
      </select>
      <input value={n} onChange={(x) => setN(x.target.value)} placeholder={t('close.notes')} className="mt-3 min-h-[48px] w-full rounded-lg border-2 px-3" />
      <div className="mt-4 flex justify-end gap-2"><Button variant="secondary" onClick={onClose}>{t('cancel')}</Button><Button onClick={() => onSubmit(r, n)}>{t('save')}</Button></div>
    </ConfirmDialog>
  );
}

function ReassignDialog({ e, facilities, onClose, onSubmit }: { e: Esc | null; facilities: { id: string; name: string; status: string }[];
  onClose: () => void; onSubmit: (f: string, r: string) => void }) {
  const { t } = useI18n();
  const [f, setF] = useState('');
  const [r, setR] = useState('');
  return (
    <ConfirmDialog open={!!e} title={t('esc.reassign')} onClose={onClose}>
      <select value={f} onChange={(x) => setF(x.target.value)} className="min-h-[48px] w-full rounded-lg border-2 px-3" aria-label={t('esc.facility')}>
        <option value="">—</option>
        {facilities.filter((x) => x.status === 'open').map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
      </select>
      <input value={r} onChange={(x) => setR(x.target.value)} placeholder={t('esc.reasonMin')} className="mt-3 min-h-[48px] w-full rounded-lg border-2 px-3" />
      <div className="mt-4 flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose}>{t('cancel')}</Button>
        <Button disabled={!f || r.trim().length < 10} onClick={() => onSubmit(f, r)}>{t('esc.reassign')}</Button>
      </div>
    </ConfirmDialog>
  );
}
