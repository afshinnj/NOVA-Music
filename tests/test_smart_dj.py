import os
import random
import sqlite3
import tempfile
import unittest
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from nova_music.smart_dj.recommender import SmartDJRecommender, normalize_genre
from nova_music.smart_dj.storage import initialize
from nova_music.smart_dj.tracking import ListeningTracker, record_favorite


class SmartDJDatabaseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "library.sqlite3"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute(
            "CREATE TABLE tracks(path TEXT PRIMARY KEY,title TEXT,artist TEXT,album TEXT,"
            "duration REAL,favorite INTEGER DEFAULT 0)"
        )
        initialize(self.conn)
        self.tracker = ListeningTracker(self.conn, sample_rate=1000)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def add_track(self, name, artist="Artist", genre=None, favorite=0):
        path = self.root / name
        path.write_bytes(b"test audio placeholder")
        self.conn.execute(
            "INSERT INTO tracks(path,title,artist,album,duration,favorite,genre) VALUES(?,?,?,?,?,?,?)",
            (str(path), name, artist, "Album", 10.0, favorite, genre),
        )
        self.conn.commit()
        return str(path)

    def add_session(self, path, outcome, listened_ms, ratio, manual=False, stamp="2026-10-10T12:00:00+00:00"):
        import uuid
        session = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO listening_sessions(session_id,track_path,started_at,ended_at,duration_ms,"
            "listened_ms,completion_ratio,outcome,manual_selection) VALUES(?,?,?,?,?,?,?,?,?)",
            (session, path, stamp, stamp, 10_000, listened_ms, ratio, outcome, int(manual)),
        )
        return session

    def test_additive_migration_preserves_existing_library_and_is_idempotent(self):
        path = self.add_track("legacy.mp3")
        self.conn.execute("DELETE FROM tracks WHERE path=?", (path,))
        self.conn.execute("INSERT INTO tracks(path,title,artist,album,duration,favorite) VALUES(?,?,?,?,?,1)",
                          (path, "Legacy", "Singer", "Old album", 123.0))
        self.conn.commit()
        initialize(self.conn)
        initialize(self.conn)
        row = self.conn.execute("SELECT title,artist,album,duration,favorite,genre FROM tracks").fetchone()
        self.assertEqual(row, ("Legacy", "Singer", "Old album", 123.0, 1, None))
        self.assertTrue({"listening_events", "listening_sessions", "smart_dj_settings"}.issubset(
            {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}))

    def test_short_manual_skip_is_not_a_completed_play(self):
        path = self.add_track("short.mp3")
        self.tracker.start(path, 10_000, manual_selection=True, frame_counter=0)
        self.tracker.sync_frames(5_000)
        self.tracker.finish("skipped", frame_counter=5_000)
        session = self.conn.execute("SELECT listened_ms,completion_ratio,outcome,manual_selection FROM listening_sessions").fetchone()
        self.assertEqual(session, (5000, 0.5, "skipped", 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM listening_sessions WHERE outcome='completed'").fetchone()[0], 0)

    def test_decoder_error_is_neither_completion_nor_skip(self):
        path = self.add_track("broken.mp3")
        self.tracker.start(path, 10_000, frame_counter=0)
        self.tracker.sync_frames(1_000)
        self.tracker.finish("error", frame_counter=1_000)
        self.assertEqual(self.conn.execute("SELECT outcome FROM listening_sessions").fetchone()[0], "error")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM listening_sessions WHERE outcome='completed'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM listening_sessions WHERE outcome='skipped'").fetchone()[0], 0)

    def test_pause_resume_and_seek_do_not_add_synthetic_time_or_duplicate_session(self):
        path = self.add_track("pause.mp3")
        self.tracker.start(path, 30_000, frame_counter=0)
        self.tracker.sync_frames(4_000)
        self.tracker.pause(frame_counter=4_000)
        self.tracker.sync_frames(40_000)  # counter is sampled after paused playback; this interval is excluded
        self.tracker.start(path, 30_000, frame_counter=40_000)
        self.tracker.sync_frames(42_000)
        self.tracker.finish("stopped", frame_counter=42_000)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM listening_sessions").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT listened_ms FROM listening_sessions").fetchone()[0], 6000)
        event_types = [r[0] for r in self.conn.execute("SELECT event_type FROM listening_events")]
        self.assertEqual(event_types.count("started"), 1)
        self.assertEqual(event_types.count("resumed"), 1)

    def test_checkpoints_survive_reopening_the_database(self):
        path = self.add_track("checkpoint.mp3")
        self.tracker.start(path, 20_000, frame_counter=0)
        self.tracker.sync_frames(5_000)
        self.conn.close()
        reopened = sqlite3.connect(self.db_path)
        self.assertEqual(reopened.execute("SELECT listened_ms FROM listening_sessions").fetchone()[0], 5000)
        reopened.close()

    def test_favorite_changes_are_logged_only_when_state_changes(self):
        path = self.add_track("favorite.mp3")
        record_favorite(self.conn, path, True)
        record_favorite(self.conn, path, False)
        kinds = [r[0] for r in self.conn.execute("SELECT event_type FROM listening_events ORDER BY id")]
        self.assertEqual(kinds, ["favorite_added", "favorite_removed"])

    def test_empty_genre_is_unknown_and_profile_does_not_fail(self):
        path = self.add_track("unknown.mp3", genre=None)
        self.assertIsNone(normalize_genre(None))
        self.assertIsNone(normalize_genre("  "))
        profile = SmartDJRecommender(self.conn).profile()
        self.assertEqual(profile["missing_genre_count"], 1)
        self.assertEqual(profile["genres"], [])
        self.assertEqual(self.conn.execute("SELECT path FROM tracks").fetchone()[0], path)

    def test_deleted_files_are_excluded_from_recommendations(self):
        missing = str(self.root / "missing.mp3")
        self.conn.execute("INSERT INTO tracks(path,title,artist,album,duration,favorite,genre) VALUES(?,?,?,?,?,?,?)",
                          (missing, "Missing", "Artist", "Album", 10, 0, "Rock"))
        visible = self.add_track("visible.mp3")
        rows = SmartDJRecommender(self.conn, random.Random(2)).build_playlist()
        self.assertEqual([row["path"] for row in rows], [visible])

    def test_modes_change_relative_preferences_without_zeroing_other_tracks(self):
        popular = self.add_track("popular.mp3", artist="A", genre="Rock", favorite=1)
        discovery = self.add_track("discovery.mp3", artist="B", genre="Rock")
        unrelated = self.add_track("unrelated.mp3", artist="C", genre="Jazz")
        for _ in range(12):
            self.add_session(popular, "completed", 10_000, 1.0, manual=True)
        self.add_session(discovery, "completed", 10_000, 1.0)
        dj = SmartDJRecommender(self.conn, random.Random(4))
        favorites = {r["path"]: r["score"] for r in dj.build_playlist("Favorites", 20)}
        discover = {r["path"]: r["score"] for r in dj.build_playlist("Discovery", 20)}
        self.assertGreater(favorites[popular], favorites[discovery])
        self.assertGreater(discover[discovery], discover[unrelated])
        self.assertGreater(discover[unrelated], 0)

    def test_cold_start_makes_a_diverse_nonempty_queue_without_history(self):
        paths = [self.add_track(f"{i}.mp3", artist=f"Artist {i%3}", genre=("Rock", "Jazz", None)[i%3]) for i in range(6)]
        playlist = SmartDJRecommender(self.conn, random.Random(7)).build_playlist("Balanced", 80)
        self.assertEqual(len(playlist), len(paths))
        self.assertEqual(len({r["path"] for r in playlist}), len(paths))
        self.assertGreaterEqual(len({r["artist"] for r in playlist}), 2)

    def test_rebuilding_playlist_does_not_delete_listening_history(self):
        path = self.add_track("retained.mp3")
        self.add_session(path, "completed", 10_000, 1.0)
        before = self.conn.execute("SELECT COUNT(*) FROM listening_sessions").fetchone()[0]
        SmartDJRecommender(self.conn, random.Random(9)).build_playlist()
        SmartDJRecommender(self.conn, random.Random(10)).build_playlist("Discovery")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM listening_sessions").fetchone()[0], before)

    def test_genres_are_normalized_without_classifying_missing_tags(self):
        self.assertEqual(normalize_genre("Alternative Rock"), "Rock")
        self.assertEqual(normalize_genre("Hip-Hop"), "Rap/Hip-Hop")
        self.assertEqual(normalize_genre("Uncatalogued field recording"), "Other")
        self.assertIsNone(normalize_genre(None))


class SmartDJWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_window_migrates_existing_db_and_generates_smart_queue(self):
        from unittest.mock import patch
        from nova_music.ui.main_window import MainWindow
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = root / "AppData" / "Local" / "NOVA Music"
            data_dir.mkdir(parents=True)
            audio_dir = root / "audio"
            audio_dir.mkdir()
            first, second = audio_dir / "one.mp3", audio_dir / "two.mp3"
            first.write_bytes(b"placeholder"); second.write_bytes(b"placeholder")
            db_path = data_dir / "library.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE tracks(path TEXT PRIMARY KEY,title TEXT,artist TEXT,album TEXT,duration REAL,favorite INTEGER DEFAULT 0)")
            conn.executemany("INSERT INTO tracks(path,title,artist,album,duration,favorite) VALUES(?,?,?,?,?,?)", [
                (str(first), "One", "Artist A", "Album A", 10, 0),
                (str(second), "Two", "Artist B", "Album B", 10, 1),
            ])
            conn.commit(); conn.close()
            with patch.object(Path, "home", return_value=root):
                window = MainWindow()
                self.assertEqual(window.track_list.count(), 2)
                self.assertEqual(window.stack.count(), 4)
                window.smart_mode.setCurrentText("Balanced")
                window._build_smart_playlist()
                loop = QEventLoop()
                deadline = QTimer()
                deadline.setSingleShot(True)
                deadline.timeout.connect(loop.quit)
                poll = QTimer()
                def check_done():
                    if window.build_smart_btn.isEnabled():loop.quit()
                poll.timeout.connect(check_done); poll.start(20); deadline.start(5000); loop.exec()
                poll.stop()
                self.assertTrue(window.build_smart_btn.isEnabled())
                self.assertEqual(window.smart_list.count(), 2)
                self.assertEqual(set(window.queue_paths), {str(first), str(second)})
                wav_path=root/"plain.wav"
                with wave.open(str(wav_path),"wb") as wav_file:
                    wav_file.setnchannels(2); wav_file.setsampwidth(2); wav_file.setframerate(44100)
                    wav_file.writeframes(b"\0\0\0\0"*4410)
                wav_metadata=window._metadata(str(wav_path))
                self.assertAlmostEqual(wav_metadata[3],0.1,places=2)
                broken = root / "corrupt.mp3"; broken.write_bytes(b"not audio")
                with patch.object(window, "_ffprobe_duration", return_value=0.0):
                    metadata = window._metadata(str(broken))
                self.assertEqual(metadata[0], "corrupt")
                self.assertIsNone(metadata[1])
                self.assertIsNone(metadata[2])
                self.assertEqual(metadata[3], 0.0)
                window._set_history_enabled(False)
                self.assertFalse(window.tracker.enabled)
                self.assertEqual(window.conn.execute("SELECT value FROM smart_dj_settings WHERE key='history_enabled'").fetchone()[0], "0")
                window.close()
                self.app.processEvents()

    def test_decoder_nonzero_exit_uses_failure_signal_not_track_end(self):
        import numpy as np
        from nova_music.audio.engine import AudioEngine
        engine=AudioEngine()
        class FailedProcess:
            returncode=2
            def poll(self):return 2
        engine._process=FailedProcess(); engine._playing=True; engine._paused=False
        ended=[]; failed=[]; states=[]
        engine.trackEnded.connect(lambda:ended.append(True))
        engine.trackFailed.connect(lambda:failed.append(True))
        engine.playbackStateChanged.connect(states.append)
        engine._audio_callback(np.zeros((128,2),dtype=np.float32),128,None,None)
        self.assertEqual(ended,[])
        self.assertEqual(failed,[True])
        self.assertEqual(states,[False])
        engine._process=None; engine.close()

    def test_worker_reports_database_errors_without_crashing(self):
        from nova_music.smart_dj.worker import SmartDJWorker
        with tempfile.TemporaryDirectory() as temp:
            bad_db = Path(temp) / "invalid.sqlite3"
            bad_db.write_text("not a sqlite database")
            worker = SmartDJWorker(str(bad_db), "Balanced", 50)
            errors = []
            worker.failed.connect(errors.append)
            loop = QEventLoop()
            timer = QTimer(); timer.setSingleShot(True); timer.timeout.connect(loop.quit)
            worker.finished.connect(loop.quit)
            worker.start(); timer.start(5000); loop.exec(); worker.wait()
            self.assertTrue(errors)

    def test_corrupt_library_database_falls_back_without_closing_app(self):
        from unittest.mock import patch
        from nova_music.ui.main_window import MainWindow
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); data_dir=root/"AppData"/"Local"/"NOVA Music"; data_dir.mkdir(parents=True)
            (data_dir/"library.sqlite3").write_text("corrupted sqlite contents")
            with patch.object(Path,"home",return_value=root):
                window=MainWindow()
                self.assertFalse(window.database_available)
                self.assertIsNotNone(window.status)
                self.assertFalse(window.build_smart_btn.isEnabled())
                self.assertIn("موقت",window.status.text())
                window.close(); self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
