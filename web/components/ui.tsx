'use client';

import { useEffect, useRef, type ReactNode } from 'react';
import { useI18n } from '@/lib/i18n';
import { CATEGORY, STATUS, type Tone } from '@/lib/vocab';

/* UI-UX §13 component library (web side). Every status = icon + word + colour, never colour alone (§3.1). */

const TONE: Record<Tone, { cls: string; icon: string }> = {
  done: { cls: 'bg-success/10 text-success border-success/40', icon: '✔' },
  waiting: { cls: 'bg-warning/20 text-ink border-warning', icon: '⏳' },
  problem: { cls: 'bg-emergency/10 text-emergency border-emergency/40', icon: '✖' },
  offline: { cls: 'bg-offline/10 text-offline border-offline/40', icon: '☁' },
  info: { cls: 'bg-primary-tint text-primary border-primary/40', icon: 'ℹ' },
};

export function StatusChip({ tone, label }: { tone: Tone; label: string }) {
  const t = TONE[tone];
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-sm font-semibold ${t.cls}`}>
      <span aria-hidden="true">{t.icon}</span>
      {label}
    </span>
  );
}

export function CaseStatusChip({ status }: { status: string }) {
  const { lang } = useI18n();
  const s = STATUS[status] ?? { en: status, hi: status, tone: 'info' as Tone };
  return <StatusChip tone={s.tone} label={lang === 'hi' ? s.hi : s.en} />;
}

export function CategoryLabel({ code }: { code?: string | null }) {
  const { lang } = useI18n();
  if (!code) return <span>—</span>;
  const c = CATEGORY[code] ?? { en: code, hi: code, icon: '•' };
  return (
    <span className="inline-flex items-center gap-1">
      <span aria-hidden="true">{c.icon}</span>
      {lang === 'hi' ? c.hi : c.en}
    </span>
  );
}

type BtnProps = {
  children: ReactNode;
  onClick?: () => void;
  type?: 'button' | 'submit';
  disabled?: boolean;
  variant?: 'primary' | 'secondary' | 'danger' | 'success';
  className?: string;
  ariaLabel?: string;
};

export function Button({ children, onClick, type = 'button', disabled, variant = 'primary', className = '', ariaLabel }: BtnProps) {
  const v = {
    primary: 'bg-primary text-white hover:bg-primary-dark',
    secondary: 'border-2 border-primary text-primary bg-white hover:bg-primary-tint',
    danger: 'border-2 border-emergency text-emergency bg-white hover:bg-emergency/5',
    success: 'bg-success text-white hover:brightness-95',
  }[variant];
  return (
    <button type={type} onClick={onClick} disabled={disabled} aria-label={ariaLabel}
      className={`min-h-[48px] rounded-xl px-5 text-base font-semibold disabled:opacity-50 ${v} ${className}`}>
      {children}
    </button>
  );
}

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <section className={`rounded-xl border border-slate-200 bg-white p-4 shadow-sm ${className}`}>{children}</section>;
}

export function EmptyState({ title, body, action }: { title: string; body?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-slate-300 bg-white p-10 text-center">
      <svg aria-hidden="true" width="56" height="56" viewBox="0 0 56 56" className="text-primary/40">
        <circle cx="28" cy="28" r="26" fill="currentColor" />
        <path d="M18 29l7 7 13-15" stroke="white" strokeWidth="4" fill="none" strokeLinecap="round" />
      </svg>
      <p className="text-lg font-semibold">{title}</p>
      {body && <p className="text-ink-muted">{body}</p>}
      {action}
    </div>
  );
}

export function PersonCallCard({ name, role, phone }: { name?: string | null; role: string; phone?: string | null }) {
  const { t } = useI18n();
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg bg-primary-tint px-3 py-2">
      <div>
        <p className="font-semibold">{name || '—'}</p>
        <p className="text-sm text-ink-muted">{role}</p>
      </div>
      {phone && !phone.includes('x') ? (
        <a href={`tel:${phone}`} className="min-h-[44px] rounded-lg bg-primary px-3 py-2 font-semibold text-white">📞 {t('call')}</a>
      ) : (
        <span className="text-sm text-ink-muted">{phone}</span>
      )}
    </div>
  );
}

/** Vertical lifecycle stepper with shared wording (UI-UX §13 CaseStepper, §14). */
export function CaseStepper({ status, events }: { status: string; events: { toStatus?: string | null; occurredAt: string }[] }) {
  const { lang } = useI18n();
  const order = ['created', 'matched', 'accepted', 'transport_assigned', 'in_transit', 'arrived_seen', 'closed', 'follow_up'];
  const at: Record<string, string> = {};
  for (const e of events) if (e.toStatus) at[e.toStatus] = e.occurredAt;
  const cur = order.indexOf(status);
  return (
    <ol className="space-y-2" aria-label="Case progress">
      {order.map((s, i) => {
        const done = i <= cur && status !== 'cancelled';
        const label = lang === 'hi' ? STATUS[s].hi : STATUS[s].en;
        return (
          <li key={s} className="flex items-center gap-3">
            <span aria-hidden="true" className={`flex h-7 w-7 items-center justify-center rounded-full text-sm font-bold ${
              done ? 'bg-success text-white' : i === cur + 1 ? 'bg-warning text-ink' : 'bg-slate-200 text-ink-muted'}`}>
              {done ? '✔' : i + 1}
            </span>
            <span className={done ? 'font-semibold' : 'text-ink-muted'}>{label}</span>
            {at[s] && <time className="ml-auto text-sm text-ink-muted">{new Date(at[s]).toLocaleTimeString()}</time>}
          </li>
        );
      })}
      {status === 'cancelled' && <li><CaseStatusChip status="cancelled" /></li>}
    </ol>
  );
}

/** Modal confirmation (UI-UX §13 ConfirmDialog) using the native <dialog> — focus trap + Esc for keyboard users. */
export function ConfirmDialog({ open, title, children, onClose }: { open: boolean; title: string; children: ReactNode; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog ref={ref} onClose={onClose} aria-labelledby="dlg-title"
      className="w-full max-w-lg rounded-2xl p-0 backdrop:bg-black/40">
      <div className="p-6">
        <h2 id="dlg-title" className="mb-4 text-xl font-bold">{title}</h2>
        {children}
      </div>
    </dialog>
  );
}

export function FacilityCapabilityChips({ matched, missing, labels }: { matched: string[]; missing: string[]; labels: Record<string, string> }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {matched.map((c) => <StatusChip key={c} tone="done" label={labels[c] ?? c} />)}
      {missing.map((c) => (
        <span key={c} className="inline-flex items-center gap-1 rounded-full border border-slate-300 px-2.5 py-0.5 text-sm text-offline">
          <span aria-hidden="true">✖</span>{labels[c] ?? c}
        </span>
      ))}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const e = error as { title?: string; detail?: string | null; code?: string };
  return (
    <p role="alert" className="rounded-lg border border-emergency/40 bg-emergency/5 p-3 text-emergency">
      ✖ {e.title ?? 'Error'}{e.detail ? ` — ${e.detail}` : ''}{e.code ? ` (${e.code})` : ''}
    </p>
  );
}

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="space-y-3" aria-busy="true" aria-live="polite">
      {Array.from({ length: lines }).map((_, i) => <div key={i} className="h-16 animate-pulse rounded-xl bg-slate-200" />)}
    </div>
  );
}
