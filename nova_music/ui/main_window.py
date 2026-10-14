import os, sqlite3, random, shutil, subprocess, json, re
import base64, hashlib
from pathlib import Path
from mutagen import File as MutagenFile
from PySide6.QtCore import Qt, QTimer, QSize, QRect, QThread, Signal, QPropertyAnimation
from PySide6.QtGui import QAction, QKeySequence, QPainter, QColor, QIcon, QPixmap, QFont, QFontMetrics, QCursor
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QFileDialog, QLineEdit, QSlider,
    QFrame, QMessageBox, QStackedWidget, QComboBox, QCheckBox, QTabWidget,
    QMenu, QStyledItemDelegate, QStyle, QInputDialog, QProgressDialog, QTreeWidget, QTreeWidgetItem, QDialog, QGraphicsOpacityEffect, QToolButton, QFormLayout, QScrollArea, QProgressBar)
from PySide6.QtWidgets import QSizeGrip
from nova_music.audio.engine import AudioEngine
from nova_music.ui.eq_widget import EqualizerWidget
from nova_music.smart_dj.storage import initialize as initialize_smart_dj
from nova_music.smart_dj.tracking import ListeningTracker, record_favorite
from nova_music.smart_dj.recommender import MODES, SmartDJRecommender
from nova_music.smart_dj.worker import SmartDJWorker
from nova_music.ui.icons import icon
from nova_music.audio.metadata import write_metadata
from nova_music.ui.metadata_dialog import MetadataEditorDialog

AUDIO_EXTS = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".wma", ".opus"}

class WatchedFolderScanner(QThread):
    pathsFound = Signal(list)
    def __init__(self, folders, known_paths, parent=None):
        super().__init__(parent); self.folders=folders; self.known_paths=known_paths
    def run(self):
        discovered=[]; seen=set(self.known_paths)
        for folder in self.folders:
            for root,_,names in os.walk(folder):
                if self.isInterruptionRequested():return
                for name in names:
                    if Path(name).suffix.lower() not in AUDIO_EXTS:continue
                    path=os.path.abspath(os.path.join(root,name)); key=os.path.normcase(path)
                    if key not in seen:seen.add(key); discovered.append(path)
        if not self.isInterruptionRequested():self.pathsFound.emit(discovered)

class SpectrumWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent); self.levels = [0.0] * 24; self.light_theme=False; self.setMinimumHeight(82)
    def set_levels(self, levels): self.levels = list(levels); self.update()
    def set_theme(self,mode):self.light_theme=(mode=="light"); self.update()
    def paintEvent(self, event):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing); p.fillRect(self.rect(), QColor("#edf0f4" if self.light_theme else "#181818"))
        count, gap = len(self.levels), 4; barw = max(2, (self.width()-(count+1)*gap)/count)
        for i, level in enumerate(self.levels):
            h = max(2, int((self.height()-10)*max(0,min(1,level))))
            p.setBrush(QColor("#087b39" if self.light_theme else "#1ED760")); p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(int(gap+i*(barw+gap)), self.height()-h-4, int(barw), h, 3, 3)
        p.end()

