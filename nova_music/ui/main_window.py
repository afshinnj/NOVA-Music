import os, sqlite3, random, shutil, subprocess
from pathlib import Path
from mutagen import File as MutagenFile
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QPainter, QColor
from PySide6.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QFileDialog, QLineEdit, QSlider,
    QFrame, QMessageBox, QStackedWidget, QComboBox, QCheckBox, QTabWidget,
    QInputDialog, QMenu)
from nova_music.audio.engine import AudioEngine
from nova_music.ui.eq_widget import EqualizerWidget
from nova_music.smart_dj.storage import initialize as initialize_smart_dj
from nova_music.smart_dj.tracking import ListeningTracker, record_favorite
from nova_music.smart_dj.recommender import MODES, SmartDJRecommender
from nova_music.smart_dj.worker import SmartDJWorker

AUDIO_EXTS = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".wma", ".opus"}

class SpectrumWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent); self.levels = [0.0] * 24; self.setMinimumHeight(82)
    def set_levels(self, levels): self.levels = list(levels); self.update()
    def paintEvent(self, event):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing); p.fillRect(self.rect(), QColor("#11151b"))
        count, gap = len(self.levels), 4; barw = max(2, (self.width()-(count+1)*gap)/count)
        for i, level in enumerate(self.levels):
            h = max(2, int((self.height()-10)*max(0,min(1,level))))
            p.setBrush(QColor("#1ED760")); p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(int(gap+i*(barw+gap)), self.height()-h-4, int(barw), h, 3, 3)
        p.end()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NOVA Music 8"); self.resize(1280, 820); self.setMinimumSize(980, 650)
        self.engine = AudioEngine(self)
        self.engine.positionChanged.connect(self._position_changed)
        self.engine.durationChanged.connect(self._duration_changed)
        self.engine.playbackStateChanged.connect(self._play_state)
        self.engine.spectrumReady.connect(self._spectrum)
        self.engine.errorOccurred.connect(self._show_audio_error)
        self.engine.trackEnded.connect(self._track_ended)
        self.engine.trackFailed.connect(self._track_failed)
        self.current_path = ""; self.duration = 0; self.is_playing = False
        self.shuffle = False; self.repeat_mode = 0; self.queue_paths = []
        data_dir = Path.home() / "AppData" / "Local" / "NOVA Music"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = data_dir / "library.sqlite3"
        self.database_available = True
        self.database_error = ""
        self.conn = None
        try:
            self.conn = sqlite3.connect(self.database_path,timeout=5)
            self.conn.execute("CREATE TABLE IF NOT EXISTS tracks(path TEXT PRIMARY KEY,title TEXT,artist TEXT,album TEXT,duration REAL,favorite INTEGER DEFAULT 0)")
            initialize_smart_dj(self.conn)
            setting = self.conn.execute("SELECT value FROM smart_dj_settings WHERE key='history_enabled'").fetchone()
            self.history_enabled = setting is None or setting[0] == "1"
        except sqlite3.Error as exc:
            self.database_error = str(exc)
            self.database_available = False
            if self.conn:
                self.conn.close()
            self.database_path = None
            self.conn = sqlite3.connect(":memory:")
            self.conn.execute("CREATE TABLE tracks(path TEXT PRIMARY KEY,title TEXT,artist TEXT,album TEXT,duration REAL,favorite INTEGER DEFAULT 0)")
            initialize_smart_dj(self.conn)
            self.history_enabled = False
        self.tracker = ListeningTracker(self.conn, self.engine.sample_rate, self.history_enabled)
        self._pending_manual_selection = False
        self._pending_start_frames = self.engine.output_frames_total
        self._ignore_end_state_signal = False
        self.smart_queue = []
        self.smart_worker = None
        self.conn.commit()
        self._build_ui(); self._load_library(); self._style(); self._shortcuts()
        if not self.database_available:
            self.status.setText("پایگاه دادهٔ محلی در دسترس نیست؛ کتابخانه موقت است و پس از بستن ذخیره نمی‌شود.")
            self.smart_status.setText(f"خطای پایگاه داده: {self.database_error}")
            self.build_smart_btn.setEnabled(False); self.history_toggle.setEnabled(False); self.clear_history_btn.setEnabled(False)

    def _build_ui(self):
        central=QWidget(); self.setCentralWidget(central); root=QVBoxLayout(central); root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        body=QHBoxLayout(); body.setSpacing(0); root.addLayout(body,1)
        sidebar=QFrame(); sidebar.setObjectName("sidebar"); sidebar.setFixedWidth(210); side=QVBoxLayout(sidebar)
        logo=QLabel("◉ NOVA MUSIC"); logo.setObjectName("logo"); side.addWidget(logo)
        self.nav_all=QPushButton("♫   کتابخانه"); self.nav_favs=QPushButton("♡   علاقه‌مندی‌ها"); self.nav_eq=QPushButton("≋   اکولایزر"); self.nav_smart=QPushButton("✦   Smart DJ")
        for b in (self.nav_all,self.nav_favs,self.nav_eq,self.nav_smart): b.setObjectName("navButton"); b.setCursor(Qt.CursorShape.PointingHandCursor); side.addWidget(b)
        side.addStretch(); side.addWidget(QLabel("پخش محلی موسیقی")); body.addWidget(sidebar)
        content=QWidget(); layout=QVBoxLayout(content); layout.setContentsMargins(24,20,24,16); layout.setSpacing(12)
        header=QHBoxLayout(); self.heading=QLabel("کتابخانه موسیقی"); self.heading.setObjectName("pageTitle"); header.addWidget(self.heading); header.addStretch()
        self.search=QLineEdit(); self.search.setPlaceholderText("جستجوی آهنگ، خواننده یا آلبوم…"); self.search.setClearButtonEnabled(True); self.search.setFixedWidth(300); self.search.textChanged.connect(self._filter_tracks); header.addWidget(self.search)
        self.add_files_btn=QPushButton("+ افزودن آهنگ"); self.add_files_btn.clicked.connect(self._choose_files); header.addWidget(self.add_files_btn)
        self.add_folder_btn=QPushButton("افزودن پوشه"); self.add_folder_btn.clicked.connect(self._choose_folder); header.addWidget(self.add_folder_btn); layout.addLayout(header)
        self.stack=QStackedWidget()
        lib=QWidget(); ll=QVBoxLayout(lib); self.track_list=QListWidget(); self.track_list.itemDoubleClicked.connect(self._play_item); self.track_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu); self.track_list.customContextMenuRequested.connect(lambda p:self._track_context_menu(self.track_list,p)); ll.addWidget(self.track_list); self.stack.addWidget(lib)
        fav=QWidget(); fl=QVBoxLayout(fav); self.fav_list=QListWidget(); self.fav_list.itemDoubleClicked.connect(self._play_item); self.fav_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu); self.fav_list.customContextMenuRequested.connect(lambda p:self._track_context_menu(self.fav_list,p)); fl.addWidget(self.fav_list); self.stack.addWidget(fav)
        eqp=QWidget(); el=QVBoxLayout(eqp); self.eq=EqualizerWidget(); self.eq.bandChanged.connect(self.engine.set_eq_band); self.eq.enabledChanged.connect(self.engine.set_eq_enabled); el.addWidget(self.eq); self.stack.addWidget(eqp)
        smart=QWidget(); sl=QVBoxLayout(smart); sl.setSpacing(10)
        card=QFrame(); card.setObjectName("smartCard"); cl=QVBoxLayout(card)
        card_title=QLabel("Smart DJ  ·  پیشنهاددهندهٔ آفلاین"); card_title.setObjectName("sectionTitle"); cl.addWidget(card_title)
        options=QHBoxLayout(); self.smart_mode=QComboBox(); self.smart_mode.addItems(MODES); self.smart_mode.setCurrentText("Balanced")
        self.diversity_slider=QSlider(Qt.Orientation.Horizontal); self.diversity_slider.setRange(0,100); self.diversity_slider.setValue(50); self.diversity_value=QLabel("50٪")
        self.diversity_slider.valueChanged.connect(lambda v:self.diversity_value.setText(f"{v}٪"))
        self.build_smart_btn=QPushButton("ساخت / بازسازی صف"); self.build_smart_btn.clicked.connect(self._build_smart_playlist)
        options.addWidget(QLabel("حالت")); options.addWidget(self.smart_mode); options.addWidget(QLabel("تنوع")); options.addWidget(self.diversity_slider,1); options.addWidget(self.diversity_value); options.addWidget(self.build_smart_btn); cl.addLayout(options)
        privacy=QHBoxLayout(); self.history_toggle=QCheckBox("ثبت تاریخچهٔ شنیدن روی همین دستگاه"); self.history_toggle.setChecked(self.history_enabled); self.history_toggle.toggled.connect(self._set_history_enabled)
        self.clear_history_btn=QPushButton("پاک‌کردن تاریخچه"); self.clear_history_btn.clicked.connect(self._clear_listening_history)
        privacy.addWidget(self.history_toggle); privacy.addStretch(); privacy.addWidget(self.clear_history_btn); cl.addLayout(privacy)
        self.smart_stats=QLabel("هنوز آماری ثبت نشده است."); self.smart_stats.setObjectName("muted"); self.smart_stats.setWordWrap(True); cl.addWidget(self.smart_stats)
        sl.addWidget(card)
        tabs=QTabWidget(); self.smart_list=QListWidget(); self.smart_list.itemDoubleClicked.connect(self._play_smart_item); self.smart_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu); self.smart_list.customContextMenuRequested.connect(self._smart_context_menu); tabs.addTab(self.smart_list,"پیشنهادها")
        self.history_list=QListWidget(); tabs.addTab(self.history_list,"تاریخچه")
        self.top_played_list=QListWidget(); tabs.addTab(self.top_played_list,"بیشتر شنیده‌شده")
        self.top_skipped_list=QListWidget(); tabs.addTab(self.top_skipped_list,"بیشتر ردشده")
        sl.addWidget(tabs,1)
        smart_actions=QHBoxLayout(); self.play_suggestion_btn=QPushButton("پخش انتخاب‌شده"); self.play_suggestion_btn.clicked.connect(self._play_selected_suggestion)
        self.favorite_suggestion_btn=QPushButton("افزودن به علاقه‌مندی‌ها"); self.favorite_suggestion_btn.clicked.connect(self._favorite_selected_suggestion)
        self.remove_suggestion_btn=QPushButton("حذف از پیشنهادها"); self.remove_suggestion_btn.clicked.connect(self._remove_selected_suggestion)
        smart_actions.addWidget(self.play_suggestion_btn); smart_actions.addWidget(self.favorite_suggestion_btn); smart_actions.addWidget(self.remove_suggestion_btn); smart_actions.addStretch(); sl.addLayout(smart_actions)
        self.smart_status=QLabel("برای ساخت صف شخصی، حالت و میزان تنوع را انتخاب کنید."); self.smart_status.setObjectName("muted"); sl.addWidget(self.smart_status)
        self.stack.addWidget(smart)
        layout.addWidget(self.stack,1); body.addWidget(content,1)
        now=QFrame(); now.setObjectName("nowPlaying"); nl=QVBoxLayout(now); top=QHBoxLayout(); info=QVBoxLayout()
        self.track_label=QLabel("انتخاب آهنگ"); self.track_label.setObjectName("trackTitle"); self.artist_label=QLabel("هنوز آهنگی پخش نشده"); self.artist_label.setObjectName("muted")
        info.addWidget(self.track_label); info.addWidget(self.artist_label); top.addLayout(info); top.addStretch()
        self.status=QLabel("آماده"); self.status.setObjectName("muted"); top.addWidget(self.status); nl.addLayout(top)
        self.spectrum=SpectrumWidget(); nl.addWidget(self.spectrum)
        controls=QHBoxLayout(); self.shuffle_btn=QPushButton("⤨"); self.prev_btn=QPushButton("|◀"); self.play_btn=QPushButton("▶"); self.next_btn=QPushButton("▶|"); self.repeat_btn=QPushButton("↻")
        for b in (self.shuffle_btn,self.prev_btn,self.play_btn,self.next_btn,self.repeat_btn): b.setObjectName("controlButton"); controls.addWidget(b)
        self.shuffle_btn.clicked.connect(self._toggle_shuffle); self.prev_btn.clicked.connect(self._previous); self.play_btn.clicked.connect(self._toggle_playback); self.next_btn.clicked.connect(self._next); self.repeat_btn.clicked.connect(self._cycle_repeat)
        controls.addStretch(); controls.addWidget(QLabel("صدا")); self.volume=QSlider(Qt.Orientation.Horizontal); self.volume.setRange(0,100); self.volume.setValue(75); self.volume.setFixedWidth(130); self.volume.valueChanged.connect(self.engine.set_volume); controls.addWidget(self.volume); nl.addLayout(controls)
        seek=QHBoxLayout(); self.elapsed=QLabel("00:00"); self.seek_slider=QSlider(Qt.Orientation.Horizontal); self.seek_slider.setRange(0,0); self.seek_slider.sliderMoved.connect(self.engine.set_position); self.total=QLabel("00:00")
        seek.addWidget(self.elapsed); seek.addWidget(self.seek_slider,1); seek.addWidget(self.total); nl.addLayout(seek); root.addWidget(now)
        self.nav_all.clicked.connect(lambda:self._show_page(0,"کتابخانه موسیقی")); self.nav_favs.clicked.connect(lambda:self._show_page(1,"علاقه‌مندی‌ها")); self.nav_eq.clicked.connect(lambda:self._show_page(2,"اکولایزر")); self.nav_smart.clicked.connect(lambda:self._show_page(3,"Smart DJ"))

    def _style(self):
        self.setStyleSheet("""
        QWidget{background:#171a21;color:#e8eaed;font-family:'Segoe UI';font-size:10pt}
        #sidebar{background:#101318} #logo{color:#1ED760;font-size:17pt;font-weight:700;padding:18px 8px}
        #navButton{text-align:left;padding:12px;border:0;border-radius:7px;background:transparent} #navButton:hover{background:#252a33}
        #pageTitle{font-size:22pt;font-weight:700} QLineEdit{background:#252a33;border:1px solid #343b46;border-radius:16px;padding:9px 13px}
        QPushButton{background:#262c35;border:1px solid #343b46;border-radius:8px;padding:9px 12px} QPushButton:hover{background:#333b47}
        #controlButton{min-width:38px;max-width:52px;font-size:14pt} #nowPlaying{background:#101318;border-top:1px solid #303641;padding:8px 20px}
        #trackTitle{font-size:12pt;font-weight:600} #muted{color:#9ba3ae} #sectionTitle{font-size:16pt;font-weight:700} #smartCard{background:#101318;border:1px solid #303641;border-radius:12px;padding:8px}
        QListWidget{background:#1b2028;border:1px solid #303641;border-radius:10px;padding:6px}
        QListWidget::item{padding:10px;border-radius:6px} QListWidget::item:selected{background:#17472a}
        QSlider::groove:horizontal{height:5px;background:#343b46;border-radius:2px} QSlider::handle:horizontal{background:#1ED760;width:12px;margin:-5px 0;border-radius:6px}
        QSlider::groove:vertical{width:5px;background:#343b46;border-radius:2px} QSlider::handle:vertical{background:#1ED760;height:12px;margin:0 -5px;border-radius:6px}
        """)
        self.engine.set_volume(self.volume.value())

    def _shortcuts(self):
        for key,cb in [("Space",self._toggle_playback),("Ctrl+O",self._choose_files),("Ctrl+Shift+O",self._choose_folder),("Right",self._next),("Left",self._previous)]:
            a=QAction(self); a.setShortcut(QKeySequence(key)); a.triggered.connect(cb); self.addAction(a)
    def _show_page(self,i,title):
        self.stack.setCurrentIndex(i); self.heading.setText(title); show=i not in (2,3)
        self.search.setVisible(show); self.add_files_btn.setVisible(show); self.add_folder_btn.setVisible(show)
        if i==3:self._refresh_smart_overview()
    def _choose_files(self):
        paths,_=QFileDialog.getOpenFileNames(self,"انتخاب فایل‌های صوتی","","Audio files (*.mp3 *.flac *.wav *.ogg *.m4a *.aac *.wma *.opus)")
        if paths:self._add_paths(paths)
    def _choose_folder(self):
        folder=QFileDialog.getExistingDirectory(self,"انتخاب پوشه موسیقی")
        if folder:
            self._add_paths([os.path.join(r,n) for r,_,ns in os.walk(folder) for n in ns if Path(n).suffix.lower() in AUDIO_EXTS])
    def _metadata(self,path):
        title,artist,album,duration,genre,year,track_number=Path(path).stem,None,None,0.0,None,None,None
        try:
            a=MutagenFile(path,easy=True)
            if a:
                title=(a.get("title") or [title])[0]; artist=(a.get("artist") or a.get("albumartist") or [artist])[0]
                album=(a.get("album") or [album])[0]; duration=getattr(getattr(a,"info",None),"length",0) or 0
                genre=(a.get("genre") or [None])[0]
                raw_year=(a.get("date") or a.get("year") or [None])[0]
                try:year=int(str(raw_year)[:4]) if raw_year else None
                except (TypeError,ValueError):year=None
                raw_track=(a.get("tracknumber") or [None])[0]
                try:track_number=int(str(raw_track).split("/",1)[0]) if raw_track else None
                except (TypeError,ValueError):track_number=None
            elif Path(path).suffix.lower()==".wav":
                from mutagen.wave import WAVE
                wave=WAVE(path)
                duration=getattr(getattr(wave,"info",None),"length",0) or 0
        except Exception:pass
        if duration<=0:
            duration=self._ffprobe_duration(path)
        return title,artist,album,float(duration),genre,year,track_number
    def _ffprobe_duration(self,path):
        try:
            probe=shutil.which("ffprobe")
            if not probe:
                probe_path=Path(self.engine._ffmpeg()).with_name("ffprobe.exe")
                if probe_path.is_file():probe=str(probe_path)
            if not probe:return 0.0
            result=subprocess.run([probe,"-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",path],capture_output=True,text=True,timeout=8,check=True)
            return max(0.0,float(result.stdout.strip()))
        except Exception:return 0.0
    def _add_paths(self,paths):
        added=0
        for path in paths:
            path=os.path.abspath(path)
            if not os.path.isfile(path) or Path(path).suffix.lower() not in AUDIO_EXTS:continue
            t,a,al,d,g,y,tn=self._metadata(path)
            is_new=self.conn.execute("SELECT 1 FROM tracks WHERE path=?",(path,)).fetchone() is None
            cur=self.conn.execute("INSERT INTO tracks(path,title,artist,album,duration,genre,year,track_number) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET title=excluded.title,artist=excluded.artist,album=excluded.album,"
                "duration=excluded.duration,genre=excluded.genre,year=excluded.year,track_number=excluded.track_number",
                (path,t,a,al,d,g,y,tn))
            added += int(is_new)
        self.conn.commit(); self._load_library(); self.status.setText(f"{added} آهنگ اضافه شد")
    def _load_library(self):
        self.track_list.clear(); self.fav_list.clear(); self.queue_paths=[]
        rows = self.conn.execute("SELECT path,title,artist,album,duration,favorite,genre,year,track_number FROM tracks ORDER BY artist,title").fetchall()
        missing = {row[0] for row in rows if not os.path.exists(row[0])}
        if missing:
            self.conn.executemany("DELETE FROM tracks WHERE path=?", ((path,) for path in missing))
            self.conn.commit()
        for path,title,artist,album,duration,fav,genre,year,track_number in rows:
            if path in missing:continue
            tag_text=f"{artist or 'ناشناخته'}  •  {album or 'بدون آلبوم'}"+(f"  •  {genre}" if genre else "")
            self.queue_paths.append(path); item=QListWidgetItem(f"{title}\n{tag_text}"); item.setData(Qt.ItemDataRole.UserRole,path); self.track_list.addItem(item)
            if fav:
                it=QListWidgetItem(f"♥  {title}\n{tag_text}"); it.setData(Qt.ItemDataRole.UserRole,path); self.fav_list.addItem(it)
        if self.smart_queue:
            available=set(self.queue_paths)
            current=[self.current_path] if self.current_path in available and (self.engine._playing or self.engine._paused) else []
            self.queue_paths=current+[p for p in self.smart_queue if p in available and p not in current]
        self._filter_tracks(self.search.text())
    def _filter_tracks(self,text):
        q=text.casefold().strip()
        for listing in (self.track_list,self.fav_list):
            for i in range(listing.count()):listing.item(i).setHidden(q not in listing.item(i).text().casefold())
    def _play_item(self,item):
        path=item.data(Qt.ItemDataRole.UserRole)
        if path:self._play_path(path,manual=True)
    def _play_path(self,path,manual=False):
        if self.tracker.active:self.tracker.finish("skipped",self.engine.output_frames_total)
        self._pending_manual_selection=bool(manual)
        self.current_path=path; t,a,al,_,_,_,_=self._metadata(path); self.track_label.setText(t); self.artist_label.setText(f"{a or 'ناشناخته'}  •  {al or 'بدون آلبوم'}")
        self.engine.set_source(path); self._pending_start_frames=self.engine.output_frames_total
        self.engine.play(); self._highlight_current()
    def _highlight_current(self):
        for listing in (self.track_list,self.fav_list):
            for i in range(listing.count()):listing.item(i).setSelected(listing.item(i).data(Qt.ItemDataRole.UserRole)==self.current_path)
    def _toggle_favorite_at(self,pos):
        item=self.track_list.itemAt(pos)
        if not item:return
        path=item.data(Qt.ItemDataRole.UserRole)
        row=self.conn.execute("SELECT favorite FROM tracks WHERE path=?",(path,)).fetchone()
        self._set_favorite(path,not bool(row and row[0]))
    def _remove_favorite_at(self,pos):
        item=self.fav_list.itemAt(pos)
        if item:
            self._set_favorite(item.data(Qt.ItemDataRole.UserRole),False)
    def _set_favorite(self,path,favorite):
        row=self.conn.execute("SELECT favorite FROM tracks WHERE path=?",(path,)).fetchone()
        if not row or bool(row[0])==bool(favorite):return
        self.conn.execute("UPDATE tracks SET favorite=? WHERE path=?",(int(favorite),path))
        if self.history_enabled:record_favorite(self.conn,path,bool(favorite))
        self.conn.commit(); self._load_library()
        if self.stack.currentIndex()==3:self._refresh_smart_overview()
    def _track_context_menu(self,listing,pos):
        item=listing.itemAt(pos)
        if not item:return
        path=item.data(Qt.ItemDataRole.UserRole)
        row=self.conn.execute("SELECT favorite FROM tracks WHERE path=?",(path,)).fetchone()
        menu=QMenu(self)
        favorite_action=menu.addAction("حذف از علاقه‌مندی‌ها" if row and row[0] else "افزودن به علاقه‌مندی‌ها")
        edit_action=menu.addAction("ویرایش عنوان، خواننده و ژانر")
        selected=menu.exec(listing.viewport().mapToGlobal(pos))
        if selected==favorite_action:self._set_favorite(path,not bool(row and row[0]))
        elif selected==edit_action:self._edit_track_metadata(path)
    def _edit_track_metadata(self,path):
        row=self.conn.execute("SELECT title,artist,genre FROM tracks WHERE path=?",(path,)).fetchone()
        if not row:return
        values=[]
        for label,value in zip(("عنوان آهنگ","خواننده","ژانر"),row):
            text,ok=QInputDialog.getText(self,"ویرایش اطلاعات آهنگ",label,QLineEdit.EchoMode.Normal,value or "")
            if not ok:return
            values.append(text.strip() or None)
        try:
            self.conn.execute("UPDATE tracks SET title=?,artist=?,genre=? WHERE path=?",(*values,path)); self.conn.commit()
            self._load_library()
            if path==self.current_path:
                self.track_label.setText(values[0] or Path(path).stem)
                self.artist_label.setText(f"{values[1] or 'ناشناخته'}  •  {self.conn.execute('SELECT album FROM tracks WHERE path=?',(path,)).fetchone()[0] or 'بدون آلبوم'}")
            if self.stack.currentIndex()==3:self._refresh_smart_overview()
        except sqlite3.Error as exc:QMessageBox.warning(self,"خطای پایگاه داده",str(exc))
    def _set_history_enabled(self,enabled):
        self.history_enabled=bool(enabled)
        try:
            self._sync_tracker_frames()
            self.tracker.set_enabled(self.history_enabled)
            self.conn.execute("INSERT INTO smart_dj_settings(key,value) VALUES('history_enabled',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",("1" if enabled else "0",))
            self.conn.commit()
            if enabled and self.is_playing:
                self.tracker.start(self.current_path,self.engine.duration_ms,False,self.engine.output_frames_total)
            self.smart_status.setText("ثبت تاریخچه فعال است؛ داده‌ها فقط روی این دستگاه ذخیره می‌شوند." if enabled else "ثبت تاریخچه خاموش شد؛ پخش همچنان ادامه دارد.")
        except sqlite3.Error as exc:QMessageBox.warning(self,"خطای پایگاه داده",str(exc))
    def _clear_listening_history(self):
        answer=QMessageBox.question(self,"پاک‌کردن تاریخچه","همهٔ تاریخچه و آمار شنیدن این دستگاه پاک شود؟",QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
        if answer!=QMessageBox.StandardButton.Yes:return
        self._sync_tracker_frames(); self.tracker.finish("stopped",self.engine.output_frames_total)
        try:
            self.conn.execute("DELETE FROM listening_events"); self.conn.execute("DELETE FROM listening_sessions"); self.conn.commit()
            if self.history_enabled and self.is_playing:self.tracker.start(self.current_path,self.engine.duration_ms,False,self.engine.output_frames_total)
            self._refresh_smart_overview(); self.smart_status.setText("تاریخچهٔ قبلی پاک شد.")
        except sqlite3.Error as exc:QMessageBox.warning(self,"خطای پایگاه داده",str(exc))
    def _build_smart_playlist(self):
        if not self.database_available:
            self.smart_status.setText("Smart DJ تا زمان رفع خطای پایگاه داده در دسترس نیست."); return
        if self.smart_worker and self.smart_worker.isRunning():return
        self.build_smart_btn.setEnabled(False); self.smart_status.setText("در حال تحلیل محلی کتابخانه و تاریخچه…")
        excluded=(self.current_path,) if self.engine._playing or self.engine._paused else ()
        worker=SmartDJWorker(str(self.database_path),self.smart_mode.currentText(),self.diversity_slider.value(),excluded,self)
        worker.ready.connect(self._smart_playlist_ready); worker.failed.connect(self._smart_playlist_failed)
        worker.finished.connect(lambda w=worker:self._smart_worker_finished(w))
        self.smart_worker=worker; worker.start()
    def _smart_playlist_ready(self,payload):
        self.build_smart_btn.setEnabled(True)
        recommendations=payload.get("recommendations",[])
        self.smart_list.clear(); self.smart_queue=[]
        for row in recommendations:
            text=f"{row['title']}  ·  {row['artist']}\n{row['reason']}  ·  امتیاز {row['score']:.0f}"
            item=QListWidgetItem(text); item.setData(Qt.ItemDataRole.UserRole,row["path"]); item.setData(Qt.ItemDataRole.UserRole+1,row); self.smart_list.addItem(item); self.smart_queue.append(row["path"])
        if self.engine._playing or self.engine._paused:
            self.queue_paths=[self.current_path]+[p for p in self.smart_queue if p!=self.current_path]
        else:self.queue_paths=list(self.smart_queue)
        self._show_smart_payload(payload)
        self.smart_status.setText(f"{len(recommendations)} آهنگ پیشنهاد شد؛ آهنگ در حال پخش قطع نشد." if recommendations else "کتابخانه آهنگ قابل‌دسترسی برای پیشنهاد ندارد.")
    def _smart_playlist_failed(self,message):
        self.build_smart_btn.setEnabled(True); self.smart_status.setText("ساخت فهرست ناموفق بود.")
        QMessageBox.warning(self,"Smart DJ",message)
    def _smart_worker_finished(self,worker):
        if self.smart_worker is worker:self.smart_worker=None
    def _show_smart_payload(self,payload):
        profile=payload["profile"]
        genres="، ".join(f"{name} ({share:.0f}٪)" for name,share in profile.get("genre_shares",[])) or "دادهٔ کافی نیست"
        artists="، ".join(f"{name} ({score:.1f})" for name,score in profile["artists"]) or "دادهٔ کافی نیست"
        albums="، ".join(f"{name} ({score:.1f})" for name,score in profile["albums"]) or "دادهٔ کافی نیست"
        self.smart_stats.setText(f"آهنگ‌های کتابخانه: {profile['track_count']}  ·  تحلیل‌شده: {profile['analyzed_count']}  ·  رویدادهای شنیدن: {profile['event_count']}  ·  بدون ژانر: {profile['missing_genre_count']}\nسهم از امتیاز ژانرهای دارای داده: {genres}\nخوانندگان محبوب: {artists}\nآلبوم‌های پرتکرار: {albums}")
        self.history_list.clear()
        labels={"started":"شروع پخش","resumed":"ادامه پخش","paused":"مکث","listened":"شنیدن","completed":"تکمیل","skipped":"ردشده","stopped":"توقف","manual_selection":"انتخاب دستی","favorite_added":"افزودن به علاقه‌مندی","favorite_removed":"حذف از علاقه‌مندی"}
        for stamp,kind,path,title,listened,ratio in payload["history"]:
            extra=f" · {self._fmt(listened)} شنیده شد" if listened else ""
            self.history_list.addItem(f"{title}  ·  {labels.get(kind,kind)}{extra}  ·  {str(stamp)[:16]}")
        for widget,key in ((self.top_played_list,"top_played"),(self.top_skipped_list,"top_skipped")):
            widget.clear()
            for path,title,artist,count in payload[key]:widget.addItem(f"{title}  ·  {artist}  ·  {count} بار")
            if not widget.count():widget.addItem("هنوز داده‌ای ثبت نشده است.")
    def _refresh_smart_overview(self):
        try:
            dj=SmartDJRecommender(self.conn)
            self._show_smart_payload({"profile":dj.profile(),"history":dj.history(100),"top_played":dj.top_tracks(False),"top_skipped":dj.top_tracks(True)})
        except sqlite3.Error as exc:self.smart_stats.setText(f"خطا در خواندن تاریخچه: {exc}")
    def _play_selected_suggestion(self):
        item=self.smart_list.currentItem()
        if item:self._play_path(item.data(Qt.ItemDataRole.UserRole),manual=True)
    def _favorite_selected_suggestion(self):
        item=self.smart_list.currentItem()
        if item:self._set_favorite(item.data(Qt.ItemDataRole.UserRole),True)
    def _remove_selected_suggestion(self):
        item=self.smart_list.currentItem()
        if item:self._remove_suggestion(item)
    def _remove_suggestion(self,item):
        row=self.smart_list.row(item); self.smart_list.takeItem(row)
        self.smart_queue=[self.smart_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.smart_list.count())]
        if self.engine._playing or self.engine._paused:self.queue_paths=[self.current_path]+[p for p in self.smart_queue if p!=self.current_path]
        else:self.queue_paths=list(self.smart_queue)
    def _smart_context_menu(self,pos):
        item=self.smart_list.itemAt(pos)
        if not item:return
        self.smart_list.setCurrentItem(item); menu=QMenu(self)
        play=menu.addAction("پخش این آهنگ"); favorite=menu.addAction("افزودن به علاقه‌مندی‌ها"); remove=menu.addAction("حذف از پیشنهادها")
        selected=menu.exec(self.smart_list.viewport().mapToGlobal(pos))
        if selected==play:self._play_selected_suggestion()
        elif selected==favorite:self._favorite_selected_suggestion()
        elif selected==remove:self._remove_suggestion(item)
    def _play_smart_item(self,item):self._play_path(item.data(Qt.ItemDataRole.UserRole),manual=True)
    def _position_changed(self,ms):
        self._sync_tracker_frames()
        if not self.seek_slider.isSliderDown():self.seek_slider.setValue(ms)
        self.elapsed.setText(self._fmt(ms))
    def _duration_changed(self,ms):
        self.duration=ms; self.seek_slider.setRange(0,max(0,ms)); self.total.setText(self._fmt(ms))
    def _play_state(self,playing,force=False):
        if not playing and self._ignore_end_state_signal and not force:
            self._ignore_end_state_signal=False
            return
        if not playing and getattr(self.engine,"_end_notified",False) and not force:
            return
        self.is_playing=playing; self.play_btn.setText("Ⅱ" if playing else "▶"); self.status.setText("در حال پخش" if playing else "متوقف")
        if playing:
            self.tracker.start(self.current_path,self.engine.duration_ms,self._pending_manual_selection,self._pending_start_frames)
            self._pending_manual_selection=False
        elif getattr(self.engine,"_paused",False):
            self.tracker.pause(self.engine.output_frames_total)
        elif self.tracker.active:
            self.tracker.finish("stopped",self.engine.output_frames_total)
    def _toggle_playback(self):
        if self.engine._playing:
            self.engine.toggle(); return
        if self.engine._paused:
            self._pending_start_frames=self.engine.output_frames_total
            self.engine.toggle(); return
        if self.smart_queue and self.current_path not in self.smart_queue:
            self._play_path(self.smart_queue[0],manual=False); return
        if self.engine.duration_ms and self.engine.position_ms>=self.engine.duration_ms:
            self.engine.set_position(0)
        self._pending_start_frames=self.engine.output_frames_total
        self.engine.toggle()
    def _sync_tracker_frames(self):self.tracker.sync_frames(self.engine.output_frames_total)
    def _spectrum(self,levels):self.spectrum.set_levels(levels)
    def _show_audio_error(self,msg):
        self.status.setText("خطای موتور صوتی"); QMessageBox.warning(self,"خطای پخش",msg)
    def _track_ended(self):
        if self.tracker.active:self.tracker.finish("completed",self.engine.output_frames_total)
        self._ignore_end_state_signal=True
        self._play_state(False,force=True)
        if self.repeat_mode==2 and self.current_path:self._play_path(self.current_path,manual=False)
        else:self._next(automatic=True)
    def _track_failed(self):
        if self.tracker.active:self.tracker.finish("error",self.engine.output_frames_total)
        self._ignore_end_state_signal=True
        self._play_state(False,force=True)
        self.status.setText("پخش این آهنگ به‌دلیل خطای رمزگشایی متوقف شد")
    def _toggle_shuffle(self):
        self.shuffle=not self.shuffle; self.shuffle_btn.setStyleSheet("color:#1ED760" if self.shuffle else "")
        self.status.setText("پخش تصادفی روشن" if self.shuffle else "پخش تصادفی خاموش")
    def _cycle_repeat(self):
        self.repeat_mode=(self.repeat_mode+1)%3; self.repeat_btn.setText(["↻","↻۱","↻•"][self.repeat_mode])
        self.status.setText(["تکرار خاموش","تکرار صف","تکرار آهنگ"][self.repeat_mode])
    def _next(self,automatic=False):
        if not self.queue_paths:return
        try:i=self.queue_paths.index(self.current_path)
        except ValueError:i=-1
        if self.shuffle:
            choices=[p for p in self.queue_paths if p!=self.current_path]; path=random.choice(choices) if choices else self.current_path
        elif i+1<len(self.queue_paths):path=self.queue_paths[i+1]
        elif self.repeat_mode==1:path=self.queue_paths[0]
        else:return
        self._play_path(path,manual=not automatic)
    def _previous(self):
        if not self.queue_paths:return
        if self.engine.position_ms>3000:self.engine.set_position(0);return
        try:i=self.queue_paths.index(self.current_path)
        except ValueError:i=0
        self._play_path(self.queue_paths[max(0,i-1)],manual=True)
    @staticmethod
    def _fmt(ms):
        s=max(0,int(ms/1000));return f"{s//60:02d}:{s%60:02d}"
    def closeEvent(self,event):
        try:
            if self.smart_worker and self.smart_worker.isRunning():self.smart_worker.wait()
            self._sync_tracker_frames(); self.tracker.finish("stopped",self.engine.output_frames_total)
            self.engine.close();self.conn.close()
        finally:event.accept()
