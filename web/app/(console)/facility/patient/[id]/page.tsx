'use client';

import { use } from 'react';
import { PatientRecordView } from '@/components/PatientRecordView';

export default function PatientPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return <PatientRecordView patientId={id} />;
}
