import os, sqlite3, random
from pathlib import Path
from mutagen import File as MutagenFile
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QPainter, QColor
from PySide6.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QFileDialog, QLineEdit, QSlider,
    QFrame, QMessageBox, QStackedWidget)
from nova_music.audio.engine import AudioEngine
from nova_music.ui.eq_widget import EqualizerWidget

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
        self.current_path = ""; self.duration = 0; self.is_playing = False
        self.shuffle = False; self.repeat_mode = 0; self.queue_paths = []
        data_dir = Path.home() / "AppData" / "Local" / "NOVA Music"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(data_dir / "library.sqlite3")
        self.conn.execute("CREATE TABLE IF NOT EXISTS tracks(path TEXT PRIMARY KEY,title TEXT,artist TEXT,album TEXT,duration REAL,favorite INTEGER DEFAULT 0)")
        self.conn.commit()
        self._build_ui(); self._load_library(); self._style(); self._shortcuts()

    def _build_ui(self):
        central=QWidget(); self.setCentralWidget(central); root=QVBoxLayout(central); root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        body=QHBoxLayout(); body.setSpacing(0); root.addLayout(body,1)
        sidebar=QFrame(); sidebar.setObjectName("sidebar"); sidebar.setFixedWidth(210); side=QVBoxLayout(sidebar)
        logo=QLabel("◉ NOVA MUSIC"); logo.setObjectName("logo"); side.addWidget(logo)
        self.nav_all=QPushButton("♫   کتابخانه"); self.nav_favs=QPushButton("♡   علاقه‌مندی‌ها"); self.nav_eq=QPushButton("≋   اکولایزر")
        for b in (self.nav_all,self.nav_favs,self.nav_eq): b.setObjectName("navButton"); b.setCursor(Qt.CursorShape.PointingHandCursor); side.addWidget(b)
        side.addStretch(); side.addWidget(QLabel("پخش محلی موسیقی")); body.addWidget(sidebar)
        content=QWidget(); layout=QVBoxLayout(content); layout.setContentsMargins(24,20,24,16); layout.setSpacing(12)
        header=QHBoxLayout(); self.heading=QLabel("کتابخانه موسیقی"); self.heading.setObjectName("pageTitle"); header.addWidget(self.heading); header.addStretch()
        self.search=QLineEdit(); self.search.setPlaceholderText("جستجوی آهنگ، خواننده یا آلبوم…"); self.search.setClearButtonEnabled(True); self.search.setFixedWidth(300); self.search.textChanged.connect(self._filter_tracks); header.addWidget(self.search)
        self.add_files_btn=QPushButton("+ افزودن آهنگ"); self.add_files_btn.clicked.connect(self._choose_files); header.addWidget(self.add_files_btn)
        self.add_folder_btn=QPushButton("افزودن پوشه"); self.add_folder_btn.clicked.connect(self._choose_folder); header.addWidget(self.add_folder_btn); layout.addLayout(header)
        self.stack=QStackedWidget()
        lib=QWidget(); ll=QVBoxLayout(lib); self.track_list=QListWidget(); self.track_list.itemDoubleClicked.connect(self._play_item); self.track_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu); self.track_list.customContextMenuRequested.connect(self._toggle_favorite_at); ll.addWidget(self.track_list); self.stack.addWidget(lib)
        fav=QWidget(); fl=QVBoxLayout(fav); self.fav_list=QListWidget(); self.fav_list.itemDoubleClicked.connect(self._play_item); self.fav_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu); self.fav_list.customContextMenuRequested.connect(self._remove_favorite_at); fl.addWidget(self.fav_list); self.stack.addWidget(fav)
        eqp=QWidget(); el=QVBoxLayout(eqp); self.eq=EqualizerWidget(); self.eq.bandChanged.connect(self.engine.set_eq_band); self.eq.enabledChanged.connect(self.engine.set_eq_enabled); el.addWidget(self.eq); self.stack.addWidget(eqp)
        layout.addWidget(self.stack,1); body.addWidget(content,1)
        now=QFrame(); now.setObjectName("nowPlaying"); nl=QVBoxLayout(now); top=QHBoxLayout(); info=QVBoxLayout()
        self.track_label=QLabel("انتخاب آهنگ"); self.track_label.setObjectName("trackTitle"); self.artist_label=QLabel("هنوز آهنگی پخش نشده"); self.artist_label.setObjectName("muted")
        info.addWidget(self.track_label); info.addWidget(self.artist_label); top.addLayout(info); top.addStretch()
        self.status=QLabel("آماده"); self.status.setObjectName("muted"); top.addWidget(self.status); nl.addLayout(top)
        self.spectrum=SpectrumWidget(); nl.addWidget(self.spectrum)
        controls=QHBoxLayout(); self.shuffle_btn=QPushButton("⤨"); self.prev_btn=QPushButton("|◀"); self.play_btn=QPushButton("▶"); self.next_btn=QPushButton("▶|"); self.repeat_btn=QPushButton("↻")
        for b in (self.shuffle_btn,self.prev_btn,self.play_btn,self.next_btn,self.repeat_btn): b.setObjectName("controlButton"); controls.addWidget(b)
        self.shuffle_btn.clicked.connect(self._toggle_shuffle); self.prev_btn.clicked.connect(self._previous); self.play_btn.clicked.connect(self.engine.toggle); self.next_btn.clicked.connect(self._next); self.repeat_btn.clicked.connect(self._cycle_repeat)
        controls.addStretch(); controls.addWidget(QLabel("صدا")); self.volume=QSlider(Qt.Orientation.Horizontal); self.volume.setRange(0,100); self.volume.setValue(75); self.volume.setFixedWidth(130); self.volume.valueChanged.connect(self.engine.set_volume); controls.addWidget(self.volume); nl.addLayout(controls)
        seek=QHBoxLayout(); self.elapsed=QLabel("00:00"); self.seek_slider=QSlider(Qt.Orientation.Horizontal); self.seek_slider.setRange(0,0); self.seek_slider.sliderMoved.connect(self.engine.set_position); self.total=QLabel("00:00")
        seek.addWidget(self.elapsed); seek.addWidget(self.seek_slider,1); seek.addWidget(self.total); nl.addLayout(seek); root.addWidget(now)
        self.nav_all.clicked.connect(lambda:self._show_page(0,"کتابخانه موسیقی")); self.nav_favs.clicked.connect(lambda:self._show_page(1,"علاقه‌مندی‌ها")); self.nav_eq.clicked.connect(lambda:self._show_page(2,"اکولایزر"))

    def _style(self):
        self.setStyleSheet("""
        QWidget{background:#171a21;color:#e8eaed;font-family:'Segoe UI';font-size:10pt}
        #sidebar{background:#101318} #logo{color:#1ED760;font-size:17pt;font-weight:700;padding:18px 8px}
        #navButton{text-align:left;padding:12px;border:0;border-radius:7px;background:transparent} #navButton:hover{background:#252a33}
        #pageTitle{font-size:22pt;font-weight:700} QLineEdit{background:#252a33;border:1px solid #343b46;border-radius:16px;padding:9px 13px}
        QPushButton{background:#262c35;border:1px solid #343b46;border-radius:8px;padding:9px 12px} QPushButton:hover{background:#333b47}
        #controlButton{min-width:38px;max-width:52px;font-size:14pt} #nowPlaying{background:#101318;border-top:1px solid #303641;padding:8px 20px}
        #trackTitle{font-size:12pt;font-weight:600} #muted{color:#9ba3ae} #sectionTitle{font-size:16pt;font-weight:700}
        QListWidget{background:#1b2028;border:1px solid #303641;border-radius:10px;padding:6px}
        QListWidget::item{padding:10px;border-radius:6px} QListWidget::item:selected{background:#17472a}
        QSlider::groove:horizontal{height:5px;background:#343b46;border-radius:2px} QSlider::handle:horizontal{background:#1ED760;width:12px;margin:-5px 0;border-radius:6px}
        QSlider::groove:vertical{width:5px;background:#343b46;border-radius:2px} QSlider::handle:vertical{background:#1ED760;height:12px;margin:0 -5px;border-radius:6px}
        """)
        self.engine.set_volume(self.volume.value())

    def _shortcuts(self):
        for key,cb in [("Space",self.engine.toggle),("Ctrl+O",self._choose_files),("Ctrl+Shift+O",self._choose_folder),("Right",self._next),("Left",self._previous)]:
            a=QAction(self); a.setShortcut(QKeySequence(key)); a.triggered.connect(cb); self.addAction(a)
    def _show_page(self,i,title):
        self.stack.setCurrentIndex(i); self.heading.setText(title); show=i!=2
        self.search.setVisible(show); self.add_files_btn.setVisible(show); self.add_folder_btn.setVisible(show)
    def _choose_files(self):
        paths,_=QFileDialog.getOpenFileNames(self,"انتخاب فایل‌های صوتی","","Audio files (*.mp3 *.flac *.wav *.ogg *.m4a *.aac *.wma *.opus)")
        if paths:self._add_paths(paths)
    def _choose_folder(self):
        folder=QFileDialog.getExistingDirectory(self,"انتخاب پوشه موسیقی")
        if folder:
            self._add_paths([os.path.join(r,n) for r,_,ns in os.walk(folder) for n in ns if Path(n).suffix.lower() in AUDIO_EXTS])
    def _metadata(self,path):
        title,artist,album,duration=Path(path).stem,"ناشناخته","بدون آلبوم",0.0
        try:
            a=MutagenFile(path,easy=True)
            if a:
                title=(a.get("title") or [title])[0]; artist=(a.get("artist") or a.get("albumartist") or [artist])[0]
                album=(a.get("album") or [album])[0]; duration=getattr(getattr(a,"info",None),"length",0) or 0
        except Exception:pass
        return title,artist,album,float(duration)
    def _add_paths(self,paths):
        added=0
        for path in paths:
            path=os.path.abspath(path)
            if not os.path.isfile(path) or Path(path).suffix.lower() not in AUDIO_EXTS:continue
            t,a,al,d=self._metadata(path)
            cur=self.conn.execute("INSERT OR IGNORE INTO tracks(path,title,artist,album,duration) VALUES(?,?,?,?,?)",(path,t,a,al,d))
            added += max(0,cur.rowcount)
        self.conn.commit(); self._load_library(); self.status.setText(f"{added} آهنگ اضافه شد")
    def _load_library(self):
        self.track_list.clear(); self.fav_list.clear(); self.queue_paths=[]
        rows = self.conn.execute("SELECT path,title,artist,album,duration,favorite FROM tracks ORDER BY artist,title").fetchall()
        missing = {row[0] for row in rows if not os.path.exists(row[0])}
        if missing:
            self.conn.executemany("DELETE FROM tracks WHERE path=?", ((path,) for path in missing))
            self.conn.commit()
        for path,title,artist,album,duration,fav in rows:
            if path in missing:continue
            self.queue_paths.append(path); item=QListWidgetItem(f"{title}\\n{artist}  •  {album}"); item.setData(Qt.ItemDataRole.UserRole,path); self.track_list.addItem(item)
            if fav:
                it=QListWidgetItem(f"♥  {title}\\n{artist}  •  {album}"); it.setData(Qt.ItemDataRole.UserRole,path); self.fav_list.addItem(it)
        self._filter_tracks(self.search.text())
    def _filter_tracks(self,text):
        q=text.casefold().strip()
        for listing in (self.track_list,self.fav_list):
            for i in range(listing.count()):listing.item(i).setHidden(q not in listing.item(i).text().casefold())
    def _play_item(self,item):
        path=item.data(Qt.ItemDataRole.UserRole)
        if path:self._play_path(path)
    def _play_path(self,path):
        self.current_path=path; t,a,al,_=self._metadata(path); self.track_label.setText(t); self.artist_label.setText(f"{a}  •  {al}")
        self.engine.set_source(path); self.engine.play(); self._highlight_current()
    def _highlight_current(self):
        for listing in (self.track_list,self.fav_list):
            for i in range(listing.count()):listing.item(i).setSelected(listing.item(i).data(Qt.ItemDataRole.UserRole)==self.current_path)
    def _toggle_favorite_at(self,pos):
        item=self.track_list.itemAt(pos)
        if not item:return
        path=item.data(Qt.ItemDataRole.UserRole)
        row=self.conn.execute("SELECT favorite FROM tracks WHERE path=?",(path,)).fetchone()
        self.conn.execute("UPDATE tracks SET favorite=? WHERE path=?",(0 if row and row[0] else 1,path)); self.conn.commit(); self._load_library()
    def _remove_favorite_at(self,pos):
        item=self.fav_list.itemAt(pos)
        if item:
            self.conn.execute("UPDATE tracks SET favorite=0 WHERE path=?",(item.data(Qt.ItemDataRole.UserRole),)); self.conn.commit(); self._load_library()
    def _position_changed(self,ms):
        if not self.seek_slider.isSliderDown():self.seek_slider.setValue(ms)
        self.elapsed.setText(self._fmt(ms))
    def _duration_changed(self,ms):
        self.duration=ms; self.seek_slider.setRange(0,max(0,ms)); self.total.setText(self._fmt(ms))
    def _play_state(self,playing):
        self.is_playing=playing; self.play_btn.setText("Ⅱ" if playing else "▶"); self.status.setText("در حال پخش" if playing else "متوقف")
    def _spectrum(self,levels):self.spectrum.set_levels(levels)
    def _show_audio_error(self,msg):
        self.status.setText("خطای موتور صوتی"); QMessageBox.warning(self,"خطای پخش",msg)
    def _track_ended(self):
        if self.repeat_mode==2 and self.current_path:self._play_path(self.current_path)
        else:self._next()
    def _toggle_shuffle(self):
        self.shuffle=not self.shuffle; self.shuffle_btn.setStyleSheet("color:#1ED760" if self.shuffle else "")
        self.status.setText("پخش تصادفی روشن" if self.shuffle else "پخش تصادفی خاموش")
    def _cycle_repeat(self):
        self.repeat_mode=(self.repeat_mode+1)%3; self.repeat_btn.setText(["↻","↻۱","↻•"][self.repeat_mode])
        self.status.setText(["تکرار خاموش","تکرار صف","تکرار آهنگ"][self.repeat_mode])
    def _next(self):
        if not self.queue_paths:return
        try:i=self.queue_paths.index(self.current_path)
        except ValueError:i=-1
        if self.shuffle:
            choices=[p for p in self.queue_paths if p!=self.current_path]; path=random.choice(choices) if choices else self.current_path
        elif i+1<len(self.queue_paths):path=self.queue_paths[i+1]
        elif self.repeat_mode==1:path=self.queue_paths[0]
        else:return
        self._play_path(path)
    def _previous(self):
        if not self.queue_paths:return
        if self.engine.position_ms>3000:self.engine.set_position(0);return
        try:i=self.queue_paths.index(self.current_path)
        except ValueError:i=0
        self._play_path(self.queue_paths[max(0,i-1)])
    @staticmethod
    def _fmt(ms):
        s=max(0,int(ms/1000));return f"{s//60:02d}:{s%60:02d}"
    def closeEvent(self,event):
        try:self.engine.close();self.conn.close()
        finally:event.accept()
