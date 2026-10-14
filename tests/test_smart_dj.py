import os
import json
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

    def test_embedded_cover_is_loaded_and_used_in_song_artwork(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QColor, QPixmap
        from nova_music.ui.main_window import MainWindow
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, "home", return_value=Path(temp)):
            window=MainWindow()
            art=QPixmap(20,20); art.fill(QColor("#1ed760"))
            buffer=QBuffer(); buffer.open(QIODevice.OpenModeFlag.WriteOnly); art.save(buffer,"PNG")
            cover_bytes=bytes(buffer.data()); buffer.close()
            fake_audio=SimpleNamespace(pictures=[SimpleNamespace(data=cover_bytes)])
            with patch("nova_music.ui.main_window.MutagenFile",return_value=fake_audio):
                rendered=window._cover_pixmap("embedded-cover.flac",34)
            self.assertFalse(rendered.isNull())
            self.assertEqual(rendered.size().width(),34)
            window.close(); self.app.processEvents()

    def test_library_and_favorites_switch_between_list_and_cover_grid(self):
        from unittest.mock import patch
        from nova_music.ui.main_window import MainWindow
        from PySide6.QtWidgets import QListWidget
        with tempfile.TemporaryDirectory() as temp, patch.object(Path,"home",return_value=Path(temp)):
            root=Path(temp); song=root/"no-cover.mp3"; song.write_bytes(b"not real audio")
            window=MainWindow(); window._add_paths([str(song)])
            self.assertEqual(window.track_list.viewMode(),QListWidget.ViewMode.ListMode)
            window.view_mode_combo.setCurrentIndex(window.view_mode_combo.findData("grid"))
            self.assertEqual(window.track_list.viewMode(),QListWidget.ViewMode.IconMode)
            self.assertEqual(window.fav_list.viewMode(),QListWidget.ViewMode.IconMode)
            self.assertEqual(window.track_list.iconSize().width(),132)
            self.assertFalse(window.track_list.item(0).icon().isNull())
            self.assertEqual(window.conn.execute("SELECT value FROM smart_dj_settings WHERE key='track_view_mode'").fetchone()[0],"grid")
            window.close(); self.app.processEvents()
            window=MainWindow()
            self.assertEqual(window.view_mode_combo.currentData(),"grid")
            self.assertEqual(window.track_list.viewMode(),QListWidget.ViewMode.IconMode)
            window.close(); self.app.processEvents()

    def test_library_sorting_and_metadata_filters_apply_to_both_views(self):
        from unittest.mock import patch
        from PySide6.QtCore import Qt
        from nova_music.ui.main_window import MainWindow
        with tempfile.TemporaryDirectory() as temp, patch.object(Path,"home",return_value=Path(temp)):
            root=Path(temp); first=root/"first.mp3"; second=root/"second.mp3"
            first.write_bytes(b"first audio"); second.write_bytes(b"second audio")
            window=MainWindow(); window._add_paths([str(first),str(second)])
            window.conn.execute("UPDATE tracks SET title='Zeta',artist='Artist B',album='Album 2',genre='Rock',favorite=1 WHERE path=?",(str(first),))
            window.conn.execute("UPDATE tracks SET title='Alpha',artist='Artist A',album='Album 1',genre='Pop',favorite=1 WHERE path=?",(str(second),)); window.conn.commit()
            window._load_library()
            window.sort_combo.setCurrentIndex(window.sort_combo.findData("title"))
            self.assertEqual([window.track_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(2)],[str(second),str(first)])
            window.filter_artist_combo.setCurrentIndex(window.filter_artist_combo.findData("Artist A"))
            self.assertFalse(window.track_list.item(0).isHidden())
            self.assertTrue(window.track_list.item(1).isHidden())
            self.assertTrue(window.fav_list.item(1).isHidden())
            window.filter_genre_combo.setCurrentIndex(window.filter_genre_combo.findData("Pop"))
            self.assertFalse(window.track_list.item(0).isHidden())
            window._clear_track_filters()
            self.assertFalse(window.track_list.item(0).isHidden())
            self.assertEqual(window.conn.execute("SELECT value FROM smart_dj_settings WHERE key='track_sort_mode'").fetchone()[0],"title")
            window.close(); self.app.processEvents()

    def test_theme_switch_and_expanded_now_playing_panel(self):
        from unittest.mock import patch
        from nova_music.ui.main_window import MainWindow, TrackGridDelegate
        with tempfile.TemporaryDirectory() as temp, patch.object(Path,"home",return_value=Path(temp)):
            root=Path(temp); song=root/"panel-song.mp3"; song.write_bytes(b"not real audio")
            window=MainWindow(); window._add_paths([str(song)])
            window._set_track_view_mode("grid")
            self.assertIsInstance(window.track_list.itemDelegate(),TrackGridDelegate)
            window._set_theme("light",persist=True)
            self.assertIn("#f5f6f8",window.styleSheet())
            self.assertEqual(window.conn.execute("SELECT value FROM smart_dj_settings WHERE key='theme_mode'").fetchone()[0],"light")
            window._set_player_cover(str(song))
            self.assertFalse(window.large_cover.pixmap().isNull())
            self.assertEqual(window.panel_track_title.text(),"panel-song")
            window._pending_grid_action=("favorite",str(song)); window._commit_grid_action()
            self.assertEqual(window.conn.execute("SELECT favorite FROM tracks WHERE path=?",(str(song),)).fetchone()[0],1)
            with patch.object(window,"_play_item") as quick_play:
                window._pending_grid_action=("play",str(song)); window._commit_grid_action()
                quick_play.assert_called_once_with(window.track_list.item(0))
            window._toggle_now_playing_panel(); self.assertFalse(window.now_playing_panel.isHidden())
            window._toggle_now_playing_panel(); self.assertTrue(window.now_playing_panel.isHidden())
            window.close(); self.app.processEvents()

    def test_playlist_round_trip_and_queue_reordering(self):
        from unittest.mock import patch
        from nova_music.ui.main_window import MainWindow
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, "home", return_value=Path(temp)):
            root=Path(temp); first=root/"first.mp3"; second=root/"second.mp3"
            first.write_bytes(b"not decoded in this test"); second.write_bytes(b"not decoded in this test")
            window=MainWindow(); window._add_paths([str(first),str(second)])
            window.queue_paths=[str(second),str(first)]; window._refresh_queue_view()
            with patch("nova_music.ui.main_window.QInputDialog.getText",return_value=("Road trip",True)):
                window._save_playlist()
            playlist_id=window.conn.execute("SELECT id FROM playlists WHERE name='Road trip'").fetchone()[0]
            playlist_index=window.playlist_combo.findData(playlist_id)
            window.playlist_combo.setCurrentIndex(playlist_index)
            self.assertEqual(window.queue_paths,[str(second),str(first)])
            window.queue_list.setCurrentRow(0); window._move_queue_item(1)
            window._save_playlist()
            window.playlist_combo.setCurrentIndex(0); window.playlist_combo.setCurrentIndex(playlist_index)
            self.assertEqual(window.queue_paths,[str(first),str(second)])
            window.current_path=str(first); window.engine.position_ms=12_340; window._save_playback_state()
            window.close(); self.app.processEvents()
            window=MainWindow()
            self.assertEqual(window.current_path,str(first))
            self.assertEqual(window.engine.position_ms,12_340)
            self.assertEqual(window.queue_paths,[str(first),str(second)])
            self.assertEqual(window.stack.count(),6)
            window.close(); self.app.processEvents()

    def test_m3u_playlist_export_import_preserves_order_and_reports_missing_paths(self):
        from nova_music.ui.main_window import MainWindow
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp, patch.object(Path,"home",return_value=Path(temp)):
            root=Path(temp); music=root/"music"; music.mkdir(); export_dir=root/"exports"; export_dir.mkdir()
            first=music/"first.mp3"; second=music/"second.mp3"
            first.write_bytes(b"track one"); second.write_bytes(b"track two")
            window=MainWindow(); window._add_paths([str(first),str(second)])
            window.conn.execute("INSERT INTO playlists(name) VALUES('Road trip')")
            playlist_id=window.conn.execute("SELECT id FROM playlists WHERE name='Road trip'").fetchone()[0]
            window.conn.executemany("INSERT INTO playlist_tracks(playlist_id,position,track_path) VALUES(?,?,?)",[(playlist_id,0,str(second)),(playlist_id,1,str(first))]); window.conn.commit()
            playlist_file=export_dir/"Road trip.m3u"
            self.assertEqual(window._export_playlist(str(playlist_file),playlist_id),2)
            with playlist_file.open("a",encoding="utf-8") as output:output.write("missing.mp3\n")
            imported,missing=window._import_playlist(str(playlist_file))
            self.assertEqual((imported,missing),(2,1))
            self.assertEqual(window.queue_paths,[str(second),str(first)])
            self.assertEqual(window.conn.execute("SELECT name FROM playlists WHERE id=?",(window._active_playlist_id,)).fetchone()[0],"Road trip (2)")
            window.close(); self.app.processEvents()

    def test_watched_folder_scanner_adds_new_audio_and_persists_folder(self):
        from nova_music.ui.main_window import MainWindow
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp, patch.object(Path,"home",return_value=Path(temp)):
            root=Path(temp); music=root/"music"; music.mkdir()
            first=music/"first.mp3"; first.write_bytes(b"first audio")
            window=MainWindow(); window.watched_folders=[str(music)]; window._save_watched_folders()
            window._start_watched_scan(); worker=window._watch_worker; worker.wait(); self.app.processEvents()
            self.assertEqual(window.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0],1)
            second=music/"second.mp3"; second.write_bytes(b"second audio")
            window._start_watched_scan(); worker=window._watch_worker; worker.wait(); self.app.processEvents()
            self.assertEqual(window.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0],2)
            self.assertEqual(window.conn.execute("SELECT value FROM smart_dj_settings WHERE key='watched_folders'").fetchone()[0],json.dumps([str(music)],ensure_ascii=False))
            window.close(); self.app.processEvents()

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
                self.assertEqual(window.stack.count(), 6)
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

    def test_moved_file_relink_preserves_library_playlist_and_history_links(self):
        from unittest.mock import patch
        from nova_music.ui.main_window import MainWindow
        with tempfile.TemporaryDirectory() as temp, patch.object(Path,"home",return_value=Path(temp)):
            root=Path(temp); audio_dir=root/"music"; audio_dir.mkdir()
            old_path=audio_dir/"old.mp3"; new_path=audio_dir/"moved.mp3"
            old_path.write_bytes(b"placeholder audio")
            window=MainWindow(); window._add_paths([str(old_path)])
            window.conn.execute("UPDATE tracks SET favorite=1 WHERE path=?",(str(old_path),))
            window.conn.execute("INSERT INTO playlists(name) VALUES('Keep')")
            playlist_id=window.conn.execute("SELECT id FROM playlists WHERE name='Keep'").fetchone()[0]
            window.conn.execute("INSERT INTO playlist_tracks(playlist_id,position,track_path) VALUES(?,?,?)",(playlist_id,0,str(old_path)))
            window.conn.execute("INSERT INTO listening_events(track_path,event_type,created_at) VALUES(?,?,?)",(str(old_path),"listened","2026-10-13T10:00:00+00:00"))
            window.conn.commit(); window.current_path=str(old_path); window.queue_paths=[str(old_path)]; window._queue_mode="custom"; window._save_playback_state()
            old_path.rename(new_path); window._load_library()
            self.assertEqual(window.missing_list.count(),1)
            self.assertIsNotNone(window.conn.execute("SELECT 1 FROM tracks WHERE path=?",(str(old_path),)).fetchone())
            window._relink_file(str(old_path),str(new_path))
            self.assertEqual(window.missing_list.count(),0)
            self.assertEqual(window.track_list.count(),1)
            self.assertEqual(window.conn.execute("SELECT favorite FROM tracks WHERE path=?",(str(new_path),)).fetchone()[0],1)
            self.assertEqual(window.conn.execute("SELECT track_path FROM playlist_tracks WHERE playlist_id=?",(playlist_id,)).fetchone()[0],str(new_path))
            self.assertEqual(window.conn.execute("SELECT track_path FROM listening_events").fetchone()[0],str(new_path))
            state=window.conn.execute("SELECT value FROM smart_dj_settings WHERE key='playback_state'").fetchone()[0]
            self.assertEqual(json.loads(state)["current_path"],str(new_path))
            self.assertEqual(json.loads(state)["queue"],[str(new_path)])
            window.close(); self.app.processEvents()

    def test_duplicate_scan_reports_only_byte_identical_files(self):
        from unittest.mock import patch
        from nova_music.ui.main_window import MainWindow
        with tempfile.TemporaryDirectory() as temp, patch.object(Path,"home",return_value=Path(temp)):
            root=Path(temp); paths=[root/f"track-{index}.mp3" for index in range(3)]
            paths[0].write_bytes(b"same bytes")
            paths[1].write_bytes(b"same bytes")
            paths[2].write_bytes(b"diff bytes")
            window=MainWindow(); window._add_paths([str(path) for path in paths])
            window._scan_library_duplicates()
            self.assertEqual(window.duplicate_list.topLevelItemCount(),1)
            group=window.duplicate_list.topLevelItem(0)
            self.assertEqual(group.childCount(),2)
            self.assertIn("1 گروه",window.duplicate_status.text())
            self.assertTrue(all(path.exists() for path in paths))
            window.close(); self.app.processEvents()

    def test_scan_folder_recovers_a_unique_same_name_file(self):
        from unittest.mock import patch
        from nova_music.ui.main_window import MainWindow
        with tempfile.TemporaryDirectory() as temp, patch.object(Path,"home",return_value=Path(temp)):
            root=Path(temp); old_dir=root/"old"; new_dir=root/"new"; old_dir.mkdir(); new_dir.mkdir()
            old_path=old_dir/"song.mp3"; new_path=new_dir/"song.mp3"; old_path.write_bytes(b"placeholder")
            window=MainWindow(); window._add_paths([str(old_path)]); old_path.rename(new_path); window._load_library()
            with patch("nova_music.ui.main_window.QFileDialog.getExistingDirectory",return_value=str(new_dir)), \
                 patch("nova_music.ui.main_window.QMessageBox.information"):
                window._scan_for_moved_files()
            self.assertEqual(window.conn.execute("SELECT path FROM tracks").fetchone()[0],str(new_path))
            self.assertEqual(window.missing_list.count(),0)
            window.close(); self.app.processEvents()

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

    def test_expected_decoder_termination_during_pause_is_not_reported_as_error(self):
        import io
        from nova_music.audio.engine import AudioEngine
        class TerminatedProcess:
            stderr=io.BytesIO(b"")
            returncode=-15
            def wait(self):return self.returncode
        engine=AudioEngine(); engine._generation=7; engine._process=None
        errors=[]; engine.errorOccurred.connect(errors.append)
        engine._read_decoder_stderr(TerminatedProcess(),7)
        self.assertEqual(errors,[])
        engine.close()

    def test_crossfade_mixes_two_decks_and_switches_source(self):
        import numpy as np
        from nova_music.audio.engine import AudioEngine
        class Process:
            returncode=None
            def poll(self):return self.returncode
            def terminate(self):self.returncode=0
        engine=AudioEngine()
        engine._process=Process(); engine._next_process=Process(); engine._next_path="next.mp3"
        engine._buffer=np.full((128,2),0.8,dtype=np.float32)
        engine._next_buffer=np.full((128,2),0.4,dtype=np.float32)
        engine._playing=True; engine.duration_ms=10; engine.position_ms=9
        engine._frames_played=400; engine.crossfade_ms=1
        finished=[]; engine.crossfadeFinished.connect(lambda path,frames:finished.append((path,frames)))
        out=np.zeros((128,2),dtype=np.float32)
        engine._audio_callback(out,128,None,None)
        self.assertEqual(finished,[ ("next.mp3",128) ])
        self.assertGreater(out[0,0],out[43,0])
        self.assertAlmostEqual(float(out[43,0]),0.3,places=2)
        self.assertEqual(engine.path,"next.mp3")
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
