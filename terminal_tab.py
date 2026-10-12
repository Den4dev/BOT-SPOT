#!/usr/bin/env python3
"""Вкладка «Терминал»: встроенный SSH-терминал (paramiko shell + pyte).

НЕ импортирует botmanager. Своё SSH-соединение (интерактивный shell-канал),
свой поток чтения. Живой экран без скроллбака истории (ограничение v1).
"""
import logging
import shlex
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

import paramiko
import pyte
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

__all__ = ["TerminalTab", "TermWidget", "KEYMAP"]

LOG_PATH = Path.home() / ".botmanager_terminal.log"

_log = logging.getLogger("botmanager_terminal")
if not _log.handlers:
    try:
        h = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _log.addHandler(h)
        _log.setLevel(logging.INFO)
    except Exception:
        logging.basicConfig(level=logging.INFO)

# палитра xterm, читаемая на тёмном фоне ("black" приглушён, а не в ноль)
FG = {
    "black": "#5A6B7C", "red": "#FF5965", "green": "#35D58A",
    "brown": "#FFCA45", "blue": "#6FB6E8", "magenta": "#C792EA",
    "cyan": "#56D4DD", "white": "#F1F6FC", "default": None,
    "brightblack": "#8A9BAC", "brightred": "#FF7B85", "brightgreen": "#5EEA9B",
    "brightbrown": "#FFD968", "brightblue": "#8FC3F5", "brightmagenta": "#D8A9F5",
    "brightcyan": "#7FE3EA", "brightwhite": "#FFFFFF",
}
BG = {
    "black": "#000000", "red": "#7A2E35", "green": "#0E4A2E",
    "brown": "#5A4510", "blue": "#12395C", "magenta": "#3C2A52",
    "cyan": "#0E4A52", "white": "#3A4753", "default": None,
    "brightblack": "#2A3540", "brightred": "#8A363D", "brightgreen": "#14532D",
    "brightbrown": "#6B5416", "brightblue": "#1D4E80", "brightmagenta": "#4A3560",
    "brightcyan": "#155E66", "brightwhite": "#5A6B7C",
}

KEYMAP = {"Up": b"\x1b[A", "Down": b"\x1b[B", "Right": b"\x1b[C",
          "Left": b"\x1b[D", "Home": b"\x1b[H", "End": b"\x1b[F",
          "PageUp": b"\x1b[5~", "PageDown": b"\x1b[6~",
          "Insert": b"\x1b[2~", "Delete": b"\x1b[3~"}


