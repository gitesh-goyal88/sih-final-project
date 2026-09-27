'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useState, type ReactNode } from 'react';
import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import { homeFor, useSession } from '@/lib/session';

/* UI-UX §7.1 global layout: thin GIGW utility strip → header (logo, facility, bell, user) → sidebar (≤ 5 items). */

const NAV: Record<string, { href: string; key: string; icon: string }[]> = {
  facility_staff: [
    { href: '/facility/inbox', key: 'nav.incoming', icon: '📥' },
    { href: '/facility/in-transit', key: 'nav.inTransit', icon: '🚑' },
    { href: '/facility/patients', key: 'nav.patients', icon: '🗂' },
    { href: '/facility/capability', key: 'nav.capability', icon: '🛏' },
    { href: '/facility/reports', key: 'nav.reports', icon: '📊' },
  ],
  doctor: [
    { href: '/doctor/teleconsult', key: 'nav.teleconsult', icon: '🎧' },
    { href: '/doctor/referrals', key: 'nav.referrals', icon: '↗' },
    { href: '/facility/inbox', key: 'nav.incoming', icon: '📥' },
  ],
  district_admin: [
    { href: '/admin/map', key: 'nav.map', icon: '🗺' },
    { href: '/admin/escalations', key: 'nav.escalations', icon: '⚠' },
    { href: '/admin/facilities', key: 'nav.facilities', icon: '🏥' },
    { href: '/admin/users', key: 'nav.users', icon: '👥' },
  ],
};

function UtilityStrip() {
  const { lang, setLang, t } = useI18n();
  const [scale, setScale] = useState(100);
  const [contrast, setContrast] = useState(false);
  useEffect(() => {
    document.documentElement.style.fontSize = `${scale}%`;
  }, [scale]);
  useEffect(() => {
    document.documentElement.classList.toggle('high-contrast', contrast);
  }, [contrast]);
  return (
    <div className="bg-primary-dark text-sm text-white">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-4 px-4 py-1">
        <a href="#main" className="underline focus:outline-none focus:ring-2 focus:ring-white">{t('skipToContent')}</a>
        <span aria-hidden="true">·</span>
        <div role="group" aria-label={t('textSize')} className="flex gap-1">
          {[90, 100, 115].map((s, i) => (
            <button key={s} onClick={() => setScale(s)} aria-pressed={scale === s}
              className={`rounded px-1.5 ${scale === s ? 'bg-white text-primary-dark' : ''}`}>{['A-', 'A', 'A+'][i]}</button>
          ))}
        </div>
        <button onClick={() => setContrast((c) => !c)} aria-pressed={contrast} className="underline">{t('contrast')}</button>
        <div className="ml-auto flex items-center gap-1" role="group" aria-label={t('language')}>
          <span aria-hidden="true">🌐</span>
          <button onClick={() => setLang('hi')} aria-pressed={lang === 'hi'} className={lang === 'hi' ? 'font-bold underline' : ''}>हिन्दी</button>
          <span>/</span>
          <button onClick={() => setLang('en')} aria-pressed={lang === 'en'} className={lang === 'en' ? 'font-bold underline' : ''}>English</button>
        </div>
      </div>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const { user, ready, signOut, wsStatus } = useSession();
  const { t } = useI18n();
  const path = usePathname();
  const router = useRouter();
  useEffect(() => {
    if (ready && !user) router.replace('/login');
  }, [ready, user, router]);
  const facilityId = user?.facilityIds?.[0];
  const facility = useQuery({
    queryKey: ['facility', facilityId],
    queryFn: () => api<{ name: string }>(`/facilities/${facilityId}`),
    enabled: !!facilityId,
  });
  const bell = useQuery({
    queryKey: ['offers', 'bell'],
    queryFn: () => api<{ data: unknown[] }>('/notifications?unread=true'),
    enabled: !!user,
    refetchInterval: 30_000,
  });
  if (!ready || !user) return <div className="p-8" aria-busy="true">{t('loading')}</div>;
  const items = NAV[user.role] ?? [];
  return (
    <div className="min-h-screen">
      <UtilityStrip />
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl items-center gap-4 px-4 py-3">
          <Link href={homeFor(user.role)} className="flex items-center gap-2 text-xl font-bold text-primary">
            <Logo /> AapatMitra
          </Link>
          {facility.data && <span className="text-ink-muted">· {facility.data.name}</span>}
          {user.role === 'district_admin' && <span className="text-ink-muted">· {t('district')} {user.districtCode}</span>}
          <span className="ml-auto flex items-center gap-2 text-sm" aria-live="polite">
            <span aria-hidden="true" className={`h-2.5 w-2.5 rounded-full ${wsStatus === 'open' ? 'bg-success' : 'bg-offline'}`} />
            {wsStatus === 'open' ? t('live') : t('reconnecting')}
          </span>
          <span className="relative text-lg" aria-label={t('notifications', { n: bell.data?.data.length ?? 0 })}>
            🔔{!!bell.data?.data.length && (
              <span className="absolute -right-2 -top-1 rounded-full bg-accent px-1.5 text-xs font-bold text-white">{bell.data.data.length}</span>
            )}
          </span>
          <span className="font-semibold">{user.name}</span>
          <button onClick={() => void signOut()} className="rounded-lg border px-3 py-1 text-sm">{t('logout')}</button>
        </div>
      </header>
      <div className="mx-auto flex max-w-7xl gap-6 px-4 py-6">
        <nav aria-label={t('mainNav')} className="w-52 shrink-0">
          <ul className="space-y-1">
            {items.map((it) => {
              const active = path.startsWith(it.href);
              return (
                <li key={it.href}>
                  <Link href={it.href} aria-current={active ? 'page' : undefined}
                    className={`flex min-h-[48px] items-center gap-2 rounded-lg px-3 font-semibold ${active ? 'bg-primary text-white' : 'hover:bg-primary-tint'}`}>
                    <span aria-hidden="true">{it.icon}</span>{t(it.key)}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>
        <main id="main" tabIndex={-1} className="min-w-0 flex-1 focus:outline-none">{children}</main>
      </div>
    </div>
  );
}

export function Logo() {
  return (
    <svg aria-hidden="true" width="28" height="28" viewBox="0 0 32 32">
      <circle cx="16" cy="16" r="15" fill="#1E4FA3" />
      <path d="M16 7v18M7 16h18" stroke="white" strokeWidth="4" strokeLinecap="round" />
      <circle cx="16" cy="16" r="15" fill="none" stroke="#F7931E" strokeWidth="2" />
    </svg>
  );
}
