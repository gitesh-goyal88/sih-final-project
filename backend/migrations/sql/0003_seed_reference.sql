-- 0003 — reference data from database.md §11 (idempotent: ON CONFLICT DO NOTHING).
-- Clinical items are PLACEHOLDERS until the clinical mentor signs off (TRD Q6).
-- Hindi labels not given in the spec (symptoms, risk-rule descriptions, SMS templates) were
-- written for this build and MUST be reviewed by a native speaker (UI-UX §4.4).

-- §11.1 Capabilities
INSERT INTO capabilities (code, group_name, label_en, label_hi, sort_order) VALUES
  ('doctor_on_duty',         'staff',     'Doctor on duty',                  'ड्यूटी पर डॉक्टर',       10),
  ('specialist_obgyn',       'staff',     'Gynaecologist',                   'स्त्री रोग विशेषज्ञ',     20),
  ('specialist_paediatrics', 'staff',     'Child specialist',                'बाल रोग विशेषज्ञ',       30),
  ('delivery',               'clinical',  'Normal delivery',                 'सामान्य प्रसव',          40),
  ('obstetric_emergency',    'clinical',  'Emergency obstetric care (EmOC)', 'आपात प्रसूति सेवा',      50),
  ('c_section',              'clinical',  'C-section / OT',                  'ऑपरेशन (सी-सेक्शन)',     60),
  ('sncu',                   'clinical',  'Sick newborn care unit',          'नवजात गहन इकाई',        70),
  ('nicu',                   'clinical',  'NICU',                            'एनआईसीयू',              80),
  ('trauma_stabilisation',   'clinical',  'Trauma stabilisation',            'चोट का प्राथमिक उपचार',   90),
  ('emergency_opd',          'service',   'Emergency OPD',                   'आपातकालीन ओपीडी',        100),
  ('icu',                    'clinical',  'ICU',                             'आईसीयू',                110),
  ('oxygen',                 'equipment', 'Oxygen',                          'ऑक्सीजन',               120),
  ('ventilator',             'equipment', 'Ventilator',                      'वेंटिलेटर',              130),
  ('x_ray',                  'equipment', 'X-ray',                           'एक्स-रे',               140),
  ('blood_bank',             'service',   'Blood available',                 'खून उपलब्ध',            150),
  ('lab_basic',              'service',   'Basic lab',                       'जाँच लैब',              160),
  ('ambulance_base',         'service',   'Ambulance based here',            'एम्बुलेंस यहाँ है',        170)
ON CONFLICT DO NOTHING;

-- §11.2 Emergency categories (UI §6.2 tiles; TRD §7.1 letters; IVR ≤ 4 per menu)
INSERT INTO emergency_categories (code, sms_letter, ivr_digit, label_en, label_hi, sort_order) VALUES
  ('pregnancy',   'P', 1,    'Pregnancy / labour', 'प्रसव',              1),
  ('newborn',     'N', 2,    'Newborn',            'नवजात',              2),
  ('injury',      'I', 3,    'Injury / accident',  'चोट',                3),
  ('breathing',   'B', 4,    'Breathing problem',  'साँस की तकलीफ़',       4),
  ('unconscious', 'U', NULL, 'Unconscious / fits', 'बेहोशी / दौरा',        5),
  ('other',       'O', NULL, 'Other',              'अन्य',               6)
ON CONFLICT DO NOTHING;

-- §11.2 default capabilities (national, district_code NULL)
INSERT INTO category_capability_defaults (district_code, category_code, capability_code, condition_code) VALUES
  (NULL, 'pregnancy',   'obstetric_emergency',  NULL),
  (NULL, 'pregnancy',   'c_section',            'c_section_flag'),
  (NULL, 'newborn',     'sncu',                 NULL),
  (NULL, 'injury',      'trauma_stabilisation', NULL),
  (NULL, 'injury',      'x_ray',                NULL),
  (NULL, 'breathing',   'oxygen',               NULL),
  (NULL, 'breathing',   'ventilator',           'spo2_lt_90'),
  (NULL, 'unconscious', 'emergency_opd',        NULL),
  (NULL, 'unconscious', 'doctor_on_duty',       NULL),
  (NULL, 'other',       'emergency_opd',        NULL)
ON CONFLICT DO NOTHING;

