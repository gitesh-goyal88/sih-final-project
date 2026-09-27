'use client';

import { useMutation, useQuery } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { Button, Card, ErrorNote } from '@/components/ui';
import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import { uuidv7 } from '@/lib/uuid';

/** Staff are pre-provisioned by the district admin only — no self sign-up (SEC-ID-07, TRD Q4). */
export default function UsersPage() {
  const { t } = useI18n();
  const villages = useQuery({ queryKey: ['villages'], queryFn: () => api<{ data: { id: string; name: string }[] }>('/villages') });
  const facilities = useQuery({ queryKey: ['admin', 'facilities'], queryFn: () => api<{ data: { id: string; name: string }[] }>('/facilities') });
  const devices = useQuery({ queryKey: ['admin', 'devices'], queryFn: () => api<{ data: { id: string; role: string; platform: string; lastSyncAt?: string; revoked: boolean }[] }>('/admin/devices') });
  const [form, setForm] = useState({ role: 'asha', staffId: '', name: '', phone: '', villageIds: [] as string[], facilityId: '' });
  const create = useMutation({
    mutationFn: () => api('/admin/users', { body: { id: uuidv7(), role: form.role, staffId: form.staffId, name: form.name, phone: form.phone,
      villageIds: form.role === 'asha' ? form.villageIds : [], facilityId: ['doctor', 'facility_staff'].includes(form.role) ? form.facilityId || null : null } }),
  });
  const revoke = useMutation({
    mutationFn: (id: string) => api(`/admin/devices/${id}/revoke`, { body: { wipe: true, reason: 'Lost or stolen device reported to admin' } }),
    onSuccess: () => void devices.refetch(),
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    create.mutate();
  }
  const field = 'mt-1 min-h-[48px] w-full rounded-lg border-2 px-3';
  return (
    <div className="max-w-3xl space-y-6">
      <h1 className="text-2xl font-bold">{t('nav.users')}</h1>
      <Card>
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2">
          <label>{t('users.role')}<select className={field} value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
            {['asha', 'doctor', 'facility_staff', 'district_admin'].map((r) => <option key={r} value={r}>{t(`role.${r}`)}</option>)}</select></label>
          <label>{t('login.staffId')}<input className={field} required value={form.staffId} onChange={(e) => setForm({ ...form, staffId: e.target.value })} /></label>
          <label>{t('users.name')}<input className={field} required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
          <label>{t('users.phone')}<input className={field} required value={form.phone} placeholder="+91…" onChange={(e) => setForm({ ...form, phone: e.target.value })} /></label>
          {form.role === 'asha' && (
            <fieldset className="sm:col-span-2"><legend>{t('users.villages')}</legend>
              <div className="flex flex-wrap gap-2">{(villages.data?.data ?? []).map((v) => (
                <label key={v.id} className="flex min-h-[44px] items-center gap-2 rounded-lg border px-3">
                  <input type="checkbox" checked={form.villageIds.includes(v.id)}
                    onChange={(e) => setForm({ ...form, villageIds: e.target.checked ? [...form.villageIds, v.id] : form.villageIds.filter((x) => x !== v.id) })} />{v.name}
                </label>))}</div>
            </fieldset>
          )}
          {['doctor', 'facility_staff'].includes(form.role) && (
            <label className="sm:col-span-2">{t('admin.facility')}<select className={field} value={form.facilityId} onChange={(e) => setForm({ ...form, facilityId: e.target.value })}>
              <option value="">—</option>{(facilities.data?.data ?? []).map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}</select></label>
          )}
          <Button type="submit" disabled={create.isPending} className="sm:col-span-2">{t('users.create')}</Button>
        </form>
        {create.isSuccess && <p role="status" className="mt-2 text-success">✔ {t('users.created')}</p>}
        <ErrorNote error={create.error} />
      </Card>
      <Card>
        <h2 className="mb-2 text-lg font-semibold">{t('users.devices')}</h2>
        <ul className="divide-y">{(devices.data?.data ?? []).map((d) => (
          <li key={d.id} className="flex items-center gap-2 py-2 text-sm">
            <span>{t(`role.${d.role}`)} · {d.platform}</span><span className="text-ink-muted">{d.lastSyncAt ? new Date(d.lastSyncAt).toLocaleString() : '—'}</span>
            <span className="ml-auto">{d.revoked ? t('users.revoked') : <Button variant="danger" onClick={() => revoke.mutate(d.id)}>{t('users.revokeWipe')}</Button>}</span>
          </li>))}</ul>
      </Card>
    </div>
  );
}
