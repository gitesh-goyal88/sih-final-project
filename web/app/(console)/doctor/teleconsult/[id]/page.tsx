'use client';

import Link from 'next/link';
import { useMutation, useQuery } from '@tanstack/react-query';
import { use, useCallback, useEffect, useRef, useState } from 'react';
import { PatientRecordView } from '@/components/PatientRecordView';
import { Button, Card, ErrorNote, StatusChip } from '@/components/ui';
import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import { useSession } from '@/lib/session';
import { uuidv7 } from '@/lib/uuid';

type Sess = { id: string; patientId: string; status: string; mode: string; outcomeEntryId?: string | null };
type Ice = { iceServers: RTCIceServer[] };
const MEDICINES = ['IFA', 'Calcium', 'Paracetamol', 'Amoxicillin', 'ORS', 'Zinc', 'Metformin', 'Amlodipine', 'Labetalol'];

/**
 * TRD §12: audio-first WebRTC (Opus ~16 kbps, DTX/FEC), optional low-res video auto-disabled below 120 kbps,
 * signalling over /ws, TURN (UDP 3478 + TLS 443). A dropped call switches to async: the doctor finishes
 * with notes + a pick-list care plan. No recording (DPDP minimisation).
 */
export default function CallPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { t } = useI18n();
  const { socket } = useSession();
  const sess = useQuery({ queryKey: ['teleconsults', id], queryFn: () => api<Sess>(`/teleconsults/${id}`) });
  const pc = useRef<RTCPeerConnection | null>(null);
  const local = useRef<MediaStream | null>(null);
  const remoteAudio = useRef<HTMLAudioElement>(null);
  const remoteVideo = useRef<HTMLVideoElement>(null);
  const [state, setState] = useState<'idle' | 'calling' | 'in_call' | 'async'>('idle');
  const [muted, setMuted] = useState(false);
  const [video, setVideo] = useState(false);
  const [weak, setWeak] = useState(false);
  const [err, setErr] = useState<unknown>(null);

  const cmd = useCallback((command: string, args: Record<string, unknown> = {}) =>
    api(`/teleconsults/${id}/commands`, { body: { command, args } }), [id]);

  const hangup = useCallback(async (reason: string) => {
    socket.send({ type: 'teleconsult.hangup', sessionId: id, reason });
    pc.current?.close();
    pc.current = null;
    local.current?.getTracks().forEach((tr) => tr.stop());
    setState('async');
  }, [id, socket]);

  useEffect(() => {
    socket.subscribe(`teleconsult:${id}`);
    const off = socket.on(async (m) => {
      if (m.sessionId !== id || !pc.current) return;
      if (m.type === 'teleconsult.answer') await pc.current.setRemoteDescription({ type: 'answer', sdp: String(m.sdp) });
      if (m.type === 'teleconsult.ice' && m.candidate) {
        await pc.current.addIceCandidate({ candidate: String(m.candidate), sdpMid: m.sdpMid as string, sdpMLineIndex: m.sdpMLineIndex as number });
      }
      if (m.type === 'teleconsult.peer_left') await hangup('call_dropped');
      if (m.type === 'teleconsult.bandwidth' && Number(m.estimatedKbps) < 120) {
        setWeak(true);
        setVideo(false);
      }
    });
    return () => {
      off();
      socket.unsubscribe(`teleconsult:${id}`);
      pc.current?.close();
      local.current?.getTracks().forEach((tr) => tr.stop());
    };
  }, [id, socket, hangup]);

  async function start() {
    setErr(null);
    try {
      const { iceServers } = await api<Ice>(`/teleconsults/${id}/turn-credentials`, { method: 'POST' });
      const conn = new RTCPeerConnection({ iceServers });
      pc.current = conn;
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true }, video: false });
      local.current = stream;
      for (const track of stream.getTracks()) {
        const sender = conn.addTrack(track, stream);
        const p = sender.getParameters();
        p.encodings = [{ maxBitrate: 16_000 }]; // Opus 16 kbps mono (TRD §12)
        await sender.setParameters(p).catch(() => undefined);
      }
      conn.ontrack = (ev) => {
        if (ev.track.kind === 'audio' && remoteAudio.current) remoteAudio.current.srcObject = ev.streams[0];
        if (ev.track.kind === 'video' && remoteVideo.current) remoteVideo.current.srcObject = ev.streams[0];
      };
      conn.onicecandidate = (ev) => {
        if (ev.candidate) socket.send({ type: 'teleconsult.ice', sessionId: id, candidate: ev.candidate.candidate,
          sdpMid: ev.candidate.sdpMid, sdpMLineIndex: ev.candidate.sdpMLineIndex });
      };
      conn.onconnectionstatechange = () => {
        if (conn.connectionState === 'connected') setState('in_call');
        if (conn.connectionState === 'failed' || conn.connectionState === 'disconnected') void hangup('call_dropped');
      };
      const statsTimer = setInterval(async () => {
        const stats = await conn.getStats();
        stats.forEach((r) => {
          if (r.type === 'candidate-pair' && r.state === 'succeeded' && r.availableOutgoingBitrate) {
            const kbps = Math.round(r.availableOutgoingBitrate / 1000);
            socket.send({ type: 'teleconsult.bandwidth', sessionId: id, estimatedKbps: kbps });
            setWeak(kbps < 120);
          }
        });
      }, 5000);
      conn.addEventListener('connectionstatechange', () => { if (conn.connectionState === 'closed') clearInterval(statsTimer); });
      socket.send({ type: 'teleconsult.join', sessionId: id, role: 'doctor' });
      const offer = await conn.createOffer();
      await conn.setLocalDescription(offer);
      socket.send({ type: 'teleconsult.offer', sessionId: id, sdp: offer.sdp });
      await cmd('Start', { mode: 'audio' });
      setState('calling');
    } catch (e) {
      setErr(e);
    }
  }

  async function toggleVideo() {
    if (!pc.current || weak) return;
    if (!video) {
      const cam = await navigator.mediaDevices.getUserMedia({ video: { width: 160, height: 120, frameRate: 10 } });
      const track = cam.getVideoTracks()[0];
      const sender = pc.current.addTrack(track, cam);
      const p = sender.getParameters();
      p.encodings = [{ maxBitrate: 150_000, maxFramerate: 10 }];
      await sender.setParameters(p).catch(() => undefined);
      setVideo(true);
    } else {
      pc.current.getSenders().filter((s) => s.track?.kind === 'video').forEach((s) => { s.track?.stop(); pc.current?.removeTrack(s); });
      setVideo(false);
    }
  }

  function toggleMute() {
    local.current?.getAudioTracks().forEach((tr) => { tr.enabled = muted; });
    setMuted((m) => !m);
  }

  if (!sess.data) return <p aria-busy="true">{t('loading')}</p>;
  return (
    <div className="grid gap-6 xl:grid-cols-2">
      <div className="space-y-4">
        <Card>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-xl font-bold">{t('tele.call')}</h1>
            <StatusChip tone={state === 'in_call' ? 'done' : state === 'async' ? 'offline' : 'waiting'} label={t(`tele.state.${state}`)} />
            {weak && <StatusChip tone="waiting" label={t('tele.weak')} />}
          </div>
          <audio ref={remoteAudio} autoPlay />
          <video ref={remoteVideo} autoPlay playsInline muted className={`mt-3 w-full rounded-lg bg-black ${video ? '' : 'hidden'}`} />
          <div className="mt-4 flex flex-wrap gap-2">
            {state === 'idle' && <Button onClick={() => void start()}>🎧 {t('tele.startCall')}</Button>}
            {(state === 'calling' || state === 'in_call') && (
              <>
                <Button variant="secondary" onClick={toggleMute} ariaLabel={t('tele.mute')}>{muted ? '🔈 ' + t('tele.unmute') : '🔇 ' + t('tele.mute')}</Button>
                <Button variant="secondary" onClick={() => void toggleVideo()} disabled={weak}>{video ? t('tele.videoOff') : t('tele.videoOn')}</Button>
                <Button variant="danger" onClick={() => void hangup('completed')}>⏹ {t('tele.end')}</Button>
              </>
            )}
            {state === 'async' && <p>{t('tele.asyncNote')}</p>}
          </div>
          <ErrorNote error={err} />
        </Card>
        <PatientRecordView patientId={sess.data.patientId} />
      </div>
      <CarePlanForm sessionId={id} patientId={sess.data.patientId} onDone={() => void cmd('End', { endReason: state === 'async' ? 'switched_async' : 'completed' })} />
    </div>
  );
}