-- §11.3 Default configuration (district_code = NULL)
INSERT INTO config_entries (district_code, key, value, description) VALUES
  (NULL, 'cascade.offer_timeout_s',               '180',          'TRD §5.2'),
  (NULL, 'cascade.total_timeout_s',               '600',          'TRD §5.2'),
  (NULL, 'cascade.max_fails_before_escalation',   '3',            'TRD §9'),
  (NULL, 'cascade.offer_mode',                    '"sequential"', 'FR-C01, Q1'),
  (NULL, 'cascade.sms_nudge_after_s',             '60',           'TRD §9'),
  (NULL, 'matching.radius_m',                     '100000',       'TRD §8'),
  (NULL, 'matching.stale_after_h',                '12',           'PRD / TRD §8'),
  (NULL, 'facility.capability_reminder_after_h',  '6',            'UI §7.2'),
  (NULL, 'volunteer.round_timeout_s',             '120',          'TRD §10.2'),
  (NULL, 'volunteer.parallel_offers',             '3',            'ADR-09'),
  (NULL, 'volunteer.ping_fresh_s',                '600',          'TRD §10.2'),
  (NULL, 'volunteer.offer_expiry_s',              '45',           'UI §6.4 "Auto-skip in 0:45"'),
  (NULL, 'notify.push_ack_timeout_s',             '60',           'TRD §11'),
  (NULL, 'notify.sms_to_ivr_after_s',             '180',          'TRD §11'),
  (NULL, 'sla.created_to_matched_s',              '30',           'TRD §5.2'),
  (NULL, 'sla.leg_eta_multiplier',                '2',            'TRD §5.2'),
  (NULL, 'sla.leg_eta_buffer_s',                  '900',          'TRD §5.2'),
  (NULL, 'sla.close_reminder_h',                  '24',           'TRD §5.2'),
  (NULL, 'sla.close_escalation_h',                '72',           'TRD §5.2'),
  (NULL, 'followup.after_close_days',             '3',            'TRD T10'),
  (NULL, 'incentive.transport_trip',              '10',           'placeholder, district-editable'),
  (NULL, 'incentive.referral_closed',             '5',            'placeholder, district-editable'),
  (NULL, 'incentive.asha_followup',               '2',            'placeholder, district-editable'),
  (NULL, 'i18n.languages',                        '["hi","en"]',  'TRD §15.4 — regional language pending TRD Q5'),
  (NULL, 'sms.dedupe_window_min',                 '30',           'TRD §7.2')
ON CONFLICT DO NOTHING;

-- §11.5 Symptoms (Yes/No tiles)
INSERT INTO symptoms (code, cohort, label_en, label_hi, sort_order) VALUES
  ('bleeding',               'pregnant', 'Bleeding',                    'खून आना',                10),
  ('convulsions',            'pregnant', 'Fits / convulsions',          'दौरे पड़ना',              20),
  ('severe_headache',        'pregnant', 'Severe headache',             'तेज़ सिरदर्द',             30),
  ('blurred_vision',         'pregnant', 'Blurred vision',              'धुंधला दिखना',            40),
  ('reduced_fetal_movement', 'pregnant', 'Baby moving less',            'बच्चे की हलचल कम',        50),
  ('water_broke',            'pregnant', 'Water broke',                 'पानी की थैली फटना',        60),
  ('swelling_face_hands',    'pregnant', 'Swelling of face or hands',   'चेहरे या हाथों पर सूजन',    70),
  ('not_feeding',            'newborn',  'Not feeding',                 'दूध नहीं पी रहा',          10),
  ('fast_breathing',         'newborn',  'Fast breathing',              'तेज़ साँस',               20),
  ('cold_to_touch',          'newborn',  'Cold to touch',               'छूने पर ठंडा',            30),
  ('yellow_skin',            'newborn',  'Yellow skin or eyes',         'त्वचा या आँखें पीली',       40),
  ('umbilical_redness',      'newborn',  'Redness around navel',        'नाल के पास लाली',          50),
  ('chest_pain',             'chronic',  'Chest pain',                  'सीने में दर्द',             10),
  ('breathlessness',         'chronic',  'Short of breath',             'साँस फूलना',              20),
  ('missed_medicines',       'chronic',  'Missed medicines',            'दवा छूट गई',              30),
  ('fever',                  NULL,       'Fever',                       'बुखार',                  10),
  ('vomiting',               NULL,       'Vomiting',                    'उल्टी',                  20),
  ('unconscious',            NULL,       'Unconscious',                 'बेहोश',                  30),
  ('injury_bleeding',        NULL,       'Injury with bleeding',        'चोट से खून बहना',          40)
