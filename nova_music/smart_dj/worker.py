"""Run playlist analysis away from the Qt UI thread."""
from __future__ import annotations

import sqlite3

from PySide6.QtCore import QThread, Signal

from .recommender import SmartDJRecommender


class SmartDJWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, database_path: str, mode: str, diversity: int,
                 exclude_paths=(), parent=None):
        super().__init__(parent)
        self.database_path = database_path
        self.mode = mode
        self.diversity = diversity
        self.exclude_paths = tuple(exclude_paths)

    def run(self) -> None:
        conn = None
        try:
            conn = sqlite3.connect(self.database_path, timeout=10)
            conn.row_factory = None
            dj = SmartDJRecommender(conn)
            payload = {
                "recommendations": dj.build_playlist(
                    self.mode, self.diversity, 50, self.exclude_paths
                ),
                "profile": dj.profile(),
                "history": dj.history(100),
                "top_played": dj.top_tracks(False),
                "top_skipped": dj.top_tracks(True),
            }
            self.ready.emit(payload)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                conn.close()
