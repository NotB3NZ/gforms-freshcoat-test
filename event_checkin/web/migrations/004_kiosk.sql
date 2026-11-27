-- Kiosk is a server-controlled property of an authenticated staff session.
-- Attendance keeps its existing QR/manual meaning and export format.
ALTER TABLE staff_sessions ADD COLUMN kiosk INTEGER NOT NULL DEFAULT 0;
ALTER TABLE staff_sessions ADD COLUMN kiosk_seconds INTEGER NOT NULL DEFAULT 5;
ALTER TABLE staff_sessions ADD COLUMN kiosk_muted INTEGER NOT NULL DEFAULT 0;
