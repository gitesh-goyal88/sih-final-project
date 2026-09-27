/**
 * One vocabulary for status everywhere (UI-UX §14, API-Guide Appendix E).
 * Status pills are icon + word + colour — never colour alone (WCAG 2.1 AA, FR-W03).
 */
export type Tone = 'done' | 'waiting' | 'problem' | 'offline' | 'info';

export const STATUS: Record<string, { en: string; hi: string; tone: Tone }> = {
  created: { en: 'SOS / Referral created', hi: 'SOS / रेफ़रल बना', tone: 'waiting' },
  matched: { en: 'Facility matched', hi: 'अस्पताल मिला', tone: 'waiting' },
  accepted: { en: 'Facility accepted', hi: 'अस्पताल ने स्वीकार किया', tone: 'done' },
  transport_assigned: { en: 'Transport assigned', hi: 'वाहन तय', tone: 'waiting' },
  in_transit: { en: 'In transit', hi: 'रास्ते में', tone: 'waiting' },
  arrived_seen: { en: 'Arrived & seen', hi: 'पहुँचे और देखे गए', tone: 'done' },
  closed: { en: 'Closed', hi: 'बंद', tone: 'done' },
  follow_up: { en: 'Follow-up task → ASHA', hi: 'फ़ॉलो-अप → ASHA', tone: 'info' },
  cancelled: { en: 'Cancelled', hi: 'रद्द', tone: 'offline' },
};

export const OFFER_RESULT: Record<string, { en: string; hi: string; tone: Tone }> = {
  pending: { en: 'Waiting', hi: 'इंतज़ार', tone: 'waiting' },
  accepted: { en: 'Accepted', hi: 'स्वीकार', tone: 'done' },
  declined: { en: 'Declined', hi: 'मना', tone: 'problem' },
  timeout: { en: 'No answer', hi: 'जवाब नहीं', tone: 'problem' },
  superseded: { en: 'Taken elsewhere', hi: 'कहीं और', tone: 'offline' },
  withdrawn: { en: 'Withdrawn', hi: 'वापस', tone: 'offline' },
};

// UI §7.2 decline options → TRD codes (API decision D7)
export const DECLINE_REASONS = [
  { code: 'no_specialist', en: 'No doctor', hi: 'डॉक्टर नहीं' },
  { code: 'no_bed', en: 'No bed', hi: 'बेड नहीं' },
  { code: 'equipment_down', en: 'No equipment', hi: 'उपकरण नहीं' },
  { code: 'not_our_capability', en: 'Not our service', hi: 'यह सेवा नहीं' },
  { code: 'other', en: 'Other', hi: 'अन्य' },
] as const;

export const CATEGORY: Record<string, { en: string; hi: string; icon: string }> = {
  pregnancy: { en: 'Pregnancy / labour', hi: 'प्रसव', icon: '🤰' },
  newborn: { en: 'Newborn', hi: 'नवजात', icon: '👶' },
  injury: { en: 'Injury / accident', hi: 'चोट', icon: '🩹' },
  breathing: { en: 'Breathing problem', hi: 'साँस की तकलीफ़', icon: '🫁' },
  unconscious: { en: 'Unconscious / fits', hi: 'बेहोशी / दौरा', icon: '😵' },
  other: { en: 'Other', hi: 'अन्य', icon: '➕' },
};

export const ESCALATION_REASON: Record<string, string> = {
  platform_fault: 'Not matched in 30 s (platform)',
  no_capable_facility: 'No capable facility nearby',
  cascade_exhausted: 'Facilities not accepting',
  volunteer_exhausted: 'No driver found',
  leg_sla_breach: 'Driver late on a leg',
  custodian_unresponsive: 'Driver not responding',
  closure_overdue: 'Not closed in 72 h',
  unverified_sms: 'Unverified SMS — call back',
  manual: 'Raised manually',
};
