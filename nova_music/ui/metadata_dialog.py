"""Compact editor for a track's embedded metadata and artwork."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QFileDialog, QVBoxLayout, QDialogButtonBox)

from nova_music.audio.metadata import read_metadata


class MetadataEditorDialog(QDialog):
    def __init__(self, path, cover_data=None, parent=None):
        super().__init__(parent)
        self.path = path
        self.cover_data = cover_data
        self.remove_cover = False
        self.setWindowTitle("ویرایش اطلاعات آهنگ")
        self.setMinimumWidth(430)
        root = QVBoxLayout(self)
        root.addWidget(QLabel(Path(path).name))
        form = QFormLayout()
        self.fields = {}
        names = (("title", "عنوان"), ("artist", "خواننده / هنرمند"), ("album", "آلبوم"),
                 ("genre", "سبک موسیقی"), ("year", "سال"), ("track_number", "شمارهٔ قطعه"))
        values = read_metadata(path)
        for key, label in names:
            edit = QLineEdit(values[key])
            edit.setClearButtonEnabled(True)
            self.fields[key] = edit
            form.addRow(label, edit)
        root.addLayout(form)
        row = QHBoxLayout()
        self.preview = QLabel()
        self.preview.setFixedSize(100, 100)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background:#242424;border-radius:8px")
        self._show_cover()
        row.addWidget(self.preview)
        buttons = QVBoxLayout()
        choose = QPushButton("انتخاب کاور…")
        choose.clicked.connect(self._choose_cover)
        clear = QPushButton("حذف کاور")
        clear.clicked.connect(self._clear_cover)
        buttons.addWidget(choose); buttons.addWidget(clear); buttons.addStretch()
        row.addLayout(buttons); row.addStretch(); root.addLayout(row)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept); box.rejected.connect(self.reject); root.addWidget(box)

    def _show_cover(self):
        pix = QPixmap()
        if self.cover_data: pix.loadFromData(self.cover_data)
        if pix.isNull():
            self.preview.setText("بدون کاور")
            self.preview.setPixmap(QPixmap())
        else:
            self.preview.setText("")
            self.preview.setPixmap(pix.scaled(100, 100, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _choose_cover(self):
        path, _ = QFileDialog.getOpenFileName(self, "انتخاب تصویر کاور", "", "Images (*.jpg *.jpeg *.png)")
        if path:
            self.cover_data = Path(path).read_bytes()
            self.remove_cover = False
            self._show_cover()

    def _clear_cover(self):
        self.cover_data = None
        self.remove_cover = True
        self._show_cover()

    def values(self):
        result = {key: edit.text().strip() or None for key, edit in self.fields.items()}
        if result["year"]:
            result["year"] = int(result["year"])
        if result["track_number"]:
            result["track_number"] = int(result["track_number"])
        return result
