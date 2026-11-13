"""L'aspetto del programma: i colori dei due temi, le icone (disegnate in assets/icons e tinte del colore giusto) e il
foglio di stile. Tutto ciò che è colore, bordo, raggio e spaziatura sta qui, così le pagine restano uguali tra loro."""
from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

import utils

FONT = '"Segoe UI Variable Text", "Segoe UI", "Helvetica Neue", Arial, sans-serif'

THEMES = {
    "dark": dict(
        bg="#14151a", surface="#1c1e24", raised="#252830", input_bg="#181a1f", border="#2b2e37", border_strong="#3b3f4b",
        text="#e8e9ed", muted="#9a9fac", faint="#6c717e",
        accent="#4d88f8", accent_hover="#6b9cf9", accent_press="#3b73de", accent_soft="rgba(77,136,248,46)",
        accent_text="#8db4fb", on_accent="#ffffff", selection="rgba(77,136,248,64)",
        danger="#e5534b", danger_hover="#ec6d66", success="#43b46f", warning="#d9a233",
    ),
    "light": dict(
        bg="#f3f4f7", surface="#ffffff", raised="#eef0f4", input_bg="#ffffff", border="#e0e3e9", border_strong="#ccd0d8",
        text="#1b1d22", muted="#626875", faint="#98a0ac",
        accent="#2f6df0", accent_hover="#4a80f3", accent_press="#245bd0", accent_soft="rgba(47,109,240,30)",
        accent_text="#2358c9", on_accent="#ffffff", selection="rgba(47,109,240,44)",
        danger="#cf3f36", danger_hover="#dc5a52", success="#27985a", warning="#b27d16",
    ),
}

ICON_DIR = Path(utils.resource_path("assets/icons"))
_cache_dir = Path(tempfile.gettempdir()) / "ClipMerger_icone"


def pixmap(name: str, color: str, size: int = 20, scale: int = 2) -> QPixmap:
    """The icon `name` drawn in `color` (the SVGs are black outlines, tinted here), sharp on high-density screens."""
    renderer = QSvgRenderer(str(ICON_DIR / f"{name}.svg"))
    px = QPixmap(size * scale, size * scale)
    px.fill(Qt.transparent)
    painter = QPainter(px)
    renderer.render(painter, QRectF(0, 0, size * scale, size * scale))
    painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
    painter.fillRect(px.rect(), QColor(color))
    painter.end()
    px.setDevicePixelRatio(scale)
    return px


def icon(name: str, color: str, selected_color: str | None = None, size: int = 20) -> QIcon:
    ic = QIcon()
    ic.addPixmap(pixmap(name, color, size), QIcon.Normal)
    ic.addPixmap(pixmap(name, selected_color or color, size), QIcon.Selected)
    ic.addPixmap(pixmap(name, selected_color or color, size), QIcon.Active)
    return ic


def png_for_stylesheet(theme: str, name: str, color: str, size: int = 16) -> str:
    """A PNG of the icon on disk, because the stylesheet can only point at files."""
    folder = _cache_dir / theme
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}_{color.lstrip('#')}_{size}.png"
    if not path.exists():
        pixmap(name, color, size, 2).save(str(path), "PNG")
    return path.as_posix()


