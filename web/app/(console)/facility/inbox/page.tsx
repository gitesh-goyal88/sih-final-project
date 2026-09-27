'use client';

import Link from 'next/link';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Button, CategoryLabel, ConfirmDialog, EmptyState, ErrorNote, Skeleton, StatusChip } from '@/components/ui';
import { api, ProblemError } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import { useSession } from '@/lib/session';
import { ago, mmss, remainingS, serverOffsetMs } from '@/lib/time';
import { uuidv7 } from '@/lib/uuid';
import { DECLINE_REASONS, OFFER_RESULT } from '@/lib/vocab';

type Inbox = {
  offer: { id: string; caseId: string; expiresAt: string; offeredAt: string; openedAt?: string | null; result: string;
           staleCapability: boolean; capabilityUnconfirmed: boolean; source: string; declineReason?: string | null };
  case: { id: string; shortCode: string; type: string; emergencyCategory?: string | null; verified: boolean;
          neededCapabilities: { code: string }[]; status: string };
  patient: { id?: string; firstName?: string | null; ageYears?: number | null; sex?: string | null; highRisk: boolean; shortCode?: string | null };
  from: { ashaName?: string; villageName?: string; originFacilityName?: string };
  etaMin?: number | null;
  serverTime: string;
};

function beep() {
  try {
    const ctx = new AudioContext();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.frequency.value = 880;
    o.connect(g).connect(ctx.destination);
    g.gain.setValueAtTime(0.2, ctx.currentTime);
    o.start();
    o.stop(ctx.currentTime + 0.4);
  } catch {
    /* autoplay blocked until the user interacts */
  }
}

