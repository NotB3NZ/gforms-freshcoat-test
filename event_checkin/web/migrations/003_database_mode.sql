-- A preview database must never become an operational admission database.
CREATE TABLE IF NOT EXISTS web_database_mode (
 id INTEGER PRIMARY KEY CHECK (id = 1), mode TEXT NOT NULL
);