class PageHeader(QWidget):
    """The top of every page: its name, one line saying what it is for, and (on the right) its main buttons."""

    def __init__(self, title: str, subtitle: str = ""):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 4)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.title = QLabel(title)
        self.title.setObjectName("pageTitle")
        texts.addWidget(self.title)
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("pageSubtitle")
        self.subtitle.setWordWrap(True)
        self.subtitle.setVisible(bool(subtitle))
        texts.addWidget(self.subtitle)
        row.addLayout(texts, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        row.addLayout(self.actions)

    def add_action(self, widget: QWidget) -> None:
        self.actions.addWidget(widget)


def build_stylesheet(theme: str) -> str:
    c = THEMES[theme]
    down = png_for_stylesheet(theme, "giu", c["muted"], 12)
    up = png_for_stylesheet(theme, "su", c["muted"], 12)
    check = png_for_stylesheet(theme, "spunta", "#ffffff", 12)
    return f"""
* {{ font-family: {FONT}; font-size: 13px; }}
QMainWindow, QDialog {{ background-color: {c['bg']}; }}
QWidget {{ color: {c['text']}; }}
QLabel, QCheckBox, QRadioButton {{ background: transparent; color: {c['text']}; }}
QLabel#pageTitle {{ font-size: 22px; font-weight: 700; }}
QLabel#pageSubtitle {{ color: {c['muted']}; }}
QLabel#sideTitle {{ font-size: 17px; font-weight: 700; }}
QLabel#sideVersion {{ color: {c['faint']}; font-size: 11px; }}
QLabel#muted {{ color: {c['muted']}; }}

QGroupBox {{ background-color: {c['surface']}; border: 1px solid {c['border']}; border-radius: 10px; margin-top: 26px; padding: 16px 14px 12px 14px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 2px; top: 2px; padding: 0 2px; color: {c['muted']}; font-weight: 600; background: transparent; }}
QGroupBox::indicator, QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {c['border_strong']}; background-color: {c['input_bg']}; }}
QGroupBox::indicator:checked, QCheckBox::indicator:checked {{ background-color: {c['accent']}; border-color: {c['accent']}; image: url({check}); }}
QGroupBox::indicator:disabled, QCheckBox::indicator:disabled {{ background-color: {c['raised']}; border-color: {c['border']}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {c['faint']}; }}
QRadioButton::indicator {{ width: 16px; height: 16px; border-radius: 9px; border: 1px solid {c['border_strong']}; background-color: {c['input_bg']}; }}
QRadioButton::indicator:checked {{ border: 5px solid {c['accent']}; background-color: {c['on_accent']}; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background-color: {c['input_bg']}; border: 1px solid {c['border_strong']}; border-radius: 8px; padding: 6px 10px;
    color: {c['text']}; selection-background-color: {c['accent']}; selection-color: {c['on_accent']}; }}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{ border-color: {c['faint']}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {{ border-color: {c['accent']}; }}
QLineEdit:read-only {{ color: {c['muted']}; background-color: {c['raised']}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{ color: {c['faint']}; background-color: {c['raised']}; border-color: {c['border']}; }}
QComboBox {{ padding-right: 28px; }}
QComboBox::drop-down {{ border: none; width: 28px; subcontrol-origin: padding; subcontrol-position: center right; }}
QComboBox::down-arrow {{ image: url({down}); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background-color: {c['surface']}; color: {c['text']}; border: 1px solid {c['border_strong']}; border-radius: 8px;
    selection-background-color: {c['accent_soft']}; selection-color: {c['accent_text']}; outline: none; padding: 4px; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 4px; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border; width: 20px; border: none; background: transparent; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-position: top right; margin: 2px 2px 0 0; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-position: bottom right; margin: 0 2px 2px 0; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover, QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{ background-color: {c['raised']}; border-radius: 4px; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({up}); width: 10px; height: 10px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({down}); width: 10px; height: 10px; }}

QPushButton {{ background-color: {c['accent']}; color: {c['on_accent']}; border: 1px solid {c['accent']}; border-radius: 8px; padding: 7px 16px; font-weight: 600; }}
QPushButton:hover {{ background-color: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
QPushButton:pressed {{ background-color: {c['accent_press']}; border-color: {c['accent_press']}; }}
QPushButton:disabled {{ background-color: {c['raised']}; color: {c['faint']}; border-color: {c['border']}; }}
QPushButton#secondary {{ background-color: {c['surface']}; color: {c['text']}; border: 1px solid {c['border_strong']}; font-weight: 500; }}
QPushButton#secondary:hover {{ background-color: {c['raised']}; }}
QPushButton#secondary:pressed {{ background-color: {c['border']}; }}
QPushButton#secondary:disabled {{ background-color: {c['bg']}; color: {c['faint']}; border-color: {c['border']}; }}
QPushButton#danger {{ background-color: {c['danger']}; border-color: {c['danger']}; }}
QPushButton#danger:hover {{ background-color: {c['danger_hover']}; border-color: {c['danger_hover']}; }}
QPushButton::menu-indicator {{ image: none; width: 0px; }}
QToolButton {{ background-color: transparent; color: {c['text']}; border: 1px solid transparent; border-radius: 8px; padding: 5px 8px; }}
QToolButton:hover {{ background-color: {c['raised']}; }}
QToolButton:checked {{ background-color: transparent; }}
QToolButton#themeToggle {{ background-color: {c['surface']}; border: 1px solid {c['border_strong']}; padding: 6px 8px; }}
QToolButton#themeToggle:hover {{ background-color: {c['raised']}; }}

QTableWidget, QTreeWidget, QListWidget {{ background-color: {c['surface']}; alternate-background-color: {c['surface']}; border: 1px solid {c['border']}; border-radius: 10px;
    gridline-color: transparent; color: {c['text']}; outline: 0; }}
QTableWidget::item {{ padding: 4px 6px; border: none; border-bottom: 1px solid {c['border']}; }}
QTreeWidget::item {{ padding: 4px 6px; border: none; }}
QTableWidget::item:selected, QTreeWidget::item:selected {{ background-color: {c['selection']}; color: {c['text']}; }}
QTableView::indicator, QTreeView::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {c['border_strong']}; background-color: {c['input_bg']}; }}
QTableView::indicator:checked, QTreeView::indicator:checked {{ background-color: {c['accent']}; border-color: {c['accent']}; image: url({check}); }}
QHeaderView {{ background-color: {c['surface']}; border: none; }}
QHeaderView::section {{ background-color: {c['surface']}; color: {c['muted']}; border: none; border-bottom: 1px solid {c['border']}; padding: 9px 8px; font-weight: 600; }}
QTableCornerButton::section {{ background-color: {c['surface']}; border: none; border-bottom: 1px solid {c['border']}; }}

QProgressBar {{ background-color: {c['raised']}; border: none; border-radius: 7px; text-align: center; color: {c['text']}; font-size: 11px; min-height: 14px; max-height: 14px; }}
QProgressBar::chunk {{ background-color: {c['accent']}; border-radius: 7px; }}

QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c['border_strong']}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {c['faint']}; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {c['border_strong']}; border-radius: 4px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {c['faint']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QMenu {{ background-color: {c['surface']}; color: {c['text']}; border: 1px solid {c['border_strong']}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 24px 7px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background-color: {c['accent_soft']}; color: {c['accent_text']}; }}
QMenu::item:disabled {{ color: {c['faint']}; }}
QMenu::separator {{ height: 1px; background: {c['border']}; margin: 6px 8px; }}
QToolTip {{ background-color: {c['surface']}; color: {c['text']}; border: 1px solid {c['border_strong']}; padding: 5px 8px; }}

QWidget#sidebar {{ background-color: {c['surface']}; border-right: 1px solid {c['border']}; }}
QListWidget#nav {{ background: transparent; border: none; border-radius: 0; outline: none; font-size: 14px; }}
QListWidget#nav::item {{ padding: 9px 12px; border-radius: 8px; margin: 2px 4px; color: {c['muted']}; }}
QListWidget#nav::item:hover:!selected {{ background-color: {c['raised']}; color: {c['text']}; }}
QListWidget#nav::item:selected {{ background-color: {c['accent_soft']}; color: {c['accent_text']}; font-weight: 600; }}

QLabel#dropZone {{ border: 1px dashed {c['border_strong']}; border-radius: 10px; color: {c['muted']}; padding: 12px; background-color: transparent; }}
QFrame#banner {{ background-color: {c['accent_soft']}; border: 1px solid {c['accent']}; border-radius: 10px; }}
QFrame#banner QWidget {{ background: transparent; }}
QFrame#banner QPushButton {{ background-color: {c['surface']}; color: {c['text']}; border: 1px solid {c['border_strong']}; font-weight: 500; }}
QFrame#queueBar {{ background-color: {c['surface']}; border-top: 1px solid {c['border']}; }}
QFrame#queueBar QLabel {{ background: transparent; }}
QFrame#queueBar QPushButton {{ background-color: {c['surface']}; color: {c['text']}; border: 1px solid {c['border_strong']}; font-weight: 500; }}
"""
