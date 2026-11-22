-- Portable SQL: PostgreSQL and SQLite. All times are UTC epoch seconds.
-- The migration runner holds the events row during mutations (BEGIN IMMEDIATE on SQLite).
CREATE TABLE IF NOT EXISTS events (
 event_id TEXT PRIMARY KEY, timezone TEXT NOT NULL, admissions_open INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS participants (
 event_id TEXT NOT NULL REFERENCES events(event_id), participant_id TEXT NOT NULL,
 record_id TEXT NOT NULL, source_id TEXT NOT NULL, price_category TEXT NOT NULL,
 token_digest TEXT, identity_digest TEXT NOT NULL, name TEXT NOT NULL, email TEXT NOT NULL,
 tier TEXT NOT NULL, eligible INTEGER NOT NULL, reason TEXT NOT NULL,
 PRIMARY KEY(event_id, participant_id), UNIQUE(event_id,record_id), UNIQUE(token_digest)
);
CREATE TABLE IF NOT EXISTS staff_sessions (
 session_id TEXT PRIMARY KEY, cookie_digest TEXT NOT NULL UNIQUE, csrf TEXT NOT NULL,
 nickname TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attendance (
 event_id TEXT NOT NULL, participant_id TEXT NOT NULL,
 checked_in_at DOUBLE PRECISION NOT NULL, checked_in_by TEXT NOT NULL,
 staff_session_id TEXT REFERENCES staff_sessions(session_id), method TEXT NOT NULL,
 request_id TEXT NOT NULL, PRIMARY KEY(event_id,participant_id),
 FOREIGN KEY(event_id,participant_id) REFERENCES participants(event_id,participant_id)
);
CREATE TABLE IF NOT EXISTS requests (
 event_id TEXT NOT NULL, request_id TEXT NOT NULL, participant_id TEXT NOT NULL,
 method TEXT NOT NULL, staff_session_id TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(event_id,request_id)
);
CREATE TABLE IF NOT EXISTS outbox (
 event_id TEXT NOT NULL, participant_id TEXT NOT NULL, request_id TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0,
 delivered_at DOUBLE PRECISION, PRIMARY KEY(event_id,participant_id),
 FOREIGN KEY(event_id,participant_id) REFERENCES attendance(event_id,participant_id)
);
CREATE TABLE IF NOT EXISTS sync_state (
 event_id TEXT PRIMARY KEY REFERENCES events(event_id), last_success DOUBLE PRECISION,
 last_attempt DOUBLE PRECISION, error TEXT NOT NULL DEFAULT '', export_error TEXT NOT NULL DEFAULT '',
 lease_owner TEXT NOT NULL DEFAULT '', lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
 requested INTEGER NOT NULL DEFAULT 1, omitted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS login_attempts (
 bucket TEXT PRIMARY KEY, window_at DOUBLE PRECISION NOT NULL, attempts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS participants_search ON participants(event_id,email);
CREATE INDEX IF NOT EXISTS attendance_recent ON attendance(event_id,checked_in_at);