function CarePlanForm({ sessionId, patientId, onDone }: { sessionId: string; patientId: string; onDone: () => void }) {
  const { t } = useI18n();
  const [notes, setNotes] = useState('');
  const [meds, setMeds] = useState<string[]>([]);
  const [days, setDays] = useState(7);
  const [refer, setRefer] = useState(false);
  const save = useMutation({
    mutationFn: async () => {
      await api(`/patients/${patientId}/entries`, { body: { id: uuidv7(), kind: 'teleconsult', notes: notes || 'Teleconsult', teleconsultSessionId: sessionId } });
      await api('/care-plans', { body: {
        id: uuidv7(), patientId, teleconsultSessionId: sessionId, summary: notes || null,
        nextVisitOn: new Date(Date.now() + days * 86400_000).toISOString().slice(0, 10),
        items: [...meds.map((m, i) => ({ id: uuidv7(), kind: 'medicine', medicineName: m, frequencyText: '1 daily', durationDays: 30, sortOrder: i + 1 })),
                { id: uuidv7(), kind: 'visit', dueOffsetDays: days, taskType: 'teleconsult_followup', sortOrder: 90 }],
      } });
      onDone();
    },
  });
  return (
    <Card className="h-fit space-y-4">
      <h2 className="text-lg font-semibold">{t('tele.carePlan')}</h2>
      <label className="block">
        <span className="font-semibold">{t('close.notes')} <span className="font-normal text-ink-muted">({t('optional')})</span></span>
        <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={4} maxLength={2000} className="mt-1 w-full rounded-lg border-2 p-2" />
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
        <div className="mt-1 flex gap-2">
          {[7, 14, 30].map((d) => (
            <button key={d} type="button" aria-pressed={days === d} onClick={() => setDays(d)}
              className={`min-h-[44px] rounded-lg border px-3 ${days === d ? 'bg-primary text-white' : ''}`}>{t('close.inDays', { n: d })}</button>
          ))}
        </div>
      </fieldset>
      <label className="flex min-h-[44px] items-center gap-2">
        <input type="checkbox" checked={refer} onChange={(e) => setRefer(e.target.checked)} /> {t('tele.refer')}
      </label>
      {refer && (
        <Link className="font-semibold text-primary underline" href={`/doctor/referrals?patientId=${patientId}&teleconsultSessionId=${sessionId}`}>
          ↗ {t('tele.openReferral')}
        </Link>
      )}
      <Button onClick={() => save.mutate()} disabled={save.isPending} className="w-full">{t('tele.saveFinish')}</Button>
      {save.isSuccess && <p role="status" className="text-success">✔ {t('tele.saved')}</p>}
      <ErrorNote error={save.error} />
    </Card>
  );
}
