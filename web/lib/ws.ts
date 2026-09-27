/**
 * WebSocket client (API-Guide §9, SEC-API-08): one-time ticket sent as the FIRST message (kept out of URLs),
 * heartbeat every 25 s, re-auth every 14 min, reconnect with backoff 1 s → 30 s + jitter.
 * Messages are nudges (IDs only) — consumers invalidate queries and re-fetch over REST.
 */
import { api } from './api';

export type WsMessage = { type: string; [k: string]: unknown };
type Listener = (m: WsMessage) => void;

export class ConsoleSocket {
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private channels = new Set<string>();
  private backoff = 1000;
  private timers: ReturnType<typeof setInterval>[] = [];
  private closed = false;
  status: 'connecting' | 'open' | 'closed' = 'closed';
  onStatus: ((s: ConsoleSocket['status']) => void) | null = null;

  on(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  subscribe(channel: string): void {
    this.channels.add(channel);
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: 'subscribe', channel, id: channel }));
  }

  unsubscribe(channel: string): void {
    this.channels.delete(channel);
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: 'unsubscribe', channel }));
  }

  send(msg: WsMessage): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  private setStatus(s: ConsoleSocket['status']) {
    this.status = s;
    this.onStatus?.(s);
  }

  async connect(): Promise<void> {
    this.closed = false;
    this.setStatus('connecting');
    let ticket: string;
    try {
      ticket = (await api<{ ticket: string }>('/ws/ticket', { method: 'POST' })).ticket;
    } catch {
      return this.retry();
    }
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/api/v1/ws`);
    this.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ type: 'auth', ticket }));
    ws.onmessage = (ev) => {
      let msg: WsMessage;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (msg.type === 'ready') {
        this.backoff = 1000;
        this.setStatus('open');
        for (const ch of this.channels) ws.send(JSON.stringify({ type: 'subscribe', channel: ch, id: ch }));
        this.clearTimers();
        this.timers.push(setInterval(() => ws.send(JSON.stringify({ type: 'ping' })), 25_000));
        this.timers.push(setInterval(() => void this.reauth(), 14 * 60_000));
      }
      if (msg.type === 'session.revoked') location.assign('/login');
      for (const l of this.listeners) l(msg);
    };
    ws.onclose = () => {
      this.clearTimers();
      this.setStatus('closed');
      if (!this.closed) this.retry();
    };
  }

  private async reauth(): Promise<void> {
    try {
      const { ticket } = await api<{ ticket: string }>('/ws/ticket', { method: 'POST' });
      this.ws?.send(JSON.stringify({ type: 'auth', ticket }));
    } catch {
      this.ws?.close();
    }
  }

  private retry(): Promise<void> {
    const wait = Math.min(this.backoff, 30_000) + Math.random() * 500;
    this.backoff = Math.min(this.backoff * 2, 30_000);
    return new Promise((resolve) => setTimeout(() => resolve(this.closed ? undefined : this.connect()), wait));
  }

  private clearTimers() {
    for (const t of this.timers) clearInterval(t);
    this.timers = [];
  }

  close(): void {
    this.closed = true;
    this.clearTimers();
    this.ws?.close();
  }
}
