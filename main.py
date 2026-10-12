import sys
from PySide6.QtWidgets import QApplication
from nova_music.ui.main_window import MainWindow

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("NOVA Music")
    app.setOrganizationName("NOVA")
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
