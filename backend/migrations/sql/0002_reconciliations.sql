-- 0002 — schema changes that later design documents require on top of database.md v2.0.
-- Each item cites the document that asks for it. Expand-only (database.md §13).

-- D5 / SEC-API-07 / SF-12: idempotency store keyed by (actor_id, key), not key alone.
-- API-Guide Appendix F D5: "schema needs a migration to change the PK".
-- actor_id is NULL for webhook-derived keys, so a NULLS NOT DISTINCT unique index is used
-- instead of a primary key (PK columns cannot be NULL).
ALTER TABLE idempotency_keys DROP CONSTRAINT idempotency_keys_pkey;
CREATE UNIQUE INDEX ik_actor_key_uq ON idempotency_keys (actor_id, key) NULLS NOT DISTINCT;

-- SF-01 / SEC-CUS-01 / SEC-MOB-09: "the server stores the public key on devices".
-- API-Guide §3.2: device.publicKeyEd25519 (base64 SPKI) sent in /auth/otp/verify.
ALTER TABLE devices ADD COLUMN public_key_spki bytea;

-- SEC-CH-04 / SF-13: verification level recorded; API-Guide §5 Case.verificationLevel.
ALTER TABLE cases ADD COLUMN verification_level text NOT NULL DEFAULT 'app';
ALTER TABLE cases ADD CONSTRAINT cases_verification_level_ck CHECK (verification_level IN
  ('app','code','tag','number_only','callback','none'));

-- API-Guide §7.1: arrived_seen --> cancelled (T11, patient_deceased / duplicate only — service guard).
INSERT INTO case_status_transitions (from_status, to_status, trd_ref) VALUES
  ('arrived_seen', 'cancelled', 'T11')
ON CONFLICT DO NOTHING;

-- SEC-FRD-04 / API-Guide §6.9: POST /me/leaderboard-opt-out.
ALTER TABLE users ADD COLUMN leaderboard_opt_out boolean NOT NULL DEFAULT false;
