"""Visual tokens for the ShareDownload desktop interface."""

STYLESHEET = """
QWidget { font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif; font-size: 12px; color: #172439; }
QMainWindow, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget { background: #f3f6fa; }

/* Dialog and Popup Modals */
QDialog, QMessageBox { background-color: #ffffff; color: #0f172a; border-radius: 8px; }
QMessageBox QLabel { background: transparent; color: #1e293b; font-size: 13px; min-height: 40px; }
QMessageBox QPushButton { background-color: #ffffff; color: #1e293b; border: 1px solid #cbd5e1; border-radius: 6px; padding: 7px 18px; min-width: 85px; font-weight: 600; font-size: 12px; }
QMessageBox QPushButton:hover { background-color: #f1f5f9; border-color: #94a3b8; }
QMessageBox QPushButton:default { background-color: #2563d9; color: #ffffff; border: 1px solid #2563d9; }
QMessageBox QPushButton:default:hover { background-color: #1d4ed8; }

QFrame#sidebar { background: #112641; border: none; }
QFrame#sidebar QLabel { background: transparent; color: #c3d1e3; }
QLabel#brand { color: #ffffff; font-size: 18px; font-weight: 700; }
QPushButton#nav { background: transparent; border: 0; border-radius: 8px; color: #c3d1e3; text-align: left; padding: 12px 15px; }
QPushButton#nav:checked { background: #233c60; color: #ffffff; font-weight: 700; }
QFrame#topbar, QFrame#card, QFrame#metric, QFrame#hero { background: #ffffff; border: 1px solid #e3e9f0; border-radius: 10px; }
QFrame#hero { background: #f0f5ff; border-color: #d7e3f6; }
QLabel#pageTitle { font-size: 24px; font-weight: 700; color: #172439; }
QLabel#formTitle { font-size: 20px; font-weight: 700; }
QLabel#fileTitle { font-size: 19px; font-weight: 700; color: #0f172a; }
QLabel#pathLabel { font-size: 13px; font-weight: 600; color: #1e3a8a; font-family: 'Consolas', 'Segoe UI', monospace; }
QLabel#metricValue { font-size: 22px; font-weight: 700; color: #0f172a; }
QLabel#percent { font-size: 30px; font-weight: 700; color: #174db7; }
QLabel#muted { color: #334155; font-size: 12px; }
QLabel#caption { color: #475569; font-size: 11px; font-weight: 500; }
QLabel#formError { color: #b02f39; font-weight: 600; }
QLabel#state { color: #174db7; font-weight: 700; letter-spacing: 0.5px; }
QLabel#warning { color: #8c5711; font-weight: 600; }
QGroupBox { background: #ffffff; border: 1px solid #e3e9f0; border-radius: 10px; margin-top: 16px; padding: 18px 14px 12px; font-weight: 700; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 5px; }
QLineEdit, QSpinBox, QDoubleSpinBox { background: white; border: 1px solid #ced8e5; border-radius: 6px; padding: 7px; min-height: 21px; selection-background-color: #2563d9; color: #0f172a; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus { border: 2px solid #2563d9; }
QPushButton { background: white; border: 1px solid #d4dfea; border-radius: 7px; padding: 9px 14px; font-weight: 600; color: #172439; }
QPushButton:hover { background: #edf4ff; border-color: #9cb9eb; }
QPushButton:focus { border: 2px solid #2563d9; }
QPushButton:disabled { color: #596b81; background: #edf0f4; }
QPushButton#primary { background: #2563d9; color: #ffffff; border-color: #2563d9; }
QPushButton#primary:hover { background: #174db7; }
QPushButton#primary:disabled { background: #edf0f4; color: #596b81; border-color: #d4dfea; }
QPushButton#danger { color: #a7353f; border-color: #dcb7bd; }
QPushButton#removeServer { padding: 7px 10px; }
QProgressBar { background: #dce7f8; border: 0; border-radius: 5px; min-height: 10px; max-height: 10px; text-align: center; }
QProgressBar::chunk { background: #2563d9; border-radius: 5px; }
QTableWidget { background: white; alternate-background-color: #f8fafd; border: 1px solid #e3e9f0; gridline-color: #e8edf3; selection-background-color: #edf4ff; selection-color: #172439; font-size: 12px; }
QHeaderView::section { background: #f1f5f9; color: #334155; font-weight: 700; border: 0; border-bottom: 2px solid #cbd5e1; padding: 10px; font-size: 12px; }
QListWidget { background: white; border: 0; }
QListWidget::item { border-bottom: 1px solid #ecf0f4; padding: 12px; color: #1e293b; }
QListWidget::item:selected { background: #edf4ff; color: #172439; }
"""
