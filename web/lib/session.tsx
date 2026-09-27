'use client';

import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { api, refreshSession, tokenStore, type User } from './api';
import { useI18n } from './i18n';
import { ConsoleSocket, type WsMessage } from './ws';

type Session = {
  user: User | null;
  ready: boolean;
  signIn: (token: string, user: User) => void;
  signOut: () => Promise<void>;
  socket: ConsoleSocket;
  wsStatus: ConsoleSocket['status'];
};
const SessionCtx = createContext<Session | null>(null);

export function homeFor(role: string | undefined): string {
  if (role === 'facility_staff') return '/facility/inbox';
  if (role === 'doctor') return '/doctor/teleconsult';
  if (role === 'district_admin') return '/admin/map';
  return '/login';
}

function SessionInner({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [wsStatus, setWsStatus] = useState<ConsoleSocket['status']>('closed');
  const socket = useRef(new ConsoleSocket()).current;
  const qc = useQueryClient();
  const router = useRouter();
  const { lang } = useI18n();

  useEffect(() => tokenStore.setLang(lang), [lang]);

  // FR-W02: on tab reload, restore the session from the refresh cookie first
  useEffect(() => {
    tokenStore.onSignedOut(() => {
      setUser(null);
      router.replace('/login');
    });
    refreshSession()
      .then((s) => {
        if (s) setUser(s.user);
      })
      .finally(() => setReady(true));
  }, [router]);

  // WS events invalidate queries (API-Guide §16.2) — the message is a nudge, not the data
  useEffect(() => {
    if (!user) return;
    socket.onStatus = setWsStatus;
    const off = socket.on((m: WsMessage) => {
      if (m.type.startsWith('offer.')) void qc.invalidateQueries({ queryKey: ['offers'] });
      if (m.type.startsWith('case.') || m.type.startsWith('leg.')) {
        void qc.invalidateQueries({ queryKey: ['case', m.caseId] });
        void qc.invalidateQueries({ queryKey: ['cases'] });
      }
      if (m.type.startsWith('escalation.') || m.type === 'case.status_changed') void qc.invalidateQueries({ queryKey: ['admin'] });
      if (m.type === 'facility.updated') void qc.invalidateQueries({ queryKey: ['facility'] });
      if (m.type.startsWith('teleconsult.')) void qc.invalidateQueries({ queryKey: ['teleconsults'] });
    });
    void socket.connect();
    return () => {
      off();
      socket.close();
    };
  }, [user, socket, qc]);

  const signIn = useCallback((token: string, u: User) => {
    tokenStore.set(token);
    setUser(u);
  }, []);

  const signOut = useCallback(async () => {
    try {
      await api('/auth/logout', { method: 'POST', body: { allDevices: false } });
    } finally {
      tokenStore.set(null);
      setUser(null);
      qc.clear();
      router.replace('/login');
    }
  }, [qc, router]);

  const value = useMemo(() => ({ user, ready, signIn, signOut, socket, wsStatus }), [user, ready, signIn, signOut, socket, wsStatus]);
  return <SessionCtx.Provider value={value}>{children}</SessionCtx.Provider>;
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [qc] = useState(
    () => new QueryClient({ defaultOptions: { queries: { staleTime: 5_000, refetchOnWindowFocus: true, retry: 1 } } }),
  );
  return (
    <QueryClientProvider client={qc}>
      <SessionInner>{children}</SessionInner>
    </QueryClientProvider>
  );
}

export function useSession(): Session {
  const s = useContext(SessionCtx);
  if (!s) throw new Error('SessionProvider missing');
  return s;
}