class TermWidget(QPlainTextEdit):
    """Живой экран pyte. Отправка наружу через on_send(bytes)."""

    resized = Signal(int, int)  # cols, rows

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        f = QFont("Cascadia Code", 10)
        f.setStyleHint(QFont.Monospace)
        self.setFont(f)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setCursorWidth(8)
        # скроллбары выключены навсегда: их появление меняет вьюпорт, что
        # порождает новый resizeEvent -> петля ресайзов с потерей строк.
        # Живой экран всегда ровно по размеру вьюпорта, скролл не нужен (v1).
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._cols, self._rows = 80, 24
        self._screen = pyte.Screen(self._cols, self._rows)
        self._stream = pyte.ByteStream(self._screen)
        self.on_send: Optional[Callable[[bytes], None]] = None
        self._render_pending = False
        self._pending_size = None
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._apply_resize)
        self.setPlaceholderText("Терминал…")

    # --- вход ---
    def feed_data(self, data: bytes) -> None:
        try:
            self._stream.feed(data)
        except Exception as e:  # noqa: BLE001 — битый кусок не должен ронять экран
            _log.warning("pyte feed failed: %s", e)
            return
        if not self._render_pending:
            self._render_pending = True
            QTimer.singleShot(50, self._render)

    def reset_screen(self, cols: int = 80, rows: int = 24) -> None:
        self._cols, self._rows = cols, rows
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)
        self._render()

    def show_message(self, text: str) -> None:
        self._render_pending = False
        self.setPlainText(text)

    # --- рендер ---
    def _fmt_for(self, fg: str, bg: str, bold: bool):
        if fg in (None, "default") and bg in (None, "default") and not bold:
            return None
        fmt = QTextCharFormat()
        if fg not in (None, "default") and FG.get(fg):
            fmt.setForeground(QColor(FG[fg]))
        if bg not in (None, "default") and BG.get(bg):
            fmt.setBackground(QColor(BG[bg]))
        if bold:
            fmt.setFontWeight(QFont.Bold)
        return fmt

    def _render(self) -> None:
        self._render_pending = False
        scr = self._screen
        try:
            self.clear()
            cur = QTextCursor(self.document())
            for y in range(scr.lines):
                buf_line = scr.buffer[y]
                x = 0
                while x < scr.columns:
                    ch = buf_line[x]
                    key = (ch.fg, ch.bg, ch.bold)
                    x2 = x + 1
                    while x2 < scr.columns:
                        c2 = buf_line[x2]
                        if (c2.fg, c2.bg, c2.bold) != key:
                            break
                        x2 += 1
                    run = "".join(buf_line[i].data for i in range(x, x2))
                    if run.strip(" "):
                        fmt = self._fmt_for(*key)
                        if fmt is None:
                            cur.insertText(run)
                        else:
                            cur.insertText(run, fmt)
                    x = x2
                if y < scr.lines - 1:
                    cur.insertBlock()
            cy = min(max(scr.cursor.y, 0), scr.lines - 1)
            block = self.document().findBlockByNumber(cy)
            if block.isValid():
                cx = min(max(scr.cursor.x, 0), scr.columns)
                cur.setPosition(block.position() + min(cx, max(block.length() - 1, 0)))
                self.setTextCursor(cur)
            self.ensureCursorVisible()
        except Exception as e:  # noqa: BLE001
            _log.warning("term render failed: %s", e)

    # --- геометрия ---
    def term_size(self):
        fm = QFontMetrics(self.font())
        cw = max(fm.horizontalAdvance("M"), 1)
        lh = max(fm.height(), 1)
        vp = self.viewport()
        cols = max(20, min(240, vp.width() // cw))
        rows = max(5, min(80, vp.height() // lh))
        return cols, rows

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        # дебаунс: каскад событий при показе/тяге окна схлопывается в один ресайз,
        # иначе pyte.resize() режет верхние строки при каждом промежуточном размере
        self._pending_size = self.term_size()
        self._resize_timer.start(250)

    def _apply_resize(self) -> None:
        pending = self._pending_size
        self._pending_size = None
        if pending is None:
            return
        cols, rows = pending
        scr = self._screen
        if (cols, rows) == (scr.columns, scr.lines):
            return
        # Snapshot-Replay: pyte.resize() при уменьшении строк теряет контент,
        # поэтому сохраняем текст, ресайзим и пишем обратно (цвета при этом
        # теряются — приемлемо для события ресайза)
        snap = [scr.display[y] for y in range(scr.lines)]
        cx, cy = scr.cursor.x, scr.cursor.y
        try:
            scr.resize(lines=rows, columns=cols)
        except Exception as e:  # noqa: BLE001
            _log.warning("screen resize failed: %s", e)
            return
        self._cols, self._rows = cols, rows
        # сначала срезаем пустой хвост: контент из 3 строк на 30-строчном экране
        # обязан пережить ужатие, а не замениться пустыми строками хвоста
        stripped = list(snap)
        while stripped and not stripped[-1].strip():
            stripped.pop()
        if len(stripped) > rows:
            keep = stripped[-rows:]
            offset = len(stripped) - rows
        else:
            keep = stripped
            offset = 0
        try:
            self._stream.feed(b"\x1b[H\x1b[J")
            for i, ln in enumerate(keep):
                if i:
                    self._stream.feed(b"\r\n")
                t = ln.rstrip().replace("\x1b", "")
                if t:
                    self._stream.feed(t.encode("utf-8", errors="ignore"))
            cy2 = min(max(cy - offset, 0), rows - 1)
            cx2 = min(max(cx, 0), cols - 1)
            self._stream.feed(f"\x1b[{cy2 + 1};{cx2 + 1}H".encode())
        except Exception as e:  # noqa: BLE001
            _log.warning("resize replay failed: %s", e)
        self.resized.emit(cols, rows)
        self._render()

    # --- ввод ---
    def keyPressEvent(self, e) -> None:
        mods = e.modifiers()
        key = e.key()
        # копировать/вставить — только с Shift (голый Ctrl+C это SIGINT)
        if mods & Qt.ControlModifier and mods & Qt.ShiftModifier:
            if key == Qt.Key_C:
                self.copy()
                e.accept()
                return
            if key == Qt.Key_V:
                self._paste_clipboard()
                e.accept()
                return
        if key == Qt.Key_Insert:
            if mods & Qt.ControlModifier and not (mods & Qt.ShiftModifier):
                self.copy()
                e.accept()
                return
            if mods & Qt.ShiftModifier and not (mods & Qt.ControlModifier):
                self._paste_clipboard()
                e.accept()
                return
        data = None
        if mods & Qt.ControlModifier and not (mods & (Qt.ShiftModifier | Qt.AltModifier)):
            if Qt.Key_A <= key <= Qt.Key_Z:
                data = bytes([key - Qt.Key_A + 1])  # Ctrl+A..Z -> \x01..\x1a
            elif key == Qt.Key_BracketLeft:
                data = b"\x1b"  # Ctrl+[
        if data is None:
            kn = {
                Qt.Key_Return: b"\r", Qt.Key_Enter: b"\r",
                Qt.Key_Backspace: b"\x7f", Qt.Key_Tab: b"\t",
                Qt.Key_Escape: b"\x1b", Qt.Key_Delete: b"\x1b[3~",
                Qt.Key_Up: b"\x1b[A", Qt.Key_Down: b"\x1b[B",
                Qt.Key_Right: b"\x1b[C", Qt.Key_Left: b"\x1b[D",
                Qt.Key_Home: b"\x1b[H", Qt.Key_End: b"\x1b[F",
                Qt.Key_PageUp: b"\x1b[5~", Qt.Key_PageDown: b"\x1b[6~",
                Qt.Key_Insert: b"\x1b[2~",
            }.get(key, "text")
            if kn == "text":
                t = e.text()
                data = t.encode("utf-8", errors="ignore") or None
            else:
                data = kn
        if data and self.on_send is not None:
            try:
                self.on_send(data)
            except Exception as e:  # noqa: BLE001
                _log.warning("term send failed: %s", e)
        e.accept()

    def _paste_clipboard(self) -> None:
        try:
            from PySide6.QtWidgets import QApplication as _QApp
            t = _QApp.clipboard().text()
        except Exception:
            return
        if t and self.on_send is not None:
            try:
                self.on_send(t.replace("\r\n", "\n").encode("utf-8", errors="ignore"))
            except Exception as e:  # noqa: BLE001
                _log.warning("term paste failed: %s", e)


class TerminalTab(QWidget):
    """Вкладка терминала: ленивый коннект, shell-канал, чтение в фоне."""

    _connected_ok = Signal(object, object)  # client, chan
    _failed = Signal(str)
    _incoming = Signal(bytes)
    _closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._creds = None
        self._profile = ""
        self._client = None
        self._chan = None
        self._connected = False
        self._reader = None
        self._alive = False
        self._pending_cd = ""
        self._shown = False
        self._lock = threading.Lock()

        self._connected_ok.connect(self._on_connected_chan)
        self._failed.connect(self._on_failed)
        self._incoming.connect(self._on_data)
        self._closed.connect(self._on_closed)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 12)
        lay.setSpacing(10)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.status = QLabel("Нет подключения")
        self.status.setObjectName("statusLine")
        self.btn_conn = QPushButton("Подключить")
        self.btn_conn.setObjectName("btnPrimary")
        self.btn_conn.setCursor(Qt.PointingHandCursor)
        self.btn_conn.clicked.connect(self.toggle_connection)
        self.btn_clear = QPushButton("Очистить")
        self.btn_clear.setObjectName("btnGhost")
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.clicked.connect(self.clear)
        hint = QLabel("Enter — ввод · Ctrl+C — прервать · Ctrl+Shift+C/V — копировать/вставить")
        hint.setObjectName("statusLine")
        bar.addWidget(self.status)
        bar.addStretch()
        bar.addWidget(hint)
        bar.addWidget(self.btn_clear)
        bar.addWidget(self.btn_conn)
        lay.addLayout(bar)

        self.term = TermWidget()
        self.term.on_send = self.send
        self.term.resized.connect(self._on_resized)
        lay.addWidget(self.term, 1)
        self.term.show_message("Выберите профиль и нажмите «Подключиться» — "
                               "или откройте терминал из списка ботов.")

    # --- подключение ---
    def on_connected(self, creds: dict) -> None:
        new_profile = creds.get("profile")
        if self._connected and new_profile != self._profile:
            self.disconnect(silent=True)
        self._creds = dict(creds)

    def activate(self) -> None:
        self._shown = True
        if self._creds and not self._connected:
            self.connect_async()

    def toggle_connection(self) -> None:
        if self._connected:
            self.disconnect()
        elif self._creds:
            self.connect_async()
        else:
            self.status.setText("Нет подключения: выберите профиль и нажмите «Подключиться»")

    def connect_async(self) -> None:
        if self._connected or not self._creds:
            return
        creds = dict(self._creds)
        self.status.setText("Подключение…")
        self.btn_conn.setEnabled(False)
        cols, rows = self.term._cols, self.term._rows
        t = threading.Thread(target=self._connect_work, args=(creds, cols, rows),
                             daemon=True, name="term-connect")
        t.start()

    def _connect_work(self, creds: dict, cols: int, rows: int) -> None:
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(creds["host"], port=int(creds.get("port") or 22),
                           username=creds["user"], password=creds.get("password") or None,
                           key_filename=creds.get("key") or None, timeout=10,
                           allow_agent=True, look_for_keys=True)
            client.get_transport().set_keepalive(20)
            chan = client.get_transport().open_session()
            chan.get_pty(term="xterm-256color", width=cols, height=rows)
            chan.invoke_shell()
            self._connected_ok.emit(client, chan)
        except Exception as e:  # noqa: BLE001
            _log.warning("term connect failed: %s", e)
            self._failed.emit(str(e) or type(e).__name__)

    def _on_connected_chan(self, client, chan) -> None:
        with self._lock:
            self._client, self._chan = client, chan
            self._connected = True
            self._alive = True
        self._profile = (self._creds or {}).get("profile", "")
        self.btn_conn.setEnabled(True)
        self.btn_conn.setText("Отключить")
        self.status.setText(f"Подключено: {self._profile}")
        cols, rows = self.term.term_size()
        self.term.reset_screen(cols, rows)
        try:
            chan.resize_pty(width=cols, height=rows)
        except Exception:
            pass
        if self._pending_cd:
            cd, self._pending_cd = self._pending_cd, ""
            self.send(f"cd {shlex.quote(cd)}\n".encode())
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="term-read")
        self._reader.start()

    def _on_failed(self, msg: str) -> None:
        self.btn_conn.setEnabled(True)
        self.status.setText(f"Не подключилось: {msg}")
        self.term.show_message(f"Не подключилось: {msg}")

    def _on_data(self, data: bytes) -> None:
        self.term.feed_data(data)

    def _on_closed(self) -> None:
        was = self._connected
        self._connected = False
        self._alive = False
        with self._lock:
            self._client, self._chan = None, None
        self.btn_conn.setText("Подключить")
        if was:
            self.status.setText("Сессия закрыта сервером")
            self.term.show_message(self.term.toPlainText() + "\n[сессия закрыта]")

    def _read_loop(self) -> None:
        try:
            while self._alive:
                try:
                    data = self._chan.recv(4096)
                except Exception:
                    break
                if not data:
                    break
                self._incoming.emit(bytes(data))
        except Exception as e:  # noqa: BLE001
            _log.warning("term read failed: %s", e)
        finally:
            self._closed.emit()

    def _on_resized(self, cols: int, rows: int) -> None:
        chan = self._chan
        if chan is not None:
            try:
                chan.resize_pty(width=cols, height=rows)
            except Exception:
                pass

    # --- действия ---
    def send(self, data: bytes) -> None:
        chan = self._chan
        if chan is None:
            return
        try:
            chan.send(data)
        except Exception as e:  # noqa: BLE001
            _log.warning("term send failed: %s", e)
            self._closed.emit()

    def clear(self) -> None:
        cols, rows = self.term.term_size()
        self.term.reset_screen(cols, rows)

    def disconnect(self, silent: bool = False) -> None:
        self._alive = False
        with self._lock:
            chan, client = self._chan, self._client
            self._chan, self._client = None, None
        self._connected = False
        for c in (chan, client):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
        self.btn_conn.setText("Подключить")
        self.btn_conn.setEnabled(True)
        if not silent:
            self.status.setText("Отключено")
            self.term.show_message("Отключено. Нажмите «Подключить».")
        self._pending_cd = ""

    def open_here(self, path: str) -> None:
        """Открыть терминал в папке бота: коннект при необходимости + cd."""
        if self._connected and self._chan is not None:
            if path:
                self.send(f"cd {shlex.quote(path)}\n".encode())
            return
        self._pending_cd = path or ""
        if self._creds:
            self.connect_async()
        else:
            self.status.setText("Нет подключения: выберите профиль и нажмите «Подключиться»")
