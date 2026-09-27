import type { Metadata } from 'next';
import type { ReactNode } from 'react';
import './globals.css';

// every page is rendered per request so Next can stamp the CSP nonce from middleware.ts on its scripts
export const dynamic = 'force-dynamic';
import { Providers } from './providers';

export const metadata: Metadata = {
  title: 'AapatMitra Console',
  description: 'Rural care access & continuity — facility, doctor and district console',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
