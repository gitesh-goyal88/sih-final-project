/** @type {import('next').NextConfig} */
const API_ORIGIN = process.env.AM_API_ORIGIN || 'http://localhost:8000';

const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  output: 'standalone',
  outputFileTracingRoot: import.meta.dirname,
  // Local dev: proxy the API so the refresh cookie stays first-party (FR-W02). Compose/NGINX does this in demo/prod.
  async rewrites() {
    return process.env.NODE_ENV === 'development' || process.env.AM_API_ORIGIN
      ? [{ source: '/api/v1/:path*', destination: `${API_ORIGIN}/api/v1/:path*` }]
      : [];
  },
  // SECURITY Appendix B (CSP with a per-request nonce is set in middleware.ts)
  async headers() {
    return [{
      source: '/:path*',
      headers: [
        { key: 'X-Content-Type-Options', value: 'nosniff' },
        { key: 'Referrer-Policy', value: 'no-referrer' },
        { key: 'Cross-Origin-Opener-Policy', value: 'same-origin' },
        { key: 'Cross-Origin-Resource-Policy', value: 'same-origin' },
        { key: 'Strict-Transport-Security', value: 'max-age=31536000; includeSubDomains' },
      ],
    }];
  },
};

export default nextConfig;
