import { NextResponse, type NextRequest } from 'next/server';

/**
 * SECURITY Appendix B: strict CSP with a per-request nonce, no third-party scripts, frame-ancestors 'none'.
 * Permissions-Policy grants camera/microphone only on the teleconsult route (SEC-WEB-01).
 */
export function middleware(req: NextRequest) {
  const nonce = btoa(crypto.randomUUID());
  const dev = process.env.NODE_ENV === 'development';
  const host = req.nextUrl.host;
  const tiles = new URL(process.env.NEXT_PUBLIC_TILE_URL || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'.replace(/[{}]/g, ''))
    .origin;
  const csp = [
    "default-src 'none'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ''}`,
    // dev HMR injects <style> tags; production ships CSS files and keeps the strict nonce policy
    dev ? "style-src 'self' 'unsafe-inline'" : `style-src 'self' 'nonce-${nonce}'`,
    `img-src 'self' data: blob: ${tiles}`,
    "font-src 'self'",
    `connect-src 'self' ws://${host} wss://${host} ${tiles}`,
    "media-src 'self' blob:",
    "worker-src 'self' blob:",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    ...(dev || req.nextUrl.protocol === 'http:' ? [] : ['upgrade-insecure-requests']),
  ].join('; ');
  const teleconsult = req.nextUrl.pathname.startsWith('/doctor/teleconsult');
  const permissions = teleconsult
    ? 'camera=(self), microphone=(self), geolocation=(), payment=(), usb=()'
    : 'camera=(), microphone=(), geolocation=(), payment=(), usb=()';

  const headers = new Headers(req.headers);
  headers.set('x-nonce', nonce);
  headers.set('Content-Security-Policy', csp);
  const res = NextResponse.next({ request: { headers } });
  res.headers.set('Content-Security-Policy', csp);
  res.headers.set('Permissions-Policy', permissions);
  return res;
}

export const config = {
  matcher: [{ source: '/((?!api|_next/static|_next/image|favicon.ico).*)' }],
};
