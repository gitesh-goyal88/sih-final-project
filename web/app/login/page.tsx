'use client';

import { useRouter } from 'next/navigation';
import { useRef, useState, type FormEvent } from 'react';
import { Logo } from '@/components/AppShell';
import { Button, ErrorNote } from '@/components/ui';
import { api, deviceId, type User } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import { homeFor, useSession } from '@/lib/session';

/** Staff ID + OTP (API-Guide §3.1, M1). Staff accounts are pre-provisioned by the district admin (SEC-ID-07). */
export default function LoginPage() {
  const { t, lang, setLang } = useI18n();
  const { signIn } = useSession();
  const router = useRouter();
  const [staffId, setStaffId] = useState('');
  const [challenge, setChallenge] = useState<string | null>(null);
  const [digits, setDigits] = useState(['', '', '', '', '', '']);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [resendAt, setResendAt] = useState(0);
  const boxes = useRef<(HTMLInputElement | null)[]>([]);

  async function requestOtp(e?: FormEvent) {
    e?.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ challengeId: string; resendAfterS: number }>('/auth/otp/request', {
        body: { staffId: staffId.trim(), purpose: 'login' },
      });
      setChallenge(r.challengeId);
      setResendAt(Date.now() + r.resendAfterS * 1000);
      setTimeout(() => boxes.current[0]?.focus(), 50);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  async function verify(e?: FormEvent) {
    e?.preventDefault();
    const otp = digits.join('');
    if (otp.length !== 6 || !challenge) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ accessToken: string; user: User }>('/auth/otp/verify', {
        body: { challengeId: challenge, otp, device: { id: deviceId(), platform: 'web', model: navigator.userAgent.slice(0, 80) } },
      });
      signIn(r.accessToken, r.user);
      router.replace(homeFor(r.user.role));
    } catch (err) {
      setError(err);
      setDigits(['', '', '', '', '', '']);
      boxes.current[0]?.focus();
    } finally {
      setBusy(false);
    }
  }

  function setDigit(i: number, v: string) {
    const clean = v.replace(/\D/g, '');
    if (clean.length > 1) {
      const all = clean.slice(0, 6).split('');
      setDigits([...all, ...Array(6 - all.length).fill('')]);
      boxes.current[Math.min(all.length, 5)]?.focus();
      return;
    }
    const next = [...digits];
    next[i] = clean;
    setDigits(next);
    if (clean && i < 5) boxes.current[i + 1]?.focus();
  }

  return (
    <main id="main" className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-md rounded-2xl bg-white p-8 shadow">
        <div className="mb-6 flex items-center justify-between">
          <h1 className="flex items-center gap-2 text-2xl font-bold text-primary"><Logo /> AapatMitra</h1>
          <button onClick={() => setLang(lang === 'hi' ? 'en' : 'hi')} className="rounded-lg border px-3 py-1">
            🌐 {lang === 'hi' ? 'English' : 'हिन्दी'}
          </button>
        </div>
        {!challenge ? (
          <form onSubmit={requestOtp} className="space-y-4">
            <label className="block">
              <span className="mb-1 block font-semibold">{t('login.staffId')}</span>
              <input value={staffId} onChange={(e) => setStaffId(e.target.value)} required autoComplete="username"
                className="min-h-[48px] w-full rounded-lg border-2 border-slate-300 px-3 text-lg uppercase" placeholder="FAC-CHC-01" />
            </label>
            <Button type="submit" disabled={busy || !staffId.trim()} className="w-full">{t('login.sendOtp')}</Button>
          </form>
        ) : (
          <form onSubmit={verify} className="space-y-4">
            <p className="font-semibold">{t('login.enterOtp')}</p>
            <div className="flex justify-between gap-2" role="group" aria-label={t('login.otpBoxes')}>
              {digits.map((d, i) => (
                <input key={i} ref={(el) => { boxes.current[i] = el; }} value={d} inputMode="numeric" maxLength={6}
                  aria-label={`${t('login.digit')} ${i + 1}`} autoComplete={i === 0 ? 'one-time-code' : 'off'}
                  onChange={(e) => setDigit(i, e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Backspace' && !d && i > 0) boxes.current[i - 1]?.focus(); }}
                  className="h-14 w-12 rounded-lg border-2 border-slate-300 text-center text-2xl font-bold" />
              ))}
            </div>
            <Button type="submit" disabled={busy || digits.join('').length !== 6} className="w-full">{t('login.verify')}</Button>
            <button type="button" onClick={() => void requestOtp()} disabled={Date.now() < resendAt || busy}
              className="w-full text-sm text-primary underline disabled:text-ink-muted">{t('login.resend')}</button>
          </form>
        )}
        <div className="mt-4"><ErrorNote error={error} /></div>
        {process.env.NEXT_PUBLIC_DEMO_HINTS === '1' && (
          <p className="mt-6 rounded-lg bg-primary-tint p-3 text-sm">
            {t('login.demoHint')} <a className="font-semibold underline" href="/api/v1/dev/sms" target="_blank" rel="noreferrer">/api/v1/dev/sms</a>
          </p>
        )}
      </div>
    </main>
  );
}
