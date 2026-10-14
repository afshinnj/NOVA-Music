import sys
from pathlib import Path
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication
from nova_music.ui.main_window import MainWindow

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("NOVA Music")
    app.setOrganizationName("NOVA")
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / "nova_music" / "assets" / "nova_music.ico")))
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
