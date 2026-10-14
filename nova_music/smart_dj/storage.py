"""Additive SQLite migration and event storage for Smart DJ."""
from __future__ import annotations

import sqlite3


def initialize(conn: sqlite3.Connection) -> None:
    """Upgrade the existing library in place without dropping user data."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
    for name, sql_type in (("genre", "TEXT"), ("year", "INTEGER"), ("track_number", "INTEGER")):
        if name not in columns:
            conn.execute(f"ALTER TABLE tracks ADD COLUMN {name} {sql_type}")

    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_tracks_genre ON tracks(genre);
        CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist);
        CREATE TABLE IF NOT EXISTS listening_sessions (
            session_id TEXT PRIMARY KEY,
            track_path TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            listened_ms INTEGER NOT NULL DEFAULT 0,
            completion_ratio REAL NOT NULL DEFAULT 0,
            outcome TEXT,
            manual_selection INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS listening_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_path TEXT NOT NULL,
            event_type TEXT NOT NULL,
            listened_ms INTEGER NOT NULL DEFAULT 0,
            completion_ratio REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            session_id TEXT
        );
        CREATE TABLE IF NOT EXISTS smart_dj_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS playlist_tracks (
            playlist_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            track_path TEXT NOT NULL,
            PRIMARY KEY (playlist_id, position),
            UNIQUE (playlist_id, track_path)
        );
        CREATE INDEX IF NOT EXISTS idx_listening_sessions_track
            ON listening_sessions(track_path, started_at);
        CREATE INDEX IF NOT EXISTS idx_listening_sessions_outcome
            ON listening_sessions(outcome, started_at);
        CREATE INDEX IF NOT EXISTS idx_listening_events_created
            ON listening_events(created_at);
        CREATE INDEX IF NOT EXISTS idx_listening_events_session
            ON listening_events(session_id, id);
    """)
    conn.commit()
