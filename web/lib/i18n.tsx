'use client';

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';
import en from '@/messages/en.json';
import hi from '@/messages/hi.json';

/** All strings live in message files — no hard-coded UI text (UI-UX §4.2). Language: hi / en (+ regional, TRD Q5). */
export type Lang = 'en' | 'hi';
const MESSAGES: Record<Lang, Record<string, string>> = { en, hi };

type Ctx = { lang: Lang; setLang: (l: Lang) => void; t: (key: string, vars?: Record<string, string | number>) => string };
const I18n = createContext<Ctx | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>('en');
  useEffect(() => {
    try {
      const saved = localStorage.getItem('am_lang');
      if (saved === 'hi' || saved === 'en') setLangState(saved);
    } catch {
      /* private mode */
    }
  }, []);
  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);
  const setLang = useCallback((l: Lang) => {
    setLangState(l);
    try {
      localStorage.setItem('am_lang', l);
    } catch {
      /* ignore */
    }
  }, []);
  const t = useCallback(
    (key: string, vars?: Record<string, string | number>) => {
      let s = MESSAGES[lang][key] ?? MESSAGES.en[key] ?? key;
      for (const [k, v] of Object.entries(vars ?? {})) s = s.replaceAll(`{${k}}`, String(v));
      return s;
    },
    [lang],
  );
  return <I18n.Provider value={{ lang, setLang, t }}>{children}</I18n.Provider>;
}

export function useI18n(): Ctx {
  const ctx = useContext(I18n);
  if (!ctx) throw new Error('I18nProvider missing');
  return ctx;
}
