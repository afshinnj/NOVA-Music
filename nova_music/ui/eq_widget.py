from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider, QCheckBox, QPushButton

BANDS = ["31", "62", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]

class EqualizerWidget(QWidget):
    bandChanged = Signal(int, float)
    enabledChanged = Signal(bool)
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel("اکولایزر ۱۰ بانده"); title.setObjectName("sectionTitle")
        self.enabled = QCheckBox("فعال"); self.enabled.setChecked(True); self.enabled.toggled.connect(self.enabledChanged)
        reset = QPushButton("بازنشانی"); reset.clicked.connect(self.reset)
        header.addWidget(title); header.addStretch(); header.addWidget(self.enabled); header.addWidget(reset)
        root.addLayout(header)
        row = QHBoxLayout(); self.sliders = []
        for index, name in enumerate(BANDS):
            col = QVBoxLayout(); value_label = QLabel("0 dB"); value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            slider = QSlider(Qt.Orientation.Vertical); slider.setRange(-12, 12); slider.setValue(0)
            slider.setTickInterval(3); slider.setTickPosition(QSlider.TickPosition.TicksBothSides)
            slider.valueChanged.connect(lambda value, i=index, label=value_label: self._changed(i, value, label))
            band = QLabel(name); band.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(value_label); col.addWidget(slider, 1, Qt.AlignmentFlag.AlignHCenter); col.addWidget(band)
            row.addLayout(col); self.sliders.append(slider)
        root.addLayout(row)
        note = QLabel("پردازش اکولایزر روی صدای خروجی اعمال می‌شود؛ محدوده هر باند −12 تا +12 dB.")
        note.setObjectName("muted"); root.addWidget(note)
    def _changed(self, index, value, label):
        label.setText(f"{value:+d} dB"); self.bandChanged.emit(index, float(value))
    def reset(self):
        for slider in self.sliders: slider.setValue(0)
