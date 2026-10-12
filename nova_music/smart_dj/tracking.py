"""Persist playback behavior from actual output-frame and player-state signals."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ListeningTracker:
    CHECKPOINT_MS = 5_000

    def __init__(self, conn: sqlite3.Connection, sample_rate: int = 44_100, enabled: bool = True):
        self.conn = conn
        self.sample_rate = sample_rate
        self.enabled = enabled
        self.session_id: str | None = None
        self.track_path = ""
        self.duration_ms = 0
        self.listened_frames = 0
        self.pending_frames = 0
        self.last_output_counter = 0
        self.paused = False
        self.manual_selection = False

    @property
    def active(self) -> bool:
        return self.session_id is not None

    def start(self, path: str, duration_ms: int, manual_selection: bool = False,
              frame_counter: int = 0) -> None:
        if not self.enabled or not path:
            return
        if self.active and self.track_path == path and self.paused:
            self.last_output_counter = int(frame_counter)
            self.paused = False
            self._event("resumed")
            return
        if self.active:
            self.finish("skipped")
        self.session_id = uuid.uuid4().hex
        self.track_path = path
        self.duration_ms = max(0, int(duration_ms))
        self.listened_frames = self.pending_frames = 0
        self.last_output_counter = int(frame_counter)
        self.paused = False
        self.manual_selection = bool(manual_selection)
        now = _now()
        self.conn.execute(
            "INSERT INTO listening_sessions(session_id,track_path,started_at,duration_ms,manual_selection) VALUES(?,?,?,?,?)",
            (self.session_id, path, now, self.duration_ms, int(self.manual_selection)),
        )
        self._event("started", timestamp=now)
        if self.manual_selection:
            self._event("manual_selection", timestamp=now)
        self.conn.commit()

    def sync_frames(self, output_counter: int) -> None:
        output_counter = int(output_counter)
        if not self.enabled or not self.active:
            self.last_output_counter = output_counter
            return
        delta = max(0, output_counter - self.last_output_counter)
        self.last_output_counter = output_counter
        if self.paused or delta <= 0:
            return
        self.listened_frames += delta
        self.pending_frames += delta
        if self.pending_frames * 1000 >= self.sample_rate * self.CHECKPOINT_MS:
            self._flush_listened()

    def pause(self, frame_counter: int | None = None) -> None:
        if not self.active or self.paused:
            return
        if frame_counter is not None:self.sync_frames(frame_counter)
        self._flush_listened()
        self.paused = True
        self._event("paused")
        self.conn.commit()

    def finish(self, outcome: str, frame_counter: int | None = None) -> None:
        if not self.active:
            return
        if frame_counter is not None:self.sync_frames(frame_counter)
        self._flush_listened()
        ratio = self._ratio()
        now = _now()
        self.conn.execute(
            "UPDATE listening_sessions SET ended_at=?,completion_ratio=?,outcome=? WHERE session_id=?",
            (now, ratio, outcome, self.session_id),
        )
        self._event(outcome, timestamp=now)
        self.conn.commit()
        self.session_id = None
        self.track_path = ""
        self.duration_ms = 0
        self.listened_frames = self.pending_frames = 0
        self.paused = False
        self.last_output_counter = 0
        self.manual_selection = False

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self.enabled and not enabled:
            self.finish("stopped")
        self.enabled = enabled

    def _ratio(self) -> float:
        if self.duration_ms <= 0:
            return 0.0
        return min(1.0, (self.listened_frames * 1000 / self.sample_rate) / self.duration_ms)

    def _flush_listened(self) -> None:
        if not self.active or self.pending_frames <= 0:
            return
        delta_ms = int(self.pending_frames * 1000 / self.sample_rate)
        self.pending_frames = 0
        ratio = self._ratio()
        self.conn.execute(
            "UPDATE listening_sessions SET listened_ms=listened_ms+?,completion_ratio=? WHERE session_id=?",
            (delta_ms, ratio, self.session_id),
        )
        self._event("listened", listened_ms=delta_ms, ratio=ratio)
        self.conn.commit()

    def _event(self, event_type: str, listened_ms: int = 0, ratio: float | None = None,
               timestamp: str | None = None) -> None:
        if not self.session_id:
            return
        self.conn.execute(
            "INSERT INTO listening_events(track_path,event_type,listened_ms,completion_ratio,created_at,session_id) VALUES(?,?,?,?,?,?)",
            (self.track_path, event_type, int(listened_ms), self._ratio() if ratio is None else ratio,
             timestamp or _now(), self.session_id),
        )


def record_favorite(conn: sqlite3.Connection, path: str, favorite: bool) -> None:
    conn.execute(
        "INSERT INTO listening_events(track_path,event_type,created_at) VALUES(?,?,?)",
        (path, "favorite_added" if favorite else "favorite_removed", _now()),
    )
    conn.commit()
