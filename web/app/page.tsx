'use client';

import { useRouter } from 'next/navigation';
import { useEffect } from 'react';
import { homeFor, useSession } from '@/lib/session';

export default function Home() {
  const { user, ready } = useSession();
  const router = useRouter();
  useEffect(() => {
    if (ready) router.replace(user ? homeFor(user.role) : '/login');
  }, [ready, user, router]);
  return <p className="p-8" aria-busy="true">…</p>;
}
