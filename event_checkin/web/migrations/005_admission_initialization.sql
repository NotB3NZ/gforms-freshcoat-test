-- First successful production reconciliation may open admissions once.
-- Later restarts preserve both open and deliberately closed event states.
ALTER TABLE events ADD COLUMN admissions_initialized INTEGER NOT NULL DEFAULT 0;
UPDATE events SET admissions_initialized=1 WHERE admissions_open=1;