export default function InboxPage() {
  const { t, lang } = useI18n();
  const { user, socket } = useSession();
  const qc = useQueryClient();
  const fid = user?.facilityIds?.[0];
  const [announce, setAnnounce] = useState('');
  const [, tick] = useState(0);
  const [acceptFor, setAcceptFor] = useState<Inbox | null>(null);
  const [declineFor, setDeclineFor] = useState<Inbox | null>(null);
  const [moved, setMoved] = useState<Record<string, boolean>>({});
  const opened = useRef(new Set<string>());

  const pending = useQuery({
    queryKey: ['offers', fid, 'pending'],
    queryFn: () => api<{ data: Inbox[] }>(`/facilities/${fid}/offers?result=pending`),
    enabled: !!fid,
    refetchInterval: 15_000,
  });
  const history = useQuery({
    queryKey: ['offers', fid, 'history'],
    queryFn: () => api<{ data: Inbox[] }>(`/facilities/${fid}/offers?result=accepted,declined,timeout,superseded,withdrawn`),
    enabled: !!fid,
  });
  const cap = useQuery({ queryKey: ['facility', fid], queryFn: () => api<{ capabilityUpdatedAt: string; stale: boolean }>(`/facilities/${fid}`), enabled: !!fid });
  const catalog = useQuery({ queryKey: ['catalog'], queryFn: () => api<{ capabilities: { code: string; labelEn: string; labelHi: string }[] }>('/reference/catalog'), staleTime: 3600_000 });
  const labels = Object.fromEntries((catalog.data?.capabilities ?? []).map((c) => [c.code, lang === 'hi' ? c.labelHi : c.labelEn]));

  useEffect(() => {
    const i = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(i);
  }, []);

  // FR-W01: new offer → sound + browser notification + ARIA live announcement
  useEffect(() => socket.on((m) => {
    if (m.type !== 'offer.created' || m.facilityId !== fid) return;
    beep();
    setAnnounce(t('inbox.newCase'));
    if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
      new Notification('AapatMitra', { body: t('inbox.newCase'), tag: String(m.offerId) });
    }
  }), [socket, fid, t]);

  // /opened when the card is rendered in a visible tab — stops the SMS nudge rung
  useEffect(() => {
    if (document.visibilityState !== 'visible') return;
    for (const item of pending.data?.data ?? []) {
      if (!item.offer.openedAt && !opened.current.has(item.offer.id)) {
        opened.current.add(item.offer.id);
        void api(`/cases/${item.case.id}/offers/${item.offer.id}/opened`, { method: 'POST' }).catch(() => undefined);
      }
    }
  }, [pending.data]);

  const respond = useMutation({
    mutationFn: (v: { item: Inbox; body: Record<string, unknown>; idem: string }) =>
      api(`/cases/${v.item.case.id}/offers/${v.item.offer.id}/respond`, { body: v.body, idem: v.idem }),
    onSuccess: () => {
      setAcceptFor(null);
      setDeclineFor(null);
      void qc.invalidateQueries({ queryKey: ['offers'] });
    },
    onError: (err, v) => {
      if (err instanceof ProblemError && (err.code === 'OFFER_EXPIRED' || err.code === 'CASE_STATE_CONFLICT')) {
        setMoved((m) => ({ ...m, [v.item.offer.id]: true }));
        setAcceptFor(null);
        setDeclineFor(null);
      }
    },
  });

  const offerToTake = useMutation({
    mutationFn: (item: Inbox) => api(`/cases/${item.case.id}/commands`, {
      body: { command: 'Escalate', args: { reason: 'manual', note: 'Facility offers to take this case after expiry' } } }),
  });

  if (!fid) return <EmptyState title={t('inbox.noFacility')} />;
  const items = pending.data?.data ?? [];
  return (
    <div className="space-y-6">
      <div aria-live="assertive" className="sr-only">{announce}</div>
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold">{t('inbox.title')}</h1>
        {typeof Notification !== 'undefined' && Notification.permission === 'default' && (
          <Button variant="secondary" onClick={() => void Notification.requestPermission()}>🔔 {t('inbox.enableAlerts')}</Button>
        )}
        {cap.data && (
          <span className="ml-auto">
            {cap.data.stale
              ? <Link href="/facility/capability"><StatusChip tone="problem" label={t('inbox.capabilityStale')} /></Link>
              : <StatusChip tone="done" label={`${t('capability.updated')} ${ago(cap.data.capabilityUpdatedAt, lang)}`} />}
          </span>
        )}
      </div>
      {pending.isLoading ? <Skeleton /> : items.length === 0 ? (
        <EmptyState title={t('inbox.empty')} body={t('inbox.emptyHint')}
          action={<Link href="/facility/capability" className="font-semibold text-primary underline">{t('nav.capability')}</Link>} />
      ) : (
        <div className="overflow-x-auto rounded-xl border bg-white">
          <table className="w-full text-left">
            <caption className="sr-only">{t('inbox.title')}</caption>
            <thead className="bg-primary-tint text-sm">
              <tr>
                <th className="p-3">{t('inbox.patient')}</th><th className="p-3">{t('inbox.need')}</th>
                <th className="p-3">{t('inbox.from')}</th><th className="p-3">{t('inbox.eta')}</th>
                <th className="p-3">{t('inbox.waiting')}</th><th className="p-3">{t('inbox.action')}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const off = serverOffsetMs(item.serverTime);
                const left = remainingS(item.offer.expiresAt, off);
                const gone = moved[item.offer.id] || left === 0;
                return (
                  <tr key={item.offer.id} className={`min-h-[48px] border-t ${gone ? 'bg-slate-100 text-ink-muted' : ''}`}>
                    <td className="p-3">
                      <Link href={item.patient.id ? `/facility/patient/${item.patient.id}` : `/facility/in-transit`} className="font-semibold underline">
                        {item.patient.firstName ?? t('inbox.unknownPatient')}{item.patient.ageYears != null ? `, ${item.patient.ageYears}` : ''}
                      </Link>
                      <div className="text-sm text-ink-muted">{item.case.shortCode}{item.patient.highRisk && <> · <StatusChip tone="problem" label={t('inbox.highRisk')} /></>}</div>
                      {!item.case.verified && <StatusChip tone="waiting" label={t('inbox.unverified')} />}
                    </td>
                    <td className="p-3">
                      <CategoryLabel code={item.case.emergencyCategory} />
                      <div className="text-sm text-ink-muted">{item.case.neededCapabilities.map((c) => labels[c.code] ?? c.code).join(' + ')}</div>
                      {item.offer.capabilityUnconfirmed && <StatusChip tone="waiting" label={t('inbox.capabilityUnconfirmed')} />}
                    </td>
                    <td className="p-3 text-sm">{item.from.ashaName ? `ASHA ${item.from.ashaName}` : item.from.originFacilityName ?? '—'}{item.from.villageName ? ` · ${item.from.villageName}` : ''}</td>
                    <td className="p-3">{item.etaMin ? `${item.etaMin} min` : '—'}</td>
                    <td className="p-3">
                      {gone ? <StatusChip tone="offline" label={t('inbox.moved')} />
                        : <StatusChip tone={left < 60 ? 'problem' : 'waiting'} label={`${mmss(left)} ${t('inbox.left')}`} />}
                      <div className="text-xs text-ink-muted">{ago(item.offer.offeredAt, lang)}</div>
                    </td>
                    <td className="p-3">
                      {gone ? (
                        <Button variant="secondary" onClick={() => offerToTake.mutate(item)} disabled={offerToTake.isPending}>{t('inbox.offerToTake')}</Button>
                      ) : (
                        <div className="flex gap-2">
                          <Button variant="success" onClick={() => setAcceptFor(item)}>✔ {t('inbox.accept')}</Button>
                          <Button variant="danger" onClick={() => setDeclineFor(item)}>✖ {t('inbox.decline')}</Button>
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <ErrorNote error={respond.error && !(respond.error instanceof ProblemError && respond.error.code === 'OFFER_EXPIRED') ? respond.error : null} />
      {offerToTake.isSuccess && <p role="status" className="text-success">✔ {t('inbox.offerSent')}</p>}

      <AcceptDialog item={acceptFor} onClose={() => setAcceptFor(null)} busy={respond.isPending}
        onConfirm={(beds) => acceptFor && respond.mutate({ item: acceptFor, idem: uuidv7(), body: { decision: 'accept', ...(beds != null ? { bedsAvailable: beds } : {}) } })} />
      <DeclineDialog item={declineFor} onClose={() => setDeclineFor(null)} busy={respond.isPending}
        onConfirm={(reason, note) => declineFor && respond.mutate({ item: declineFor, idem: uuidv7(), body: { decision: 'decline', reason, note: note || null } })} />

      <section>
        <h2 className="mb-2 text-lg font-semibold">{t('inbox.history')}</h2>
        <ul className="divide-y rounded-xl border bg-white">
          {(history.data?.data ?? []).slice(0, 15).map((h) => {
            const r = OFFER_RESULT[h.offer.result];
            return (
              <li key={h.offer.id} className="flex items-center gap-3 p-3 text-sm">
                <span className="font-semibold">{h.case.shortCode}</span>
                <CategoryLabel code={h.case.emergencyCategory} />
                <span className="ml-auto"><StatusChip tone={r?.tone ?? 'info'} label={lang === 'hi' ? r?.hi ?? h.offer.result : r?.en ?? h.offer.result} /></span>
                <span className="text-ink-muted">{ago(h.offer.offeredAt, lang)}</span>
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}

function AcceptDialog({ item, onClose, onConfirm, busy }: { item: Inbox | null; onClose: () => void; onConfirm: (beds: number | null) => void; busy: boolean }) {
  const { t } = useI18n();
  const [beds, setBeds] = useState<string>('');
  return (
    <ConfirmDialog open={!!item} title={t('inbox.acceptTitle', { code: item?.case.shortCode ?? '' })} onClose={onClose}>
      <p className="mb-3">{t('inbox.acceptBody')}</p>
      <label className="mb-4 block">
        <span className="text-sm font-semibold">{t('inbox.bedsNow')}</span>
        <input type="number" min={0} value={beds} onChange={(e) => setBeds(e.target.value)} className="mt-1 min-h-[48px] w-32 rounded-lg border-2 px-3" />
      </label>
      <div className="flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose}>{t('cancel')}</Button>
        <Button variant="success" disabled={busy} onClick={() => onConfirm(beds === '' ? null : Number(beds))}>✔ {t('inbox.confirmAccept')}</Button>
      </div>
    </ConfirmDialog>
  );
}

function DeclineDialog({ item, onClose, onConfirm, busy }: { item: Inbox | null; onClose: () => void; onConfirm: (r: string, note: string) => void; busy: boolean }) {
  const { t, lang } = useI18n();
  const [reason, setReason] = useState<string>('');
  const [note, setNote] = useState('');
  return (
    <ConfirmDialog open={!!item} title={t('inbox.declineTitle', { code: item?.case.shortCode ?? '' })} onClose={onClose}>
      <fieldset className="mb-4 space-y-2">
        <legend className="mb-2 font-semibold">{t('inbox.declineReason')}</legend>
        {DECLINE_REASONS.map((r) => (
          <label key={r.code} className="flex min-h-[44px] items-center gap-3 rounded-lg border px-3">
            <input type="radio" name="reason" value={r.code} checked={reason === r.code} onChange={() => setReason(r.code)} />
            {lang === 'hi' ? r.hi : r.en}
          </label>
        ))}
      </fieldset>
      {reason === 'other' && (
        <label className="mb-4 block">
          <span className="text-sm font-semibold">{t('inbox.declineNote')}</span>
          <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} className="mt-1 min-h-[48px] w-full rounded-lg border-2 px-3" />
        </label>
      )}
      <p className="mb-4 text-sm text-ink-muted">{t('inbox.declineNext')}</p>
      <div className="flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose}>{t('cancel')}</Button>
        <Button variant="danger" disabled={busy || !reason || (reason === 'other' && !note.trim())} onClick={() => onConfirm(reason, note)}>✖ {t('inbox.confirmDecline')}</Button>
      </div>
    </ConfirmDialog>
  );
}