ON CONFLICT DO NOTHING;

-- §11.4 Initial risk rules (rule_set_version = 1, national)
INSERT INTO risk_rules (rule_set_version, district_code, code, cohort, description_en, description_hi, expression, follow_up_days) VALUES
  (1, NULL, 'preg_bp_high',      'pregnant', 'BP 140/90 or higher in pregnancy', 'गर्भावस्था में बीपी 140/90 या अधिक',
     '{"any":[{"field":"bp_systolic","op":">=","value":140},{"field":"bp_diastolic","op":">=","value":90}]}', 1),
  (1, NULL, 'preg_hb_severe',    'pregnant', 'Hb below 7 g/dL', 'हीमोग्लोबिन 7 से कम',
     '{"all":[{"field":"hb_g_dl","op":"<","value":7}]}', 2),
  (1, NULL, 'preg_bleeding',     'pregnant', 'Bleeding in pregnancy', 'गर्भावस्था में खून आना',
     '{"any":[{"symptom":"bleeding","present":true}]}', 1),
  (1, NULL, 'preg_convulsions',  'pregnant', 'Convulsions in pregnancy', 'गर्भावस्था में दौरे',
     '{"any":[{"symptom":"convulsions","present":true}]}', 1),
  (1, NULL, 'preg_reduced_fm',   'pregnant', 'Reduced fetal movement', 'बच्चे की हलचल कम',
     '{"any":[{"symptom":"reduced_fetal_movement","present":true}]}', 1),
  (1, NULL, 'nb_temp',           'newborn',  'Newborn temperature below 35.5 or above 37.5 °C', 'नवजात का तापमान 35.5 से कम या 37.5 से अधिक',
     '{"any":[{"field":"temp_c","op":"<","value":35.5},{"field":"temp_c","op":">","value":37.5}]}', 1),
  (1, NULL, 'nb_low_weight',     'newborn',  'Newborn weight below 2.0 kg', 'नवजात का वज़न 2 किलो से कम',
     '{"all":[{"field":"weight_kg","op":"<","value":2.0}]}', 2),
  (1, NULL, 'nb_not_feeding',    'newborn',  'Newborn not feeding', 'नवजात दूध नहीं पी रहा',
     '{"any":[{"symptom":"not_feeding","present":true}]}', 1),
  (1, NULL, 'nb_fast_breathing', 'newborn',  'Newborn breathing 60 or more per minute', 'नवजात की साँस 60 या अधिक प्रति मिनट',
     '{"all":[{"field":"resp_rate","op":">=","value":60}]}', 1),
  (1, NULL, 'chr_bp_crisis',     'chronic',  'BP 180/110 or higher', 'बीपी 180/110 या अधिक',
     '{"any":[{"field":"bp_systolic","op":">=","value":180},{"field":"bp_diastolic","op":">=","value":110}]}', 1),
  (1, NULL, 'chr_rbs_high',      'chronic',  'Random blood sugar above 300 mg/dL', 'शुगर 300 से अधिक',
     '{"all":[{"field":"rbs_mg_dl","op":">","value":300}]}', 2),
  (1, NULL, 'any_spo2_low',      'any',      'Oxygen (SpO2) below 94%', 'ऑक्सीजन 94% से कम',
     '{"all":[{"field":"spo2_pct","op":"<","value":94}]}', 1),
  (1, NULL, 'any_pulse',         'any',      'Pulse above 120 or below 50', 'नब्ज़ 120 से अधिक या 50 से कम',
     '{"any":[{"field":"pulse_bpm","op":">","value":120},{"field":"pulse_bpm","op":"<","value":50}]}', 1),
  (1, NULL, 'any_fever_high',    'any',      'Temperature 39 °C or higher', 'बुखार 39 °C या अधिक',
     '{"all":[{"field":"temp_c","op":">=","value":39.0}]}', 2)
ON CONFLICT DO NOTHING;

