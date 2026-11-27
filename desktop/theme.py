"""Achromatic Ledger tokens for the native ShareDownload screens."""

GROUND = "#f8f8f8"
SURFACE = "#ffffff"
RAISED = "#eeeeee"
BORDER = "#e6e6e6"
RULE = "#f0f0f0"
CONTROL = "#d4d4d4"
INK = "#0a0a0a"
MUTED = "#626262"
PRIMARY = "#141414"
DATA = "#1d5fbf"
POSITIVE = "#247044"
NEGATIVE = "#9b292c"
WARNING = "#82400e"

STYLESHEET = f"""
QWidget {{ font-size: 14px; color: {INK}; }}
QMainWindow, QDialog, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget,
QFrame#masthead {{ background: {GROUND}; }}
QFrame#masthead {{ border-bottom: 1px solid {BORDER}; }}
QFrame#transfer, QFrame#report, QGroupBox {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px;
}}
QFrame#resumeNotice {{ background: {RAISED}; border-radius: 6px; }}
QLabel#brand {{ font-size: 16px; font-weight: 600; }}
QLabel#brandMark {{ font-size: 20px; font-weight: 600; }}
QLabel#pageTitle {{ font-size: 24px; font-weight: 600; }}
QLabel#sectionTitle {{ font-size: 16px; font-weight: 600; }}
QLabel#percent {{ font-size: 30px; font-weight: 500; }}
QLabel#muted, QLabel#caption {{ color: {MUTED}; }}
QLabel#caption {{ font-size: 12px; }}
QLabel#state {{ font-weight: 600; }}
QLabel#positive {{ color: {POSITIVE}; font-weight: 600; }}
QLabel#warning {{ color: {WARNING}; }}
QLabel#formError, QLabel#negative {{ color: {NEGATIVE}; }}
QGroupBox {{ margin-top: 13px; padding: 16px 12px 12px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {SURFACE}; border: 1px solid {CONTROL}; border-radius: 6px;
    padding: 6px 8px; min-height: 22px; selection-background-color: #dfe3e5;
    selection-color: {INK}; placeholder-text-color: {MUTED};
}}
QPushButton {{
    background: {SURFACE}; border: 1px solid {CONTROL}; border-radius: 6px;
    padding: 6px 12px; min-height: 22px; font-weight: 500;
}}
QPushButton:hover {{ background: {RAISED}; border-color: #adadad; }}
QPushButton:pressed {{ background: #dedede; }}
QPushButton#primary, QMessageBox QPushButton:default {{
    background: {PRIMARY}; color: {SURFACE}; border-color: {PRIMARY};
}}
QPushButton#primary:hover, QMessageBox QPushButton:default:hover {{ background: #353535; }}
QPushButton#primary:focus, QMessageBox QPushButton:default:focus {{
    border: 2px solid #929292; padding: 5px 11px;
}}
QPushButton#danger {{ color: {NEGATIVE}; }}
QPushButton#nav {{ background: transparent; border-color: transparent; }}
QPushButton#nav:hover, QPushButton#nav:checked {{ background: {RAISED}; }}
QPushButton#nav:checked {{ font-weight: 600; }}
QPushButton:focus, QPushButton#nav:focus {{
    border: 2px solid #565656; padding: 5px 11px;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 2px solid #565656; padding: 5px 7px;
}}
QPushButton:disabled, QPushButton#primary:disabled, QLineEdit:disabled,
QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    background: {RAISED}; color: {MUTED}; border-color: {CONTROL};
}}
QProgressBar {{
    background: {RAISED}; border: none; border-radius: 4px;
    min-height: 8px; max-height: 8px;
}}
QProgressBar::chunk {{ background: {DATA}; border-radius: 4px; }}
QTableView, QListWidget {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px;
    gridline-color: {RULE}; selection-background-color: {RAISED}; selection-color: {INK};
}}
QTableView:focus, QListWidget:focus {{ border-color: #606060; }}
QHeaderView::section {{
    background: {SURFACE}; color: {MUTED}; border: none;
    border-bottom: 1px solid {BORDER}; padding: 8px 6px; font-size: 12px;
}}
QTableView::item {{ padding: 4px 6px; border-bottom: 1px solid {RULE}; }}
QListWidget::item {{ padding: 7px 10px; border-bottom: 1px solid {RULE}; }}
"""
