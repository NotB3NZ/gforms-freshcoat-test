-- Application backfill uses Unicode normalization/casefold on both database engines.
ALTER TABLE participants ADD COLUMN search_text TEXT NOT NULL DEFAULT '';