-- API-Guide §10.2 / §10.5 outbound SMS templates (DLT ids are filled in at pilot, TRD R-06).
-- Allowed content only: short code, category letter, facility name, driver first name + phone,
-- ETA minutes. Never patient names, ages, conditions or free text (SEC-CH-06).
INSERT INTO message_templates (code, language, channel, body) VALUES
  ('sos_received',         'en', 'sms', 'AapatMitra: Help is being arranged. Case {case}. Keep phone on.'),
  ('sos_received',         'hi', 'sms', 'AapatMitra: Madad ki vyavastha ho rahi hai. Case {case}. Phone chalu rakhein.'),
  ('sos_app_ack',          'en', 'sms', 'AM OK {case}'),
  ('sos_app_ack',          'hi', 'sms', 'AM OK {case}'),
  ('sos_unverified',       'en', 'sms', 'AapatMitra: Case {case}. Reply with your village name. Your ASHA will call you.'),
  ('sos_unverified',       'hi', 'sms', 'AapatMitra: Case {case}. Apne gaon ka naam likh kar bhejein. Aapki ASHA call karegi.'),
  ('family_accepted',      'en', 'sms', 'AapatMitra {case}: {facility} accepted. Driver {driver} ({driver_phone}) coming in {eta} min.'),
  ('family_accepted',      'hi', 'sms', 'AapatMitra {case}: {facility} ne haan kaha. Driver {driver} ({driver_phone}) {eta} min mein aa rahe hain.'),
  ('family_hospital_yes',  'en', 'sms', 'AapatMitra {case}: {facility} accepted. A driver is being arranged.'),
  ('family_hospital_yes',  'hi', 'sms', 'AapatMitra {case}: {facility} ne haan kaha. Driver ki vyavastha ho rahi hai.'),
  ('family_finding',       'en', 'sms', 'AapatMitra {case}: Finding another hospital. Help is still coming.'),
  ('family_finding',       'hi', 'sms', 'AapatMitra {case}: Doosra aspatal dhoondh rahe hain. Madad aa rahi hai.'),
  ('family_closed',        'en', 'sms', 'AapatMitra {case}: Treatment done. Your ASHA will visit for follow-up.'),
  ('family_closed',        'hi', 'sms', 'AapatMitra {case}: Ilaaj ho gaya. Aapki ASHA milne aayengi.'),
  ('ride_offer',           'en', 'sms', 'AapatMitra RIDE {case}: Emergency, {village}, {km}km. Reply 1 {case}=Accept 2 {case}=Can''t go'),
  ('ride_offer',           'hi', 'sms', 'AapatMitra RIDE {case}: Emergency, {village}, {km}km. 1 {case}=Haan 2 {case}=Nahin ja sakte'),
  ('ride_confirmed',       'en', 'sms', 'AapatMitra RIDE {case}: You have the ride. Open the app for the pickup point.'),
  ('ride_confirmed',       'hi', 'sms', 'AapatMitra RIDE {case}: Ride aapki hai. Pickup ke liye app kholein.'),
  ('ride_taken',           'en', 'sms', 'AapatMitra RIDE {case}: Already taken. Thank you.'),
  ('ride_taken',           'hi', 'sms', 'AapatMitra RIDE {case}: Ride le li gayi hai. Dhanyavaad.'),
  ('facility_offer_nudge', 'en', 'sms', 'AapatMitra: New case {case} ({cat}) waiting {min} min. Open console to accept.'),
  ('facility_offer_nudge', 'hi', 'sms', 'AapatMitra: Naya case {case} ({cat}) {min} min se intezaar mein. Console kholein.'),
  ('handover_code',        'en', 'sms', 'AapatMitra {case}: Your handover code is {code}. Tell it to the volunteer when you take the patient.'),
  ('handover_code',        'hi', 'sms', 'AapatMitra {case}: Aapka handover code {code} hai. Mareez lete samay volunteer ko batayein.'),
  ('escalation_admin',     'en', 'sms', 'AapatMitra: Case {case} needs attention ({reason}). Open the console.'),
  ('escalation_admin',     'hi', 'sms', 'AapatMitra: Case {case} par dhyan dein ({reason}). Console kholein.'),
  ('status_reply',         'en', 'sms', 'AapatMitra {case}: {status}'),
  ('status_reply',         'hi', 'sms', 'AapatMitra {case}: {status}'),
  ('otp',                  'en', 'sms', '<#> {otp} is your AapatMitra code. Do not share. {app_hash}'),
  ('otp',                  'hi', 'sms', '<#> {otp} aapka AapatMitra code hai. Kisi ko na batayein. {app_hash}')
ON CONFLICT DO NOTHING;