class WaveformSlider(QSlider):
    """Seek control with a compact, live amplitude trace for the current track."""
    def __init__(self,parent=None):
        super().__init__(Qt.Orientation.Horizontal,parent)
        self.samples=[0.0]*180; self.accent="#1ed760"; self.light_theme=False
        self.setMinimumHeight(22); self.setAccessibleName("موقعیت پخش؛ نوار را برای جابه‌جایی در آهنگ بکشید")
    def reset_waveform(self):self.samples=[0.0]*len(self.samples); self.update()
    def set_wave_sample(self,position_ms,duration_ms,level):
        if duration_ms<=0:return
        index=max(0,min(len(self.samples)-1,int(position_ms/duration_ms*(len(self.samples)-1))))
        value=max(0.0,min(1.0,float(level)))
        self.samples[index]=max(self.samples[index]*.58,value)
        self.update()
    def set_accent(self,color,light=False):self.accent=color; self.light_theme=light; self.update()
    def paintEvent(self,event):
        painter=QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width=max(1,self.width()-8); left=4; middle=self.height()//2
        groove=QColor("#cbd1d9" if self.light_theme else "#35383d")
        faint=QColor(self.accent); faint.setAlpha(82 if self.light_theme else 72)
        active=QColor(self.accent)
        painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(groove); painter.drawRoundedRect(left,middle-2,width,4,2,2)
        count=max(1,min(len(self.samples),width//3)); slot=width/count; progress=(self.value()-self.minimum())/max(1,self.maximum()-self.minimum())
        for i in range(count):
            start=int(i*len(self.samples)/count); end=max(start+1,int((i+1)*len(self.samples)/count))
            level=max(self.samples[start:end],default=0.0); height=max(2,int(2+level*(self.height()*.43)))
            x=int(left+(i+.5)*slot); color=active if (i+.5)/count<=progress else faint
            painter.setBrush(color); painter.drawRoundedRect(int(x-max(1,slot*.27)),middle-height//2,max(2,int(slot*.54)),height,1,1)
        handle_x=left+int(progress*width); painter.setBrush(active); painter.drawEllipse(handle_x-4,middle-4,8,8)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush); painter.setPen(QColor(self.accent)); painter.drawRoundedRect(self.rect().adjusted(1,1,-1,-1),4,4)
        painter.end()

class WindowDragLabel(QLabel):
    def __init__(self,text,window,parent=None):
        super().__init__(text,parent); self.window_target=window; self._drag_offset=None
    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton and not self.window_target.isMaximized():
            self._drag_offset=event.globalPosition().toPoint()-self.window_target.frameGeometry().topLeft()
        super().mousePressEvent(event)
    def mouseMoveEvent(self,event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window_target.move(event.globalPosition().toPoint()-self._drag_offset)
        super().mouseMoveEvent(event)
    def mouseReleaseEvent(self,event):
        self._drag_offset=None; super().mouseReleaseEvent(event)

class MiniPlayerWindow(QDialog):
    def __init__(self,owner):
        super().__init__(None,Qt.WindowType.Tool|Qt.WindowType.FramelessWindowHint|Qt.WindowType.WindowStaysOnTopHint)
        self.owner=owner; self.size_grip=None
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if self.size_grip:self.size_grip.move(self.width()-16,self.height()-16)
    def closeEvent(self,event):
        super().closeEvent(event)
        if not getattr(self.owner,"_closing_main",False):self.owner._restore_main_from_mini()

class TrackItemDelegate(QStyledItemDelegate):
    """Two-level track rows with clear title and metadata hierarchy."""
    def __init__(self,parent=None,light=False,accent="#1ed760"):
        super().__init__(parent); self.light=light; self.accent=accent
    def paint(self, painter, option, index):
        painter.save(); rect=option.rect
        selected=bool(option.state & QStyle.StateFlag.State_Selected)
        hovered=bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            background=QColor(self.accent).lighter(175).name() if self.light and selected else "#e9edf2" if self.light else QColor(self.accent).darker(290).name() if selected else "#1e1e1e"
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(background)); painter.drawRoundedRect(rect.adjusted(3,2,-3,-2),7,7)
        icon_obj=index.data(Qt.ItemDataRole.DecorationRole)
        if icon_obj:
            icon_obj.paint(painter,QRect(rect.x()+14,rect.y()+(rect.height()-34)//2,34,34))
        lines=str(index.data(Qt.ItemDataRole.DisplayRole) or "").split("\n",1)
        title=lines[0]; subtitle=lines[1] if len(lines)>1 else ""
        title_font=QFont(option.font); title_font.setPointSize(10); title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font); painter.setPen(QColor(self.accent if selected else "#181a1f" if self.light else "#f4f4f4"))
        title_rect=QRect(rect.x()+62,rect.y()+8,max(0,rect.width()-78),22)
        painter.drawText(title_rect,Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,QFontMetrics(title_font).elidedText(title,Qt.TextElideMode.ElideRight,title_rect.width()))
        if subtitle:
            sub_font=QFont(option.font); sub_font.setPointSize(8)
            painter.setFont(sub_font); painter.setPen(QColor("#59616d" if self.light else "#a7a7a7"))
            sub_rect=QRect(rect.x()+62,rect.y()+29,max(0,rect.width()-78),18)
            painter.drawText(sub_rect,Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,QFontMetrics(sub_font).elidedText(subtitle,Qt.TextElideMode.ElideRight,sub_rect.width()))
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(max(100,option.rect.width()),58)

class TrackGridDelegate(QStyledItemDelegate):
    def __init__(self,parent=None,light=False,artwork_size=132,accent="#1ed760"):
        super().__init__(parent); self.light=light; self.artwork_size=artwork_size; self.accent=accent
    def paint(self,painter,option,index):
        painter.save(); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect=option.rect.adjusted(4,4,-4,-4); selected=bool(option.state & QStyle.StateFlag.State_Selected); hovered=bool(option.state & QStyle.StateFlag.State_MouseOver)
        background="#e1f0e6" if self.light and selected else "#e9ebef" if self.light and hovered else "#ffffff" if self.light else "#26372d" if selected else "#202020" if hovered else "#181818"
        painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(background)); painter.drawRoundedRect(rect,12,12)
        image_size=min(self.artwork_size,rect.width()-20); image_rect=QRect(rect.center().x()-image_size//2,rect.top()+10,image_size,image_size)
        artwork=index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(artwork,QIcon):
            pixmap=artwork.pixmap(QSize(image_size,image_size))
            if not pixmap.isNull():painter.drawPixmap(image_rect,pixmap)
        title=str(index.data(Qt.ItemDataRole.DisplayRole) or "").split("\n",1)[0]
        artist=str(index.data(Qt.ItemDataRole.UserRole+1) or "")
        title_font=QFont(option.font); title_font.setWeight(QFont.Weight.DemiBold); title_font.setPointSize(9)
        painter.setFont(title_font); painter.setPen(QColor("#181a1f" if self.light else "#f4f4f4"))
        title_rect=QRect(rect.left()+9,image_rect.bottom()+7,rect.width()-18,20)
        painter.drawText(title_rect,Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,QFontMetrics(title_font).elidedText(title,Qt.TextElideMode.ElideRight,title_rect.width()))
        sub_font=QFont(option.font); sub_font.setPointSize(8); painter.setFont(sub_font); painter.setPen(QColor("#555d68" if self.light else "#a7a7a7"))
        sub_rect=QRect(rect.left()+9,title_rect.bottom()+1,rect.width()-18,17)
        painter.drawText(sub_rect,Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,QFontMetrics(sub_font).elidedText(artist or "هنرمند نامشخص",Qt.TextElideMode.ElideRight,sub_rect.width()))
        if hovered:
            heart=QRect(image_rect.right()-31,image_rect.top()+5,26,26); painter.setBrush(QColor(0,0,0,150)); painter.drawEllipse(heart)
            action_font=QFont(option.font); action_font.setPointSize(12); action_font.setWeight(QFont.Weight.Bold); painter.setFont(action_font)
            painter.setPen(QColor(self.accent if index.data(Qt.ItemDataRole.UserRole+4) else "#ffffff")); painter.drawText(heart,Qt.AlignmentFlag.AlignCenter,"♥" if index.data(Qt.ItemDataRole.UserRole+4) else "♡")
            play=QRect(image_rect.right()-40,image_rect.bottom()-36,34,34); painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(self.accent)); painter.drawEllipse(play)
            action_font.setPointSize(10); painter.setFont(action_font); painter.setPen(QColor("#101010")); painter.drawText(play,Qt.AlignmentFlag.AlignCenter,"▶")
        painter.restore()
    def sizeHint(self,option,index):return QSize(self.artwork_size+52,self.artwork_size+82)

class ReorderableQueueList(QListWidget):
    orderChanged=Signal(list)
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setDragEnabled(True); self.setAcceptDrops(True); self.setDropIndicatorShown(True)
        self.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
    def dropEvent(self,event):
        super().dropEvent(event)
        self.orderChanged.emit([self.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.count())])

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(self.windowFlags()|Qt.WindowType.FramelessWindowHint); self._closing_main=False
        self.setWindowTitle("NOVA Music 8"); self.resize(1280, 820); self.setMinimumSize(980, 650)
        self.setWindowIcon(QIcon(str(Path(__file__).resolve().parents[1] / "assets" / "nova_music.ico")))
        self.engine = AudioEngine(self)
        self.engine.positionChanged.connect(self._position_changed)
        self.engine.durationChanged.connect(self._duration_changed)
        self.engine.playbackStateChanged.connect(self._play_state)
        self.engine.spectrumReady.connect(self._spectrum)
        self.engine.waveformReady.connect(self._waveform_sample)
        self.engine.errorOccurred.connect(self._show_audio_error)
        self.engine.trackEnded.connect(self._track_ended)
        self.engine.trackFailed.connect(self._track_failed)
        self.engine.crossfadeFinished.connect(self._crossfade_finished)
        self.current_path = ""; self.duration = 0; self.is_playing = False
        self.shuffle = False; self.repeat_mode = 0; self.queue_paths = []
        self._queue_mode = "library"; self._active_playlist_id = None
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
        self._cover_art_cache = {}
        self.conn.commit()
        saved_theme=self.conn.execute("SELECT value FROM smart_dj_settings WHERE key='theme_mode'").fetchone()
        self.theme_mode=saved_theme[0] if saved_theme and saved_theme[0] in {"dark","light"} else "dark"
        accent_row=self.conn.execute("SELECT value FROM smart_dj_settings WHERE key='accent_mode'").fetchone(); self.accent_mode=accent_row[0] if accent_row else "dynamic"
        try:
            col_row=self.conn.execute("SELECT value FROM smart_dj_settings WHERE key='library_columns'").fetchone(); self.library_columns=json.loads(col_row[0]) if col_row else {}
        except (ValueError,TypeError):self.library_columns={}
        self.library_columns={key:bool(self.library_columns.get(key,True)) for key in ("artist","album","genre","year","duration")}
        font_row=self.conn.execute("SELECT value FROM smart_dj_settings WHERE key='font_scale'").fetchone(); self.font_scale=float(font_row[0]) if font_row and font_row[0] in {"0.9","1.0","1.15"} else 1.0
        self.watched_folders=self._read_watched_folders()
        self._watch_worker=None
        self._pending_grid_action=None
        self._grid_favorite_timer=QTimer(self); self._grid_favorite_timer.setSingleShot(True); self._grid_favorite_timer.setInterval(260); self._grid_favorite_timer.timeout.connect(self._commit_grid_action)
        self._build_ui(); self._set_track_view_mode(self.view_mode_combo.currentData()); self._load_library(); self._style(); self._shortcuts(); self._show_page(6,"خانه")
        self._restore_playback_state()
        self._state_save_timer=QTimer(self); self._state_save_timer.setInterval(5000)
        self._state_save_timer.timeout.connect(self._save_playback_state); self._state_save_timer.start()
        self._watched_scan_timer=QTimer(self); self._watched_scan_timer.setInterval(60000)
        self._watched_scan_timer.timeout.connect(self._start_watched_scan); self._watched_scan_timer.start()
        if self.watched_folders:QTimer.singleShot(1500,self._start_watched_scan)
        if not self.database_available:
            self.status.setText("پایگاه دادهٔ محلی در دسترس نیست؛ کتابخانه موقت است و پس از بستن ذخیره نمی‌شود.")
            self.smart_status.setText(f"خطای پایگاه داده: {self.database_error}")
            self.build_smart_btn.setEnabled(False); self.history_toggle.setEnabled(False); self.clear_history_btn.setEnabled(False)

    def _create_window_titlebar(self,target,title,mini=False):
        bar=QFrame(); bar.setObjectName("customTitleBar"); bar.setFixedHeight(38)
        row=QHBoxLayout(bar); row.setContentsMargins(14,2,8,2); row.setSpacing(5)
        label=WindowDragLabel(title,target,bar); label.setObjectName("customTitleLabel"); label.setCursor(Qt.CursorShape.OpenHandCursor); label.setAccessibleName("برای جابه‌جایی پنجره عنوان را بکشید"); row.addWidget(label,1)
        def add_button(text,tooltip,callback,object_name="windowControl"):
            button=QPushButton(text); button.setObjectName(object_name); button.setFixedSize(32,28); button.setToolTip(tooltip); button.setAccessibleName(tooltip); button.clicked.connect(callback); row.addWidget(button); return button
        add_button("−","کوچک‌کردن پنجره",target.showMinimized)
        if not mini:
            maximize=QPushButton("□"); maximize.setObjectName("windowControl"); maximize.setFixedSize(32,28); maximize.setToolTip("بزرگ‌نمایی یا بازگردانی پنجره"); maximize.setAccessibleName(maximize.toolTip())
            def toggle_maximize():
                if target.isMaximized():target.showNormal(); maximize.setText("□")
                else:target.showMaximized(); maximize.setText("❐")
            maximize.clicked.connect(toggle_maximize); row.addWidget(maximize)
        if mini:add_button("×","بستن مینی‌پلیر و بازگشت به پلیر اصلی",self._restore_main_from_mini,"windowCloseControl")
        else:add_button("×","بستن برنامه",target.close,"windowCloseControl")
        return bar

    def _build_ui(self):
        central=QWidget(); self.setCentralWidget(central); root=QVBoxLayout(central); root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        root.addWidget(self._create_window_titlebar(self,"NOVA MUSIC")); self.main_size_grip=QSizeGrip(central); self.main_size_grip.setFixedSize(16,16)
        body=QHBoxLayout(); body.setSpacing(0); root.addLayout(body,1)
        sidebar=QFrame(); sidebar.setObjectName("sidebar"); sidebar.setFixedWidth(230); side=QVBoxLayout(sidebar); side.setContentsMargins(20,24,20,20); side.setSpacing(8)
        brand=QHBoxLayout(); brand.setSpacing(10); mark=QLabel(); mark.setPixmap(QPixmap(str(Path(__file__).resolve().parents[1]/"assets"/"nova_music_icon.png")).scaled(38,38,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)); brand.addWidget(mark)
        logo=QLabel("NOVA MUSIC"); logo.setObjectName("logo"); brand.addWidget(logo); brand.addStretch(); side.addLayout(brand); side.addSpacing(26)
        nav_label=QLabel("مرور"); nav_label.setObjectName("sidebarLabel"); side.addWidget(nav_label)
        self.nav_all=QPushButton("کتابخانه"); self.nav_favs=QPushButton("علاقه‌مندی‌ها"); self.nav_eq=QPushButton("اکولایزر"); self.nav_smart=QPushButton("Smart DJ"); self.nav_queue=QPushButton("صف و پلی‌لیست‌ها"); self.nav_files=QPushButton("بازیابی فایل‌ها"); self.nav_lyrics=QPushButton("متن آهنگ")
        for b,ic in ((self.nav_all,"library"),(self.nav_favs,"heart"),(self.nav_eq,"sliders"),(self.nav_smart,"sparkles"),(self.nav_queue,"queue"),(self.nav_files,"folder-search"),(self.nav_lyrics,"lyrics")):
            b.setObjectName("navButton"); b.setIcon(icon(ic)); b.setIconSize(QSize(21,21)); b.setCursor(Qt.CursorShape.PointingHandCursor); b.setMinimumHeight(46); side.addWidget(b)
        side.addSpacing(20); local_label=QLabel("کتابخانهٔ محلی شما"); local_label.setObjectName("sidebarLabel"); side.addWidget(local_label)
        self.sidebar_hint=QLabel("موسیقی‌هایت را اضافه کن و گوش بده."); self.sidebar_hint.setObjectName("sidebarHint"); self.sidebar_hint.setWordWrap(True); side.addWidget(self.sidebar_hint)
        appearance_label=QLabel("تنظیمات"); appearance_label.setObjectName("sidebarLabel"); side.addWidget(appearance_label)
        self.theme_combo=QComboBox(); self.theme_combo.addItem("تم تیره","dark"); self.theme_combo.addItem("تم روشن","light"); self.theme_combo.setCurrentIndex(max(0,self.theme_combo.findData(self.theme_mode)))
        self.appearance_btn=QPushButton("تنظیمات"); self.appearance_btn.setObjectName("navButton"); self.appearance_btn.setIcon(icon("sliders")); self.appearance_btn.setIconSize(QSize(19,19)); self.appearance_btn.setMinimumHeight(44); self.appearance_btn.setToolTip("تنظیم رنگ، کاور و اندازهٔ نوشته‌ها"); side.addWidget(self.appearance_btn)
        self.sidebar=sidebar; self.sidebar_layout=side; self._sidebar_compact=False; self.logo=logo
        self.sidebar_toggle=QPushButton("جمع‌کردن نوار کناری"); self.sidebar_toggle.setObjectName("sidebarToggle"); self.sidebar_toggle.setIcon(icon("chevron-left")); self.sidebar_toggle.clicked.connect(self._toggle_sidebar); side.addWidget(self.sidebar_toggle)
        self.sidebar_extras=[nav_label,local_label,self.sidebar_hint,appearance_label]
        self.sidebar_nav=[self.nav_all,self.nav_favs,self.nav_eq,self.nav_smart,self.nav_queue,self.nav_files,self.nav_lyrics,self.appearance_btn]
        self.artwork_size_combo=QComboBox(); self.artwork_size_combo.addItem("کاور کوچک","small"); self.artwork_size_combo.addItem("کاور متوسط","medium"); self.artwork_size_combo.addItem("کاور بزرگ","large"); self.artwork_size_combo.setCurrentIndex(1)
        self.density_combo=QComboBox(); self.density_combo.addItem("فاصلهٔ معمولی","comfortable"); self.density_combo.addItem("فاصلهٔ فشرده","compact")
        self.accent_combo=QComboBox(); self.accent_combo.addItem("رنگ پویا از کاور","dynamic"); self.accent_combo.addItem("سبز","green"); self.accent_combo.addItem("آبی","blue"); self.accent_combo.addItem("بنفش","purple"); self.accent_combo.addItem("کهربایی","amber")
        self.font_scale_combo=QComboBox(); self.font_scale_combo.addItem("استاندارد","1.0"); self.font_scale_combo.addItem("کوچک‌تر","0.9"); self.font_scale_combo.addItem("بزرگ‌تر","1.15"); self.font_scale_combo.setCurrentIndex(max(0,self.font_scale_combo.findData(str(self.font_scale))))
        self.settings_page=QWidget(); self.settings_page.setObjectName("settingsPage"); self.settings_page.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        appearance_layout=QVBoxLayout(self.settings_page); appearance_layout.setContentsMargins(28,24,28,22); appearance_layout.setSpacing(12)
        appearance_title=QLabel("تنظیمات برنامه"); appearance_title.setObjectName("sectionTitle"); appearance_layout.addWidget(appearance_title)
        appearance_hint=QLabel("ظاهر، دسترس‌پذیری، دستگاه خروجی و پخش را از اینجا مدیریت کن."); appearance_hint.setObjectName("muted"); appearance_hint.setWordWrap(True); appearance_layout.addWidget(appearance_hint)
        appearance_form=QFormLayout(); appearance_form.setSpacing(12); appearance_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
        for label,control in (("پوسته",self.theme_combo),("رنگ تأکیدی",self.accent_combo),("اندازهٔ کاور",self.artwork_size_combo),("تراکم فهرست",self.density_combo),("اندازهٔ نوشته",self.font_scale_combo)):
            control.setMinimumWidth(300); control.setMinimumHeight(40); appearance_form.addRow(label,control)
        appearance_layout.addLayout(appearance_form)
        playback_title=QLabel("صدا و پخش"); playback_title.setObjectName("sectionTitle"); appearance_layout.addWidget(playback_title)
        self.settings_playback_form=QFormLayout(); self.settings_playback_form.setSpacing(10); self.settings_playback_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
        self.settings_output_device=QComboBox(); self.settings_output_device.setAccessibleName("دستگاه خروجی صدا در تنظیمات")
        for device_id,name in self.engine.output_devices():self.settings_output_device.addItem(name,device_id)
        self.settings_crossfade=QSlider(Qt.Orientation.Horizontal); self.settings_crossfade.setRange(0,12); self.settings_crossfade.setToolTip("مدت هم‌پوشانی دو آهنگ، از صفر تا دوازده ثانیه")
        self.settings_volume=QSlider(Qt.Orientation.Horizontal); self.settings_volume.setRange(0,100); self.settings_volume.setToolTip("بلندی صدای پخش")
        self.settings_playback_form.addRow("دستگاه خروجی",self.settings_output_device); self.settings_playback_form.addRow("Crossfade",self.settings_crossfade); self.settings_playback_form.addRow("بلندی صدا",self.settings_volume); appearance_layout.addLayout(self.settings_playback_form)
        appearance_layout.addStretch()
        self.appearance_btn.clicked.connect(lambda:self._show_page(8,"تنظیمات"))
        side.addStretch(); side.addWidget(QLabel("♫   ساخته‌شده برای شنیدن بهتر")); body.addWidget(sidebar)
        content=QWidget(); layout=QVBoxLayout(content); layout.setContentsMargins(24,24,24,20); layout.setSpacing(16)
        header=QHBoxLayout(); self.heading=QLabel("کتابخانه موسیقی"); self.heading.setObjectName("pageTitle"); header.addWidget(self.heading); header.addStretch()
        self.focus_btn=QPushButton("حالت خلوت"); self.focus_btn.setObjectName("focusModeButton"); self.focus_btn.setIcon(icon("expand")); self.focus_btn.setToolTip("پنهان‌کردن نوارها برای تمرکز روی موسیقی"); self.focus_btn.clicked.connect(self._toggle_focus_mode); header.addWidget(self.focus_btn)
        self.search=QLineEdit(); self.search.setObjectName("searchBox"); self.search.setPlaceholderText("جستجوی آهنگ، خواننده یا آلبوم…"); self.search.setClearButtonEnabled(True); self.search.addAction(icon("search","#b3b3b3",18),QLineEdit.ActionPosition.LeadingPosition); self.search.setFixedWidth(270); self.search.textChanged.connect(self._filter_tracks); header.addWidget(self.search)
        self.add_files_btn=QPushButton("افزودن آهنگ"); self.add_files_btn.setObjectName("add_files_btn"); self.add_files_btn.setIcon(icon("plus","#111111")); self.add_files_btn.clicked.connect(self._choose_files); header.addWidget(self.add_files_btn)
        self.add_folder_btn=QPushButton("افزودن پوشه"); self.add_folder_btn.setObjectName("add_folder_btn"); self.add_folder_btn.setIcon(icon("folder-plus","#111111")); self.add_folder_btn.clicked.connect(self._choose_folder); header.addWidget(self.add_folder_btn); layout.addLayout(header)
        filter_bar=QHBoxLayout(); filter_bar.setSpacing(8)
        self.sort_combo=QComboBox(); self.sort_combo.setToolTip("مرتب‌سازی آهنگ‌ها"); self.sort_combo.setMinimumWidth(118)
        for label,value in (("عنوان","title"),("هنرمند","artist"),("آلبوم","album"),("سال جدیدتر","year"),("مدت","duration")):self.sort_combo.addItem(label,value)
        saved_sort=self.conn.execute("SELECT value FROM smart_dj_settings WHERE key='track_sort_mode'").fetchone()
        self.sort_combo.setCurrentIndex(max(0,self.sort_combo.findData(saved_sort[0] if saved_sort else "artist")))
        self.sort_combo.currentIndexChanged.connect(self._track_sort_changed); self.sort_label=QLabel("مرتب‌سازی"); filter_bar.addWidget(self.sort_label); filter_bar.addWidget(self.sort_combo)
        self.view_mode_combo=QComboBox(); self.view_mode_combo.addItem("فهرست","list"); self.view_mode_combo.addItem("گرید","grid"); self.view_mode_combo.setToolTip("نوع نمایش آهنگ‌ها"); self.view_mode_combo.setMinimumWidth(90); filter_bar.addWidget(self.view_mode_combo)
        self.filter_artist_combo=QComboBox(); self.filter_artist_combo.setMinimumWidth(110); self.filter_artist_combo.addItem("همهٔ هنرمندان",None)
        self.filter_album_combo=QComboBox(); self.filter_album_combo.setMinimumWidth(110); self.filter_album_combo.addItem("همهٔ آلبوم‌ها",None)
        self.filter_genre_combo=QComboBox(); self.filter_genre_combo.setMinimumWidth(110); self.filter_genre_combo.addItem("همهٔ سبک‌ها",None)
        for combo in (self.filter_artist_combo,self.filter_album_combo,self.filter_genre_combo):combo.currentIndexChanged.connect(lambda _:self._filter_tracks(self.search.text())); filter_bar.addWidget(combo)
        self.clear_filters_btn=QPushButton("پاک‌کردن فیلترها"); self.clear_filters_btn.clicked.connect(self._clear_track_filters); filter_bar.addWidget(self.clear_filters_btn); self.columns_btn=QToolButton(); self.columns_btn.setText("ستون‌ها"); self.columns_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup); self.columns_menu=QMenu(self); self.columns_btn.setMenu(self.columns_menu); filter_bar.addWidget(self.columns_btn); self._build_library_columns_menu(); filter_bar.addStretch(); layout.addLayout(filter_bar)
        for combo,key,default in ((self.artwork_size_combo,"artwork_size","medium"),(self.density_combo,"density","comfortable"),(self.accent_combo,"accent_mode",self.accent_mode),(self.font_scale_combo,"font_scale",str(self.font_scale))):
            row=self.conn.execute("SELECT value FROM smart_dj_settings WHERE key=?",(key,)).fetchone(); combo.setCurrentIndex(max(0,combo.findData(row[0] if row else default)))
        sidebar_saved=self.conn.execute("SELECT value FROM smart_dj_settings WHERE key='sidebar_compact'").fetchone()
        if sidebar_saved and sidebar_saved[0]=="1":QTimer.singleShot(0,self._toggle_sidebar)
        saved_view=self.conn.execute("SELECT value FROM smart_dj_settings WHERE key='track_view_mode'").fetchone()
        saved_view=saved_view[0] if saved_view and saved_view[0] in {"list","grid"} else "list"
        self.view_mode_combo.setCurrentIndex(self.view_mode_combo.findData(saved_view))
        self.view_mode_combo.currentIndexChanged.connect(lambda _:self._set_track_view_mode(self.view_mode_combo.currentData(),persist=True))
        self.stack=QStackedWidget()
        lib=QWidget(); ll=QVBoxLayout(lib); ll.setContentsMargins(0,8,0,0); self.track_list=QListWidget(); self.track_list.setObjectName("trackList"); self.track_list.setAccessibleName("فهرست آهنگ‌های کتابخانه"); self.track_list.setIconSize(QSize(34,34)); self.track_list.setItemDelegate(TrackItemDelegate(self.track_list)); self.track_list.itemDoubleClicked.connect(self._play_item); self.track_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu); self.track_list.customContextMenuRequested.connect(lambda p:self._track_context_menu(self.track_list,p)); self.library_empty_hint=QLabel("کتابخانه‌ات خالی است\nبرای شروع، آهنگ یا پوشهٔ موسیقی را اضافه کن."); self.library_empty_hint.setObjectName("emptyState"); self.library_empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter); ll.addWidget(self.library_empty_hint); ll.addWidget(self.track_list); self.stack.addWidget(lib)
        fav=QWidget(); fl=QVBoxLayout(fav); fl.setContentsMargins(0,8,0,0); self.fav_list=QListWidget(); self.fav_list.setObjectName("trackList"); self.fav_list.setAccessibleName("فهرست آهنگ‌های مورد علاقه"); self.fav_list.setIconSize(QSize(34,34)); self.fav_list.setItemDelegate(TrackItemDelegate(self.fav_list)); self.fav_list.itemDoubleClicked.connect(self._play_item); self.fav_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu); self.fav_list.customContextMenuRequested.connect(lambda p:self._track_context_menu(self.fav_list,p)); self.favorites_empty_hint=QLabel("هنوز آهنگی به علاقه‌مندی‌ها اضافه نشده است."); self.favorites_empty_hint.setObjectName("emptyState"); self.favorites_empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter); fl.addWidget(self.favorites_empty_hint); fl.addWidget(self.fav_list); self.stack.addWidget(fav)
        self.track_list.itemClicked.connect(lambda item:self._grid_item_clicked(self.track_list,item)); self.fav_list.itemClicked.connect(lambda item:self._grid_item_clicked(self.fav_list,item))
        self.theme_combo.currentIndexChanged.connect(lambda _:self._set_theme(self.theme_combo.currentData(),persist=True))
        eqp=QWidget(); el=QVBoxLayout(eqp); self.eq=EqualizerWidget(); self.eq.bandChanged.connect(self.engine.set_eq_band); self.eq.enabledChanged.connect(self.engine.set_eq_enabled); el.addWidget(self.eq); self.stack.addWidget(eqp)
        smart=QWidget(); sl=QVBoxLayout(smart); sl.setSpacing(10)
        card=QFrame(); card.setObjectName("smartCard"); cl=QVBoxLayout(card)
        card_title=QLabel("Smart DJ  ·  پیشنهاددهندهٔ آفلاین"); card_title.setObjectName("sectionTitle"); cl.addWidget(card_title)
        options=QHBoxLayout(); self.smart_mode=QComboBox(); self.smart_mode.addItems(MODES); self.smart_mode.setCurrentText("Balanced")
        self.diversity_slider=QSlider(Qt.Orientation.Horizontal); self.diversity_slider.setRange(0,100); self.diversity_slider.setValue(50); self.diversity_value=QLabel("50٪")
        self.diversity_slider.valueChanged.connect(lambda v:self.diversity_value.setText(f"{v}٪"))
        self.build_smart_btn=QPushButton("ساخت / بازسازی صف"); self.build_smart_btn.setIcon(icon("sparkles")); self.build_smart_btn.clicked.connect(self._build_smart_playlist)
        options.addWidget(QLabel("حالت")); options.addWidget(self.smart_mode); options.addWidget(QLabel("تنوع")); options.addWidget(self.diversity_slider,1); options.addWidget(self.diversity_value); options.addWidget(self.build_smart_btn); cl.addLayout(options)
        privacy=QHBoxLayout(); self.history_toggle=QCheckBox("ثبت تاریخچهٔ شنیدن روی همین دستگاه"); self.history_toggle.setChecked(self.history_enabled); self.history_toggle.toggled.connect(self._set_history_enabled)
        self.clear_history_btn=QPushButton("پاک‌کردن تاریخچه"); self.clear_history_btn.setIcon(icon("trash")); self.clear_history_btn.clicked.connect(self._clear_listening_history)
        privacy.addWidget(self.history_toggle); privacy.addStretch(); privacy.addWidget(self.clear_history_btn); cl.addLayout(privacy)
        self.smart_stats=QLabel("هنوز آماری ثبت نشده است."); self.smart_stats.setObjectName("muted"); self.smart_stats.setWordWrap(True); cl.addWidget(self.smart_stats)
        sl.addWidget(card)
        tabs=QTabWidget(); self.smart_list=QListWidget(); self.smart_list.setIconSize(QSize(34,34)); self.smart_list.setItemDelegate(TrackItemDelegate(self.smart_list)); self.smart_list.itemDoubleClicked.connect(self._play_smart_item); self.smart_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu); self.smart_list.customContextMenuRequested.connect(self._smart_context_menu); tabs.addTab(self.smart_list,"پیشنهادها")
        self.history_list=QListWidget(); self.history_list.setIconSize(QSize(34,34)); tabs.addTab(self.history_list,"تاریخچه")
        self.top_played_list=QListWidget(); self.top_played_list.setIconSize(QSize(34,34)); tabs.addTab(self.top_played_list,"بیشتر شنیده‌شده")
        self.top_skipped_list=QListWidget(); self.top_skipped_list.setIconSize(QSize(34,34)); tabs.addTab(self.top_skipped_list,"بیشتر ردشده")
        sl.addWidget(tabs,1)
        smart_actions=QHBoxLayout(); self.play_suggestion_btn=QPushButton("پخش انتخاب‌شده"); self.play_suggestion_btn.setIcon(icon("play","#121212")); self.play_suggestion_btn.clicked.connect(self._play_selected_suggestion)
        self.favorite_suggestion_btn=QPushButton("افزودن به علاقه‌مندی‌ها"); self.favorite_suggestion_btn.setIcon(icon("heart")); self.favorite_suggestion_btn.clicked.connect(self._favorite_selected_suggestion)
        self.remove_suggestion_btn=QPushButton("حذف از پیشنهادها"); self.remove_suggestion_btn.setIcon(icon("x")); self.remove_suggestion_btn.clicked.connect(self._remove_selected_suggestion)
        smart_actions.addWidget(self.play_suggestion_btn); smart_actions.addWidget(self.favorite_suggestion_btn); smart_actions.addWidget(self.remove_suggestion_btn); smart_actions.addStretch(); sl.addLayout(smart_actions)
        self.smart_status=QLabel("برای ساخت صف شخصی، حالت و میزان تنوع را انتخاب کنید."); self.smart_status.setObjectName("muted"); sl.addWidget(self.smart_status)
        self.stack.addWidget(smart)
        queue_page=QWidget(); ql=QVBoxLayout(queue_page); ql.setContentsMargins(0,8,0,0); ql.setSpacing(12)
        playlist_bar=QHBoxLayout(); playlist_bar.addWidget(QLabel("پلی‌لیست"))
        self.playlist_combo=QComboBox(); self.playlist_combo.setMinimumWidth(220); self.playlist_combo.currentIndexChanged.connect(self._playlist_selected); playlist_bar.addWidget(self.playlist_combo)
        self.new_playlist_btn=QPushButton("پلی‌لیست جدید"); self.new_playlist_btn.setIcon(icon("plus")); self.new_playlist_btn.clicked.connect(self._create_playlist); playlist_bar.addWidget(self.new_playlist_btn)
        self.save_playlist_btn=QPushButton("ذخیرهٔ تغییرات"); self.save_playlist_btn.clicked.connect(self._save_playlist); playlist_bar.addWidget(self.save_playlist_btn)
        self.delete_playlist_btn=QPushButton("حذف پلی‌لیست"); self.delete_playlist_btn.setIcon(icon("trash")); self.delete_playlist_btn.clicked.connect(self._delete_playlist); playlist_bar.addWidget(self.delete_playlist_btn); playlist_bar.addStretch(); ql.addLayout(playlist_bar)
        playlist_io=QHBoxLayout(); self.export_playlist_btn=QPushButton("خروجی گرفتن (M3U)"); self.export_playlist_btn.clicked.connect(self._choose_playlist_export); playlist_io.addWidget(self.export_playlist_btn)
        self.import_playlist_btn=QPushButton("واردکردن پلی‌لیست (M3U)"); self.import_playlist_btn.clicked.connect(self._choose_playlist_import); playlist_io.addWidget(self.import_playlist_btn); playlist_io.addStretch(); ql.addLayout(playlist_io)
        self.queue_hint=QLabel("ترتیب صف را تغییر بده یا از منوی آهنگ‌ها، آهنگ بعدی را اضافه کن."); self.queue_hint.setObjectName("muted"); ql.addWidget(self.queue_hint)
        self.queue_list=ReorderableQueueList(); self.queue_list.setObjectName("trackList"); self.queue_list.setAccessibleName("صف پخش؛ برای تغییر ترتیب کارت‌ها آن‌ها را بکشید"); self.queue_list.setViewMode(QListWidget.ViewMode.IconMode); self.queue_list.setFlow(QListWidget.Flow.LeftToRight); self.queue_list.setWrapping(True); self.queue_list.setResizeMode(QListWidget.ResizeMode.Adjust); self.queue_list.setMovement(QListWidget.Movement.Snap); self.queue_list.setGridSize(QSize(190,222)); self.queue_list.setIconSize(QSize(132,132)); self.queue_list.setItemDelegate(TrackGridDelegate(self.queue_list,self.theme_mode=="light",132,getattr(self,"accent_color","#1ed760"))); self.queue_list.itemDoubleClicked.connect(self._play_queue_item); self.queue_list.itemClicked.connect(lambda item:self._grid_item_clicked(self.queue_list,item)); self.queue_list.orderChanged.connect(self._queue_order_changed); ql.addWidget(self.queue_list,1)
        queue_actions=QHBoxLayout()
        self.queue_up_btn=QPushButton("انتقال به بالا"); self.queue_up_btn.clicked.connect(lambda:self._move_queue_item(-1)); queue_actions.addWidget(self.queue_up_btn)
        self.queue_down_btn=QPushButton("انتقال به پایین"); self.queue_down_btn.clicked.connect(lambda:self._move_queue_item(1)); queue_actions.addWidget(self.queue_down_btn)
        self.queue_remove_btn=QPushButton("حذف از صف"); self.queue_remove_btn.setIcon(icon("x")); self.queue_remove_btn.clicked.connect(self._remove_queue_item); queue_actions.addWidget(self.queue_remove_btn)
        self.queue_clear_btn=QPushButton("پاک‌کردن صف"); self.queue_clear_btn.clicked.connect(self._clear_queue); queue_actions.addWidget(self.queue_clear_btn)
        queue_actions.addStretch(); self.play_queue_btn=QPushButton("پخش صف"); self.play_queue_btn.setIcon(icon("play","#121212")); self.play_queue_btn.clicked.connect(self._play_queue); queue_actions.addWidget(self.play_queue_btn); ql.addLayout(queue_actions)
        self.stack.addWidget(queue_page)
        files_page=QWidget(); files_layout=QVBoxLayout(files_page); files_layout.setContentsMargins(0,8,0,0); files_layout.setSpacing(12)
        self.file_tabs=QTabWidget(); files_layout.addWidget(self.file_tabs)
        missing_page=QWidget(); missing_layout=QVBoxLayout(missing_page); missing_layout.setContentsMargins(0,8,0,0); missing_layout.setSpacing(12)
        files_intro=QLabel("اگر مسیر یک آهنگ تغییر کرده، اطلاعات و جایگاهش در پلی‌لیست‌ها حفظ می‌شود. پوشهٔ جدید را اسکن کن یا فایل را دستی پیدا کن."); files_intro.setObjectName("muted"); files_intro.setWordWrap(True); missing_layout.addWidget(files_intro)
        files_actions=QHBoxLayout()
        self.scan_missing_btn=QPushButton("اسکن پوشه برای پیدا کردن فایل‌ها"); self.scan_missing_btn.setIcon(icon("folder-search")); self.scan_missing_btn.clicked.connect(self._scan_for_moved_files); files_actions.addWidget(self.scan_missing_btn)
        self.relink_missing_btn=QPushButton("پیداکردن دستی فایل انتخاب‌شده…"); self.relink_missing_btn.clicked.connect(self._choose_relink_path); self.relink_missing_btn.setEnabled(False); files_actions.addWidget(self.relink_missing_btn)
        self.missing_count=QLabel(); self.missing_count.setObjectName("muted"); files_actions.addStretch(); files_actions.addWidget(self.missing_count); missing_layout.addLayout(files_actions)
        self.missing_list=QListWidget(); self.missing_list.setObjectName("trackList"); self.missing_list.setIconSize(QSize(34,34)); self.missing_list.setItemDelegate(TrackItemDelegate(self.missing_list)); self.missing_list.currentRowChanged.connect(lambda row:self.relink_missing_btn.setEnabled(row>=0)); self.missing_list.itemDoubleClicked.connect(lambda item:self._choose_relink_path()); missing_layout.addWidget(self.missing_list,1)
        self.file_tabs.addTab(missing_page,"فایل‌های گم‌شده")
        duplicates_page=QWidget(); duplicates_layout=QVBoxLayout(duplicates_page); duplicates_layout.setContentsMargins(4,10,4,4); duplicates_layout.setSpacing(10)
        duplicates_intro=QLabel("فقط فایل‌هایی گزارش می‌شوند که محتوای باینری آن‌ها کاملاً یکسان باشد؛ اسکن هیچ فایلی را حذف یا تغییر نمی‌دهد."); duplicates_intro.setObjectName("muted"); duplicates_intro.setWordWrap(True); duplicates_layout.addWidget(duplicates_intro)
        duplicate_actions=QHBoxLayout(); self.scan_duplicates_btn=QPushButton("اسکن نسخه‌های تکراری"); self.scan_duplicates_btn.setIcon(icon("search")); self.scan_duplicates_btn.clicked.connect(self._scan_library_duplicates); duplicate_actions.addWidget(self.scan_duplicates_btn)
        self.duplicate_status=QLabel("برای بررسی، اسکن را اجرا کن."); self.duplicate_status.setObjectName("muted"); duplicate_actions.addStretch(); duplicate_actions.addWidget(self.duplicate_status); duplicates_layout.addLayout(duplicate_actions)
        self.duplicate_list=QTreeWidget(); self.duplicate_list.setObjectName("trackList"); self.duplicate_list.setHeaderHidden(True); self.duplicate_list.setIndentation(22); duplicates_layout.addWidget(self.duplicate_list,1)
        self.file_tabs.addTab(duplicates_page,"نسخه‌های تکراری")
        watched_page=QWidget(); watched_layout=QVBoxLayout(watched_page); watched_layout.setContentsMargins(4,10,4,4); watched_layout.setSpacing(10)
        watched_intro=QLabel("پوشه‌های انتخاب‌شده هر دقیقه در پس‌زمینه بررسی می‌شوند و آهنگ‌های تازه به کتابخانه اضافه می‌شوند."); watched_intro.setObjectName("muted"); watched_intro.setWordWrap(True); watched_layout.addWidget(watched_intro)
        watched_actions=QHBoxLayout(); self.add_watched_folder_btn=QPushButton("افزودن پوشه برای پایش"); self.add_watched_folder_btn.setIcon(icon("folder-plus")); self.add_watched_folder_btn.clicked.connect(self._choose_watched_folder); watched_actions.addWidget(self.add_watched_folder_btn)
        self.remove_watched_folder_btn=QPushButton("حذف پوشهٔ انتخاب‌شده"); self.remove_watched_folder_btn.clicked.connect(self._remove_watched_folder); self.remove_watched_folder_btn.setEnabled(False); watched_actions.addWidget(self.remove_watched_folder_btn)
        watched_actions.addStretch(); self.watch_status=QLabel("پایش پوشه غیرفعال است."); self.watch_status.setObjectName("muted"); watched_actions.addWidget(self.watch_status); watched_layout.addLayout(watched_actions)
        self.watched_folder_list=QListWidget(); self.watched_folder_list.setObjectName("trackList"); self.watched_folder_list.currentRowChanged.connect(lambda row:self.remove_watched_folder_btn.setEnabled(row>=0)); watched_layout.addWidget(self.watched_folder_list,1)
        self.file_tabs.addTab(watched_page,"پایش پوشه‌ها")
        self._refresh_watched_folder_list()
        self.stack.addWidget(files_page)
        lyrics_page=QWidget(); self.lyrics_page=lyrics_page; lyrics_layout=QVBoxLayout(lyrics_page); lyrics_layout.setContentsMargins(22,18,22,18); lyrics_layout.setSpacing(14)
        lyrics_header=QHBoxLayout(); self.lyrics_cover=QLabel(); self.lyrics_cover.setFixedSize(92,92); self.lyrics_cover.setObjectName("largeCoverArt"); self.lyrics_cover.setAlignment(Qt.AlignmentFlag.AlignCenter); lyrics_header.addWidget(self.lyrics_cover)
        lyrics_meta=QVBoxLayout(); self.lyrics_title=QLabel("متن آهنگ"); self.lyrics_title.setObjectName("pageTitle"); self.lyrics_artist=QLabel("یک آهنگ پخش کن تا متن آن نمایش داده شود"); self.lyrics_artist.setObjectName("muted"); lyrics_meta.addWidget(self.lyrics_title); lyrics_meta.addWidget(self.lyrics_artist); lyrics_meta.addStretch(); lyrics_header.addLayout(lyrics_meta,1); lyrics_layout.addLayout(lyrics_header)
        self.lyrics_list=QListWidget(); self.lyrics_list.setAccessibleName("متن هم‌زمان آهنگ"); self.lyrics_list.setObjectName("lyricsList"); self.lyrics_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection); lyrics_layout.addWidget(self.lyrics_list,1)
        self.lyrics_empty=QLabel("متنِ درون‌فایل نمایش داده می‌شود. برای هم‌زمان‌سازی، فایل باید متن LRC دارای زمان‌بندی داشته باشد."); self.lyrics_empty.setObjectName("muted"); self.lyrics_empty.setWordWrap(True); lyrics_layout.addWidget(self.lyrics_empty); self.stack.addWidget(lyrics_page); self.stack.addWidget(self.settings_page)
        home=QWidget(); home_outer=QVBoxLayout(home); home_outer.setContentsMargins(0,0,0,0); self.home_scroll=QScrollArea(); self.home_scroll.setWidgetResizable(True); self.home_scroll.setFrameShape(QFrame.Shape.NoFrame); home_outer.addWidget(self.home_scroll)
        self.home_dashboard=QWidget(); hl=QVBoxLayout(self.home_dashboard); hl.setContentsMargins(0,8,6,20); hl.setSpacing(18); self.home_scroll.setWidget(self.home_dashboard)
        self.home_welcome=QLabel("موسیقی تو، با حال‌وهوای خودت"); self.home_welcome.setObjectName("homeWelcome")
        home_actions=QHBoxLayout(); self.home_library_btn=QPushButton("رفتن به کتابخانه"); self.home_library_btn.setIcon(icon("library")); self.home_library_btn.clicked.connect(lambda:self._show_page(0,"کتابخانه موسیقی")); home_actions.addWidget(self.home_library_btn)
        self.home_smart_btn=QPushButton("تازه‌سازی پیشنهادها"); self.home_smart_btn.setIcon(icon("sparkles")); self.home_smart_btn.clicked.connect(self._request_home_smart_playlist); home_actions.addWidget(self.home_smart_btn); home_actions.addStretch()
        self.home_layout_btn=QPushButton("چیدمان صفحه"); self.home_layout_btn.setIcon(icon("sliders")); self.home_layout_btn.clicked.connect(self._configure_home_sections); home_actions.addWidget(self.home_layout_btn)
        hl.addWidget(self.home_welcome); hl.addLayout(home_actions)
        self.home_summary=QLabel(); self.home_summary.setObjectName("muted"); hl.addWidget(self.home_summary)
        self.home_hero=QFrame(); self.home_hero.setObjectName("homeHero"); hero_layout=QHBoxLayout(self.home_hero); hero_layout.setContentsMargins(24,22,24,22); hero_layout.setSpacing(22)
        self.home_hero_cover=QLabel(); self.home_hero_cover.setObjectName("homeHeroCover"); self.home_hero_cover.setFixedSize(148,148); self.home_hero_cover.setAlignment(Qt.AlignmentFlag.AlignCenter); hero_layout.addWidget(self.home_hero_cover)
        hero_copy=QVBoxLayout(); self.home_hero_kicker=QLabel("پیشنهاد امروز برای تو"); self.home_hero_kicker.setObjectName("homeHeroKicker"); hero_copy.addWidget(self.home_hero_kicker); self.home_hero_title=QLabel("کتابخانهٔ موسیقی تو"); self.home_hero_title.setObjectName("homeHeroTitle"); self.home_hero_title.setWordWrap(True); hero_copy.addWidget(self.home_hero_title); self.home_hero_subtitle=QLabel("با یک انتخاب، شنیدن را شروع کن."); self.home_hero_subtitle.setObjectName("homeHeroSubtitle"); self.home_hero_subtitle.setWordWrap(True); hero_copy.addWidget(self.home_hero_subtitle); hero_copy.addStretch(); self.home_hero_play=QPushButton("پخش این مجموعه"); self.home_hero_play.setObjectName("homeHeroPlay"); self.home_hero_play.setIcon(icon("play","#101010")); self.home_hero_play.clicked.connect(self._play_home_feature); hero_copy.addWidget(self.home_hero_play,0,Qt.AlignmentFlag.AlignLeft); hero_layout.addLayout(hero_copy,1); hl.addWidget(self.home_hero)
        resume_section=QWidget(); resume_layout=QVBoxLayout(resume_section); resume_layout.setContentsMargins(0,0,0,0); resume_heading=QLabel("ادامهٔ شنیدن"); resume_heading.setObjectName("homeSectionTitle"); resume_layout.addWidget(resume_heading); resume_row=QHBoxLayout(); self.home_resume_cover=QLabel(); self.home_resume_cover.setObjectName("homeResumeCover"); self.home_resume_cover.setFixedSize(58,58); resume_row.addWidget(self.home_resume_cover); resume_text=QVBoxLayout(); self.home_resume_title=QLabel("هنوز آهنگی انتخاب نشده"); self.home_resume_title.setObjectName("trackTitle"); resume_text.addWidget(self.home_resume_title); self.home_resume_subtitle=QLabel("آخرین پخش را از همین‌جا ادامه بده"); self.home_resume_subtitle.setObjectName("muted"); resume_text.addWidget(self.home_resume_subtitle); self.home_resume_progress=QProgressBar(); self.home_resume_progress.setRange(0,1000); self.home_resume_progress.setTextVisible(False); self.home_resume_progress.setFixedHeight(5); resume_text.addWidget(self.home_resume_progress); resume_row.addLayout(resume_text,1); self.home_resume_btn=QPushButton("ادامه"); self.home_resume_btn.setIcon(icon("play")); self.home_resume_btn.clicked.connect(self._continue_home_track); resume_row.addWidget(self.home_resume_btn); resume_layout.addLayout(resume_row)
        self.home_recent_section,self.home_recent_list=self._create_home_card_section("پخش‌های اخیر")
        self.home_popular_section,self.home_popular_list=self._create_home_card_section("پرشنیده‌شده‌ها")
        self.home_smart_section,self.home_smart_cards=self._create_home_card_section("پیشنهادهای Smart DJ")
        self.home_recent_list.itemDoubleClicked.connect(self._play_item); self.home_popular_list.itemDoubleClicked.connect(self._play_item); self.home_smart_cards.itemDoubleClicked.connect(self._play_home_smart_item)
        for listing in (self.home_recent_list,self.home_popular_list,self.home_smart_cards):listing.itemClicked.connect(lambda item,l=listing:self._grid_item_clicked(l,item))
        self.home_smart_status=QLabel("برای پیشنهاد شخصی، چند آهنگ گوش بده یا پیشنهادها را تازه‌سازی کن."); self.home_smart_status.setObjectName("muted"); self.home_smart_section.layout().addWidget(self.home_smart_status)
        self.home_sections={"resume":resume_section,"recent":self.home_recent_section,"popular":self.home_popular_section,"smart":self.home_smart_section}
        self.home_section_labels={"resume":"ادامهٔ شنیدن","recent":"پخش‌های اخیر","popular":"پرشنیده‌شده‌ها","smart":"پیشنهادهای Smart DJ"}
        saved_layout=self._read_home_layout(); self.home_section_order=saved_layout["order"]; self.home_section_visibility=saved_layout["visible"]
        for key in self.home_section_order:hl.addWidget(self.home_sections[key])
        hl.addStretch(); self.home_page=home; self._apply_home_sections(); self.home_page.hide()
        self.collection_page=QWidget(); cpl=QVBoxLayout(self.collection_page); cpl.setContentsMargins(0,8,0,0); back=QPushButton("بازگشت"); back.clicked.connect(self._leave_collection); cpl.addWidget(back,0,Qt.AlignmentFlag.AlignLeft)
        hero=QHBoxLayout(); self.collection_cover=QLabel(); self.collection_cover.setObjectName("collectionCover"); self.collection_cover.setFixedSize(176,176); self.collection_cover.setAlignment(Qt.AlignmentFlag.AlignCenter); hero.addWidget(self.collection_cover)
        collection_info=QVBoxLayout(); self.collection_kicker=QLabel("مجموعهٔ موسیقی"); self.collection_kicker.setObjectName("muted"); collection_info.addWidget(self.collection_kicker); self.collection_heading=QLabel(); self.collection_heading.setObjectName("sectionTitle"); self.collection_heading.setWordWrap(True); collection_info.addWidget(self.collection_heading); self.collection_hint=QLabel(); self.collection_hint.setObjectName("muted"); collection_info.addWidget(self.collection_hint); collection_info.addStretch(); self.collection_play_btn=QPushButton("پخش همهٔ آهنگ‌ها"); self.collection_play_btn.setObjectName("collectionPlayButton"); self.collection_play_btn.setIcon(icon("play")); self.collection_play_btn.clicked.connect(self._play_collection); collection_info.addWidget(self.collection_play_btn,0,Qt.AlignmentFlag.AlignLeft); hero.addLayout(collection_info,1); cpl.addLayout(hero)
        self.collection_related_section=QWidget(); related_layout=QVBoxLayout(self.collection_related_section); related_layout.setContentsMargins(0,0,0,0); related_title=QLabel("آلبوم‌های این هنرمند"); related_title.setObjectName("homeSectionTitle"); related_layout.addWidget(related_title); self.collection_related=QListWidget(); self.collection_related.setObjectName("homeCardRail"); self.collection_related.setViewMode(QListWidget.ViewMode.IconMode); self.collection_related.setFlow(QListWidget.Flow.LeftToRight); self.collection_related.setWrapping(False); self.collection_related.setMovement(QListWidget.Movement.Static); self.collection_related.setIconSize(QSize(112,112)); self.collection_related.setGridSize(QSize(164,194)); self.collection_related.setFixedHeight(210); self.collection_related.setMouseTracking(True); self.collection_related.viewport().setMouseTracking(True); self.collection_related.setItemDelegate(TrackGridDelegate(self.collection_related,self.theme_mode=="light",112,getattr(self,"accent_color","#1ed760"))); self.collection_related.itemDoubleClicked.connect(self._open_related_album); self.collection_related.itemClicked.connect(lambda item:self._grid_item_clicked(self.collection_related,item)); related_layout.addWidget(self.collection_related); cpl.addWidget(self.collection_related_section)
        collection_tools=QHBoxLayout(); collection_tracks_label=QLabel("آهنگ‌ها"); collection_tracks_label.setObjectName("homeSectionTitle"); collection_tools.addWidget(collection_tracks_label); collection_tools.addStretch(); collection_tools.addWidget(QLabel("مرتب‌سازی")); self.collection_sort_combo=QComboBox(); self.collection_sort_combo.addItem("ترتیب آلبوم","album"); self.collection_sort_combo.addItem("عنوان","title"); self.collection_sort_combo.addItem("مدت","duration"); self.collection_sort_combo.currentIndexChanged.connect(self._render_collection_tracks); collection_tools.addWidget(self.collection_sort_combo); self.collection_shuffle_btn=QPushButton("پخش تصادفی"); self.collection_shuffle_btn.setIcon(icon("shuffle")); self.collection_shuffle_btn.clicked.connect(self._play_collection_shuffle); collection_tools.addWidget(self.collection_shuffle_btn); cpl.addLayout(collection_tools)
        self.collection_list=QListWidget(); self.collection_list.setObjectName("collectionTrackGrid"); self.collection_list.setViewMode(QListWidget.ViewMode.IconMode); self.collection_list.setFlow(QListWidget.Flow.LeftToRight); self.collection_list.setWrapping(True); self.collection_list.setResizeMode(QListWidget.ResizeMode.Adjust); self.collection_list.setMovement(QListWidget.Movement.Static); self.collection_list.setIconSize(QSize(112,112)); self.collection_list.setGridSize(QSize(164,194)); self.collection_list.setMouseTracking(True); self.collection_list.viewport().setMouseTracking(True); self.collection_list.setItemDelegate(TrackGridDelegate(self.collection_list,self.theme_mode=="light",112,getattr(self,"accent_color","#1ed760"))); self.collection_list.itemDoubleClicked.connect(self._play_collection_item); self.collection_list.itemClicked.connect(lambda item:self._grid_item_clicked(self.collection_list,item)); self.collection_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu); self.collection_list.customContextMenuRequested.connect(lambda p:self._track_context_menu(self.collection_list,p)); cpl.addWidget(self.collection_list,1); self.collection_page.hide()
        layout.addWidget(self.stack,1); layout.addWidget(self.home_page,1); layout.addWidget(self.collection_page,1); body.addWidget(content,1)
        self.now_playing_panel=QFrame(); self.now_playing_panel.setObjectName("nowPlayingPanel"); self.now_playing_panel.setFixedWidth(330)
        panel_layout=QVBoxLayout(self.now_playing_panel); panel_layout.setContentsMargins(18,18,18,18); panel_layout.setSpacing(12)
        panel_header=QHBoxLayout(); panel_title=QLabel("در حال پخش"); panel_title.setObjectName("sectionTitle"); panel_header.addWidget(panel_title); panel_header.addStretch(); self.fullscreen_btn=QPushButton("⛶"); self.fullscreen_btn.setFixedSize(34,34); self.fullscreen_btn.setToolTip("نمای تمام‌صفحه"); self.fullscreen_btn.clicked.connect(self._show_fullscreen_player); panel_header.addWidget(self.fullscreen_btn)
        self.close_now_panel_btn=QPushButton("×"); self.close_now_panel_btn.setFixedSize(34,34); self.close_now_panel_btn.setToolTip("بستن پنل"); self.close_now_panel_btn.clicked.connect(self._toggle_now_playing_panel); panel_header.addWidget(self.close_now_panel_btn); panel_layout.addLayout(panel_header)
        self.large_cover=QLabel(); self.large_cover.setObjectName("largeCoverArt"); self.large_cover.setFixedSize(280,280); self.large_cover.setAlignment(Qt.AlignmentFlag.AlignCenter); panel_layout.addWidget(self.large_cover,0,Qt.AlignmentFlag.AlignHCenter)
        self.panel_track_title=QLabel("انتخاب آهنگ"); self.panel_track_title.setObjectName("panelTrackTitle"); self.panel_track_title.setWordWrap(True); panel_layout.addWidget(self.panel_track_title)
        self.panel_artist=QLabel("هنوز آهنگی پخش نشده"); self.panel_artist.setObjectName("muted"); self.panel_artist.setWordWrap(True); panel_layout.addWidget(self.panel_artist)
        self.panel_album=QLabel(""); self.panel_album.setObjectName("muted"); self.panel_album.setWordWrap(True); panel_layout.addWidget(self.panel_album)
        self.panel_genre=QLabel(""); self.panel_genre.setObjectName("muted"); self.panel_genre.setWordWrap(True); panel_layout.addWidget(self.panel_genre)
        panel_layout.addStretch(); body.addWidget(self.now_playing_panel); self.now_playing_panel.hide()
        now=QFrame(); now.setObjectName("nowPlaying"); player=QHBoxLayout(now); player.setContentsMargins(22,12,22,12); player.setSpacing(26)
        left=QWidget(); left.setMinimumWidth(205); left.setMaximumWidth(285); left_row=QHBoxLayout(left); left_row.setContentsMargins(0,0,0,0); left_row.setSpacing(12)
        self.cover=QLabel(); self.cover.setObjectName("coverArt"); self.cover.setFixedSize(64,64); self.cover.setPixmap(self._cover_pixmap("",64)); left_row.addWidget(self.cover)
        info=QVBoxLayout(); info.setSpacing(4); self.track_label=QLabel("انتخاب آهنگ"); self.track_label.setObjectName("trackTitle"); self.track_label.setWordWrap(False); self.artist_label=QLabel("هنوز آهنگی پخش نشده"); self.artist_label.setObjectName("muted"); self.artist_label.setWordWrap(False)
        self.status=QLabel("آمادهٔ پخش"); self.status.setObjectName("statusLabel"); info.addWidget(self.track_label); info.addWidget(self.artist_label); info.addWidget(self.status); left_row.addLayout(info,1)
        self.now_panel_btn=QPushButton(); self.now_panel_btn.setObjectName("nowPanelButton"); self.now_panel_btn.setIcon(icon("expand")); self.now_panel_btn.setFixedSize(34,34); self.now_panel_btn.setToolTip("نمایش اطلاعات و کاور بزرگ آهنگ در حال پخش"); self.now_panel_btn.clicked.connect(self._toggle_now_playing_panel); left_row.addWidget(self.now_panel_btn); self.mini_player_btn=QPushButton(); self.mini_player_btn.setIcon(icon("expand")); self.mini_player_btn.setFixedSize(34,34); self.mini_player_btn.setToolTip("بازکردن مینی‌پلیر شناور"); self.mini_player_btn.setAccessibleName("بازکردن مینی‌پلیر شناور"); self.mini_player_btn.clicked.connect(self._toggle_mini_player); left_row.addWidget(self.mini_player_btn); player.addWidget(left,1)
        center=QWidget(); center.setMinimumWidth(350); center_col=QVBoxLayout(center); center_col.setContentsMargins(0,0,0,0); center_col.setSpacing(1)
        controls=QHBoxLayout(); controls.setSpacing(11); controls.addStretch()
        self.shuffle_btn=QPushButton(); self.prev_btn=QPushButton(); self.play_btn=QPushButton(); self.next_btn=QPushButton(); self.repeat_btn=QPushButton()
        for b,n,tip in ((self.shuffle_btn,"shuffle","پخش تصادفی"),(self.prev_btn,"previous","آهنگ قبلی"),(self.play_btn,"play","پخش"),(self.next_btn,"next","آهنگ بعدی"),(self.repeat_btn,"repeat","تکرار")):
            b.setObjectName("playButton" if b is self.play_btn else "transportButton"); b.setIcon(icon(n,"#101010" if b is self.play_btn else "#b3b3b3",24)); b.setIconSize(QSize(23,23)); b.setToolTip(tip); b.setAccessibleName(tip); b.setCursor(Qt.CursorShape.PointingHandCursor); controls.addWidget(b)
        self.play_btn.setFixedSize(42,42); controls.addStretch(); center_col.addLayout(controls)
        self.spectrum=SpectrumWidget(); self.spectrum.setObjectName("spectrum"); self.spectrum.setFixedHeight(24); center_col.addWidget(self.spectrum)
        seek=QHBoxLayout(); seek.setSpacing(8); self.elapsed=QLabel("00:00"); self.elapsed.setObjectName("timeLabel"); self.seek_slider=WaveformSlider(); self.seek_slider.setRange(0,0); self.seek_slider.sliderMoved.connect(self.engine.set_position); self.total=QLabel("00:00"); self.total.setObjectName("timeLabel")
        seek.addWidget(self.elapsed); seek.addWidget(self.seek_slider,1); seek.addWidget(self.total); center_col.addLayout(seek); player.addWidget(center,3)
        right=QWidget(); right.setMinimumWidth(260); right_col=QVBoxLayout(right); right_col.setContentsMargins(0,0,0,0); right_col.setSpacing(8)
        output=QHBoxLayout()
        self.device_icon=QLabel(); self.device_icon.setPixmap(icon("speaker").pixmap(18,18)); output.addWidget(self.device_icon)
        self.output_device=QComboBox(); self.output_device.setMinimumWidth(180); self.output_device.setMaximumWidth(260); self.output_device.setToolTip("دستگاه خروجی صدا")
        for device_id,name in self.engine.output_devices():self.output_device.addItem(name,device_id)
        self.output_device.currentIndexChanged.connect(self._output_device_changed); output.addWidget(self.output_device,1); right_col.addLayout(output)
        settings=QHBoxLayout(); fade_label=QLabel("Crossfade"); fade_label.setObjectName("muted"); settings.addWidget(fade_label)
        self.crossfade_slider=QSlider(Qt.Orientation.Horizontal); self.crossfade_slider.setRange(0,12); self.crossfade_slider.setValue(0); self.crossfade_slider.setToolTip("مدت هم‌پوشانی آهنگ‌ها"); settings.addWidget(self.crossfade_slider,1)
        self.crossfade_value=QLabel("خاموش"); self.crossfade_value.setObjectName("muted"); self.crossfade_value.setMinimumWidth(38); settings.addWidget(self.crossfade_value); right_col.addLayout(settings)
        self.crossfade_slider.valueChanged.connect(self._crossfade_changed)
        volume_row=QHBoxLayout(); self.volume_icon=QLabel(); self.volume_icon.setPixmap(icon("volume").pixmap(18,18)); volume_row.addWidget(self.volume_icon)
        self.volume=QSlider(Qt.Orientation.Horizontal); self.volume.setRange(0,100); self.volume.setValue(75); self.volume.setToolTip("بلندی صدا"); self.volume.valueChanged.connect(self.engine.set_volume); volume_row.addWidget(self.volume,1); right_col.addLayout(volume_row)
        output_index=self.settings_output_device.findData(self.output_device.currentData()); self.settings_output_device.setCurrentIndex(max(0,output_index))
        self.settings_crossfade.setValue(self.crossfade_slider.value()); self.settings_volume.setValue(self.volume.value())
        self.settings_output_device.currentIndexChanged.connect(self._settings_output_changed)
        self.crossfade_slider.valueChanged.connect(self.settings_crossfade.setValue); self.settings_crossfade.valueChanged.connect(self.crossfade_slider.setValue)
        self.volume.valueChanged.connect(self.settings_volume.setValue); self.settings_volume.valueChanged.connect(self.volume.setValue)
        self.volume.setAccessibleName("بلندی صدا"); self.crossfade_slider.setAccessibleName("مدت Crossfade"); self.seek_slider.setAccessibleName("موقعیت پخش در موج آهنگ")
        self.player_frame=now; self.player_left=left; self.player_center=center; self.player_right=right; self.player_layout=player
        player.addWidget(right,2)
        self.shuffle_btn.clicked.connect(self._toggle_shuffle); self.prev_btn.clicked.connect(self._previous); self.play_btn.clicked.connect(self._toggle_playback); self.next_btn.clicked.connect(self._next); self.repeat_btn.clicked.connect(self._cycle_repeat); root.addWidget(now)
        self.nav_all.clicked.connect(lambda:self._show_page(0,"کتابخانه موسیقی")); self.nav_favs.clicked.connect(lambda:self._show_page(1,"علاقه‌مندی‌ها")); self.nav_eq.clicked.connect(lambda:self._show_page(2,"اکولایزر")); self.nav_smart.clicked.connect(lambda:self._show_page(3,"Smart DJ")); self.nav_queue.clicked.connect(lambda:self._show_page(4,"صف پخش و پلی‌لیست‌ها")); self.nav_files.clicked.connect(lambda:self._show_page(5,"بازیابی فایل‌های جابه‌جا‌شده")); self.nav_lyrics.clicked.connect(lambda:self._show_page(7,"متن آهنگ")); self.nav_home=QPushButton("خانه"); self.nav_home.setObjectName("navButton"); self.nav_home.setIcon(icon("home")); self.nav_home.setIconSize(QSize(21,21)); self.nav_home.setMinimumHeight(46); self.nav_home.setCursor(Qt.CursorShape.PointingHandCursor); self.nav_home.clicked.connect(lambda:self._show_page(6,"خانه")); self.sidebar_layout.insertWidget(2,self.nav_home); self.sidebar_nav.insert(0,self.nav_home)
        self.artwork_size_combo.currentIndexChanged.connect(lambda _:self._appearance_setting_changed("artwork_size",self.artwork_size_combo.currentData())); self.density_combo.currentIndexChanged.connect(lambda _:self._appearance_setting_changed("density",self.density_combo.currentData()))
        self.accent_combo.currentIndexChanged.connect(lambda _:self._accent_changed()); self.font_scale_combo.currentIndexChanged.connect(lambda _:self._font_scale_changed())
        focus_row=self.conn.execute("SELECT value FROM smart_dj_settings WHERE key='focus_mode'").fetchone(); self._focus_mode=bool(focus_row and focus_row[0]=="1"); self._focus_panel_visible_before=False
        self._update_responsive_player_layout()
        QTimer.singleShot(0,self._apply_focus_mode)

    def _style(self):
        stylesheet="""
        QWidget{background:#121212;color:#f5f5f5;font-family:'Segoe UI';font-size:10pt}
        #customTitleBar{background:#0b0c0d;border-bottom:1px solid #25272a} #customTitleLabel{background:transparent;color:#f1f3f5;font-size:9pt;font-weight:800;letter-spacing:1px;padding:0 6px}
        QPushButton#windowControl,QPushButton#windowCloseControl{background:transparent;border:0;border-radius:6px;padding:0;color:#d5d7da;font-size:12pt;font-weight:600} QPushButton#windowControl:hover{background:#303237;color:#fff} QPushButton#windowControl:focus,QPushButton#windowCloseControl:focus{border:1px solid #1ed760} QPushButton#windowCloseControl:hover{background:#c42b32;color:#fff}
        #sidebar{background:#000000} #sidebar QLabel{background:transparent} #logo{color:#f5f5f5;font-size:13pt;font-weight:800;letter-spacing:1px}
        #sidebarLabel{color:#777;font-size:9pt;font-weight:700;padding:5px 9px} #sidebarHint{color:#929292;font-size:9pt;padding:0 9px}
        #navButton{text-align:left;padding:10px 12px;transition:background-color 160ms,color 160ms;border:0;border-radius:8px;background:transparent;color:#b3b3b3;font-weight:700}
        #navButton:hover{background:#171717;color:#fff} #navButton[active="true"]{background:#242424;color:#fff;border-right:3px solid #1ed760;padding-right:9px}
        #pageTitle{font-size:23pt;font-weight:800;color:#fff} #searchBox{background:#242424;border:1px solid transparent;border-radius:22px;padding:10px 14px;color:#fff}
        #searchBox:focus{border:1px solid #777} QPushButton{background:#242424;border:0;border-radius:18px;padding:9px 15px;color:#fff;font-weight:700}
        QPushButton:hover{background:#333} QPushButton:pressed{background:#444} QPushButton:disabled{color:#656565;background:#1b1b1b}
        QPushButton:focus,QToolButton:focus,QComboBox:focus,QLineEdit:focus,QCheckBox:focus,QSlider:focus{outline:0;border:2px solid #1ed760}
        #add_files_btn,#add_folder_btn{background:#fff;color:#111;border-radius:20px;padding:10px 15px} #add_files_btn:hover,#add_folder_btn:hover{background:#e6e6e6}
        #nowPlaying{background:#181818;border-top:1px solid #292929} #nowPlaying QWidget{background:transparent} #nowPlaying QLabel{background:transparent}
        #coverArt{border-radius:7px;background:#202020} #nowPlaying QComboBox{background:#242424;border:1px solid #343434;border-radius:8px;padding:7px 10px;color:#f5f5f5}
        #trackTitle{font-size:10.5pt;font-weight:700;color:#fff} #muted{color:#a7a7a7;font-size:9pt} #statusLabel{color:#1ed760;font-size:8pt}
        #timeLabel{color:#a7a7a7;font-size:8pt} #playButton{background:#1ed760;border-radius:21px;padding:0} #playButton:hover{background:#45e982}
        #nowPlaying #playButton{background:#1ed760;border-radius:21px;padding:0} #nowPlaying #playButton:hover{background:#45e982}
        #transportButton{background:transparent;border-radius:20px;padding:8px} #transportButton:hover{background:#292929}
        #nowPlayingPanel{background:#181818;border-left:1px solid #292929} #largeCoverArt{background:#202020;border-radius:12px} #panelTrackTitle{font-size:17pt;font-weight:800;color:#fff}
        #collectionCover{background:#202020;border-radius:12px} #collectionPlayButton{background:#1ed760;color:#101010}
        #nowPanelButton[expanded="true"]{background:#292929}
        #homeWelcome{font-size:23pt;font-weight:800;color:#fff} #homeSectionTitle{font-size:15pt;font-weight:800;color:#f4f4f4;padding:2px 2px}
        QListWidget#homeCardRail{background:transparent;border:0;padding:3px} QListWidget#homeCardRail::item{padding:0;border:0}
        #homeResumeCover{background:#202020;border-radius:8px} QProgressBar{background:#343434;border:0;border-radius:3px} QProgressBar::chunk{background:#1ed760;border-radius:3px}
        #homeHero{background:#202020;border:1px solid #303030;border-radius:18px} #homeHeroTitle{font-size:22pt;font-weight:800}
        #spectrum{background:transparent} #sectionTitle{font-size:16pt;font-weight:800} #smartCard{background:#181818;border:1px solid #282828;border-radius:13px;padding:8px}
        QLineEdit{background:#242424;border:1px solid transparent;border-radius:22px;padding:10px 14px;color:#fff;selection-background-color:#1ed760}
        QComboBox{background:#242424;border:1px solid #343434;border-radius:8px;padding:7px 10px;color:#f5f5f5;selection-background-color:#333}
        QComboBox:hover{border-color:#666} QComboBox::drop-down{border:0;width:24px} QComboBox QAbstractItemView{background:#282828;color:#fff;border:1px solid #404040;selection-background-color:#1ed760;selection-color:#101010}
        QTabWidget::pane{border:1px solid #292929;border-radius:9px;background:#151515} QTabBar::tab{background:transparent;color:#a7a7a7;padding:11px 16px;border-bottom:2px solid transparent}
        QTabBar::tab:selected{color:#fff;border-bottom:2px solid #1ed760} QCheckBox{spacing:8px;color:#ddd} QCheckBox::indicator{width:17px;height:17px;border-radius:4px;border:1px solid #666;background:#242424}
        QCheckBox::indicator:checked{background:#1ed760;border-color:#1ed760}
        QListWidget{background:#121212;border:0;outline:0;padding:4px}
        QListWidget::item{padding:10px 12px;border-radius:7px;color:#e2e2e2;min-height:42px} QListWidget::item:hover{background:#1f1f1f}
        QListWidget::item:selected{background:#292929;color:#1ed760;border-left:3px solid #1ed760}
        QListWidget::item:selected:active{background:#303b33;color:#1ed760} QListWidget::item:selected:!active{background:#252a26;color:#d5e7da}
        QListWidget::item[playing="true"]{color:#1ed760;font-weight:700}
        QScrollBar:vertical{background:transparent;width:9px;margin:2px} QScrollBar::handle:vertical{background:#454545;border-radius:4px;min-height:28px}
        QScrollBar::handle:vertical:hover{background:#666} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0}
        QSlider::groove:horizontal{height:4px;background:#535353;border-radius:2px} QSlider::sub-page:horizontal{background:#1ed760;border-radius:2px}
        QSlider::handle:horizontal{background:#fff;width:11px;height:11px;margin:-4px 0;border-radius:6px}
        QSlider::groove:vertical{width:5px;background:#343b46;border-radius:2px} QSlider::handle:vertical{background:#1ed760;height:12px;margin:0 -5px;border-radius:6px}
        """
        if self.theme_mode=="light":
            palette={"#121212":"#f5f6f8","#f5f5f5":"#17191d","#000000":"#e8ebf0","#777":"#68717d","#929292":"#59616d","#b3b3b3":"#59616d","#171717":"#e5e8ee","#fff":"#17191d","#242424":"#ffffff","#333":"#e9ecf1","#444":"#dce0e6","#656565":"#747c88","#1b1b1b":"#e5e8ed","#e6e6e6":"#dfe3e9","#181818":"#ffffff","#292929":"#d9dde5","#202020":"#e8ebf0","#343434":"#d4d9e1","#a7a7a7":"#59616d","#101010":"#ffffff","#282828":"#eef0f4","#404040":"#cfd5de","#ddd":"#343a44","#666":"#626b78","#454545":"#aeb5c0","#535353":"#c8ced8","#343b46":"#c8ced8","#1ed760":"#087b39","#45e982":"#0a6d35"}
            for dark,light in palette.items():stylesheet=stylesheet.replace(dark,light)
            stylesheet+="\n#add_files_btn,#add_folder_btn{background:#087b39;color:#ffffff} #add_files_btn:hover,#add_folder_btn:hover{background:#0a6d35;color:#ffffff} #navButton[active=\"true\"]{background:#dcefe3;color:#17191d}"
        scale=self.font_scale if hasattr(self,"font_scale") else 1.0
        stylesheet=stylesheet.replace("font-size:10pt",f"font-size:{10*scale:g}pt")
        active=getattr(self,"accent_color","#1ed760"); hover=QColor(active).lighter(125).name()
        if self.theme_mode=="light":stylesheet=stylesheet.replace("#087b39",active).replace("#0a6d35",hover)
        else:stylesheet=stylesheet.replace("#1ed760",active).replace("#45e982",hover)
        stylesheet+="\nQPushButton:focus,QComboBox:focus,QLineEdit:focus,QListWidget:focus{border:2px solid "+active+"} #emptyState{color:#a7a7a7;font-size:12pt;font-weight:600;padding:36px} #fullscreenMetaLink:focus{border:1px solid "+active+"} #homeWelcome{color:"+active+"} QProgressBar::chunk{background:"+active+"} QPushButton#focusModeButton{border:1px solid #383838;border-radius:17px;padding:8px 12px} QPushButton#focusModeButton:hover{border-color:"+active+"} QListWidget#collectionTrackGrid{background:transparent;border:0} QListWidget#collectionTrackGrid::item{padding:0;border:0}"
        self.setStyleSheet(stylesheet)
        if hasattr(self,"lyrics_page") and self.lyrics_page.isVisible():
            self._load_current_lyrics(); self._sync_lyrics(self.engine.position_ms)
        self.spectrum.set_theme(self.theme_mode)
        self.seek_slider.set_accent(active,self.theme_mode=="light")
        if hasattr(self,"view_mode_combo"):
            self._set_track_view_mode(self.view_mode_combo.currentData())
        for listing in (getattr(self,"smart_list",None),getattr(self,"missing_list",None)):
            if listing:listing.setItemDelegate(TrackItemDelegate(listing,self.theme_mode=="light",active))
        if hasattr(self,"queue_list"):
            self.queue_list.setItemDelegate(TrackGridDelegate(self.queue_list,self.theme_mode=="light",132,active)); self.queue_list.setGridSize(QSize(190,222))
        for listing in (getattr(self,"home_recent_list",None),getattr(self,"home_popular_list",None),getattr(self,"home_smart_cards",None),getattr(self,"collection_related",None),getattr(self,"collection_list",None)):
            if listing:listing.setItemDelegate(TrackGridDelegate(listing,self.theme_mode=="light",112,getattr(self,"accent_color",active)))
        self._refresh_fullscreen_player()
        self.engine.set_volume(self.volume.value())
        if hasattr(self,"play_btn"):
            accent="#ffffff" if self.theme_mode=="light" else "#101010"
            self.play_btn.setIcon(icon("play",accent,24))
            muted="#59616d" if self.theme_mode=="light" else "#b3b3b3"
            add_color="#ffffff" if self.theme_mode=="light" else "#111111"
            self.add_files_btn.setIcon(icon("plus",add_color,20)); self.add_folder_btn.setIcon(icon("folder-plus",add_color,20))
            for button,name in ((self.shuffle_btn,"shuffle"),(self.prev_btn,"previous"),(self.next_btn,"next"),(self.repeat_btn,"repeat")):
                button.setIcon(icon(name,muted,24))
            self.now_panel_btn.setIcon(icon("expand",muted,20))
            self.device_icon.setPixmap(icon("speaker",muted,18).pixmap(18,18)); self.volume_icon.setPixmap(icon("volume",muted,18).pixmap(18,18))
        self._set_active_nav(getattr(self,"_active_page_index",self.stack.currentIndex()))

    def _set_active_nav(self,index):
        active_color="#17191d" if self.theme_mode=="light" else "#ffffff"
        inactive_color="#59616d" if self.theme_mode=="light" else "#b3b3b3"
        buttons=(self.nav_all,self.nav_favs,self.nav_eq,self.nav_smart,self.nav_queue,self.nav_files,self.nav_lyrics,self.appearance_btn,self.nav_home)
        page_indexes=(0,1,2,3,4,5,7,8,6); names=("library","heart","sliders","sparkles","queue","folder-search","lyrics","sliders","home")
        for i,button in enumerate(buttons):
            page_index=page_indexes[i]
            button.setIcon(icon(names[i],active_color if page_index==index else inactive_color,21))
            button.setProperty("active",page_index==index); button.style().unpolish(button); button.style().polish(button)

    def _shortcuts(self):
        for key,cb in [("Space",self._toggle_playback),("Ctrl+O",self._choose_files),("Ctrl+Shift+O",self._choose_folder),("Ctrl+L",lambda:self._show_page(7,"متن آهنگ")),("Ctrl+M",self._toggle_mini_player),("Right",self._next),("Left",self._previous)]:
            a=QAction(self); a.setShortcut(QKeySequence(key)); a.triggered.connect(cb); self.addAction(a)
    def _appearance_setting_changed(self,key,value):
        self.conn.execute("INSERT INTO smart_dj_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value)); self.conn.commit()
        self._set_track_view_mode(self.view_mode_combo.currentData())
        compact=value=="compact" if key=="density" else self.density_combo.currentData()=="compact"
        self.track_list.setSpacing(2 if compact else 6); self.fav_list.setSpacing(2 if compact else 6)
        self.status.setText("تنظیم ظاهر ذخیره شد")
    def _toggle_sidebar(self):
        self._sidebar_compact=not self._sidebar_compact
        self.sidebar.setFixedWidth(76 if self._sidebar_compact else 230)
        for widget in self.sidebar_extras+[self.logo]:widget.setVisible(not self._sidebar_compact)
        for button in self.sidebar_nav:
            button.setText("") if self._sidebar_compact else None
            button.setToolTip(("خانه","کتابخانه","علاقه‌مندی‌ها","اکولایزر","Smart DJ","صف و پلی‌لیست‌ها","بازیابی فایل‌ها","متن آهنگ","تنظیمات")[self.sidebar_nav.index(button)])
        if not self._sidebar_compact:
            for button,label in zip(self.sidebar_nav,("خانه","کتابخانه","علاقه‌مندی‌ها","اکولایزر","Smart DJ","صف و پلی‌لیست‌ها","بازیابی فایل‌ها","متن آهنگ","تنظیمات")):button.setText(label)
        self.appearance_btn.setText("" if self._sidebar_compact else "تنظیمات")
        self.sidebar_toggle.setText("" if self._sidebar_compact else "جمع‌کردن نوار کناری"); self.sidebar_toggle.setToolTip("بازکردن نوار کناری" if self._sidebar_compact else "جمع‌کردن نوار کناری"); self.sidebar_toggle.setIcon(icon("chevron-right" if self._sidebar_compact else "chevron-left"))
        self.conn.execute("INSERT INTO smart_dj_settings(key,value) VALUES('sidebar_compact',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",("1" if self._sidebar_compact else "0",)); self.conn.commit()
    def _toggle_focus_mode(self):
        self._focus_mode=not self._focus_mode
        self.conn.execute("INSERT INTO smart_dj_settings(key,value) VALUES('focus_mode',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",("1" if self._focus_mode else "0",)); self.conn.commit()
        self._apply_focus_mode()
    def _apply_focus_mode(self):
        if not hasattr(self,"focus_btn"):return
        if self._focus_mode:
            self._focus_panel_visible_before=not self.now_playing_panel.isHidden()
            self.sidebar.hide(); self.now_playing_panel.hide(); self.now_panel_btn.hide()
            self.search.hide(); self.add_files_btn.hide(); self.add_folder_btn.hide()
            self.home_library_btn.hide(); self.home_smart_btn.hide(); self.home_layout_btn.hide()
            self._set_library_controls_visible(False)
            self.focus_btn.setText("خروج از حالت خلوت"); self.focus_btn.setIcon(icon("x",self.accent_color,18)); self.focus_btn.setToolTip("بازگرداندن نوارها و کنترل‌های برنامه")
        else:
            self.sidebar.show(); self.now_panel_btn.show(); self.now_playing_panel.setVisible(self._focus_panel_visible_before)
            self.search.setVisible(self._active_page_index in (0,1)); self.add_files_btn.setVisible(self._active_page_index in (0,1)); self.add_folder_btn.setVisible(self._active_page_index in (0,1))
            self.home_library_btn.show(); self.home_smart_btn.show(); self.home_layout_btn.show()
            self._set_library_controls_visible(self._active_page_index in (0,1))
            self.focus_btn.setText("حالت خلوت"); self.focus_btn.setIcon(icon("expand",self.accent_color,18)); self.focus_btn.setToolTip("پنهان‌کردن نوارها برای تمرکز روی موسیقی")
        self._update_responsive_player_layout()
    def _create_home_card_section(self,title):
        section=QWidget(); layout=QVBoxLayout(section); layout.setContentsMargins(0,0,0,0); heading=QLabel(title); heading.setObjectName("homeSectionTitle"); layout.addWidget(heading)
        listing=QListWidget(); listing.setObjectName("homeCardRail"); listing.setViewMode(QListWidget.ViewMode.IconMode); listing.setFlow(QListWidget.Flow.LeftToRight); listing.setWrapping(False); listing.setMovement(QListWidget.Movement.Static); listing.setResizeMode(QListWidget.ResizeMode.Adjust); listing.setIconSize(QSize(112,112)); listing.setGridSize(QSize(164,194)); listing.setFixedHeight(220); listing.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded); listing.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); listing.setMouseTracking(True); listing.viewport().setMouseTracking(True); listing.setItemDelegate(TrackGridDelegate(listing,self.theme_mode=="light",112,getattr(self,"accent_color","#1ed760"))); layout.addWidget(listing)
        return section,listing
    def _read_home_layout(self):
        defaults=["resume","recent","popular","smart"]; visible={key:True for key in defaults}
        try:
            row=self.conn.execute("SELECT value FROM smart_dj_settings WHERE key='home_dashboard_layout'").fetchone()
            saved=json.loads(row[0]) if row else {}
            order=[key for key in saved.get("order",[]) if key in defaults]
            order.extend(key for key in defaults if key not in order)
            for key in defaults:visible[key]=bool(saved.get("visible",{}).get(key,True))
            return {"order":order,"visible":visible}
        except (AttributeError,TypeError,ValueError,sqlite3.Error):return {"order":defaults,"visible":visible}
    def _save_home_layout(self):
        payload=json.dumps({"order":self.home_section_order,"visible":self.home_section_visibility},ensure_ascii=False)
        self.conn.execute("INSERT INTO smart_dj_settings(key,value) VALUES('home_dashboard_layout',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(payload,)); self.conn.commit()
    def _apply_home_sections(self):
        if not hasattr(self,"home_dashboard"):return
        layout=self.home_dashboard.layout()
        for section in self.home_sections.values():layout.removeWidget(section)
        for index,key in enumerate(self.home_section_order):
            section=self.home_sections[key]; section.setVisible(self.home_section_visibility.get(key,True)); layout.insertWidget(index+4,section)
    def _configure_home_sections(self):
        dialog=QDialog(self); dialog.setWindowTitle("چیدمان صفحهٔ خانه"); dialog.setMinimumWidth(390); dialog.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        layout=QVBoxLayout(dialog); intro=QLabel("بخش‌ها را بکش تا جابه‌جا شوند؛ تیک هر بخش نمایش آن را کنترل می‌کند."); intro.setWordWrap(True); layout.addWidget(intro)
        listing=QListWidget(); listing.setDragDropMode(QListWidget.DragDropMode.InternalMove); listing.setDefaultDropAction(Qt.DropAction.MoveAction); listing.setDragEnabled(True); listing.setAcceptDrops(True); listing.setDropIndicatorShown(True)
        for key in self.home_section_order:
            item=QListWidgetItem(self.home_section_labels[key]); item.setData(Qt.ItemDataRole.UserRole,key); item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable|Qt.ItemFlag.ItemIsDragEnabled|Qt.ItemFlag.ItemIsDropEnabled); item.setCheckState(Qt.CheckState.Checked if self.home_section_visibility.get(key,True) else Qt.CheckState.Unchecked); listing.addItem(item)
        layout.addWidget(listing); buttons=QHBoxLayout(); apply_btn=QPushButton("اعمال چیدمان"); cancel_btn=QPushButton("انصراف"); buttons.addStretch(); buttons.addWidget(cancel_btn); buttons.addWidget(apply_btn); layout.addLayout(buttons); cancel_btn.clicked.connect(dialog.reject); apply_btn.clicked.connect(dialog.accept)
        if dialog.exec()!=QDialog.DialogCode.Accepted:return
        self.home_section_order=[listing.item(i).data(Qt.ItemDataRole.UserRole) for i in range(listing.count())]
        self.home_section_visibility={listing.item(i).data(Qt.ItemDataRole.UserRole):listing.item(i).checkState()==Qt.CheckState.Checked for i in range(listing.count())}
        self._save_home_layout(); self._apply_home_sections(); self._refresh_home()
    def _set_home_card_items(self,listing,rows):
        listing.clear()
        for row in rows:
            path,title,artist=row[:3]
            if not os.path.isfile(path):continue
            item=QListWidgetItem(self._cover_icon(path,112),f"{title or Path(path).stem}\n{artist or 'هنرمند نامشخص'}"); item.setData(Qt.ItemDataRole.UserRole,path); item.setData(Qt.ItemDataRole.UserRole+1,artist or "")
            favorite=self.conn.execute("SELECT favorite FROM tracks WHERE path=?",(path,)).fetchone(); item.setData(Qt.ItemDataRole.UserRole+4,bool(favorite and favorite[0])); listing.addItem(item)
    def _home_featured_path(self):
        if self.current_path and os.path.isfile(self.current_path):return self.current_path
        for listing in (self.home_recent_list,self.home_popular_list):
            path=next((listing.item(i).data(Qt.ItemDataRole.UserRole) for i in range(listing.count()) if listing.item(i).data(Qt.ItemDataRole.UserRole)),"")
            if path:return path
        return ""
    def _refresh_home_resume(self):
        path=self.current_path if self.current_path and os.path.isfile(self.current_path) else ""
        enabled=bool(path)
        self.home_resume_btn.setEnabled(enabled)
        if not enabled:
            self.home_resume_cover.setPixmap(self._cover_pixmap("",58)); self.home_resume_title.setText("هنوز آهنگی انتخاب نشده"); self.home_resume_subtitle.setText("از کتابخانه یک آهنگ پخش کن تا اینجا نمایش داده شود."); self.home_resume_progress.setValue(0); return
        row=self.conn.execute("SELECT title,artist,album FROM tracks WHERE path=?",(path,)).fetchone(); title,artist,album=row if row else (Path(path).stem,None,None); self.home_resume_cover.setPixmap(self._cover_pixmap(path,58)); self.home_resume_title.setText(title or Path(path).stem); self.home_resume_subtitle.setText(f"{artist or 'هنرمند نامشخص'}  ·  {album or 'تک‌آهنگ'}")
        total_ms=max(1,self.engine.duration_ms); fraction=max(0,min(1,self.engine.position_ms/total_ms))
        self.home_resume_progress.setValue(round(1000*fraction))
    def _play_home_feature(self):
        path=self.smart_queue[0] if self.smart_queue else self._home_featured_path()
        if path and os.path.isfile(path):
            if self.smart_queue:
                self.queue_paths=list(self.smart_queue); self._queue_mode="smart"; self._active_playlist_id=None; self._refresh_playlist_picker(); self._refresh_queue_view()
            else:
                self.queue_paths=[item.data(Qt.ItemDataRole.UserRole) for item in (self.track_list.item(i) for i in range(self.track_list.count())) if item.data(Qt.ItemDataRole.UserRole)]
                if path in self.queue_paths:self.queue_paths.remove(path)
                self.queue_paths.insert(0,path); self._queue_mode="library"; self._active_playlist_id=None; self._refresh_playlist_picker(); self._refresh_queue_view()
            self._play_path(path,manual=True)
    def _play_home_smart_item(self,item):
        path=item.data(Qt.ItemDataRole.UserRole)
        if not path or not os.path.isfile(path):return
        self.queue_paths=[path]+[candidate for candidate in self.smart_queue if candidate!=path and os.path.isfile(candidate)]; self._queue_mode="smart"; self._active_playlist_id=None
        self._refresh_playlist_picker(); self._refresh_queue_view(); self._play_path(path,manual=True)
    def _continue_home_track(self):
        if self.current_path and os.path.isfile(self.current_path):self._toggle_playback()
        else:self._play_home_feature()
    def _refresh_home(self):
        if not hasattr(self,"home_recent_list"):return
        total=self.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]; favorites=self.conn.execute("SELECT COUNT(*) FROM tracks WHERE favorite=1").fetchone()[0]
        self.home_summary.setText(f"{total} آهنگ در کتابخانه  ·  {favorites} علاقه‌مندی  ·  موسیقی‌ات، برای تو")
        recent_query="SELECT t.path,t.title,t.artist FROM listening_events e JOIN tracks t ON t.path=e.track_path GROUP BY t.path ORDER BY MAX(e.created_at) DESC LIMIT 8"
        popular_query="SELECT t.path,t.title,t.artist FROM listening_events e JOIN tracks t ON t.path=e.track_path GROUP BY t.path ORDER BY COUNT(*) DESC, MAX(e.created_at) DESC LIMIT 8"
        recent=self.conn.execute(recent_query).fetchall(); popular=self.conn.execute(popular_query).fetchall()
        self._set_home_card_items(self.home_recent_list,recent); self._set_home_card_items(self.home_popular_list,popular)
        self.home_recent_section.setVisible(self.home_section_visibility.get("recent",True) and bool(self.home_recent_list.count()))
        self.home_popular_section.setVisible(self.home_section_visibility.get("popular",True) and bool(self.home_popular_list.count()))
        featured=self.smart_queue[0] if self.smart_queue else (self._home_featured_path() or (popular[0][0] if popular else "")); self._home_featured_path_value=featured; animate_feature=bool(featured and featured!=getattr(self,"_home_last_featured_path",None)); self._home_last_featured_path=featured; self.home_hero_play.setEnabled(bool(featured))
        if featured:
            title,artist,album,_,_,_,_=self._metadata(featured); self.home_hero_cover.setPixmap(self._cover_pixmap(featured,148)); self.home_hero_title.setText(title or Path(featured).stem); self.home_hero_subtitle.setText(f"{artist or 'هنرمند نامشخص'}  ·  {album or 'آمادهٔ پخش'}"); self.home_hero_kicker.setText("انتخاب ویژهٔ Smart DJ" if self.smart_queue else "برای شنیدن دوباره")
            color=self._sample_cover_accent(featured); deep=QColor(color).darker(310).name(); mid=QColor(color).darker(205).name(); self.home_hero.setStyleSheet(f"QFrame#homeHero{{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {deep},stop:0.58 {mid},stop:1 #17191d);border:1px solid {QColor(color).darker(170).name()};border-radius:18px}} QLabel#homeHeroTitle{{font-size:22pt;font-weight:800;color:#fff;background:transparent}} QLabel#homeHeroKicker,QLabel#homeHeroSubtitle{{color:#e7e9ed;background:transparent}} QLabel#homeHeroCover{{border-radius:12px;background:#202020}} QPushButton#homeHeroPlay{{background:{color};color:#101010;border-radius:20px;padding:10px 18px;font-weight:800}}")
            if animate_feature:self._animate_widget(self.home_hero)
        else:
            self.home_hero_cover.setPixmap(self._cover_pixmap("",148)); self.home_hero_title.setText("کتابخانهٔ موسیقی تو"); self.home_hero_subtitle.setText("برای شروع، آهنگ یا پوشهٔ موسیقی اضافه کن."); self.home_hero_kicker.setText("خوش آمدی به NOVA Music"); self.home_hero.setStyleSheet("")
        self._refresh_home_resume()
        self.home_smart_cards.clear()
        for i in range(self.smart_list.count()):
            source=self.smart_list.item(i); path=source.data(Qt.ItemDataRole.UserRole); row=source.data(Qt.ItemDataRole.UserRole+1) or {}
            if not path or not os.path.isfile(path):continue
            item=QListWidgetItem(self._cover_icon(path,112),f"{row.get('title') or Path(path).stem}\n{row.get('artist') or 'هنرمند نامشخص'}"); item.setData(Qt.ItemDataRole.UserRole,path); item.setData(Qt.ItemDataRole.UserRole+1,row.get("artist") or ""); favorite=self.conn.execute("SELECT favorite FROM tracks WHERE path=?",(path,)).fetchone(); item.setData(Qt.ItemDataRole.UserRole+4,bool(favorite and favorite[0])); self.home_smart_cards.addItem(item)
        if self.home_smart_cards.count():self.home_smart_status.setText("پیشنهادها بر پایهٔ شنیدن‌های همین دستگاه چیده شده‌اند.")
        else:self.home_smart_status.setText("برای پیشنهاد شخصی، چند آهنگ گوش بده یا پیشنهادها را تازه‌سازی کن.")
        self.home_smart_cards.setVisible(bool(self.home_smart_cards.count()))
        self.home_smart_status.setVisible(not bool(self.home_smart_cards.count()))
        self.home_smart_section.setVisible(self.home_section_visibility.get("smart",True))
        if not self.smart_queue and self.history_enabled and not getattr(self,"_home_smart_requested",False) and not (self.smart_worker and self.smart_worker.isRunning()) and self.conn.execute("SELECT 1 FROM listening_events LIMIT 1").fetchone():
            self._home_smart_requested=True; QTimer.singleShot(250,self._request_home_smart_playlist)
    def _request_home_smart_playlist(self):
        if self.smart_worker and self.smart_worker.isRunning():return
        if not self.database_available:
            self.home_smart_status.setText("برای پیشنهاد شخصی، دسترسی به پایگاه داده لازم است."); return
        self._home_smart_only=True; self._build_smart_playlist()
    def _sample_cover_accent(self,path):
        pix=self._cover_pixmap(path,48)
        if pix.isNull():return "#1ed760"
        image=pix.toImage(); sums=[0,0,0]; count=0
        for y in range(0,image.height(),3):
            for x in range(0,image.width(),3):
                sample=image.pixelColor(x,y); _,sat,val,_=sample.getHsvF()
                if sat>.28 and .15<val<.92:sums[0]+=sample.red(); sums[1]+=sample.green(); sums[2]+=sample.blue(); count+=1
        return QColor(*(round(part/count) for part in sums)).name() if count else "#1ed760"
    def _show_fullscreen_player(self):
        if getattr(self,"_fullscreen_dialog",None) and self._fullscreen_dialog.isVisible():self._fullscreen_dialog.raise_(); return
        dialog=QDialog(self); dialog.setWindowTitle("NOVA Music — در حال پخش"); dialog.setModal(False); dialog.setMinimumSize(760,650)
        root=QVBoxLayout(dialog); root.setContentsMargins(48,28,48,34); root.setSpacing(14)
        top=QHBoxLayout(); close=QPushButton("بازگشت به برنامه  ·  Esc"); close.setObjectName("fullscreenBack"); close.setAccessibleName("بستن نمای تمام‌صفحه"); close.clicked.connect(dialog.close); top.addWidget(close); top.addStretch(); badge=QLabel("در حال پخش"); badge.setObjectName("fullscreenBadge"); top.addWidget(badge); root.addLayout(top)
        cover=QLabel(); cover.setAccessibleName("کاور آهنگ در حال پخش"); cover.setAlignment(Qt.AlignmentFlag.AlignCenter); cover.setMinimumSize(300,300); root.addWidget(cover,1)
        title=QLabel(); title.setObjectName("pageTitle"); title.setAlignment(Qt.AlignmentFlag.AlignCenter); root.addWidget(title)
        artist=QPushButton(); artist.setFlat(True); artist.setObjectName("fullscreenMetaLink"); artist.clicked.connect(lambda:self._open_collection("artist",self._metadata(self.current_path)[1] if self.current_path else "")); root.addWidget(artist,0,Qt.AlignmentFlag.AlignHCenter)
        album=QPushButton(); album.setFlat(True); album.setObjectName("fullscreenMetaLink"); album.clicked.connect(lambda:self._open_collection("album",self._metadata(self.current_path)[2] if self.current_path else "")); root.addWidget(album,0,Qt.AlignmentFlag.AlignHCenter)
        seekrow=QHBoxLayout(); elapsed=QLabel("00:00"); total=QLabel("00:00"); seek=QSlider(Qt.Orientation.Horizontal); seek.setRange(0,0); seek.sliderMoved.connect(self.engine.set_position); seekrow.addWidget(elapsed); seekrow.addWidget(seek,1); seekrow.addWidget(total); root.addLayout(seekrow)
        controls=QHBoxLayout(); controls.addStretch(); previous=QPushButton(); previous.setObjectName("fullscreenTransport"); previous.setAccessibleName("آهنگ قبلی"); previous.setIcon(icon("previous")); previous.clicked.connect(self._previous); controls.addWidget(previous); play=QPushButton(); play.setObjectName("fullscreenPlay"); play.setAccessibleName("پخش یا مکث"); play.setFixedSize(58,58); play.setIcon(icon("pause" if self.is_playing else "play")); play.clicked.connect(self._toggle_playback); controls.addWidget(play); nxt=QPushButton(); nxt.setObjectName("fullscreenTransport"); nxt.setAccessibleName("آهنگ بعدی"); nxt.setIcon(icon("next")); nxt.clicked.connect(self._next); controls.addWidget(nxt); controls.addStretch(); root.addLayout(controls)
        escape=QAction(dialog); escape.setShortcut(QKeySequence("Escape")); escape.triggered.connect(dialog.close); dialog.addAction(escape)
        dialog._cover=cover; dialog._title=title; dialog._artist=artist; dialog._album=album; dialog._seek=seek; dialog._elapsed=elapsed; dialog._total=total; dialog._play=play; dialog._previous=previous; dialog._next=nxt; self._fullscreen_dialog=dialog; self._refresh_fullscreen_player(); dialog.showFullScreen()
    def _sync_fullscreen_transport(self):
        dialog=getattr(self,"_fullscreen_dialog",None)
        if not dialog:return
        if not dialog._seek.isSliderDown():dialog._seek.setValue(min(int(self.engine.position_ms),dialog._seek.maximum()))
        dialog._elapsed.setText(self._fmt(self.engine.position_ms)); dialog._total.setText(self._fmt(self.duration)); accent=getattr(self,"accent_color","#1ed760"); ink=QColor(accent).darker(260).name(); muted="#69717c" if self.theme_mode=="light" else "#d5d8dd"
        dialog._play.setIcon(icon("pause" if self.is_playing else "play",ink,25)); dialog._previous.setIcon(icon("previous",muted,25)); dialog._next.setIcon(icon("next",muted,25))
    def _refresh_fullscreen_player(self):
        dialog=getattr(self,"_fullscreen_dialog",None)
        if not dialog:return
        dialog._cover.setPixmap(self._cover_pixmap(self.current_path,min(560,max(300,dialog.height()-330))))
        title,artist,album,_,_,_,_=self._metadata(self.current_path) if self.current_path else ("انتخاب آهنگ","هنوز آهنگی پخش نشده","","",None,None,None)
        dialog._title.setText(title); dialog._artist.setText(artist or "هنرمند نامشخص"); dialog._album.setText(album or "بدون آلبوم")
        dialog._seek.setRange(0,max(0,int(self.duration))); self._sync_fullscreen_transport()
        accent=getattr(self,"accent_color","#1ed760"); light=self.theme_mode=="light"
        if light:
            glow=QColor(accent).lighter(185).name(); dialog.setStyleSheet(f"QDialog{{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {glow},stop:0.44 #f5f6f8,stop:1 #e9edf2);color:#17191d}} QLabel#pageTitle{{font-size:25pt;font-weight:800;color:#17191d}} QLabel#fullscreenBadge{{color:#424a55;background:rgba(255,255,255,150);padding:8px 14px;border-radius:14px}} QPushButton#fullscreenBack{{background:rgba(255,255,255,180);color:#17191d;border:1px solid #d7dce3}} QPushButton#fullscreenMetaLink{{background:transparent;color:#505965;padding:3px}} QPushButton#fullscreenMetaLink:hover{{color:#111;text-decoration:underline}} QPushButton#fullscreenTransport{{background:transparent;color:#27303b}} QPushButton#fullscreenTransport:hover{{background:rgba(255,255,255,150)}} QPushButton#fullscreenPlay{{background:{accent};color:#111;border-radius:29px;padding:0}} QSlider::groove:horizontal{{height:5px;background:#c5cbd3;border-radius:2px}} QSlider::sub-page:horizontal{{background:{accent};border-radius:2px}} QSlider::handle:horizontal{{background:#fff;border:2px solid {accent};width:13px;height:13px;margin:-5px 0;border-radius:7px}}")
        else:
            dark=QColor(accent).darker(320).name(); dialog.setStyleSheet(f"QDialog{{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {dark},stop:0.45 #171717,stop:1 #101010);color:#fff}} QLabel#pageTitle{{font-size:25pt;font-weight:800;color:#fff}} QLabel#fullscreenBadge{{color:#eee;background:rgba(255,255,255,25);padding:8px 14px;border-radius:14px}} QPushButton#fullscreenBack{{background:rgba(30,30,30,190);color:#fff;border:1px solid #3a3a3a}} QPushButton#fullscreenBack:hover{{background:#333}} QPushButton#fullscreenMetaLink{{background:transparent;color:#ddd;padding:3px}} QPushButton#fullscreenMetaLink:hover{{color:#fff;text-decoration:underline}} QPushButton#fullscreenTransport{{background:transparent;color:#eee}} QPushButton#fullscreenTransport:hover{{background:rgba(255,255,255,20)}} QPushButton#fullscreenPlay{{background:{accent};color:#101010;border-radius:29px;padding:0}} QSlider::groove:horizontal{{height:5px;background:#555;border-radius:2px}} QSlider::sub-page:horizontal{{background:{accent};border-radius:2px}} QSlider::handle:horizontal{{background:#fff;border:2px solid {accent};width:13px;height:13px;margin:-5px 0;border-radius:7px}}")

    def _build_library_columns_menu(self):
        self._column_actions={}
        for key,label in (("artist","هنرمند"),("album","آلبوم"),("genre","سبک"),("year","سال"),("duration","مدت")):
            action=self.columns_menu.addAction(label); action.setCheckable(True); action.setChecked(self.library_columns.get(key,True))
            action.toggled.connect(lambda checked,k=key:self._library_column_toggled(k,checked)); self._column_actions[key]=action
    def _library_column_toggled(self,key,checked):
        self.library_columns[key]=bool(checked)
        self.conn.execute("INSERT INTO smart_dj_settings(key,value) VALUES('library_columns',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(self.library_columns),)); self.conn.commit(); self._load_library()
    def _build_library_metadata(self,artist,album,genre,year,duration):
        values=(("artist",artist or "هنرمند نامشخص"),("album",album or "بدون آلبوم"),("genre",genre),("year",year),("duration",self._fmt(int((duration or 0)*1000)) if duration else None))
        return "  •  ".join(str(value) for key,value in values if self.library_columns.get(key,True) and value not in (None,""))
    def _update_library_empty_states(self):
        if not hasattr(self,"library_empty_hint"):return
        total=self.track_list.count(); visible=sum(not self.track_list.item(i).isHidden() for i in range(total))
        favorites=self.fav_list.count(); visible_fav=sum(not self.fav_list.item(i).isHidden() for i in range(favorites))
        self.library_empty_hint.setText("کتابخانه‌ات خالی است\nبرای شروع، آهنگ یا پوشهٔ موسیقی را اضافه کن." if total==0 else "نتیجه‌ای برای این جستجو پیدا نشد.")
        self.favorites_empty_hint.setText("هنوز آهنگی به علاقه‌مندی‌ها اضافه نشده است." if favorites==0 else "نتیجه‌ای برای این جستجو پیدا نشد.")
        self.library_empty_hint.setVisible(visible==0); self.favorites_empty_hint.setVisible(visible_fav==0)
    def _set_library_controls_visible(self,visible):
        visible=bool(visible and not getattr(self,"_focus_mode",False))
        collection_page=getattr(self,"collection_page",None)
        if collection_page and collection_page.isVisible():visible=False
        for control in (self.search,self.add_files_btn,self.add_folder_btn,self.view_mode_combo,self.sort_label,self.sort_combo,self.filter_artist_combo,self.filter_album_combo,self.filter_genre_combo,self.clear_filters_btn,self.columns_btn):control.setVisible(visible)
    def _accent_changed(self):
        self.accent_mode=self.accent_combo.currentData()
        self.conn.execute("INSERT INTO smart_dj_settings(key,value) VALUES('accent_mode',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(self.accent_mode,)); self.conn.commit(); self._update_accent_from_cover(force=True)
    def _update_accent_from_cover(self,force=False):
        palette={"green":"#1ed760","blue":"#4da3ff","purple":"#b58cff","amber":"#f0aa45"}; color=palette.get(self.accent_mode,"#1ed760")
        if self.accent_mode=="dynamic" and self.current_path:
            pix=self._cover_pixmap(self.current_path,48)
            if not pix.isNull():
                image=pix.toImage(); sums=[0,0,0]; count=0
                for y in range(0,image.height(),3):
                    for x in range(0,image.width(),3):
                        sample=image.pixelColor(x,y); _,sat,val,_=sample.getHsvF()
                        if sat>.28 and .15<val<.92:sums[0]+=sample.red(); sums[1]+=sample.green(); sums[2]+=sample.blue(); count+=1
                if count:color=QColor(*(round(part/count) for part in sums)).name()
        changed=getattr(self,"accent_color",None)!=color; self.accent_color=color
        if changed and hasattr(self,"play_btn") and (force or self.accent_mode!="dynamic" or self.current_path):
            self._style()
            if self.view_mode_combo.currentData()=="grid":self._set_track_view_mode("grid")
    def _font_scale_changed(self):
        value=self.font_scale_combo.currentData(); self.font_scale=float(value)
        self.conn.execute("INSERT INTO smart_dj_settings(key,value) VALUES('font_scale',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(value,)); self.conn.commit(); self._style()
    def _open_collection(self,kind,value):
        value=(value or "").strip()
        if not value:return
        if not self.collection_page.isVisible():
            self._collection_return_home=not self.home_page.isHidden(); self._collection_return_index=getattr(self,"_active_page_index",0)
        self.collection_list.clear()
        if kind=="artist":rows=self.conn.execute("SELECT path,title,artist,album,duration,genre FROM tracks WHERE artist=? COLLATE NOCASE ORDER BY album COLLATE NOCASE,title COLLATE NOCASE",(value,)).fetchall(); title=f"هنرمند · {value}"
        else:rows=self.conn.execute("SELECT path,title,artist,album,duration,genre FROM tracks WHERE album=? COLLATE NOCASE ORDER BY track_number,title COLLATE NOCASE",(value,)).fetchall(); title=f"آلبوم · {value}"
        self._collection_kind=kind; self._collection_name=value; self._collection_rows=[row for row in rows if os.path.isfile(row[0])]; self._collection_paths=[]
        self.collection_heading.setText(value); self.collection_kicker.setText("هنرمند" if kind=="artist" else "آلبوم")
        total_ms=sum(int((row[4] or 0)*1000) for row in self._collection_rows)
        self.collection_hint.setText(f"{len(self._collection_rows)} آهنگ  ·  {self._fmt(total_ms)}")
        self._render_collection_tracks()
        self.collection_cover.setPixmap(self._cover_pixmap(self._collection_paths[0] if self._collection_paths else "",176)); self.collection_play_btn.setEnabled(bool(self._collection_paths))
        related=[]
        if kind=="artist":related=self.conn.execute("SELECT album,MIN(path),MAX(artist) FROM tracks WHERE artist=? COLLATE NOCASE AND COALESCE(album,'')<>'' GROUP BY album COLLATE NOCASE ORDER BY album COLLATE NOCASE",(value,)).fetchall()
        self.collection_related.clear(); self.collection_related_section.setVisible(kind=="artist" and bool(related))
        for album,path,artist in related:
            if os.path.isfile(path):
                item=QListWidgetItem(self._cover_icon(path,112),f"{album}\n{artist or value}"); item.setData(Qt.ItemDataRole.UserRole,album); item.setData(Qt.ItemDataRole.UserRole+1,artist or value); item.setData(Qt.ItemDataRole.UserRole+2,path); fav=self.conn.execute("SELECT favorite FROM tracks WHERE path=?",(path,)).fetchone(); item.setData(Qt.ItemDataRole.UserRole+4,bool(fav and fav[0])); self.collection_related.addItem(item)
        self.stack.hide(); self.home_page.hide(); self.collection_page.show(); self.heading.setText(title); self._active_page_index=0; self._set_active_nav(0); self._set_library_controls_visible(False)
        self._animate_widget(self.collection_page)
    def _play_collection(self):
        if not getattr(self,"_collection_paths",None):return
        self._start_collection_queue(list(self._collection_paths))
    def _play_collection_shuffle(self):
        if not getattr(self,"_collection_paths",None):return
        paths=list(self._collection_paths); random.shuffle(paths); self._start_collection_queue(paths)
    def _start_collection_queue(self,paths):
        if not paths:return
        self.queue_paths=list(paths); self._queue_mode="custom"; self._active_playlist_id=None; self._refresh_playlist_picker(); self._refresh_queue_view(); self._play_path(paths[0],manual=True)
    def _play_collection_item(self,item):
        path=item.data(Qt.ItemDataRole.UserRole)
        if not path:return
        paths=list(self._collection_paths)
        if path in paths:paths.remove(path)
        self._start_collection_queue([path]+paths)
    def _render_collection_tracks(self,index=-1):
        if not hasattr(self,"collection_list"):return
        rows=list(getattr(self,"_collection_rows",[])); mode=self.collection_sort_combo.currentData() if hasattr(self,"collection_sort_combo") else "album"
        if mode=="title":rows.sort(key=lambda row:str(row[1] or Path(row[0]).stem).casefold())
        elif mode=="duration":rows.sort(key=lambda row:float(row[4] or 0),reverse=True)
        else:rows.sort(key=lambda row:(str(row[3] or "").casefold(),str(row[1] or "").casefold()))
        self.collection_list.clear(); self._collection_paths=[]
        for path,title,artist,album,duration,genre in rows:
            self._collection_paths.append(path)
            item=QListWidgetItem(self._cover_icon(path,112),f"{title or Path(path).stem}\n{artist or 'هنرمند نامشخص'}  ·  {album or 'بدون آلبوم'}  ·  {self._fmt(int((duration or 0)*1000))}")
            item.setData(Qt.ItemDataRole.UserRole,path); item.setData(Qt.ItemDataRole.UserRole+1,artist or ""); item.setData(Qt.ItemDataRole.UserRole+2,album or "")
            fav=self.conn.execute("SELECT favorite FROM tracks WHERE path=?",(path,)).fetchone(); item.setData(Qt.ItemDataRole.UserRole+4,bool(fav and fav[0])); self.collection_list.addItem(item)
        if rows:
            self.collection_cover.setPixmap(self._cover_pixmap(rows[0][0],176))
            total_ms=sum(int((row[4] or 0)*1000) for row in rows); self.collection_hint.setText(f"{len(rows)} آهنگ  ·  {self._fmt(total_ms)}")
    def _open_related_album(self,item):
        album=item.data(Qt.ItemDataRole.UserRole)
        if album:
            self._collection_parent=(self._collection_kind,self._collection_name)
            self._open_collection("album",album)
    def _leave_collection(self):
        parent=getattr(self,"_collection_parent",None)
        if parent:
            self._collection_parent=None; self._open_collection(*parent); return
        if getattr(self,"_collection_return_home",False):self._show_page(6,"خانه")
        else:
            index=getattr(self,"_collection_return_index",0); titles=("کتابخانه موسیقی","علاقه‌مندی‌ها","اکولایزر","Smart DJ","صف پخش و پلی‌لیست‌ها","بازیابی فایل‌های جابه‌جا‌شده")
            self._show_page(index,titles[index] if 0<=index<len(titles) else "کتابخانه موسیقی")
    def _show_page(self,i,title):
        self._active_page_index=i; self.collection_page.hide()
        if i==6:self.stack.hide(); self.home_page.show()
        else:self.home_page.hide(); self.stack.show(); self.stack.setCurrentIndex({7:6,8:7}.get(i,i))
        self.heading.setText(title); show=i in (0,1)
        self._set_active_nav(i)
        self._set_library_controls_visible(show)
        if i==3:self._refresh_smart_overview()
        if i==4:self._refresh_queue_view()
        if i==5:self._refresh_missing_files()
        if i==6:self._refresh_home()
        if i==7:self._load_current_lyrics()
        if hasattr(self,"_page_animation"):
            self._page_animation.stop()
        if i!=6:
            effect=QGraphicsOpacityEffect(self.stack); self.stack.setGraphicsEffect(effect); effect.setOpacity(0.92)
            self._page_animation=QPropertyAnimation(effect,b"opacity",self); self._page_animation.setDuration(160); self._page_animation.setStartValue(0.92); self._page_animation.setEndValue(1.0); self._page_animation.finished.connect(lambda:self.stack.setGraphicsEffect(None)); self._page_animation.start()
    def _choose_files(self):
        paths,_=QFileDialog.getOpenFileNames(self,"انتخاب فایل‌های صوتی","","Audio files (*.mp3 *.flac *.wav *.ogg *.m4a *.aac *.wma *.opus)")
        if paths:self._add_paths(paths)
    def _choose_folder(self):
        folder=QFileDialog.getExistingDirectory(self,"انتخاب پوشه موسیقی")
        if folder:
            self._add_paths([os.path.join(r,n) for r,_,ns in os.walk(folder) for n in ns if Path(n).suffix.lower() in AUDIO_EXTS])
    def _read_watched_folders(self):
        row=self.conn.execute("SELECT value FROM smart_dj_settings WHERE key='watched_folders'").fetchone()
        try:values=json.loads(row[0]) if row else []
        except (TypeError,ValueError):values=[]
        if not isinstance(values,list):return []
        folders=[]; seen=set()
        for value in values:
            if not isinstance(value,str):continue
            path=os.path.abspath(os.path.expanduser(value)); key=os.path.normcase(path)
            if os.path.isdir(path) and key not in seen:seen.add(key); folders.append(path)
        return folders
    def _save_watched_folders(self):
        self.conn.execute("INSERT INTO smart_dj_settings(key,value) VALUES('watched_folders',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(self.watched_folders,ensure_ascii=False),)); self.conn.commit()
    def _refresh_watched_folder_list(self):
        if not hasattr(self,"watched_folder_list"):return
        self.watched_folder_list.clear()
        for folder in self.watched_folders:
            item=QListWidgetItem(folder); item.setToolTip(folder); self.watched_folder_list.addItem(item)
        self.watch_status.setText(f"{len(self.watched_folders)} پوشه برای پایش فعال است." if self.watched_folders else "پایش پوشه غیرفعال است.")
        self.remove_watched_folder_btn.setEnabled(self.watched_folder_list.currentRow()>=0)
    def _choose_watched_folder(self):
        folder=QFileDialog.getExistingDirectory(self,"انتخاب پوشه برای پایش")
        if not folder:return
        folder=os.path.abspath(folder)
        if any(os.path.normcase(folder)==os.path.normcase(path) for path in self.watched_folders):return
        self.watched_folders.append(folder); self._save_watched_folders(); self._refresh_watched_folder_list()
        self.watch_status.setText("پوشه اضافه شد؛ در حال اسکن…")
        if self._watch_worker and self._watch_worker.isRunning():self._watch_worker.requestInterruption()
        else:self._start_watched_scan()
    def _remove_watched_folder(self):
        row=self.watched_folder_list.currentRow()
        if row<0:return
        self.watched_folders.pop(row); self._save_watched_folders(); self._refresh_watched_folder_list()
        if self._watch_worker and self._watch_worker.isRunning():self._watch_worker.requestInterruption()
    def _start_watched_scan(self):
        if not self.watched_folders or (self._watch_worker and self._watch_worker.isRunning()):return
        known={os.path.normcase(os.path.abspath(path)) for (path,) in self.conn.execute("SELECT path FROM tracks")}
        worker=WatchedFolderScanner(list(self.watched_folders),known,self); self._watch_worker=worker
        worker.pathsFound.connect(self._watched_scan_finished)
        worker.finished.connect(lambda worker=worker:self._watch_worker_finished(worker))
        worker.start()
    def _watch_worker_finished(self,worker):
        if self._watch_worker is worker:self._watch_worker=None
    def _watched_scan_finished(self,paths):
        if not paths:
            self.watch_status.setText("پوشه‌ها بررسی شدند؛ آهنگ تازه‌ای نبود."); return
        new_paths=[path for path in paths if not self.conn.execute("SELECT 1 FROM tracks WHERE path=?",(path,)).fetchone()]
        if new_paths:
            self._add_paths(new_paths)
            self.watch_status.setText(f"{len(new_paths)} آهنگ تازه به کتابخانه اضافه شد.")
        else:self.watch_status.setText("پوشه‌ها بررسی شدند؛ آهنگ تازه‌ای نبود.")
    def _embedded_cover_data(self,path):
        """Read common embedded cover formats supported by Mutagen."""
        try:
            folder=Path(path).parent
            for name in ("cover.jpg","cover.jpeg","cover.png","cover.webp","folder.jpg","folder.png","front.jpg","front.png","album.jpg","album.png"):
                candidate=folder/name
                if candidate.is_file():return candidate.read_bytes()
            audio=MutagenFile(path)
            if not audio:return None
            pictures=getattr(audio,"pictures",None) or []
            if pictures and getattr(pictures[0],"data",None):return bytes(pictures[0].data)
            tags=getattr(audio,"tags",None)
            if not tags:return None
            getall=getattr(tags,"getall",None)
            if getall:
                frames=getall("APIC")
                if frames and getattr(frames[0],"data",None):return bytes(frames[0].data)
            covers=tags.get("covr") if hasattr(tags,"get") else None
            if covers:
                try:return bytes(covers[0])
                except (TypeError,ValueError):pass
            for key in ("metadata_block_picture","coverart"):
                values=tags.get(key) if hasattr(tags,"get") else None
                if not values:continue
                for value in values:
                    try:
                        decoded=base64.b64decode(value)
                        if key=="metadata_block_picture":
                            from mutagen.flac import Picture
                            decoded=Picture(decoded).data
                        if decoded:return bytes(decoded)
                    except Exception:continue
            values=tags.get("WM/Picture") if hasattr(tags,"get") else None
            for value in values or []:
                picture=getattr(value,"value",value)
                data=getattr(picture,"data",None)
                if data:return bytes(data)
        except Exception:
            pass
        return None
    def _cover_pixmap(self,path,size=64):
        key=str(path or "")
        cached=self._cover_art_cache.get(key)
        if cached is None:
            data=self._embedded_cover_data(key) if key else None
            cached=QPixmap()
            if data:cached.loadFromData(data)
            from_fallback=False
            if cached.isNull() and key:
                cached=self._cover_art_cache.get("")
                from_fallback=cached is not None and not cached.isNull()
            if cached is None or cached.isNull():
                cached=QPixmap(str(Path(__file__).resolve().parents[1]/"assets"/"nova_music_icon.png"))
                from_fallback=False
            if not cached.isNull() and not from_fallback:
                cached=cached.scaled(192,192,Qt.AspectRatioMode.KeepAspectRatioByExpanding,Qt.TransformationMode.SmoothTransformation)
                side=min(cached.width(),cached.height())
                cached=cached.copy((cached.width()-side)//2,(cached.height()-side)//2,side,side)
            self._cover_art_cache[key]=cached
        return cached.scaled(size,size,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation) if not cached.isNull() else QPixmap()
    def _cover_icon(self,path,size=34):
        return QIcon(self._cover_pixmap(path,size))
    def _set_player_cover(self,path):
        self._animate_cover(self.cover,path,64)
        self._update_accent_from_cover()
        self._refresh_fullscreen_player()
        if hasattr(self,"large_cover"):
            self._animate_cover(self.large_cover,path,self.large_cover.width())
            row=self.conn.execute("SELECT title,artist,album,genre FROM tracks WHERE path=?",(path,)).fetchone() if path else None
            title,artist,album,genre=row if row else (Path(path).stem if path else "انتخاب آهنگ","","","")
            self.panel_track_title.setText(title or Path(path).stem)
            self.panel_artist.setText(artist or "هنرمند نامشخص")
            self.panel_album.setText(album or "بدون آلبوم")
            self.panel_genre.setText(genre or "سبک نامشخص")
        if hasattr(self,"lyrics_cover") and self.current_path:self._animate_cover(self.lyrics_cover,self.current_path,92)
        if hasattr(self,"mini_player") and self.mini_player.isVisible():self._sync_mini_player()
    def _animate_cover(self,label,path,size):
        if getattr(label,"_cover_path",None)==path:
            label.setPixmap(self._cover_pixmap(path,size)); return
        version=getattr(label,"_cover_version",0)+1; label._cover_version=version; label._cover_path=path
        old=getattr(label,"_cover_animation",None)
        if old:old.stop()
        effect=label.graphicsEffect()
        if not isinstance(effect,QGraphicsOpacityEffect):
            effect=QGraphicsOpacityEffect(label); label.setGraphicsEffect(effect)
        effect.setOpacity(1.0)
        fade_out=QPropertyAnimation(effect,b"opacity",self); fade_out.setDuration(90); fade_out.setStartValue(1.0); fade_out.setEndValue(.25)
        def swap_and_fade_in():
            if getattr(label,"_cover_version",0)!=version:return
            label.setPixmap(self._cover_pixmap(path,size))
            fade_in=QPropertyAnimation(effect,b"opacity",self); fade_in.setDuration(180); fade_in.setStartValue(.25); fade_in.setEndValue(1.0)
            label._cover_animation=fade_in; fade_in.start()
        fade_out.finished.connect(swap_and_fade_in); label._cover_animation=fade_out; fade_out.start()
    def _toggle_now_playing_panel(self):
        visible=self.now_playing_panel.isHidden()
        self.now_playing_panel.setVisible(visible); self.now_panel_btn.setProperty("expanded",visible)
        self.now_panel_btn.style().unpolish(self.now_panel_btn); self.now_panel_btn.style().polish(self.now_panel_btn)
        if visible:self._animate_widget(self.now_playing_panel)
        self._update_responsive_player_layout()
    def _toggle_mini_player(self):
        if not hasattr(self,"mini_player"):
            dialog=MiniPlayerWindow(self); dialog.setWindowTitle("NOVA Music — Mini Player"); dialog.setObjectName("miniPlayer"); dialog.setMinimumSize(300,142); dialog.resize(330,154)
            outer=QVBoxLayout(dialog); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0); outer.addWidget(self._create_window_titlebar(dialog,"NOVA MUSIC · MINI",mini=True))
            row=QHBoxLayout(); row.setContentsMargins(12,8,12,8); cover=QLabel(); cover.setFixedSize(112,112); cover.setObjectName("largeCoverArt"); cover.setAlignment(Qt.AlignmentFlag.AlignCenter); row.addWidget(cover)
            col=QVBoxLayout(); title=QLabel("آهنگی انتخاب نشده"); title.setWordWrap(True); title.setObjectName("trackTitle"); artist=QLabel(""); artist.setObjectName("muted"); artist.setWordWrap(True); col.addWidget(title); col.addWidget(artist)
            controls=QHBoxLayout(); previous=QPushButton(); previous.setIcon(icon("previous")); previous.setToolTip("آهنگ قبلی"); previous.clicked.connect(self._previous); play=QPushButton(); play.setIcon(icon("play")); play.setToolTip("پخش یا مکث"); play.clicked.connect(self._toggle_playback); next_button=QPushButton(); next_button.setIcon(icon("next")); next_button.setToolTip("آهنگ بعدی"); next_button.clicked.connect(self._next)
            for button in (previous,play,next_button):button.setAccessibleName(button.toolTip()); controls.addWidget(button)
            col.addLayout(controls); row.addLayout(col,1); outer.addLayout(row,1)
            self.mini_player=dialog; self.mini_cover=cover; self.mini_title=title; self.mini_artist=artist; self.mini_play=play
            dialog.size_grip=QSizeGrip(dialog); dialog.size_grip.setFixedSize(16,16); dialog.size_grip.raise_()
            dialog.setStyleSheet("QDialog#miniPlayer{background:#181818;border:1px solid #393939;border-radius:14px}")
        self._sync_mini_player()
        if self.mini_player.isVisible():self._restore_main_from_mini()
        else:self.hide(); self.mini_player.show(); self.mini_player.raise_()
    def _restore_main_from_mini(self):
        if hasattr(self,"mini_player"):self.mini_player.hide()
        self.showNormal(); self.raise_(); self.activateWindow()
    def _sync_mini_player(self):
        if not hasattr(self,"mini_player"):return
        path=self.current_path; row=self.conn.execute("SELECT title,artist FROM tracks WHERE path=?",(path,)).fetchone() if path else None
        self.mini_title.setText((row[0] or Path(path).stem) if row else (Path(path).stem if path else "آهنگی انتخاب نشده")); self.mini_artist.setText(row[1] or "هنرمند نامشخص" if row else "")
        self._animate_cover(self.mini_cover,path,112); self.mini_play.setIcon(icon("pause" if self.engine._playing else "play"))
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,"main_size_grip"):self.main_size_grip.move(self.centralWidget().width()-16,self.centralWidget().height()-16)
        self._update_responsive_player_layout()
    def _update_responsive_player_layout(self):
        if not hasattr(self,"now_playing_panel"):return
        panel_width=max(236,min(330,int(self.width()*.255)))
        self.now_playing_panel.setFixedWidth(panel_width)
        cover_size=max(172,min(280,panel_width-50))
        self.large_cover.setFixedSize(cover_size,cover_size)
        self.large_cover.setPixmap(self._cover_pixmap(self.current_path,cover_size))
        compact=self.width()<1120; ultra_compact=self.width()<1020
        margins=16 if compact else 24
        if hasattr(self,"search"):self.search.setFixedWidth(185 if compact else 270)
        if hasattr(self,"player_layout"):
            self.player_layout.setContentsMargins(12 if compact else 22,10 if compact else 12,12 if compact else 22,10 if compact else 12)
            self.player_layout.setSpacing(12 if compact else 26)
            self.player_left.setMinimumWidth(158 if compact else 205); self.player_left.setMaximumWidth(235 if compact else 285)
            self.player_center.setMinimumWidth(245 if compact else 350); self.player_right.setMinimumWidth(192 if compact else 260)
            self.output_device.setMinimumWidth(105 if compact else 180)
            self.spectrum.setVisible(not ultra_compact)
        if self.centralWidget() and self.centralWidget().layout():
            body=self.centralWidget().layout().itemAt(0).layout()
            if body and body.count()>1:
                content=body.itemAt(1).widget()
                if content and content.layout():content.layout().setContentsMargins(margins,18,margins,16)
    def _animate_widget(self,widget):
        effect=QGraphicsOpacityEffect(widget); widget.setGraphicsEffect(effect)
        animation=QPropertyAnimation(effect,b"opacity",self); animation.setDuration(190); animation.setStartValue(.72); animation.setEndValue(1.0)
        if not hasattr(self,"_ui_animations"):self._ui_animations=[]
        self._ui_animations.append(animation)
        animation.finished.connect(lambda w=widget,a=animation:(w.setGraphicsEffect(None),self._ui_animations.remove(a) if a in self._ui_animations else None))
        animation.start()
    def _grid_item_clicked(self,listing,item):
        if listing.viewMode()!=QListWidget.ViewMode.IconMode:return
        point=listing.viewport().mapFromGlobal(QCursor.pos()); cell=listing.visualItemRect(item)
        image_size=min(listing.iconSize().width(),cell.width()-28); image_left=cell.left()+(cell.width()-image_size)//2
        heart_rect=QRect(image_left+image_size-31,cell.top()+19,26,26)
        action_path=item.data(Qt.ItemDataRole.UserRole+2) if listing is self.collection_related else item.data(Qt.ItemDataRole.UserRole)
        if heart_rect.contains(point):
            self._pending_grid_action=("favorite",action_path,listing)
            self._grid_favorite_timer.start()
        else:
            play_rect=QRect(image_left+image_size-40,cell.top()+image_size-22,34,34)
            if play_rect.contains(point):self._pending_grid_action=("play",item.data(Qt.ItemDataRole.UserRole),listing); self._grid_favorite_timer.start()
            else:self._grid_favorite_timer.stop(); self._pending_grid_action=None
    def _commit_grid_action(self):
        action=self._pending_grid_action; self._pending_grid_action=None
        if not action:return
        kind,path,listing=action
        if kind=="play":
            item=next((listing.item(i) for i in range(listing.count()) if listing.item(i).data(Qt.ItemDataRole.UserRole)==path),None)
            if listing is self.collection_list and item:self._play_collection_item(item)
            elif listing is self.collection_related and item:self._open_related_album(item); self._play_collection()
            elif listing is self.home_smart_cards and item:self._play_home_smart_item(item)
            elif listing is self.queue_list and item:self._play_path(path,manual=True)
            elif item:self._play_item(item)
            return
        row=self.conn.execute("SELECT favorite,artist,album FROM tracks WHERE path=?",(path,)).fetchone()
        if row:self._set_favorite(path,not bool(row[0]))
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
        self.track_list.clear(); self.fav_list.clear(); old_queue=list(self.queue_paths); library_paths=[]
        order_by={"title":"COALESCE(title,'') COLLATE NOCASE, COALESCE(artist,'') COLLATE NOCASE",
                  "artist":"COALESCE(artist,'') COLLATE NOCASE, COALESCE(title,'') COLLATE NOCASE",
                  "album":"COALESCE(album,'') COLLATE NOCASE, COALESCE(track_number,0), COALESCE(title,'') COLLATE NOCASE",
                  "year":"year IS NULL, year DESC, COALESCE(title,'') COLLATE NOCASE",
                  "duration":"duration DESC, COALESCE(title,'') COLLATE NOCASE"}
        sort_mode=self.sort_combo.currentData() if hasattr(self,"sort_combo") else "artist"
        rows=self.conn.execute(f"SELECT path,title,artist,album,duration,favorite,genre,year,track_number FROM tracks ORDER BY {order_by.get(sort_mode,order_by['artist'])}").fetchall()
        self._refresh_track_filter_options(rows)
        missing = {row[0] for row in rows if not os.path.exists(row[0])}
        for path,title,artist,album,duration,fav,genre,year,track_number in rows:
            if path in missing:continue
            tag_text=self._build_library_metadata(artist,album,genre,year,duration) or "اطلاعات تکمیلی ثبت نشده"
            library_paths.append(path); item=QListWidgetItem(self._cover_icon(path),f"{title}\n{tag_text}"); item.setData(Qt.ItemDataRole.UserRole,path); item.setData(Qt.ItemDataRole.UserRole+1,artist or ""); item.setData(Qt.ItemDataRole.UserRole+2,album or ""); item.setData(Qt.ItemDataRole.UserRole+3,genre or ""); item.setData(Qt.ItemDataRole.UserRole+4,bool(fav)); item.setData(Qt.ItemDataRole.UserRole+5,year); item.setData(Qt.ItemDataRole.UserRole+6,duration); item.setData(Qt.ItemDataRole.AccessibleDescriptionRole,f"{title or Path(path).stem}، {artist or 'هنرمند نامشخص'}"); self.track_list.addItem(item)
            if fav:
                it=QListWidgetItem(self._cover_icon(path),f"{title}\n{tag_text}"); it.setData(Qt.ItemDataRole.UserRole,path); it.setData(Qt.ItemDataRole.UserRole+1,artist or ""); it.setData(Qt.ItemDataRole.UserRole+2,album or ""); it.setData(Qt.ItemDataRole.UserRole+3,genre or ""); it.setData(Qt.ItemDataRole.UserRole+4,True); it.setData(Qt.ItemDataRole.UserRole+5,year); it.setData(Qt.ItemDataRole.UserRole+6,duration); it.setData(Qt.ItemDataRole.AccessibleDescriptionRole,f"{title or Path(path).stem}، {artist or 'هنرمند نامشخص'}"); self.fav_list.addItem(it)
        available=set(library_paths)
        if self._queue_mode=="library":self.queue_paths=library_paths
        elif self._queue_mode=="smart":
            current=[self.current_path] if self.current_path in available and (self.engine._playing or self.engine._paused) else []
            self.queue_paths=current+[p for p in self.smart_queue if p in available and p not in current]
        elif self._queue_mode=="playlist" and self._active_playlist_id is not None:
            self.queue_paths=[p for (p,) in self.conn.execute("SELECT track_path FROM playlist_tracks WHERE playlist_id=? ORDER BY position",(self._active_playlist_id,)) if p in available]
        else:self.queue_paths=[p for p in old_queue if p in available]
        self._filter_tracks(self.search.text())
        self._update_library_empty_states()
        self._refresh_playlist_picker(); self._refresh_queue_view(); self._refresh_missing_files()
    def _scan_library_duplicates(self):
        rows=self.conn.execute("SELECT path,title,artist FROM tracks ORDER BY artist,title").fetchall()
        by_size={}
        for path,title,artist in rows:
            try:
                size=os.path.getsize(path)
                if os.path.isfile(path):by_size.setdefault(size,[]).append((path,title,artist))
            except OSError:continue
        candidates=[item for group in by_size.values() if len(group)>1 for item in group]
        self.duplicate_list.clear()
        if not candidates:
            self.duplicate_status.setText("نسخهٔ کاملاً یکسانی پیدا نشد."); return
        progress=QProgressDialog("در حال محاسبهٔ هش فایل‌ها…","لغو",0,len(candidates),self)
        progress.setWindowModality(Qt.WindowModality.WindowModal); progress.setMinimumDuration(300)
        hashes={}; canceled=False
        for index,(path,title,artist) in enumerate(candidates):
            if progress.wasCanceled():canceled=True; break
            try:
                digest=hashlib.sha256()
                with open(path,"rb") as audio_file:
                    chunk_count=0
                    while True:
                        chunk=audio_file.read(1024*1024)
                        if not chunk:break
                        digest.update(chunk); chunk_count+=1
                        if chunk_count%16==0:
                            QApplication.processEvents()
                            if progress.wasCanceled():canceled=True; break
                if canceled:break
                hashes.setdefault((os.path.getsize(path),digest.hexdigest()),[]).append((path,title,artist))
            except OSError:pass
            progress.setValue(index+1); QApplication.processEvents()
        progress.close()
        if canceled:
            self.duplicate_status.setText("اسکن لغو شد؛ نتیجهٔ ناقص نمایش داده نشد."); return
        groups=[group for group in hashes.values() if len(group)>1]
        for number,group in enumerate(groups,1):
            size=os.path.getsize(group[0][0]); root=QTreeWidgetItem([f"گروه {number}  •  {len(group)} نسخه  •  {size/1024/1024:.2f} مگابایت"])
            self.duplicate_list.addTopLevelItem(root)
            for path,title,artist in group:
                child=QTreeWidgetItem([f"{title or Path(path).stem}  •  {artist or 'هنرمند نامشخص'}\\n{path}"]); child.setToolTip(0,path); root.addChild(child)
            root.setExpanded(True)
        copies=sum(len(group) for group in groups)
        self.duplicate_status.setText(f"{len(groups)} گروه و {copies} فایل تکراری پیدا شد." if groups else "نسخهٔ کاملاً یکسانی پیدا نشد.")
    def _refresh_missing_files(self):
        if not hasattr(self,"missing_list"):return
        self.missing_list.clear()
        rows=self.conn.execute("SELECT path,title,artist,album FROM tracks ORDER BY artist,title").fetchall()
        for path,title,artist,album in rows:
            if os.path.isfile(path):continue
            subtitle=f"{artist or 'ناشناخته'}  •  {album or 'بدون آلبوم'}\n{path}"
            item=QListWidgetItem(self._cover_icon(path),f"{title or Path(path).stem}\n{subtitle}")
            item.setData(Qt.ItemDataRole.UserRole,path); item.setToolTip(path); self.missing_list.addItem(item)
        count=self.missing_list.count()
        self.missing_count.setText(f"{count} فایل پیدا نشده" if count else "همهٔ فایل‌ها در دسترس‌اند")
        self.relink_missing_btn.setEnabled(self.missing_list.currentRow()>=0)
    def _choose_relink_path(self):
        item=self.missing_list.currentItem()
        if not item:return
        old_path=item.data(Qt.ItemDataRole.UserRole)
        new_path,_=QFileDialog.getOpenFileName(self,"پیداکردن فایل جابه‌جاشده",str(Path(old_path).parent),
            "Audio files (*.mp3 *.flac *.wav *.ogg *.m4a *.aac *.wma *.opus)")
        if not new_path:return
        try:
            self._relink_file(old_path,new_path)
            self.status.setText("فایل دوباره پیدا و به کتابخانه وصل شد")
        except (OSError,ValueError,sqlite3.Error) as exc:
            QMessageBox.warning(self,"اتصال دوباره ناموفق بود",str(exc))
    def _relink_file(self,old_path,new_path,refresh=True):
        old_path=os.path.abspath(old_path); new_path=os.path.abspath(new_path)
        if not os.path.isfile(new_path) or Path(new_path).suffix.lower() not in AUDIO_EXTS:
            raise ValueError("فایل انتخاب‌شده پیدا نشد یا قالب صوتی پشتیبانی‌شده‌ای ندارد.")
        if old_path==new_path:raise ValueError("این همان مسیر قبلی فایل است.")
        if self.conn.execute("SELECT 1 FROM tracks WHERE path=?",(new_path,)).fetchone():
            raise ValueError("این فایل از قبل در کتابخانه ثبت شده است.")
        if not self.conn.execute("SELECT 1 FROM tracks WHERE path=?",(old_path,)).fetchone():
            raise ValueError("رکورد فایل قدیمی در کتابخانه پیدا نشد.")
        state_row=self.conn.execute("SELECT value FROM smart_dj_settings WHERE key='playback_state'").fetchone()
        state={}
        if state_row:
            try:
                parsed=json.loads(state_row[0])
                if isinstance(parsed,dict):state=parsed
            except (ValueError,TypeError):pass
        was_current=self.current_path==old_path or state.get("current_path")==old_path
        was_playing=was_current and self.engine._playing
        was_paused=was_current and self.engine._paused
        saved_position=int(self.engine.position_ms) if self.current_path==old_path else int(state.get("position_ms",0) or 0) if state.get("current_path")==old_path else 0
        try:
            self.conn.execute("BEGIN")
            self.conn.execute("UPDATE tracks SET path=? WHERE path=?",(new_path,old_path))
            self.conn.execute("DELETE FROM playlist_tracks WHERE track_path=?",(new_path,))
            self.conn.execute("UPDATE playlist_tracks SET track_path=? WHERE track_path=?",(new_path,old_path))
            self.conn.execute("UPDATE listening_sessions SET track_path=? WHERE track_path=?",(new_path,old_path))
            self.conn.execute("UPDATE listening_events SET track_path=? WHERE track_path=?",(new_path,old_path))
            if state:
                if state.get("current_path")==old_path:state["current_path"]=new_path
                state["queue"]=[new_path if path==old_path else path for path in state.get("queue",[])]
                self.conn.execute("UPDATE smart_dj_settings SET value=? WHERE key='playback_state'",(json.dumps(state,ensure_ascii=False),))
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback(); raise
        self.queue_paths=[new_path if path==old_path else path for path in self.queue_paths]
        self.smart_queue=[new_path if path==old_path else path for path in self.smart_queue]
        if state.get("queue"):
            known={path for (path,) in self.conn.execute("SELECT path FROM tracks")}
            restored=[path for path in state["queue"] if path in known and os.path.isfile(path)]
            if restored:self.queue_paths=restored
        self._cover_art_cache.pop(old_path,None); self._cover_art_cache.pop(new_path,None)
        if was_current:
            self.current_path=new_path; self.engine.set_source(new_path)
            if self.engine.duration_ms:saved_position=min(saved_position,self.engine.duration_ms)
            self.engine.position_ms=saved_position
            self.engine._frames_played=int(saved_position*self.engine.sample_rate/1000)
            self.engine._last_position_emit_frame=self.engine._frames_played
        if refresh:self._load_library()
        if was_current:
            title,artist,album,_,_,_,_=self._metadata(new_path)
            self.track_label.setText(title); self.artist_label.setText(f"{artist or 'ناشناخته'}  •  {album or 'بدون آلبوم'}")
            self._set_player_cover(new_path); self.seek_slider.setValue(saved_position); self.elapsed.setText(self._fmt(saved_position))
            if was_playing:self.engine.play()
            elif was_paused:
                self.engine._paused=True; self.status.setText("مکث‌شده")
            else:self.status.setText("آمادهٔ ادامه از محل قبلی؛ برای شروع پخش را بزن")
        if refresh:
            self._save_playback_state()
        return new_path
    def _scan_for_moved_files(self):
        folder=QFileDialog.getExistingDirectory(self,"انتخاب پوشهٔ جدید موسیقی")
        if not folder:return
        missing=[row for row in self.conn.execute("SELECT path,title,artist,album,duration FROM tracks") if not os.path.isfile(row[0])]
        if not missing:
            QMessageBox.information(self,"بررسی فایل‌ها","فایل گم‌شده‌ای در کتابخانه وجود ندارد."); return
        known={row[0] for row in self.conn.execute("SELECT path FROM tracks")}
        candidates=[]
        for root,_,names in os.walk(folder):
            candidates.extend(os.path.abspath(os.path.join(root,name)) for name in names
                if Path(name).suffix.lower() in AUDIO_EXTS and os.path.abspath(os.path.join(root,name)) not in known)
        progress=QProgressDialog("در حال بررسی برچسب‌های فایل‌های صوتی…","لغو",0,len(candidates),self)
        progress.setWindowModality(Qt.WindowModality.WindowModal); progress.setMinimumDuration(500)
        candidate_rows=[]
        for index,path in enumerate(candidates):
            if progress.wasCanceled():break
            try:
                audio=MutagenFile(path,easy=True)
                tags={key:str((audio.get(key) or [""])[0]).casefold().strip() for key in ("title","artist","album")} if audio else {}
                duration=float(getattr(getattr(audio,"info",None),"length",0) or 0) if audio else 0
            except Exception:tags={}; duration=0
            candidate_rows.append((path,Path(path).stem.casefold(),tags,duration))
            progress.setValue(index+1)
            if index%40==0:QApplication.processEvents()
        was_canceled=progress.wasCanceled(); progress.close()
        if was_canceled:return
        matches=[]; ambiguous=0
        for old_path,title,artist,album,duration in missing:
            old_stem=Path(old_path).stem.casefold(); title=(title or old_stem).casefold().strip(); artist=(artist or "").casefold().strip(); album=(album or "").casefold().strip()
            scored=[]
            for path,stem,tags,candidate_duration in candidate_rows:
                score=5 if stem==old_stem else 0
                if tags.get("title")==title:score+=4
                if artist and tags.get("artist")==artist:score+=3
                if album and tags.get("album")==album:score+=1
                if duration and candidate_duration and abs(duration-candidate_duration)<=max(2.0,duration*0.02):score+=3
                if score>=5:scored.append((score,path))
            scored.sort(reverse=True)
            if scored and (len(scored)==1 or scored[0][0]>scored[1][0]):matches.append((old_path,scored[0][1]))
            elif scored:ambiguous+=1
        restored=0
        for old_path,new_path in matches:
            try:self._relink_file(old_path,new_path,refresh=False); restored+=1
            except (OSError,ValueError,sqlite3.Error):continue
        self._load_library(); self._save_playback_state()
        QMessageBox.information(self,"بررسی فایل‌ها",f"{restored} فایل دوباره وصل شد.\n{ambiguous} مورد تطبیق مبهم داشت و برای جلوگیری از اتصال اشتباه دست‌نخورده ماند.")
    def _restore_playback_state(self):
        if not self.database_available:return
        row=self.conn.execute("SELECT value FROM smart_dj_settings WHERE key='playback_state'").fetchone()
        if not row:return
        try:
            state=json.loads(row[0])
            available={path for (path,) in self.conn.execute("SELECT path FROM tracks") if os.path.isfile(path)}
            queue=state.get("queue",[])
            self.queue_paths=[path for path in queue if path in available]
            mode=state.get("queue_mode","library")
            if mode not in {"library","playlist","custom","smart"}:mode="library"
            self._queue_mode="custom" if mode in {"custom","smart","playlist"} else "library"
            playlist_id=state.get("playlist_id")
            if playlist_id is not None and self.conn.execute("SELECT 1 FROM playlists WHERE id=?",(playlist_id,)).fetchone():
                self._active_playlist_id=int(playlist_id)
            else:self._active_playlist_id=None
            if self._queue_mode=="library" and not self.queue_paths:
                self.queue_paths=[p for (p,) in self.conn.execute("SELECT path FROM tracks ORDER BY artist,title") if p in available]
            path=state.get("current_path")
            if not path or path not in available:
                self._refresh_playlist_picker(); self._refresh_queue_view(); return
            self.current_path=path
            self.engine.set_source(path)
            position=max(0,int(state.get("position_ms",0) or 0))
            if self.engine.duration_ms:position=min(position,self.engine.duration_ms)
            self.engine.position_ms=position
            self.engine._frames_played=int(position*self.engine.sample_rate/1000)
            self.engine._last_position_emit_frame=self.engine._frames_played
            title,artist,album,_,_,_,_=self._metadata(path)
            self.track_label.setText(title)
            self.artist_label.setText(f"{artist or 'ناشناخته'}  •  {album or 'بدون آلبوم'}")
            self._set_player_cover(path)
            self.seek_slider.setValue(position); self.elapsed.setText(self._fmt(position))
            self.status.setText("آمادهٔ ادامه از محل قبلی؛ برای شروع پخش را بزن")
            self._refresh_playlist_picker(); self._refresh_queue_view(); self._highlight_current()
        except (ValueError, TypeError, sqlite3.Error, OSError):
            return
    def _save_playback_state(self):
        if not self.database_available or not self.conn:return
        state={"current_path":self.current_path if self.current_path and os.path.isfile(self.current_path) else None,
               "position_ms":int(self.engine.position_ms),"queue":self.queue_paths,
               "queue_mode":self._queue_mode,"playlist_id":self._active_playlist_id}
        try:
            self.conn.execute("INSERT INTO smart_dj_settings(key,value) VALUES('playback_state',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (json.dumps(state,ensure_ascii=False),))
            self.conn.commit()
        except sqlite3.Error:
            pass
    def _filter_tracks(self,text):
        q=text.casefold().strip()
        selected=(self.filter_artist_combo.currentData(),self.filter_album_combo.currentData(),self.filter_genre_combo.currentData())
        for listing in (self.track_list,self.fav_list):
            for i in range(listing.count()):
                item=listing.item(i); values=(item.data(Qt.ItemDataRole.UserRole+1),item.data(Qt.ItemDataRole.UserRole+2),item.data(Qt.ItemDataRole.UserRole+3))
                searchable=" ".join(str(item.data(role) or "") for role in (Qt.ItemDataRole.UserRole+1,Qt.ItemDataRole.UserRole+2,Qt.ItemDataRole.UserRole+3,Qt.ItemDataRole.UserRole+5,Qt.ItemDataRole.UserRole+6))
                text_match=not q or q in (item.text()+" "+searchable).casefold()
                filter_match=all(not wanted or (value or "").casefold()==wanted.casefold() for value,wanted in zip(values,selected))
                item.setHidden(not (text_match and filter_match))
        self._update_library_empty_states()
    def _refresh_track_filter_options(self,rows):
        if not hasattr(self,"filter_artist_combo"):return
        columns=((self.filter_artist_combo,2,"همهٔ هنرمندان"),(self.filter_album_combo,3,"همهٔ آلبوم‌ها"),(self.filter_genre_combo,6,"همهٔ سبک‌ها"))
        for combo,index,all_label in columns:
            selected=combo.currentData(); values=sorted({str(row[index]).strip() for row in rows if row[index] and str(row[index]).strip()},key=str.casefold)
            combo.blockSignals(True); combo.clear(); combo.addItem(all_label,None)
            for value in values:combo.addItem(value,value)
            target=combo.findData(selected) if selected else 0
            combo.setCurrentIndex(max(0,target)); combo.blockSignals(False)
    def _track_sort_changed(self,index):
        mode=self.sort_combo.currentData()
        self.conn.execute("INSERT INTO smart_dj_settings(key,value) VALUES('track_sort_mode',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(mode,)); self.conn.commit()
        self._load_library()
    def _clear_track_filters(self):
        self.search.clear()
        for combo in (self.filter_artist_combo,self.filter_album_combo,self.filter_genre_combo):combo.setCurrentIndex(0)
    def _set_track_view_mode(self,mode,persist=False):
        mode=mode if mode in {"list","grid"} else "list"
        art_size={"small":96,"medium":132,"large":168}.get(self.artwork_size_combo.currentData() if hasattr(self,"artwork_size_combo") else "medium",132)
        compact=hasattr(self,"density_combo") and self.density_combo.currentData()=="compact"
        for listing in (self.track_list,self.fav_list):
            if mode=="grid":
                listing.setViewMode(QListWidget.ViewMode.IconMode); listing.setFlow(QListWidget.Flow.LeftToRight)
                listing.setWrapping(True); listing.setResizeMode(QListWidget.ResizeMode.Adjust); listing.setMovement(QListWidget.Movement.Static)
                listing.setSpacing(3 if compact else 8); listing.setIconSize(QSize(art_size,art_size)); listing.setGridSize(QSize(art_size+52,art_size+82)); listing.setWordWrap(True); listing.setMouseTracking(True); listing.viewport().setMouseTracking(True)
                listing.setItemDelegate(TrackGridDelegate(listing,self.theme_mode=="light",art_size,getattr(self,"accent_color","#1ed760")))
            else:
                listing.setViewMode(QListWidget.ViewMode.ListMode); listing.setIconSize(QSize(34,34)); listing.setGridSize(QSize()); listing.setSpacing(2 if compact else 6)
                listing.setWordWrap(False); listing.setItemDelegate(TrackItemDelegate(listing,self.theme_mode=="light",getattr(self,"accent_color","#1ed760")))
        if persist:
            self.conn.execute("INSERT INTO smart_dj_settings(key,value) VALUES('track_view_mode',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(mode,)); self.conn.commit()
    def _set_theme(self,mode,persist=False):
        if mode not in {"dark","light"}:mode="dark"
        self.theme_mode=mode
        for listing in (getattr(self,"track_list",None),getattr(self,"fav_list",None),getattr(self,"home_recent_list",None),getattr(self,"home_popular_list",None),getattr(self,"home_smart_cards",None),getattr(self,"collection_list",None),getattr(self,"collection_related",None)):
            if listing and self.view_mode_combo.currentData()=="grid":listing.setItemDelegate(TrackGridDelegate(listing,mode=="light",{"small":96,"medium":132,"large":168}.get(self.artwork_size_combo.currentData(),132),getattr(self,"accent_color","#1ed760")))
        if persist:
            self.conn.execute("INSERT INTO smart_dj_settings(key,value) VALUES('theme_mode',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(mode,)); self.conn.commit()
        if hasattr(self,"play_btn"):
            self._style()
            self._set_active_nav(getattr(self,"_active_page_index",self.stack.currentIndex()))
    def _refresh_playlist_picker(self):
        if not hasattr(self,"playlist_combo"):return
        selected=self._active_playlist_id
        self.playlist_combo.blockSignals(True); self.playlist_combo.clear()
        self.playlist_combo.addItem("صف جاری",None)
        for playlist_id,name in self.conn.execute("SELECT id,name FROM playlists ORDER BY name COLLATE NOCASE"):
            self.playlist_combo.addItem(name,playlist_id)
        index=self.playlist_combo.findData(selected) if selected is not None else 0
        self.playlist_combo.setCurrentIndex(max(0,index)); self.playlist_combo.blockSignals(False)
        self.delete_playlist_btn.setEnabled(selected is not None)
        self.export_playlist_btn.setEnabled(selected is not None)
        self.save_playlist_btn.setText("ذخیرهٔ تغییرات" if selected is not None else "ذخیره به‌عنوان پلی‌لیست")
    def _playlist_selected(self,index):
        if index<0:return
        playlist_id=self.playlist_combo.itemData(index)
        if playlist_id is None:
            self._active_playlist_id=None
            if self._queue_mode=="playlist":self._queue_mode="custom"
            self._refresh_queue_view(); self._save_playback_state(); return
        self._active_playlist_id=int(playlist_id); self._queue_mode="playlist"
        self.queue_paths=[row[0] for row in self.conn.execute("SELECT track_path FROM playlist_tracks WHERE playlist_id=? ORDER BY position",(playlist_id,)) if os.path.isfile(row[0])]
        self._refresh_queue_view(); self._schedule_crossfade(); self._save_playback_state()
    def _refresh_queue_view(self):
        if not hasattr(self,"queue_list"):return
        self.queue_list.clear()
        for path in self.queue_paths:
            row=self.conn.execute("SELECT title,artist,album FROM tracks WHERE path=?",(path,)).fetchone()
            title,artist,album=row if row else (Path(path).stem,"ناشناخته","")
            item=QListWidgetItem(self._cover_icon(path),f"{title}\n{artist or 'ناشناخته'}  •  {album or 'بدون آلبوم'}")
            item.setData(Qt.ItemDataRole.UserRole,path); item.setData(Qt.ItemDataRole.UserRole+1,artist or "ناشناخته"); item.setData(Qt.ItemDataRole.UserRole+2,album or "")
            item.setToolTip(f"{title}\n{artist or 'ناشناخته'} · {album or 'بدون آلبوم'}"); item.setData(Qt.ItemDataRole.AccessibleTextRole,f"{title}, {artist or 'ناشناخته'}"+(", در حال پخش" if path==self.current_path else "")); self.queue_list.addItem(item)
            if path==self.current_path:item.setSelected(True)
        try:
            current_index=self.queue_paths.index(self.current_path)
            next_path=self.queue_paths[current_index+1] if current_index+1<len(self.queue_paths) else ""
        except ValueError:next_path=self.queue_paths[0] if self.queue_paths else ""
        next_row=self.conn.execute("SELECT title FROM tracks WHERE path=?",(next_path,)).fetchone() if next_path else None
        next_title=(next_row[0] or Path(next_path).stem) if next_row else (Path(next_path).stem if next_path else "")
        self.queue_hint.setText(f"{len(self.queue_paths)} آهنگ در صف"+(f"  ·  بعدی: {next_title}" if next_title else "  ·  برای شروع، از کتابخانه آهنگ اضافه کن"))
    def _queue_order_changed(self,paths):
        self.queue_paths=[path for path in paths if path]
        self._queue_mode="custom"; self._schedule_crossfade(); self._save_playback_state()
        self.status.setText("ترتیب صف به‌روزرسانی شد")
    def _create_playlist(self):
        name,ok=QInputDialog.getText(self,"پلی‌لیست جدید","نام پلی‌لیست:")
        name=name.strip()
        if not ok or not name:return
        try:
            cursor=self.conn.execute("INSERT INTO playlists(name) VALUES(?)",(name,)); self.conn.commit()
            self._active_playlist_id=cursor.lastrowid; self._queue_mode="playlist"; self.queue_paths=[]
            self._refresh_playlist_picker(); self._refresh_queue_view(); self._save_playback_state()
        except sqlite3.IntegrityError:
            QMessageBox.warning(self,"نام تکراری","پلی‌لیستی با این نام وجود دارد.")
    def _save_playlist(self):
        if self._active_playlist_id is None:
            name,ok=QInputDialog.getText(self,"ذخیرهٔ پلی‌لیست","نام پلی‌لیست:")
            name=name.strip()
            if not ok or not name:return
            try:
                cursor=self.conn.execute("INSERT INTO playlists(name) VALUES(?)",(name,)); self._active_playlist_id=cursor.lastrowid
            except sqlite3.IntegrityError:
                QMessageBox.warning(self,"نام تکراری","پلی‌لیستی با این نام وجود دارد."); return
        try:
            self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?",(self._active_playlist_id,))
            self.conn.executemany("INSERT INTO playlist_tracks(playlist_id,position,track_path) VALUES(?,?,?)",
                ((self._active_playlist_id,index,path) for index,path in enumerate(self.queue_paths)))
            self.conn.commit(); self._queue_mode="playlist"; self._refresh_playlist_picker()
            self._save_playback_state(); self.status.setText("پلی‌لیست ذخیره شد")
        except sqlite3.Error as exc:
            self.conn.rollback(); QMessageBox.warning(self,"ذخیرهٔ پلی‌لیست ناموفق بود",str(exc))
    def _choose_playlist_export(self):
        playlist_id=self._active_playlist_id
        if playlist_id is None:return
        name=self.playlist_combo.currentText()
        path,_=QFileDialog.getSaveFileName(self,"خروجی گرفتن از پلی‌لیست",f"{name}.m3u","M3U playlist (*.m3u)")
        if not path:return
        if Path(path).suffix.lower()!=".m3u":path+=".m3u"
        try:
            count=self._export_playlist(path,playlist_id)
            self.status.setText(f"پلی‌لیست با {count} آهنگ خروجی گرفته شد")
        except (OSError,sqlite3.Error) as exc:
            QMessageBox.warning(self,"خروجی پلی‌لیست ناموفق بود",str(exc))
    def _export_playlist(self,path,playlist_id=None):
        playlist_id=playlist_id or self._active_playlist_id
        if playlist_id is None:raise ValueError("ابتدا یک پلی‌لیست انتخاب کن.")
        base=Path(path).resolve().parent
        rows=self.conn.execute("SELECT pt.track_path,t.title,t.artist,t.duration FROM playlist_tracks pt LEFT JOIN tracks t ON t.path=pt.track_path WHERE pt.playlist_id=? ORDER BY pt.position",(playlist_id,)).fetchall()
        lines=["#EXTM3U"]
        for track_path,title,artist,duration in rows:
            label=" - ".join(part for part in (artist,title) if part) or Path(track_path).stem
            try:duration=int(float(duration or -1))
            except (TypeError,ValueError):duration=-1
            lines.append(f"#EXTINF:{duration},{label}")
            try:stored_path=os.path.relpath(track_path,base).replace("\\","/")
            except ValueError:stored_path=os.path.abspath(track_path).replace("\\","/")
            lines.append(stored_path)
        Path(path).write_text("\n".join(lines)+"\n",encoding="utf-8-sig")
        return len(rows)
    def _choose_playlist_import(self):
        path,_=QFileDialog.getOpenFileName(self,"واردکردن پلی‌لیست","","M3U playlist (*.m3u *.m3u8)")
        if not path:return
        try:
            imported,missing=self._import_playlist(path)
            self.status.setText(f"پلی‌لیست وارد شد: {imported} آهنگ"+(f"؛ {missing} مسیر پیدا نشد" if missing else ""))
            QMessageBox.information(self,"واردکردن پلی‌لیست",f"{imported} آهنگ وارد شد."+(f"\n{missing} مسیر پیدا نشد و رد شد." if missing else ""))
        except (OSError,ValueError,sqlite3.Error) as exc:
            QMessageBox.warning(self,"واردکردن پلی‌لیست ناموفق بود",str(exc))
    def _import_playlist(self,path):
        source=Path(path)
        lines=source.read_text(encoding="utf-8-sig").splitlines()
        if not any(line.strip() and not line.lstrip().startswith("#") for line in lines):
            raise ValueError("فایل M3U هیچ مسیر آهنگی ندارد.")
        by_name={}
        for (known_path,) in self.conn.execute("SELECT path FROM tracks"):
            by_name.setdefault(Path(known_path).name.casefold(),[]).append(known_path)
        resolved=[]; missing=0
        for raw in lines:
            raw=raw.strip()
            if not raw or raw.startswith("#"):continue
            candidate=Path(raw.replace("/",os.sep))
            if not candidate.is_absolute():candidate=source.parent/candidate
            candidate=os.path.abspath(candidate)
            if os.path.isfile(candidate):selected=candidate
            else:
                matches=by_name.get(Path(raw).name.casefold(),[])
                selected=matches[0] if len(matches)==1 and os.path.isfile(matches[0]) else None
            if selected is None:missing+=1
            elif selected not in resolved:resolved.append(selected)
        if not resolved:return 0,missing
        base_name=source.stem.strip() or "پلی‌لیست واردشده"
        name=base_name; suffix=2
        while self.conn.execute("SELECT 1 FROM playlists WHERE name=? COLLATE NOCASE",(name,)).fetchone():
            name=f"{base_name} ({suffix})"; suffix+=1
        try:
            cursor=self.conn.execute("INSERT INTO playlists(name) VALUES(?)",(name,)); playlist_id=cursor.lastrowid
            self.conn.executemany("INSERT INTO playlist_tracks(playlist_id,position,track_path) VALUES(?,?,?)",((playlist_id,index,track) for index,track in enumerate(resolved)))
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback(); raise
        self._active_playlist_id=playlist_id; self._queue_mode="playlist"; self.queue_paths=resolved
        self._refresh_playlist_picker(); self._refresh_queue_view(); self._save_playback_state()
        return len(resolved),missing
    def _delete_playlist(self):
        playlist_id=self._active_playlist_id
        if playlist_id is None:return
        name=self.playlist_combo.currentText()
        if QMessageBox.question(self,"حذف پلی‌لیست",f"پلی‌لیست «{name}» حذف شود؟",QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:return
        self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?",(playlist_id,)); self.conn.execute("DELETE FROM playlists WHERE id=?",(playlist_id,)); self.conn.commit()
        self._active_playlist_id=None; self._queue_mode="custom"; self.queue_paths=[]
        self._refresh_playlist_picker(); self._refresh_queue_view(); self._save_playback_state()
    def _move_queue_item(self,offset):
        row=self.queue_list.currentRow(); target=row+offset
        if row<0 or not 0<=target<len(self.queue_paths):return
        self.queue_paths[row],self.queue_paths[target]=self.queue_paths[target],self.queue_paths[row]
        self._queue_mode="custom"
        self._refresh_queue_view(); self.queue_list.setCurrentRow(target); self._schedule_crossfade(); self._save_playback_state()
    def _remove_queue_item(self):
        row=self.queue_list.currentRow()
        if row<0:return
        removed=self.queue_paths.pop(row)
        if removed==self.current_path and (self.engine._playing or self.engine._paused):
            self.queue_paths.insert(row,removed)
            self.status.setText("آهنگ در حال پخش تا پایان در صف باقی می‌ماند")
        self._queue_mode="custom"
        self._refresh_queue_view(); self._schedule_crossfade(); self._save_playback_state()
    def _clear_queue(self):
        self.queue_paths=[self.current_path] if self.current_path and (self.engine._playing or self.engine._paused) else []
        self._queue_mode="custom"
        self._refresh_playlist_picker(); self._refresh_queue_view(); self._schedule_crossfade(); self._save_playback_state()
    def _play_queue_item(self,item):
        path=item.data(Qt.ItemDataRole.UserRole)
        if path:self._play_path(path,manual=True)
    def _play_queue(self):
        if self.queue_paths:
            row=self.queue_list.currentRow()
            self._play_path(self.queue_paths[row if row>=0 else 0],manual=True)
    def _add_to_queue(self,path,play_next=False):
        if not os.path.isfile(path):return
        if play_next and path==self.current_path:return
        if path in self.queue_paths:
            if not play_next:return
            self.queue_paths.remove(path)
        if play_next:
            try:index=self.queue_paths.index(self.current_path)+1
            except ValueError:index=0
            self.queue_paths.insert(index,path)
        else:self.queue_paths.append(path)
        self._queue_mode="custom"
        self._refresh_playlist_picker(); self._refresh_queue_view(); self._schedule_crossfade()
        self.status.setText("آهنگ به ابتدای صف اضافه شد" if play_next else "آهنگ به انتهای صف اضافه شد")
    def _add_track_to_playlist(self,playlist_id,path):
        existing=self.conn.execute("SELECT COALESCE(MAX(position),-1)+1 FROM playlist_tracks WHERE playlist_id=?",(playlist_id,)).fetchone()[0]
        self.conn.execute("INSERT OR IGNORE INTO playlist_tracks(playlist_id,position,track_path) VALUES(?,?,?)",(playlist_id,existing,path)); self.conn.commit()
        if playlist_id==self._active_playlist_id:self._playlist_selected(self.playlist_combo.currentIndex())
        self.status.setText("آهنگ به پلی‌لیست اضافه شد")
    def _play_item(self,item):
        self._grid_favorite_timer.stop(); self._pending_grid_action=None
        path=item.data(Qt.ItemDataRole.UserRole)
        if path:
            self._queue_mode="library"; self._active_playlist_id=None
            self.queue_paths=[self.track_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.track_list.count())]
            self._refresh_playlist_picker(); self._refresh_queue_view(); self._play_path(path,manual=True)
    def _load_current_lyrics(self):
        self.lyrics_list.clear(); path=self.current_path; self.lyrics_times=[]
        tint=QColor(getattr(self,"accent_color","#1ed760")).darker(260).name(); base="#f3f5f8" if self.theme_mode=="light" else "#111315"
        self.lyrics_page.setStyleSheet(f"QWidget{{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {tint},stop:0.62 {base});}} QListWidget#lyricsList{{background:transparent;border:0;}} QListWidget#lyricsList::item{{padding:12px 16px;min-height:30px;border-radius:10px;}} QListWidget#lyricsList::item:selected{{background:{QColor(getattr(self,'accent_color','#1ed760')).name()};color:#111;font-weight:800;}}")
        row=self.conn.execute("SELECT title,artist FROM tracks WHERE path=?",(path,)).fetchone() if path else None
        self.lyrics_title.setText((row[0] or Path(path).stem) if row else "متن آهنگ")
        self.lyrics_artist.setText(row[1] or "هنرمند نامشخص" if row else "یک آهنگ پخش کن تا متن آن نمایش داده شود")
        self.lyrics_cover.setPixmap(self._cover_pixmap(path,92))
        if not path:
            self.lyrics_empty.setText("یک آهنگ پخش کن تا متن آن نمایش داده شود."); return
        text=""
        if path:
            try:
                audio=MutagenFile(path)
                tags=getattr(audio,"tags",None)
                if tags:
                    for key,value in tags.items():
                        if str(key).upper().startswith("USLT"):
                            text=getattr(value,"text",""); break
                        if str(key).casefold() in {"lyrics","unsyncedlyrics","lyric"}:
                            text=str(value[0] if isinstance(value,(list,tuple)) else value); break
            except Exception:
                text=""
        if not text:
            self.lyrics_empty.setText("برای این فایل متنِ تعبیه‌شده پیدا نشد. فایل LRC هم‌نام را کنار آهنگ بگذار تا نمایش داده شود.")
            if path:
                lrc=Path(path).with_suffix(".lrc")
                try:
                    if lrc.is_file():text=lrc.read_text(encoding="utf-8-sig")
                except (OSError,UnicodeError):text=""
        timed=[]; plain=[]
        for line in text.splitlines():
            matches=list(re.finditer(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]",line))
            lyric=re.sub(r"\[[^\]]+\]","",line).strip()
            if matches:
                for match in matches:
                    fraction=(match.group(3) or "0"); millis=int(fraction.ljust(3,"0")[:3])
                    timed.append((int(match.group(1))*60000+int(match.group(2))*1000+millis,lyric))
            elif lyric:plain.append(lyric)
        if timed:
            for stamp,lyric in sorted(timed):
                item=QListWidgetItem(lyric or "♪"); item.setData(Qt.ItemDataRole.UserRole,stamp); self.lyrics_list.addItem(item)
            self.lyrics_times=[self.lyrics_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.lyrics_list.count())]
            self.lyrics_empty.setText("متن LRC هم‌زمان با پخش پیمایش می‌شود.")
        else:
            for lyric in plain:self.lyrics_list.addItem(QListWidgetItem(lyric))
            self.lyrics_empty.setText("متن معمولی است و زمان‌بندی ندارد." if plain else self.lyrics_empty.text())
    def _sync_lyrics(self,ms):
        if not getattr(self,"lyrics_times",None) or self._active_page_index!=7:return
        active=-1
        for i,stamp in enumerate(self.lyrics_times):
            if stamp>ms:break
            active=i
        if active>=0:
            self.lyrics_list.setCurrentRow(active); self.lyrics_list.scrollToItem(self.lyrics_list.item(active),QListWidget.ScrollHint.PositionAtCenter)
    def _play_path(self,path,manual=False):
        if self.tracker.active:self.tracker.finish("skipped",self.engine.output_frames_total)
        self._pending_manual_selection=bool(manual)
        self.seek_slider.reset_waveform()
        self.current_path=path; t,a,al,_,_,_,_=self._metadata(path); self.track_label.setText(t); self.artist_label.setText(f"{a or 'ناشناخته'}  •  {al or 'بدون آلبوم'}"); self._set_player_cover(path)
        if getattr(self,"_active_page_index",-1)==7:self._load_current_lyrics()
        if hasattr(self,"mini_player"):self._sync_mini_player()
        self.engine.set_source(path); self._pending_start_frames=self.engine.output_frames_total
        self.engine.play(); self._highlight_current()
        self._schedule_crossfade()
        if getattr(self,"_active_page_index",-1)==6:self._refresh_home()
    def _output_device_changed(self,index):
        self.settings_output_device.blockSignals(True); self.settings_output_device.setCurrentIndex(max(0,self.settings_output_device.findData(self.output_device.itemData(index)))); self.settings_output_device.blockSignals(False)
        try:self.engine.set_output_device(self.output_device.itemData(index))
        except Exception as exc:
            QMessageBox.warning(self,"دستگاه خروجی",f"بازکردن دستگاه انتخاب‌شده ناموفق بود:\n{exc}")
            self.output_device.blockSignals(True); i=self.output_device.findData(self.engine.output_device); self.output_device.setCurrentIndex(max(0,i)); self.output_device.blockSignals(False)
            self.settings_output_device.blockSignals(True); self.settings_output_device.setCurrentIndex(max(0,self.settings_output_device.findData(self.engine.output_device))); self.settings_output_device.blockSignals(False)
    def _settings_output_changed(self,index):
        device_id=self.settings_output_device.itemData(index); target=self.output_device.findData(device_id)
        if target>=0 and target!=self.output_device.currentIndex():self.output_device.setCurrentIndex(target)
    def _crossfade_changed(self,seconds):
        self.engine.set_crossfade(seconds*1000)
        self.crossfade_value.setText("خاموش" if seconds==0 else f"{seconds} ثانیه")
        self._schedule_crossfade()
    def _schedule_crossfade(self):
        if not self.engine.crossfade_ms or not self.queue_paths:return
        try:i=self.queue_paths.index(self.current_path)
        except ValueError:i=-1
        if self.shuffle:
            choices=[p for p in self.queue_paths if p!=self.current_path]; path=random.choice(choices) if choices else ""
        elif i+1<len(self.queue_paths):path=self.queue_paths[i+1]
        elif self.repeat_mode==1:path=self.queue_paths[0]
        else:path=""
        self.engine.set_next_source(path)
    def _crossfade_finished(self,path,frame_counter):
        if self.tracker.active:self.tracker.finish("completed",frame_counter)
        self.current_path=path
        self.seek_slider.reset_waveform()
        self._set_player_cover(path)
        t,a,al,_,_,_,_=self._metadata(path); self.track_label.setText(t); self.artist_label.setText(f"{a or 'ناشناخته'}  •  {al or 'بدون آلبوم'}")
        if getattr(self,"_active_page_index",-1)==7:self._load_current_lyrics()
        self._pending_manual_selection=False; self._pending_start_frames=frame_counter
        if self.is_playing:self.tracker.start(path,self.engine.duration_ms,False,frame_counter)
        self._highlight_current(); self._schedule_crossfade()
    def _highlight_current(self):
        for listing in (self.track_list,self.fav_list,self.queue_list):
            for i in range(listing.count()):listing.item(i).setSelected(listing.item(i).data(Qt.ItemDataRole.UserRole)==self.current_path)
    def _add_track_menu_actions(self,menu,path):
        add_queue=menu.addAction("افزودن به انتهای صف")
        play_next=menu.addAction("پخش بعدی")
        submenu=menu.addMenu("افزودن به پلی‌لیست")
        playlists=self.conn.execute("SELECT id,name FROM playlists ORDER BY name COLLATE NOCASE").fetchall()
        if not playlists:submenu.setEnabled(False)
        for playlist_id,name in playlists:
            action=submenu.addAction(name); action.setData(playlist_id)
        return add_queue,play_next,submenu
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
        for listing in (getattr(self,"home_recent_list",None),getattr(self,"home_popular_list",None),getattr(self,"home_smart_cards",None),getattr(self,"collection_list",None),getattr(self,"collection_related",None)):
            if not listing:continue
            for i in range(listing.count()):
                item=listing.item(i)
                if item.data(Qt.ItemDataRole.UserRole)==path or (listing is getattr(self,"collection_related",None) and item.data(Qt.ItemDataRole.UserRole+2)==path):item.setData(Qt.ItemDataRole.UserRole+4,bool(favorite))
            listing.viewport().update()
        if getattr(self,"_active_page_index",-1)==6:self._refresh_home()
        if getattr(self,"collection_page",None) and self.collection_page.isVisible():self._render_collection_tracks()
        if self.stack.currentIndex()==3:self._refresh_smart_overview()
    def _track_context_menu(self,listing,pos):
        item=listing.itemAt(pos)
        if not item:return
        path=item.data(Qt.ItemDataRole.UserRole)
        row=self.conn.execute("SELECT favorite,artist,album FROM tracks WHERE path=?",(path,)).fetchone()
        menu=QMenu(self)
        favorite_action=menu.addAction("حذف از علاقه‌مندی‌ها" if row and row[0] else "افزودن به علاقه‌مندی‌ها")
        edit_action=menu.addAction("ویرایش کامل اطلاعات و کاور…")
        artist_action=menu.addAction(f"نمایش هنرمند · {row[1]}") if row and row[1] else None
        album_action=menu.addAction(f"نمایش آلبوم · {row[2]}") if row and row[2] else None
        menu.addSeparator(); add_queue,play_next,playlist_menu=self._add_track_menu_actions(menu,path)
        selected=menu.exec(listing.viewport().mapToGlobal(pos))
        if selected==favorite_action:self._set_favorite(path,not bool(row and row[0]))
        elif selected==edit_action:self._edit_track_metadata(path)
        elif selected==artist_action:self._open_collection("artist",row[1])
        elif selected==album_action:self._open_collection("album",row[2])
        elif selected==add_queue:self._add_to_queue(path)
        elif selected==play_next:self._add_to_queue(path,play_next=True)
        elif selected and selected in playlist_menu.actions():self._add_track_to_playlist(selected.data(),path)
    def _edit_track_metadata(self,path):
        if not os.path.isfile(path):return
        dialog=MetadataEditorDialog(path,self._embedded_cover_data(path),self)
        if dialog.exec()!=dialog.DialogCode.Accepted:return
        try:
            values=dialog.values()
            write_metadata(path,values,dialog.cover_data,dialog.remove_cover)
            duration=self._metadata(path)[3]
            self.conn.execute("UPDATE tracks SET title=?,artist=?,album=?,genre=?,year=?,track_number=?,duration=? WHERE path=?",
                (values["title"] or Path(path).stem,values["artist"],values["album"],values["genre"],values["year"],values["track_number"],duration,path))
            self.conn.commit()
            self._cover_art_cache.pop(path,None)
            self._load_library()
            if path==self.current_path:
                self.track_label.setText(values["title"] or Path(path).stem)
                self.artist_label.setText(f"{values['artist'] or 'ناشناخته'}  •  {values['album'] or 'بدون آلبوم'}")
                self._set_player_cover(path)
            if self.stack.currentIndex()==3:self._refresh_smart_overview()
            self.status.setText("اطلاعات فایل ذخیره شد")
        except (OSError, ValueError, sqlite3.Error, Exception) as exc:
            QMessageBox.warning(self,"ذخیرهٔ اطلاعات ناموفق بود",str(exc))
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
        self.build_smart_btn.setEnabled(False); self.home_smart_btn.setEnabled(False); self.smart_status.setText("در حال تحلیل محلی کتابخانه و تاریخچه…")
        excluded=(self.current_path,) if self.engine._playing or self.engine._paused else ()
        worker=SmartDJWorker(str(self.database_path),self.smart_mode.currentText(),self.diversity_slider.value(),excluded,self)
        worker.ready.connect(self._smart_playlist_ready); worker.failed.connect(self._smart_playlist_failed)
        worker.finished.connect(lambda w=worker:self._smart_worker_finished(w))
        self.smart_worker=worker; worker.start()
    def _smart_playlist_ready(self,payload):
        self.build_smart_btn.setEnabled(True); self.home_smart_btn.setEnabled(True)
        home_only=getattr(self,"_home_smart_only",False); self._home_smart_only=False
        recommendations=payload.get("recommendations",[])
        self.smart_list.clear(); self.smart_queue=[]
        for row in recommendations:
            text=f"{row['title']}  ·  {row['artist']}\n{row['reason']}  ·  امتیاز {row['score']:.0f}"
            item=QListWidgetItem(self._cover_icon(row["path"]),text); item.setData(Qt.ItemDataRole.UserRole,row["path"]); item.setData(Qt.ItemDataRole.UserRole+1,row); self.smart_list.addItem(item); self.smart_queue.append(row["path"])
        if not home_only:
            if self.engine._playing or self.engine._paused:self.queue_paths=[self.current_path]+[p for p in self.smart_queue if p!=self.current_path]
            else:self.queue_paths=list(self.smart_queue)
            self._queue_mode="smart"; self._active_playlist_id=None
            self._refresh_playlist_picker(); self._refresh_queue_view()
            if self.engine._playing:self._schedule_crossfade()
        self._show_smart_payload(payload)
        self.smart_status.setText(f"{len(recommendations)} آهنگ پیشنهاد شد؛ آهنگ در حال پخش قطع نشد." if recommendations else "کتابخانه آهنگ قابل‌دسترسی برای پیشنهاد ندارد.")
        self._refresh_home()
    def _smart_playlist_failed(self,message):
        self.build_smart_btn.setEnabled(True); self.home_smart_btn.setEnabled(True); self._home_smart_only=False; self.smart_status.setText("ساخت فهرست ناموفق بود.")
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
            self.history_list.addItem(QListWidgetItem(self._cover_icon(path),f"{title}  ·  {labels.get(kind,kind)}{extra}  ·  {str(stamp)[:16]}"))
        for widget,key in ((self.top_played_list,"top_played"),(self.top_skipped_list,"top_skipped")):
            widget.clear()
            for path,title,artist,count in payload[key]:widget.addItem(QListWidgetItem(self._cover_icon(path),f"{title}  ·  {artist}  ·  {count} بار"))
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
        self._queue_mode="smart"; self._active_playlist_id=None
        self._refresh_playlist_picker(); self._refresh_queue_view()
    def _smart_context_menu(self,pos):
        item=self.smart_list.itemAt(pos)
        if not item:return
        self.smart_list.setCurrentItem(item); menu=QMenu(self)
        path=item.data(Qt.ItemDataRole.UserRole)
        play=menu.addAction("پخش این آهنگ"); favorite=menu.addAction("افزودن به علاقه‌مندی‌ها"); remove=menu.addAction("حذف از پیشنهادها")
        menu.addSeparator(); add_queue,play_next,playlist_menu=self._add_track_menu_actions(menu,path)
        selected=menu.exec(self.smart_list.viewport().mapToGlobal(pos))
        if selected==play:self._play_selected_suggestion()
        elif selected==favorite:self._favorite_selected_suggestion()
        elif selected==remove:self._remove_suggestion(item)
        elif selected==add_queue:self._add_to_queue(path)
        elif selected==play_next:self._add_to_queue(path,play_next=True)
        elif selected and selected in playlist_menu.actions():self._add_track_to_playlist(selected.data(),path)
    def _play_smart_item(self,item):self._play_path(item.data(Qt.ItemDataRole.UserRole),manual=True)
    def _position_changed(self,ms):
        self._sync_fullscreen_transport()
        self._sync_tracker_frames()
        self._sync_lyrics(ms)
        if not self.seek_slider.isSliderDown():self.seek_slider.setValue(ms)
        self.elapsed.setText(self._fmt(ms))
        if getattr(self,"_active_page_index",-1)==6:self._refresh_home_resume()
    def _duration_changed(self,ms):
        self.duration=ms; self._sync_fullscreen_transport(); self.seek_slider.setRange(0,max(0,ms)); self.total.setText(self._fmt(ms))
        if getattr(self,"_active_page_index",-1)==6:self._refresh_home_resume()
    def _play_state(self,playing,force=False):
        if not playing and self._ignore_end_state_signal and not force:
            self._ignore_end_state_signal=False
            return
        if not playing and getattr(self.engine,"_end_notified",False) and not force:
            return
        self.is_playing=playing; self._sync_fullscreen_transport(); self.play_btn.setIcon(icon("pause" if playing else "play","#101010",24)); self.play_btn.setToolTip("مکث" if playing else "پخش")
        if hasattr(self,"mini_player"):self.mini_play.setIcon(icon("pause" if playing else "play"))
        self.status.setText("در حال پخش" if playing else ("آمادهٔ پخش" if not self.engine._paused else "مکث‌شده"))
        if getattr(self,"_active_page_index",-1)==6:self._refresh_home_resume()
        if playing:
            self.tracker.start(self.current_path,self.engine.duration_ms,self._pending_manual_selection,self._pending_start_frames)
            self._pending_manual_selection=False
        elif getattr(self.engine,"_paused",False):
            self.tracker.pause(self.engine.output_frames_total)
        elif self.tracker.active:
            self.tracker.finish("stopped",self.engine.output_frames_total)
        self._save_playback_state()
    def _toggle_playback(self):
        if self.engine._playing:
            self.engine.toggle(); return
        if self.engine._paused:
            self._pending_start_frames=self.engine.output_frames_total
            self.engine.toggle(); return
        if self._queue_mode=="smart" and self.smart_queue and self.current_path not in self.smart_queue:
            self._play_path(self.smart_queue[0],manual=False); return
        if self.engine.duration_ms and self.engine.position_ms>=self.engine.duration_ms:
            self.engine.set_position(0)
        self._pending_start_frames=self.engine.output_frames_total
        self.engine.toggle()
    def _sync_tracker_frames(self):self.tracker.sync_frames(self.engine.output_frames_total)
    def _spectrum(self,levels):
        self.spectrum.set_levels(levels)
    def _waveform_sample(self,position_ms,duration_ms,level):
        self.seek_slider.set_wave_sample(position_ms,duration_ms,level)
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
        self.shuffle_btn.setIcon(icon("shuffle","#1ed760" if self.shuffle else "#b3b3b3",23))
        self.status.setText("پخش تصادفی روشن" if self.shuffle else "پخش تصادفی خاموش")
    def _cycle_repeat(self):
        self.repeat_mode=(self.repeat_mode+1)%3; self.repeat_btn.setIcon(icon("repeat","#1ed760" if self.repeat_mode else "#b3b3b3",23))
        self.repeat_btn.setToolTip(["تکرار خاموش","تکرار صف","تکرار آهنگ"][self.repeat_mode])
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
        self._closing_main=True
        if hasattr(self,"mini_player"):self.mini_player.hide()
        try:
            if self.smart_worker and self.smart_worker.isRunning():self.smart_worker.wait()
            if hasattr(self,"_watched_scan_timer"):self._watched_scan_timer.stop()
            if self._watch_worker and self._watch_worker.isRunning():
                self._watch_worker.requestInterruption(); self._watch_worker.wait(5000)
            self._save_playback_state()
            if hasattr(self,"_state_save_timer"):self._state_save_timer.stop()
            self._sync_tracker_frames(); self.tracker.finish("stopped",self.engine.output_frames_total)
            self.engine.close();self.conn.close()
        finally:event.accept()
