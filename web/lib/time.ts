/** Countdowns use the SERVER clock (API-Guide §2.4, FR-W01): offset = serverTime − local time at receipt. */
export function serverOffsetMs(serverTime: string | undefined): number {
  return serverTime ? new Date(serverTime).getTime() - Date.now() : 0;
}

export function remainingS(expiresAt: string, offsetMs: number): number {
  return Math.max(0, Math.round((new Date(expiresAt).getTime() - (Date.now() + offsetMs)) / 1000));
}

export function mmss(s: number): string {
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, '0')}`;
}

export function ago(iso: string | null | undefined, lang: string): string {
  if (!iso) return '—';
  const min = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (min < 1) return lang === 'hi' ? 'अभी' : 'just now';
  if (min < 60) return lang === 'hi' ? `${min} मिनट पहले` : `${min} min ago`;
  const h = Math.round(min / 60);
  return lang === 'hi' ? `${h} घंटे पहले` : `${h} h ago`;
}
