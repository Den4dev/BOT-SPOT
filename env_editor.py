#!/usr/bin/env python3
"""Редактор .env бота: разбор с сохранением комментариев, маска секретов.

НЕ импортирует botmanager. Сетью занимается вызывающий код (Win).
"""
import re
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLineEdit, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)
from ui_anim import derived, AnimatedButton

__all__ = ["EnvDialog", "parse_dotenv", "serialize_env", "is_secret", "quote_value"]

SECRET_RE = re.compile(r"TOKEN|SECRET|KEY|PASSWORD|PASSWD|CREDENTIAL", re.I)
KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def is_secret(key: str) -> bool:
    return bool(SECRET_RE.search(key or ""))


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        q = v[0]
        inner = v[1:-1]
        if q == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n")
        return inner
    return v


def parse_dotenv(text: str):
    """Возвращает (lines, entries). entries: [{key, value, idx, secret}]."""
    lines = (text or "").splitlines()
    entries = []
    for i, raw in enumerate(lines):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        m = KEY_RE.match(s)
        if not m:
            continue
        key = m.group(1)
        entries.append({"key": key, "value": _unquote(m.group(2)),
                        "idx": i, "secret": is_secret(key)})
    return lines, entries


def quote_value(v: str) -> str:
    if re.match(r"^[\w./:@+~-]*$", v or ""):
        return v or ""
    esc = (v or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{esc}"'


def serialize_env(lines: list, edits: dict) -> str:
    """edits: {line_idx: new_value}. Возвращает новый текст."""
    out = list(lines)
    for idx, val in edits.items():
        if 0 <= idx < len(out):
            m = KEY_RE.match(out[idx].strip())
            if m:
                out[idx] = f"{m.group(1)}={quote_value(val)}"
    text = "\n".join(out)
    if lines and (text and not text.endswith("\n")):
        text += "\n"
    return text


class EnvDialog(QDialog):
    """Таблица переменных. Сохранить → accept, текст забирать через result_text()."""

    def __init__(self, parent, bot_name: str, text: str):
        super().__init__(parent)
        self.setWindowTitle(f"Переменные: {bot_name}")
        self.setModal(True)
        self._lines, self._entries = parse_dotenv(text)
        self._edits: dict = {}
        self._revealed = set()
        lay = QVBoxLayout(self)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Переменная", "Значение"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.itemDoubleClicked.connect(lambda _it: self.do_edit())
        lay.addWidget(self.table, 1)

        bar = QHBoxLayout()
        self.btn_show = AnimatedButton("Показать/скрыть")
        self.btn_show.setObjectName("btnGhost")
        self.btn_copy = AnimatedButton("Копировать")
        self.btn_copy.setObjectName("btnGhost")
        self.btn_edit = AnimatedButton("Изменить")
        self.btn_edit.setObjectName("btnGhost")
        self.btn_add = AnimatedButton("+ Добавить")
        self.btn_add.setObjectName("btnGhost")
        for b in (self.btn_show, self.btn_copy, self.btn_edit, self.btn_add):
            b.setCursor(Qt.PointingHandCursor)
            bar.addWidget(b)
        bar.addStretch()
        lay.addLayout(bar)

        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Save).setText("Сохранить на сервер")
        box.button(QDialogButtonBox.Cancel).setText("Отмена")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self.setMinimumSize(560, 420)

        self.btn_show.clicked.connect(self.do_show)
        self.btn_copy.clicked.connect(self.do_copy)
        self.btn_edit.clicked.connect(self.do_edit)
        self.btn_add.clicked.connect(self.do_add)
        self._render()

    def _value(self, e: dict) -> str:
        if e["idx"] in self._edits:
            return self._edits[e["idx"]]
        return e["value"]

    def _render(self) -> None:
        self.table.setRowCount(len(self._entries))
        for i, e in enumerate(self._entries):
            ki = QTableWidgetItem(e["key"])
            if e["idx"] in self._edits:
                ki.setForeground(QColor(derived()["wn"]))
            self.table.setItem(i, 0, ki)
            if e["secret"] and e["key"] not in self._revealed:
                vi = QTableWidgetItem("••••••••")
            else:
                vi = QTableWidgetItem(self._value(e))
            self.table.setItem(i, 1, vi)

    def _sel(self) -> Optional[dict]:
        r = self.table.currentRow()
        if r < 0 or r >= len(self._entries):
            return None
        return self._entries[r]

    def do_show(self) -> None:
        e = self._sel()
        if e is None:
            return
        if e["key"] in self._revealed:
            self._revealed.discard(e["key"])
        else:
            self._revealed.add(e["key"])
        self._render()

    def do_copy(self) -> None:
        e = self._sel()
        if e is None:
            return
        QApplication.clipboard().setText(self._value(e))

    def do_edit(self) -> None:
        e = self._sel()
        if e is None:
            return
        text, ok = QInputDialog.getText(self, "Изменить", f"{e['key']}:",
                                        text=self._value(e))
        if not ok:
            return
        self._edits[e["idx"]] = text
        self._render()

    def do_add(self) -> None:
        key, ok = QInputDialog.getText(self, "Новая переменная", "Имя (KEY):")
        if not ok or not key.strip():
            return
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            QMessageBox.warning(self, "Переменные", "Имя: латиница, цифры и _")
            return
        val, ok = QInputDialog.getText(self, "Новая переменная", f"{key} =")
        if not ok:
            return
        self._lines.append(f"{key}={quote_value(val)}")
        self._entries.append({"key": key, "value": val, "idx": len(self._lines) - 1,
                              "secret": is_secret(key)})
        self._edits[len(self._lines) - 1] = val
        self._render()

    def result_text(self) -> str:
        return serialize_env(self._lines, self._edits)

    def has_changes(self) -> bool:
        return bool(self._edits)
