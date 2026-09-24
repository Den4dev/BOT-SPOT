#!/usr/bin/env python3
"""Вкладка «Файлы» — двухпанельный SFTP-менеджер в стиле FileZilla.

Живёт отдельно от вкладки «Боты»: своё SSH-соединение, свои фоновые потоки,
своё состояние. НЕ импортирует botmanager (исключает циклические импорты).
"""
import json
import logging
import os
import posixpath
import queue as queue_mod
import shutil
import stat as statmod
import threading
import time
import traceback
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

import paramiko
from PySide6.QtCore import QDir, QFileInfo, QMimeData, QObject, QProcess, QProcessEnvironment, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QDrag
from PySide6.QtWidgets import QFileIconProvider
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox, QFrame,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QProgressBar, QPushButton, QSplitter, QStyle, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

__all__ = ["FilesTab", "SEG_QSS"]

STATE_PATH = Path.home() / ".botmanager_files.json"
LOG_PATH = Path.home() / ".botmanager_files.log"

_log = logging.getLogger("botmanager_files")
if not _log.handlers:
    try:
        h = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _log.addHandler(h)
        _log.setLevel(logging.INFO)
    except Exception:
        logging.basicConfig(level=logging.INFO)


# ---------- стили (живут здесь, THEME_QSS не трогаем) ----------
SEG_QSS = """
QPushButton#segLeft, QPushButton#segMid, QPushButton#segMid2, QPushButton#segRight {
    background: #12304D; color: #D5E3F0; border: 1px solid #235074;
    padding: 7px 22px; font-weight: 700; font-size: 12px;
}
QPushButton#segLeft { border-top-left-radius: 6px; border-bottom-left-radius: 6px; border-right: none; }
QPushButton#segMid, QPushButton#segMid2 { border-radius: 0; border-right: none; }
QPushButton#segRight { border-top-right-radius: 6px; border-bottom-right-radius: 6px; }
QPushButton#segLeft:hover, QPushButton#segMid:hover, QPushButton#segMid2:hover, QPushButton#segRight:hover { background: #1A4066; }
QPushButton#segLeft:checked, QPushButton#segMid:checked, QPushButton#segMid2:checked, QPushButton#segRight:checked {
    background: #168AF5;
    color: #FFFFFF;
    border: 1px solid #48AEFF;
    font-weight: 800;
}
QPushButton#segLeft:checked, QPushButton#segMid:checked, QPushButton#segMid2:checked { border-right: none; }
"""

FILES_QSS = """
QFrame#banner { background: #102B45; border: 1px solid #234A6B; border-radius: 10px; }
QFrame#bannerErr { background: #102B45; border: 1px solid #8A3038; border-radius: 10px; }
QLabel#bannerText { color: #D6E2ED; font-weight: 600; }
QFrame#bannerErr QLabel#bannerText { color: #F1F6FC; }
QLabel#statusLine { color: #B6CCE0; font-size: 11px; }
QLabel#paneTitle { font-size: 13px; font-weight: 700; color: #F1F6FC; }
QTableWidget#fileTable {
    background: transparent; alternate-background-color: rgba(12, 41, 66, 150);
    color: #F1F6FC; gridline-color: #0C2942; border: 1px solid #245679; border-radius: 10px;
}
QTableWidget#fileTable::item { padding: 4px 6px; border: none; }
QTableWidget#fileTable::item:hover { background: #123A5C; }
QTableWidget#fileTable::item:selected { background: #154C77; color: #FFFFFF; }
QTableWidget#queueTable {
    background: transparent; alternate-background-color: rgba(12, 41, 66, 150);
    color: #F1F6FC; gridline-color: #0C2942;
    border: 1px solid #245679; border-radius: 10px;
}
QTableWidget#queueTable::item { padding: 3px 6px; border: none; }
QTableWidget#queueTable::item:selected { background: #154C77; color: #FFFFFF; }
QProgressBar {
    background: #0A2036; border: 1px solid #214968; border-radius: 6px;
    text-align: center; color: #D6E2ED; font-size: 10px; height: 14px;
}
QProgressBar::chunk {
    background: #168AF5;
    border-radius: 5px;
}
"""


# ---------- модель ----------
@dataclass
class Entry:
    name: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    mode: str = ""       # "rwxr-xr-x" — только сервер
    is_link: bool = False


def human_size(n: int) -> str:
    if n is None or n < 0:
        return ""
    f = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if f < 1024 or unit == "ТБ":
            return f"{f:.0f} {unit}" if unit == "Б" else f"{f:.1f} {unit}"
        f /= 1024
    return ""


def fmt_time(ts: float) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
    except (ValueError, OSError):
        return ""


def friendly(e: BaseException) -> str:
    """Человекочитаемый текст ошибки для строки статуса."""
    if isinstance(e, PermissionError) or getattr(e, "errno", None) == 13 \
            or "Permission denied" in str(e):
        return f"Нет прав: {e}"
    name = type(e).__name__
    s = str(e) or name
    if isinstance(e, (TimeoutError, ConnectionError, OSError)) or "SSH" in name \
            or "socket" in name.lower() or "timed out" in s.lower() \
            or "not connected" in s.lower() or "No such file" in s:
        return f"Нет соединения / нет файла: {s}"
    return s


_CONN_LOST_MARKS = ("dropped", "closed", "reset by peer", "broken pipe",
                    "forcibly closed", "timed out", "not connected")


def is_conn_lost(e: BaseException) -> bool:
    """Похоже ли на обрыв соединения (а не на обычную ошибку вроде прав/пути).

    PermissionError и FileNotFoundError сюда НЕ попадают: это штатные ответы
    живого сервера.
    """
    if isinstance(e, PermissionError) or getattr(e, "errno", None) == 13 \
            or "Permission denied" in str(e):
        return False
    if isinstance(e, (TimeoutError, ConnectionError)):
        return True
    if isinstance(e, OSError) and getattr(e, "errno", None) not in (None, 2):
        s = str(e).lower()
        if "no such file" in s:
            return False
        return any(m in s for m in _CONN_LOST_MARKS) or "socket" in type(e).__name__.lower()
    name = type(e).__name__
    if "SSH" in name or "EOF" in name:
        return True
    s = (str(e) or "").lower()
    return any(m in s for m in _CONN_LOST_MARKS)


def sanitized_env(env: Optional[dict] = None) -> dict:
    """Копия окружения без мусора замороженного приложения.

    PySide6 дописывает свой каталог в PATH, а в exe-сборке это Temp/_MEI... —
    дочерняя Qt-программа (напр. DB Browser for SQLite) находит там чужие
    плагины и падает с 'невалидными метаданными' в .dll. Вычищаем такие пути
    и QT_*/QML_*/PYSIDE_*-переменные перед запуском внешних программ.
    """
    src = dict(os.environ) if env is None else dict(env)
    out = {}
    for k, v in src.items():
        ku = k.upper()
        if ku.startswith(("QT_", "QML_", "PYSIDE_")):
            continue
        if ku == "PATH" and isinstance(v, str):
            v = os.pathsep.join(p for p in v.split(os.pathsep) if p and "_mei" not in p.lower())
        out[k] = v
    return out


def open_local_file(path: str) -> bool:
    """Открыть файл/папку программой по умолчанию с чистым окружением (см. sanitized_env)."""
    if os.name != "nt":
        return QDesktopServices.openUrl(QUrl.fromLocalFile(path))
    proc = QProcess()
    pe = QProcessEnvironment()
    for k, v in sanitized_env().items():
        pe.insert(k, v)
    proc.setProcessEnvironment(pe)
    proc.setProgram("cmd")
    proc.setArguments(["/c", "start", "", os.path.normpath(path)])
    try:
        d = os.path.dirname(os.path.abspath(path))
    except (ValueError, OSError):
        d = os.path.expanduser("~")
    proc.setWorkingDirectory(d)
    return proc.startDetached()


# ---------- фоновый runner (свой, botmanager.bg использовать нельзя) ----------
class DaemonRunner(QObject):
    """Один демон-поток + очередь задач. Результат возвращается сигналом в UI-поток."""
    arrived = Signal(object, object, object)  # (callback, result, error)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._q: "queue_mod.Queue[tuple[Callable, Callable]]" = queue_mod.Queue()
        self.arrived.connect(lambda cb, res, err: cb(res, err))
        threading.Thread(target=self._loop, daemon=True, name="files-runner").start()

    def submit(self, fn: Callable, cb: Callable) -> None:
        self._q.put((fn, cb))

    def _loop(self) -> None:
        while True:
            fn, cb = self._q.get()
            try:
                res, err = fn(), None
            except Exception as e:  # noqa: BLE001
                _log.warning("files task failed: %s\n%s", e, traceback.format_exc())
                res, err = None, e
            self.arrived.emit(cb, res, err)


# ---------- бэкенды ----------
class LocalBackend:
    """Файлы компьютера. Корневой уровень ('') — диски на Windows, '/' на POSIX."""

    def home(self) -> str:
        return str(Path.home())

    def join(self, a: str, b: str) -> str:
        if not a or Path(b).is_absolute():
            return b
        return str(Path(a) / b)

    def parent(self, p: str) -> str:
        if not p:
            return ""
        pp = Path(p)
        if pp.parent == pp:
            return ""
        return str(pp.parent)

    def listdir(self, path: str) -> list[Entry]:
        if not path:
            if os.name == "nt":
                out = []
                for d in QDir.drives():
                    name = d.absolutePath().replace("\\", "/")
                    if not name.endswith("/"):
                        name += "/"
                    out.append(Entry(name=name, is_dir=True))
                return out
            path = "/"
        out = []
        with os.scandir(path) as it:
            for e in it:
                try:
                    is_link = e.is_symlink()
                    is_dir = e.is_dir(follow_symlinks=False)
                    if is_link:
                        try:
                            is_dir = Path(e.path).is_dir()
                        except OSError:
                            pass
                    st = e.stat(follow_symlinks=False)
                    out.append(Entry(name=e.name, is_dir=is_dir, size=st.st_size,
                                     mtime=st.st_mtime, is_link=is_link))
                except OSError:
                    continue
        return out

    def mkdir(self, path: str) -> None:
        os.mkdir(path)

    def rename(self, src: str, dst: str) -> None:
        os.rename(src, dst)

    def remove(self, path: str, is_dir: bool) -> None:
        if is_dir:
            shutil.rmtree(path)
        else:
            os.remove(path)

    def stat(self, path: str) -> Entry:
        st = os.stat(path)
        return Entry(name=Path(path).name, is_dir=statmod.S_ISDIR(st.st_mode),
                     size=st.st_size, mtime=st.st_mtime)

    def exists(self, path: str) -> bool:
        return os.path.lexists(path)


class SftpBackend:
    """Файлы сервера через paramiko SFTPClient. Пути — только POSIX."""

    def __init__(self, sftp: "paramiko.SFTPClient"):
        self.s = sftp

    def home(self) -> str:
        try:
            return self.s.normalize(".")
        except Exception:
            return "/"

    def join(self, a: str, b: str) -> str:
        if not a or b.startswith("/"):
            return b or "/"
        return posixpath.join(a, b)

    def parent(self, p: str) -> str:
        if not p or p == "/":
            return "/"
        return posixpath.dirname(p.rstrip("/")) or "/"

    def _entry(self, base: str, attr) -> Entry:
        full = base.rstrip("/") + "/" + attr.filename if base != "/" else "/" + attr.filename
        is_link = statmod.S_ISLNK(attr.st_mode or 0)
        is_dir = statmod.S_ISDIR(attr.st_mode or 0)
        if is_link:
            try:
                is_dir = statmod.S_ISDIR(self.s.stat(full).st_mode or 0)
            except OSError:
                pass
        mode = statmod.filemode(attr.st_mode)[1:] if attr.st_mode else ""
        return Entry(name=attr.filename, is_dir=is_dir,
                     size=attr.st_size or 0, mtime=attr.st_mtime or 0.0,
                     mode=mode, is_link=is_link)

    def listdir(self, path: str) -> list[Entry]:
        path = path or "/"
        return [self._entry(path, a) for a in self.s.listdir_attr(path)]

    def mkdir(self, path: str) -> None:
        self.s.mkdir(path)

    def mkdirs(self, path: str) -> None:
        """mkdir -p."""
        parts: list[str] = []
        p = path.rstrip("/") or "/"
        while p not in ("", "/") and not self.exists(p):
            parts.append(p)
            p = posixpath.dirname(p)
        for d in reversed(parts):
            try:
                self.s.mkdir(d)
            except OSError as e:
                if not self.exists(d):
                    raise e

    def rename(self, src: str, dst: str) -> None:
        try:
            self.s.posix_rename(src, dst)
        except AttributeError:
            self.s.rename(src, dst)

    def remove(self, path: str, is_dir: bool) -> None:
        if not is_dir:
            self.s.remove(path)
            return
        # снизу вверх: сначала самое глубокое, корень — последним
        for dirpath, filenames, dirnames in reversed(list(self._walk(path))):
            for f in filenames:
                try:
                    self.s.remove(dirpath.rstrip("/") + "/" + f)
                except OSError:
                    pass
            for d in dirnames:
                try:
                    self.s.rmdir(dirpath.rstrip("/") + "/" + d)
                except OSError:
                    pass
        self.s.rmdir(path)

    def _walk(self, root: str):
        """Нерекурсивный обход без следования по ссылкам. Yield (dir, files, dirs)."""
        try:
            attrs = self.s.listdir_attr(root)
        except OSError:
            return
        files, dirs = [], []
        for a in attrs:
            full = root.rstrip("/") + "/" + a.filename
            if statmod.S_ISLNK(a.st_mode or 0):
                files.append(a.filename)  # ссылки не обходим
            elif statmod.S_ISDIR(a.st_mode or 0):
                dirs.append(a.filename)
            else:
                files.append(a.filename)
        yield root, files, dirs
        for d in dirs:
            yield from self._walk(root.rstrip("/") + "/" + d)

    def stat(self, path: str) -> Entry:
        st = self.s.stat(path)
        try:
            is_link = statmod.S_ISLNK(self.s.lstat(path).st_mode or 0)
        except OSError:
            is_link = False
        return Entry(name=posixpath.basename(path), is_dir=statmod.S_ISDIR(st.st_mode or 0),
                     size=st.st_size or 0, mtime=st.st_mtime or 0.0,
                     mode=statmod.filemode(st.st_mode)[1:] if st.st_mode else "",
                     is_link=is_link)

    def exists(self, path: str) -> bool:
        try:
            self.s.stat(path)
            return True
        except OSError:
            return False

    def chmod(self, path: str, mode: int) -> None:
        self.s.chmod(path, mode)


# ---------- диалог конфликта имён ----------
class ConflictDialog(QDialog):
    OVERWRITE, SKIP, RENAME = "overwrite", "skip", "rename"

    def __init__(self, parent, filename: str, src_size: int, dst_size: int):
        super().__init__(parent)
        self.setWindowTitle("Файл уже существует")
        self.setModal(True)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{filename}</b>"))
        info = QLabel(f"Источник: {human_size(src_size)}\nНазначение: {human_size(dst_size)}\n\nЧто делать?")
        lay.addWidget(info)
        self.apply_all = QCheckBox("Применить ко всем")
        lay.addWidget(self.apply_all)
        box = QDialogButtonBox()
        self.b_over = box.addButton("Перезаписать", QDialogButtonBox.AcceptRole)
        self.b_ren = box.addButton("Переименовать", QDialogButtonBox.ActionRole)
        self.b_skip = box.addButton("Пропустить", QDialogButtonBox.RejectRole)
        self.b_over.clicked.connect(lambda: self.done(1))
        self.b_ren.clicked.connect(lambda: self.done(2))
        self.b_skip.clicked.connect(lambda: self.done(3))
        lay.addWidget(box)
        self.setMinimumWidth(340)

    def decision(self):
        code = self.exec()
        d = {1: self.OVERWRITE, 2: self.RENAME}.get(code, self.SKIP)
        return d, self.apply_all.isChecked()


@dataclass
class ConflictReq:
    filename: str
    src_size: int
    dst_size: int
    event: threading.Event = field(default_factory=threading.Event)
    decision: str = "skip"
    apply_all: bool = False


class Cancelled(Exception):
    pass


# ---------- очередь передач ----------
@dataclass
class TxFile:
    src: str
    dst: str
    size: int


@dataclass
class _Task:
    job_id: int
    direction: str  # "up" | "down"
    display: str
    src_paths: list
    dst_dir: str


class TransferManager(QObject):
    progressed = Signal(int, int, int, float)  # job_id, done_bytes, total_bytes, speed
    job_done = Signal(int, bool, str)          # job_id, ok, message
    conflict_needed = Signal(object)           # ConflictReq
    conn_lost = Signal()                       # обрыв соединения во время передачи

    WORKERS = 2

    def __init__(self, open_sftp: Callable[[], "paramiko.SFTPClient"],
                 local: LocalBackend, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._open_sftp = open_sftp
        self._local = local
        self._q: "queue_mod.Queue[Optional[_Task]]" = queue_mod.Queue()
        self._jobs: dict[int, dict] = {}
        self._seq = 0
        self._lock = threading.Lock()
        self._apply_all: Optional[str] = None
        self.conflict_needed.connect(self._on_conflict_gui)
        for n in range(self.WORKERS):
            threading.Thread(target=self._worker, daemon=True, name=f"files-tx-{n}").start()

    # --- API из UI-потока ---
    def submit(self, direction: str, display: str, src_paths: list, dst_dir: str) -> int:
        with self._lock:
            self._seq += 1
            jid = self._seq
            self._jobs[jid] = {"status": "queued", "cancel": threading.Event(),
                               "display": display, "direction": direction}
        self._q.put(_Task(jid, direction, display, list(src_paths), dst_dir))
        return jid

    def cancel(self, job_id: int) -> None:
        job = self._jobs.get(job_id)
        if job:
            job["cancel"].set()
            if job["status"] == "queued":
                job["status"] = "cancelled"
                self.job_done.emit(job_id, False, "Отменено")

    def cancel_all(self) -> None:
        for jid in list(self._jobs):
            self.cancel(jid)

    def reset_session(self) -> None:
        """Смена профиля: отменяем всё, забываем 'для всех'."""
        self._apply_all = None
        self.cancel_all()

    # --- воркеры ---
    def _is_cancelled(self, jid: int) -> bool:
        return self._jobs[jid]["cancel"].is_set()

    def _worker(self) -> None:
        while True:
            task = self._q.get()
            if task is None:
                return
            jid = task.job_id
            job = self._jobs.get(jid)
            if job is None or job["status"] == "cancelled":
                continue
            job["status"] = "running"
            try:
                self._run_task(task)
                if self._is_cancelled(jid):
                    self.job_done.emit(jid, False, "Отменено")
                else:
                    self.job_done.emit(jid, True, "Готово")
            except Cancelled:
                self.job_done.emit(jid, False, "Отменено")
            except Exception as e:  # noqa: BLE001
                _log.warning("transfer failed: %s\n%s", e, traceback.format_exc())
                self.job_done.emit(jid, False, friendly(e))
                if is_conn_lost(e):
                    self.conn_lost.emit()

    def _run_task(self, task: _Task) -> None:
        jid = task.job_id
        sftp = self._open_sftp()  # свой SFTPClient на общем транспорте
        try:
            remote = SftpBackend(sftp)
            if task.direction == "up":
                files = self._expand_up(task.src_paths, task.dst_dir, remote, jid)
            else:
                files = self._expand_down(sftp, task.src_paths, task.dst_dir, jid)
            total = sum(f.size for f in files)
            done = 0
            self.progressed.emit(jid, 0, total, 0.0)
            for f in files:
                if self._is_cancelled(jid):
                    raise Cancelled()
                dst = self._resolve_conflict(jid, task.direction, f, remote)
                if dst is None:
                    continue  # пропущен
                done = self._copy_one(jid, task.direction, f.src, dst, f.size,
                                      done, total, sftp, remote)
        finally:
            try:
                sftp.close()
            except Exception:
                pass

    # --- разворачивание папок в список файлов ---
    def _expand_up(self, srcs: list, dst_dir: str, remote: SftpBackend, jid: int) -> list[TxFile]:
        files: list[TxFile] = []
        for src in srcs:
            if self._is_cancelled(jid):
                raise Cancelled()
            if os.path.isdir(src) and not os.path.islink(src):
                base = os.path.basename(src.rstrip(os.sep)) or src
                for root, _dirs, names in os.walk(src):
                    rel = os.path.relpath(root, src)
                    rdir = dst_dir.rstrip("/") + "/" + base if rel == "." else \
                        dst_dir.rstrip("/") + "/" + base + "/" + rel.replace(os.sep, "/")
                    remote.mkdirs(rdir)
                    if self._is_cancelled(jid):
                        raise Cancelled()
                    for n in names:
                        full = os.path.join(root, n)
                        if os.path.islink(full) or not os.path.isfile(full):
                            continue
                        files.append(TxFile(src=full, size=os.path.getsize(full),
                                            dst=rdir + "/" + n))
                # пустые папки уже созданы через mkdirs
            elif os.path.isfile(src) or os.path.islink(src):
                files.append(TxFile(src=src, size=os.path.getsize(src),
                                    dst=dst_dir.rstrip("/") + "/" + os.path.basename(src)))
        return files

    def _expand_down(self, sftp, srcs: list, dst_dir: str, jid: int) -> list[TxFile]:
        remote = SftpBackend(sftp)
        files: list[TxFile] = []
        for src in srcs:
            if self._is_cancelled(jid):
                raise Cancelled()
            try:
                st = sftp.stat(src)
            except OSError as e:
                raise e
            if statmod.S_ISDIR(st.st_mode or 0):
                base = posixpath.basename(src.rstrip("/")) or "dir"
                for dirpath, filenames, _dirs in remote._walk(src):
                    rel = posixpath.relpath(dirpath, src)
                    ldir = os.path.join(dst_dir, base) if rel == "." else \
                        os.path.join(dst_dir, base, *rel.split("/"))
                    os.makedirs(ldir, exist_ok=True)
                    if self._is_cancelled(jid):
                        raise Cancelled()
                    for n in filenames:
                        rfile = dirpath.rstrip("/") + "/" + n
                        try:
                            a = sftp.stat(rfile)
                        except OSError:
                            continue
                        if statmod.S_ISDIR(a.st_mode or 0):
                            continue
                        files.append(TxFile(src=rfile, size=a.st_size or 0,
                                            dst=os.path.join(ldir, n)))
            else:
                files.append(TxFile(src=src, size=st.st_size or 0,
                                    dst=os.path.join(dst_dir, posixpath.basename(src))))
        return files

    # --- конфликты ---
    def _resolve_conflict(self, jid: int, direction: str, f: TxFile, remote: SftpBackend) -> Optional[str]:
        if direction == "up":
            try:
                st = remote.stat(f.dst)
                dst_size = st.size
            except OSError:
                return f.dst
        else:
            if not os.path.lexists(f.dst):
                return f.dst
            dst_size = os.path.getsize(f.dst) if os.path.isfile(f.dst) else 0
        decision = self._apply_all
        if decision is None:
            req = ConflictReq(filename=posixpath.basename(f.dst) if direction == "up"
                              else os.path.basename(f.dst),
                              src_size=f.size, dst_size=dst_size)
            self.conflict_needed.emit(req)
            while not req.event.wait(0.2):
                if self._is_cancelled(jid):
                    return None
            decision, apply_all = req.decision, req.apply_all
            if apply_all:
                self._apply_all = decision
        if decision == "skip":
            return None
        if decision == "rename":
            return self._unique_name(direction, f.dst, remote)
        return f.dst

    def _on_conflict_gui(self, req: ConflictReq) -> None:
        # выполняется в UI-потоке (сигнал)
        if self._apply_all is not None:
            req.decision, req.apply_all = self._apply_all, True
            req.event.set()
            return
        parent = self.parent() if isinstance(self.parent(), QWidget) else None
        dlg = ConflictDialog(parent, req.filename, req.src_size, req.dst_size)
        try:
            req.decision, req.apply_all = dlg.decision()
        except Exception as e:  # noqa: BLE001
            _log.warning("conflict dialog failed: %s", e)
            req.decision, req.apply_all = "skip", False
        req.event.set()

    @staticmethod
    def _unique_name(direction: str, dst: str, remote: SftpBackend) -> str:
        if direction == "up":
            base, dot, ext = dst.rpartition(".")
            if not dot or "/" in base:
                base, ext = dst, ""
            i = 2
            while True:
                cand = f"{base} ({i}){('.' + ext) if ext else ''}"
                try:
                    remote.stat(cand)
                    i += 1
                except OSError:
                    return cand
        else:
            root, ext = os.path.splitext(dst)
            if not os.path.lexists(dst):
                return dst
            i = 2
            while os.path.lexists(f"{root} ({i}){ext}"):
                i += 1
            return f"{root} ({i}){ext}"

    # --- копирование одного файла ---
    def _copy_one(self, jid: int, direction: str, src: str, dst: str, size: int,
                  done: int, total: int, sftp, remote: SftpBackend) -> int:
        cancel = self._jobs[jid]["cancel"]
        state = {"last_t": time.monotonic(), "last_done": done, "speed": 0.0, "done": done}

        def cb(sent: int, _total: int):
            if cancel.is_set():
                raise Cancelled()
            now = time.monotonic()
            state["done"] = done + sent
            if now - state["last_t"] >= 0.15:
                dt = now - state["last_t"]
                state["speed"] = (state["done"] - state["last_done"]) / dt if dt > 0 else 0.0
                state["last_t"], state["last_done"] = now, state["done"]
                self.progressed.emit(jid, state["done"], total, state["speed"])

        if direction == "up":
            tmp = dst + ".part"
            try:
                sftp.put(src, tmp, callback=cb, confirm=False)
                remote.rename(tmp, dst)
            except Exception:
                try:
                    sftp.remove(tmp)
                except Exception:
                    pass
                raise
        else:
            tmp = dst + ".part"
            try:
                sftp.get(src, tmp, callback=cb)
                os.replace(tmp, dst)
            except Exception:
                try:
                    if os.path.lexists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                raise
        state["done"] = done + size
        self.progressed.emit(jid, state["done"], total, state["speed"])
        return state["done"]


# ---------- таблица панели ----------
class _PaneTable(QTableWidget):
    def __init__(self, pane: "FilePane"):
        super().__init__(pane)
        self._pane = pane
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)
        self.viewport().setAcceptDrops(True)

    def startDrag(self, actions) -> None:
        paths = self._pane.selected_full_paths()
        if not paths:
            return
        import json as _json
        mime = QMimeData()
        mime.setData("application/x-bm-files",
                     _json.dumps({"side": self._pane.side, "paths": paths}).encode("utf-8"))
        if self._pane.side == "local":
            mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasFormat("application/x-bm-files") or e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e) -> None:
        if e.mimeData().hasFormat("application/x-bm-files") or e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e) -> None:
        import json as _json
        md = e.mimeData()
        if md.hasFormat("application/x-bm-files"):
            try:
                data = _json.loads(bytes(md.data("application/x-bm-files")).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return
            if data.get("side") == self._pane.side:
                return
            e.acceptProposedAction()
            self._pane.transfer_requested.emit(data["side"], data.get("paths", []), self._pane.path)
            return
        if md.hasUrls():
            locals_ = [u.toLocalFile() for u in md.urls() if u.isLocalFile() and u.toLocalFile()]
            if not locals_:
                return
            e.acceptProposedAction()
            if self._pane.side == "remote":
                self._pane.transfer_requested.emit("local", locals_, self._pane.path)
            else:
                self._pane.copy_local_urls(locals_)
            return
        super().dropEvent(e)


# ---------- панель ----------
class FilePane(QFrame):
    transfer_requested = Signal(str, list, str)  # src_side, src_paths, dst_dir
    status_message = Signal(str)
    navigated = Signal(str)
    conn_lost = Signal()  # только remote-панель: обрыв соединения при операции

    COLS_LOCAL = ["Имя", "Размер", "Изменён"]
    COLS_REMOTE = ["Имя", "Размер", "Изменён", "Права"]

    def __init__(self, side: str, title: str, show_mode: bool, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.side = side
        self.backend = None
        self.runner = DaemonRunner(self)
        self.path = ""
        self.entries: list[Entry] = []
        self.sort_col = 0
        self.sort_desc = False
        self.show_hidden = False
        st = self.style()
        self._icon_dir = st.standardIcon(QStyle.SP_DirIcon)
        self._icon_file = st.standardIcon(QStyle.SP_FileIcon)
        self._icon_provider = QFileIconProvider()
        self._icon_cache: dict = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 14)
        lay.setSpacing(8)

        self.lbl_title = QLabel(title)
        self.lbl_title.setObjectName("paneTitle")
        lay.addWidget(self.lbl_title)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.edit_path = QLineEdit()
        self.edit_path.setPlaceholderText("путь…")
        self.edit_path.returnPressed.connect(self._on_path_entered)
        self.btn_up = QPushButton()
        self.btn_up.setObjectName("btnGhost")
        self.btn_up.setFixedWidth(36)
        self.btn_up.setIcon(self.style().standardIcon(QStyle.SP_ArrowUp))
        self.btn_up.setToolTip("Вверх")
        self.btn_up.setCursor(Qt.PointingHandCursor)
        self.btn_up.clicked.connect(self.go_parent)
        self.btn_refresh = QPushButton()
        self.btn_refresh.setObjectName("btnGhost")
        self.btn_refresh.setFixedWidth(36)
        self.btn_refresh.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.btn_refresh.setToolTip("Обновить")
        self.btn_refresh.setCursor(Qt.PointingHandCursor)
        self.btn_refresh.clicked.connect(self.reload)
        self.chk_hidden = QCheckBox("скрытые")
        self.chk_hidden.toggled.connect(self._on_hidden_toggled)
        self.btn_mkdir = QPushButton("+ Папка")
        self.btn_mkdir.setObjectName("btnGhost")
        self.btn_mkdir.setCursor(Qt.PointingHandCursor)
        self.btn_mkdir.clicked.connect(self.do_mkdir)
        self.btn_send = QPushButton("Загрузить ↑" if side == "local" else "↓ Скачать")
        self.btn_send.setObjectName("btnGhost")
        self.btn_send.setCursor(Qt.PointingHandCursor)
        self.btn_send.clicked.connect(self.send_selected)
        bar.addWidget(self.edit_path, 1)
        for w in (self.btn_up, self.btn_refresh, self.btn_mkdir, self.btn_send):
            bar.addWidget(w)
        bar.addWidget(self.chk_hidden)
        lay.addLayout(bar)

        self.table = _PaneTable(self)
        self.table.setObjectName("fileTable")
        self.table.viewport().setAutoFillBackground(False)
        cols = self.COLS_REMOTE if show_mode else self.COLS_LOCAL
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        h = self.table.horizontalHeader()
        h.setHighlightSections(False)
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(cols)):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        h.sectionClicked.connect(self._on_sort_clicked)
        self.table.itemDoubleClicked.connect(self._on_double_click)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self._sc_rename = QAction(self)
        self._sc_rename.setShortcut("F2")
        self._sc_rename.triggered.connect(self.do_rename)
        self.addAction(self._sc_rename)
        self._sc_del = QAction(self)
        self._sc_del.setShortcut(Qt.Key_Delete)
        self._sc_del.triggered.connect(self.do_delete)
        self.addAction(self._sc_del)
        self._sc_open = QAction(self.table)
        self._sc_open.setShortcut(Qt.Key_Return)
        self._sc_open.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self._sc_open.triggered.connect(self._on_enter_pressed)
        self.table.addAction(self._sc_open)
        lay.addWidget(self.table, 1)

    # --- backend ---
    def set_backend(self, backend) -> None:
        self.backend = backend

    def _need_backend(self) -> bool:
        if self.backend is None:
            self.status_message.emit("Нет подключения к серверу")
            return False
        return True

    def _op_error(self, err) -> None:
        """Ошибка операции: в статус + сигнал обрыва (только remote-панель)."""
        self.status_message.emit(friendly(err))
        if self.side == "remote" and is_conn_lost(err):
            self.conn_lost.emit()

    # --- навигация (всё через runner) ---
    def go(self, path: str) -> None:
        if not self._need_backend():
            return
        be = self.backend

        def work():
            return path, be.listdir(path)

        def done(res, err):
            if err:
                msg = friendly(err)
                self.status_message.emit(msg)
                if self.side == "remote" and is_conn_lost(err):
                    self.conn_lost.emit()
                self.edit_path.setText(self._pretty_path())
                if not self.entries:
                    self.show_notice(msg)
                return
            _path, entries = res
            self.path = _path
            self.entries = entries
            self.edit_path.setText(self._pretty_path())
            self._render()
            self.navigated.emit(self.path)

        self.runner.submit(work, done)

    def _pretty_path(self) -> str:
        if self.side == "local" and not self.path:
            return "Этот компьютер"
        return self.path

    def reload(self) -> None:
        self.go(self.path)

    def go_parent(self) -> None:
        if not self._need_backend():
            return
        self.go(self.backend.parent(self.path))

    def _on_path_entered(self) -> None:
        self.go(self.edit_path.text().strip())

    def _on_hidden_toggled(self, on: bool) -> None:
        self.show_hidden = on
        self._render()

    # --- таблица ---
    def _visible(self) -> list[Entry]:
        items = self.entries if self.show_hidden else [e for e in self.entries if not e.name.startswith(".")]
        key = {0: lambda e: e.name.lower(), 1: lambda e: e.size,
               2: lambda e: e.mtime, 3: lambda e: e.mode}.get(self.sort_col, lambda e: e.name.lower())
        dirs = sorted([e for e in items if e.is_dir], key=key, reverse=self.sort_desc)
        files = sorted([e for e in items if not e.is_dir], key=key, reverse=self.sort_desc)
        return dirs + files

    def _file_icon(self, name: str):
        """Настоящая иконка ОС по расширению (py ≠ txt), с кэшем."""
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in self._icon_cache:
            try:
                self._icon_cache[ext] = self._icon_provider.icon(QFileInfo(name))
            except Exception:
                self._icon_cache[ext] = self._icon_file
        return self._icon_cache[ext]

    def _show_dotdot(self) -> bool:
        """Строка '..' — везде, кроме корня (там выше идти некуда)."""
        if self.backend is None:
            return False
        if self.side == "local":
            return bool(self.path)
        return self.path not in ("", "/")

    def show_notice(self, text: str) -> None:
        """Заглушка вместо пустой таблицы: 'нет подключения', текст ошибки и т.п."""
        self.table.clearSpans()
        self.table.setRowCount(1)
        it = QTableWidgetItem(f"  {text}")
        it.setFlags(Qt.NoItemFlags)
        it.setData(Qt.UserRole, {"dotdot": False, "entry": None})
        self.table.setItem(0, 0, it)
        if self.table.columnCount() > 1:
            self.table.setSpan(0, 0, 1, self.table.columnCount())

    def _render(self) -> None:
        self.table.clearSpans()
        rows = self._visible()
        show_mode = self.table.columnCount() == 4
        dotdot = self._show_dotdot()
        self.table.setRowCount(len(rows) + (1 if dotdot else 0))
        start = 0
        if dotdot:
            it = QTableWidgetItem("..")
            it.setIcon(self._icon_dir)
            it.setData(Qt.UserRole, {"dotdot": True, "entry": None})
            self.table.setItem(0, 0, it)
            for j in range(1, self.table.columnCount()):
                self.table.setItem(0, j, QTableWidgetItem(""))
            start = 1
        for k, e in enumerate(rows):
            i = k + start
            name = e.name + (" →" if e.is_link else "")
            vals = [name,
                    "" if e.is_dir else human_size(e.size),
                    fmt_time(e.mtime)] + ([e.mode if not e.is_dir or e.mode else ""] if show_mode else [])
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setData(Qt.UserRole, {"dotdot": False, "entry": e})
                if j == 0:
                    it.setIcon(self._icon_dir if e.is_dir else self._file_icon(e.name))
                self.table.setItem(i, j, it)
        arrows = {0: " ▲" if not self.sort_desc else " ▼"}.get(self.sort_col, "")
        base = self.COLS_REMOTE if show_mode else self.COLS_LOCAL
        self.table.setHorizontalHeaderLabels(
            [(c + arrows) if n == self.sort_col else c for n, c in enumerate(base)])

    def _on_sort_clicked(self, col: int) -> None:
        if col == self.sort_col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = col, False
        self._render()

    def _row_info(self, row: int) -> dict:
        it = self.table.item(row, 0)
        return it.data(Qt.UserRole) if it else {}

    def selected_entries(self) -> list[Entry]:
        rows = sorted({it.row() for it in self.table.selectedItems()})
        out = []
        for r in rows:
            info = self._row_info(r) or {}
            if not info.get("dotdot") and info.get("entry") is not None:
                out.append(info["entry"])
        return out

    def _selection_has_dotdot(self) -> bool:
        for it in self.table.selectedItems():
            if it.column() == 0 and ((it.data(Qt.UserRole) or {}).get("dotdot")):
                return True
        return False

    def selected_full_paths(self) -> list[str]:
        be = self.backend
        return [be.join(self.path, e.name) for e in self.selected_entries()] if be else []

    def _on_double_click(self, item: QTableWidgetItem) -> None:
        info = item.data(Qt.UserRole) or {}
        if info.get("dotdot"):
            return self.go_parent()
        e = info.get("entry")
        if e is None:
            return
        if e.is_dir:
            self.go(self.backend.join(self.path, e.name))
        else:
            full = self.backend.join(self.path, e.name)
            self.transfer_requested.emit(self.side, [full], "")

    def _on_enter_pressed(self) -> None:
        rows = sorted({it.row() for it in self.table.selectedItems() if it.column() == 0})
        if len(rows) != 1:
            return
        info = self._row_info(rows[0]) or {}
        if info.get("dotdot"):
            return self.go_parent()
        e = info.get("entry")
        if e is None:
            return
        if e.is_dir:
            self.go(self.backend.join(self.path, e.name))
        elif self.side == "local":
            self.do_open_local()
        else:
            self.send_selected()

    def send_selected(self) -> None:
        paths = self.selected_full_paths()
        if not paths:
            self.status_message.emit("Ничего не выбрано")
            return
        self.transfer_requested.emit(self.side, paths, "")

    # --- контекстное меню и операции ---
    def _on_context_menu(self, pos) -> None:
        sel = self.selected_entries()
        dotdot = self._selection_has_dotdot()
        m = QMenu(self)
        if self.side == "local":
            a_open = m.addAction("Открыть")
            a_open.setEnabled(len(sel) == 1 and not dotdot)
            a_open.triggered.connect(self.do_open_local)
            m.addSeparator()
        a_go = m.addAction("↑ Загрузить" if self.side == "local" else "↓ Скачать")
        a_go.setEnabled(bool(sel) and not dotdot)
        a_go.triggered.connect(self.send_selected)
        m.addSeparator()
        a_mkdir = m.addAction("Новая папка")
        a_mkdir.triggered.connect(self.do_mkdir)
        a_ren = m.addAction("Переименовать (F2)")
        a_ren.setEnabled(len(sel) == 1 and not dotdot)
        a_ren.triggered.connect(self.do_rename)
        a_del = m.addAction("Удалить (Del)")
        a_del.setEnabled(bool(sel) and not dotdot)
        a_del.triggered.connect(self.do_delete)
        a_chmod = None
        if self.side == "remote":
            a_chmod = m.addAction("Права (chmod)…")
            a_chmod.setEnabled(len(sel) == 1)
            a_chmod.triggered.connect(self.do_chmod)
        a_copy = m.addAction("Копировать путь")
        a_copy.setEnabled(len(sel) == 1 and not dotdot)
        a_copy.triggered.connect(self.do_copy_path)
        m.addSeparator()
        a_ref = m.addAction("Обновить")
        a_ref.triggered.connect(self.reload)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _full(self, e: Entry) -> str:
        return self.backend.join(self.path, e.name)

    def do_mkdir(self) -> None:
        if not self._need_backend():
            return
        name, ok = QInputDialog.getText(self, "Новая папка", "Имя папки:")
        if not ok or not name.strip():
            return
        target = self.backend.join(self.path, name.strip())
        be = self.backend

        def done(_res, err):
            if err:
                self._op_error(err)
            else:
                self.status_message.emit(f"Папка создана: {name.strip()}")
                self.reload()

        self.runner.submit(lambda: be.mkdir(target), done)

    def do_rename(self) -> None:
        sel = self.selected_entries()
        if len(sel) != 1 or not self._need_backend():
            return
        e = sel[0]
        name, ok = QInputDialog.getText(self, "Переименовать", "Новое имя:", text=e.name)
        if not ok or not name.strip() or name.strip() == e.name:
            return
        be, src = self.backend, self._full(e)
        dst = be.join(self.path, name.strip())

        def done(_res, err):
            if err:
                self._op_error(err)
            else:
                self.reload()

        self.runner.submit(lambda: be.rename(src, dst), done)

    def do_delete(self) -> None:
        sel = self.selected_entries()
        if not sel or not self._need_backend():
            return
        names = ", ".join(e.name for e in sel[:5]) + ("…" if len(sel) > 5 else "")
        if QMessageBox.question(self, "Удалить",
                                f"Удалить ({len(sel)}): {names}?") != QMessageBox.Yes:
            return
        be, path = self.backend, self.path
        items = [(self._full(e), e.is_dir) for e in sel]

        def work():
            for full, is_dir in items:
                be.remove(full, is_dir)
            return path, be.listdir(path)

        def done(res, err):
            if err:
                self._op_error(err)
                self.reload()
                return
            _path, entries = res
            self.entries = entries
            self._render()
            self.status_message.emit(f"Удалено: {len(items)}")

        self.status_message.emit("Удаление…")
        self.runner.submit(work, done)

    def do_chmod(self) -> None:
        sel = self.selected_entries()
        if len(sel) != 1 or self.side != "remote" or not self._need_backend():
            return
        e = sel[0]
        cur = ""
        if e.mode and len(e.mode) >= 9:
            m = e.mode[:9]
            cur = "".join(
                str((4 if t[0] == "r" else 0) + (2 if t[1] == "w" else 0)
                    + (1 if t[2] in ("x", "s") else 0))
                for t in (m[0:3], m[3:6], m[6:9]))
        mode_s, ok = QInputDialog.getText(self, "Права (chmod)", "Например 755:", text=cur or "644")
        if not ok or not mode_s.strip():
            return
        try:
            mode = int(mode_s.strip(), 8)
        except ValueError:
            return self.status_message.emit("Права должны быть числом, например 755")
        be, full = self.backend, self._full(e)

        def done(_res, err):
            if err:
                self._op_error(err)
            else:
                self.reload()

        self.runner.submit(lambda: be.chmod(full, mode), done)

    def do_open_local(self) -> None:
        """Открыть файл/папку программой Windows по умолчанию. Только левая панель."""
        sel = self.selected_entries()
        if len(sel) != 1 or self.side != "local":
            return
        full = self._full(sel[0])
        if open_local_file(full):
            self.status_message.emit(f"Открываю: {sel[0].name}")
        else:
            self.status_message.emit(f"Не удалось открыть: {full}")

    def do_copy_path(self) -> None:
        sel = self.selected_entries()
        if len(sel) == 1:
            QApplication.clipboard().setText(self._full(sel[0]))
            self.status_message.emit("Путь скопирован")

    def copy_local_urls(self, paths: list[str]) -> None:
        """Drop из проводника на локальную панель: копируем внутрь текущей папки."""
        if self.side != "local" or self.backend is None:
            return
        dst_dir = self.path or self.backend.home()

        def work():
            skipped = 0
            for src in paths:
                base = os.path.basename(src.rstrip(os.sep)) or "file"
                dst = os.path.join(dst_dir, base)
                try:
                    if os.path.isdir(src) and not os.path.islink(src):
                        shutil.copytree(src, dst, symlinks=True,
                                        ignore_dangling_symlinks=True)
                    else:
                        if os.path.lexists(dst):
                            skipped += 1
                            continue
                        shutil.copy2(src, dst)
                except OSError:
                    skipped += 1
            return skipped

        def done(res, err):
            if err:
                self._op_error(err)
            else:
                self.status_message.emit("Скопировано" + (f", пропущено: {res}" if res else ""))
                self.reload()

        self.runner.submit(work, done)


# ---------- очередь (виджет) ----------
class QueueWidget(QFrame):
    clear_finished = Signal()
    cancel_all = Signal()
    cancel_job = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        top = QHBoxLayout()
        t = QLabel("Очередь передач")
        t.setObjectName("paneTitle")
        top.addWidget(t)
        top.addStretch()
        self.btn_clear = QPushButton("Очистить завершённые")
        self.btn_clear.setObjectName("btnGhost")
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.clicked.connect(self.clear_finished.emit)
        self.btn_cancel_all = QPushButton("Отменить всё")
        self.btn_cancel_all.setObjectName("btnGhost")
        self.btn_cancel_all.setCursor(Qt.PointingHandCursor)
        self.btn_cancel_all.clicked.connect(self.cancel_all.emit)
        top.addWidget(self.btn_clear)
        top.addWidget(self.btn_cancel_all)
        lay.addLayout(top)

        self.table = QTableWidget(0, 6)
        self.table.setObjectName("queueTable")
        self.table.viewport().setAutoFillBackground(False)
        self.table.setHorizontalHeaderLabels(["Файл", "", "Прогресс", "Скорость", "Статус", ""])
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        h = self.table.horizontalHeader()
        h.setHighlightSections(False)
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        lay.addWidget(self.table)
        self._rows: dict[int, int] = {}  # job_id -> row

    def add_job(self, job_id: int, display: str, direction: str) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._rows[job_id] = row
        arrow = "↑" if direction == "up" else "↓"
        self.table.setItem(row, 0, QTableWidgetItem(display))
        self.table.setItem(row, 1, QTableWidgetItem(arrow))
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        self.table.setCellWidget(row, 2, bar)
        self.table.setItem(row, 3, QTableWidgetItem(""))
        self.table.setItem(row, 4, QTableWidgetItem("В очереди"))
        btn = QPushButton("×")
        btn.setObjectName("btnGhost")
        btn.setFixedWidth(32)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda _=False, j=job_id: self.cancel_job.emit(j))
        self.table.setCellWidget(row, 5, btn)

    def update_job(self, job_id: int, done_b: int, total_b: int, speed: float) -> None:
        row = self._rows.get(job_id)
        if row is None:
            return
        bar = self.table.cellWidget(row, 2)
        if bar and total_b > 0:
            bar.setValue(int(done_b * 100 / total_b))
        self.table.setItem(row, 3, QTableWidgetItem(
            f"{human_size(int(speed))}/с" if speed > 0 else ""))
        st = self.table.item(row, 4)
        if st and st.text() in ("В очереди", "Передача"):
            st.setText(f"Передача · {human_size(done_b)} / {human_size(total_b)}")

    def finish_job(self, job_id: int, ok: bool, message: str) -> None:
        row = self._rows.get(job_id)
        if row is None:
            return
        bar = self.table.cellWidget(row, 2)
        if bar and ok:
            bar.setValue(100)
        self.table.setItem(row, 4, QTableWidgetItem("Готово" if ok else message[:60]))

    def remove_finished(self) -> None:
        for row in range(self.table.rowCount() - 1, -1, -1):
            it = self.table.item(row, 4)
            t = it.text() if it else ""
            if t == "Готово" or t == "Отменено" or t.startswith("Ошибка") \
                    or t.startswith("Нет "):
                self._drop_row(row)

    def _drop_row(self, row: int) -> None:
        for jid, r in list(self._rows.items()):
            if r == row:
                del self._rows[jid]
            elif r > row:
                self._rows[jid] = r - 1
        self.table.removeRow(row)

    def clear_all_jobs(self) -> None:
        self._rows.clear()
        self.table.setRowCount(0)


# ---------- вкладка ----------
class FilesTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(FILES_QSS)
        self._creds: Optional[dict] = None
        self._pending_remote = ""
        self._ssh: Optional["paramiko.SSHClient"] = None
        self._sftp = None
        self._connected = False
        self._activated = False
        self._profile = ""
        self._state = self._load_state()
        self._local = LocalBackend()
        self._conn_lock = threading.Lock()
        self._ops = DaemonRunner(self)  # разовые операции (подключение и т.п.)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 12)
        lay.setSpacing(10)

        # баннер
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        blay = QHBoxLayout(self.banner)
        blay.setContentsMargins(12, 8, 12, 8)
        self.banner_text = QLabel("")
        self.banner_text.setObjectName("bannerText")
        self.banner_text.setWordWrap(True)
        self.btn_reconnect = QPushButton("Переподключить")
        self.btn_reconnect.setObjectName("btnGhost")
        self.btn_reconnect.setCursor(Qt.PointingHandCursor)
        self.btn_reconnect.clicked.connect(lambda: self.activate(force=True))
        blay.addWidget(self.banner_text, 1)
        blay.addWidget(self.btn_reconnect)
        self.banner.hide()
        lay.addWidget(self.banner)

        # панели
        self.pane_local = FilePane("local", "Мой компьютер", show_mode=False)
        self.pane_local.set_backend(self._local)
        self.pane_local.chk_hidden.setChecked(bool(self._state.get("hidden", False)))
        self.pane_local.show_hidden = bool(self._state.get("hidden", False))
        self.pane_remote = FilePane("remote", "Сервер", show_mode=True)
        self.pane_remote.chk_hidden.setChecked(bool(self._state.get("hidden", False)))
        self.pane_remote.show_hidden = bool(self._state.get("hidden", False))
        self.pane_remote.show_notice("Подключитесь к серверу карточкой выше")
        for pane in (self.pane_local, self.pane_remote):
            pane.transfer_requested.connect(self._on_transfer_requested)
            pane.status_message.connect(self.set_status)
            pane.navigated.connect(self._on_pane_navigated)
            pane.chk_hidden.toggled.connect(self._on_hidden_global)
        self.pane_remote.conn_lost.connect(self._on_conn_lost)
        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(self.pane_local)
        self.split.addWidget(self.pane_remote)
        self.split.setSizes(self._state.get("splitter", [1, 1]))
        self.split.splitterMoved.connect(lambda *_: self._save_state())
        lay.addWidget(self.split, 1)

        # очередь
        self.manager = TransferManager(self._open_sftp, self._local, self)
        self.queue = QueueWidget()
        self.queue.setMaximumHeight(210)
        self.manager.progressed.connect(self.queue.update_job)
        self.manager.job_done.connect(self._on_job_done)
        self.manager.conn_lost.connect(self._on_conn_lost)
        self.queue.cancel_job.connect(self.manager.cancel)
        self.queue.cancel_all.connect(self.manager.cancel_all)
        self.queue.clear_finished.connect(self.queue.remove_finished)
        lay.addWidget(self.queue)

        self.status = QLabel("Выберите профиль и нажмите «Подключиться»")
        self.status.setObjectName("statusLine")
        lay.addWidget(self.status)

        # локальную панель грузим сразу (сеть не нужна)
        start = self._state.get("last_local") or self._local.home()
        self.pane_local.go(start)

    # --- состояние ---
    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            st = self._load_state()
            st["last_local"] = self.pane_local.path
            st["hidden"] = self.pane_local.show_hidden
            st["splitter"] = self.split.sizes()
            if self._profile:
                st.setdefault("profiles", {})[self._profile] = {
                    "local": self.pane_local.path, "remote": self.pane_remote.path}
            STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            _log.warning("save state failed: %s", e)

    def _on_pane_navigated(self, _path: str) -> None:
        self._save_state()

    def _on_hidden_global(self, on: bool) -> None:
        for pane in (self.pane_local, self.pane_remote):
            if pane.chk_hidden.isChecked() != on:
                pane.chk_hidden.blockSignals(True)
                pane.chk_hidden.setChecked(on)
                pane.chk_hidden.blockSignals(False)
            pane.show_hidden = on
            pane._render()
        self._save_state()

    # --- подключение (ленивое, своё соединение) ---
    def on_connected(self, creds: dict) -> None:
        """Вызывается из Win после успешного подключения. Только запоминает параметры."""
        new_profile = creds.get("profile")
        old_profile = (self._creds or {}).get("profile")
        self._creds = dict(creds)
        if not (self._connected or self._activated):
            return  # лениво: подключимся при первом открытии вкладки
        if new_profile == old_profile and self._connected and self._transport_alive():
            return  # та же живая сессия — ничего не делаем
        self._reset_connection()
        if self._activated:
            self._connect_async()

    def _transport_alive(self) -> bool:
        try:
            t = self._ssh.get_transport() if self._ssh else None
            return bool(t and t.is_active())
        except Exception:
            return False

    def activate(self, force: bool = False) -> None:
        """Первое открытие вкладки (или кнопка 'Переподключить')."""
        self._activated = True
        if not self._creds:
            self._show_banner("Подключитесь к серверу карточкой выше — логин вводить второй раз не нужно.",
                              error=False)
            self.set_status("Нет подключения")
            return
        if self._connected and not force:
            self.pane_local.reload()
            self.pane_remote.reload()
            return
        self._connect_async()

    def _on_conn_lost(self) -> None:
        """Обрыв соединения обнаружен операцией/передачей: сброс + баннер «Переподключить»."""
        if not self._connected:
            return
        self._reset_connection()
        self._show_banner("Соединение с сервером потеряно. Нажмите «Переподключить».", error=True)
        self.set_status("Нет соединения")

    def _reset_connection(self) -> None:
        with self._conn_lock:
            ssh, sftp = self._ssh, self._sftp
            self._ssh, self._sftp, self._connected = None, None, False
        for c in (ssh, sftp):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
        self.manager.reset_session()
        self.queue.clear_all_jobs()
        self.pane_remote.set_backend(None)
        self.pane_remote.entries = []
        self.pane_remote.path = ""
        self.pane_remote.show_notice("Нет соединения. Нажмите «Переподключить».")

    def _connect_async(self) -> None:
        self._reset_connection()
        self._show_banner("Подключение к серверу…", error=False)
        self.btn_reconnect.hide()
        self.set_status("Подключение…")
        creds = dict(self._creds)

        def work():
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(creds["host"], port=int(creds.get("port") or 22),
                           username=creds["user"], password=creds.get("password") or None,
                           key_filename=creds.get("key") or None, timeout=10,
                           allow_agent=True, look_for_keys=True)
            client.get_transport().set_keepalive(20)
            return client

        def done(client, err):
            if err:
                _log.warning("files connect failed: %s", err)
                self._show_banner(f"Не подключилось: {friendly(err)}", error=True)
                self.btn_reconnect.show()
                self.set_status("Нет соединения")
                return
            try:
                sftp = client.open_sftp()
            except Exception as e:  # noqa: BLE001
                _log.warning("open_sftp failed: %s", e)
                try:
                    client.close()
                except Exception:
                    pass
                self._show_banner(f"SFTP не открылся: {friendly(e)}", error=True)
                self.btn_reconnect.show()
                self.set_status("Нет соединения")
                return
            with self._conn_lock:
                self._ssh = client
                self._sftp = sftp
                self._connected = True
            self.banner.hide()
            self._profile = creds.get("profile", "")
            remote_be = SftpBackend(self._sftp)
            self.pane_remote.set_backend(remote_be)
            saved = (self._load_state().get("profiles", {}).get(self._profile, {}))
            if saved.get("local"):
                self.pane_local.go(saved["local"])
            pending, self._pending_remote = self._pending_remote, ""
            self.pane_remote.go(pending or saved.get("remote") or remote_be.home())
            self.set_status(f"Подключено: {self._profile}")

        self._ops.submit(work, done)

    def open_remote_dir(self, path: str) -> None:
        """Открыть папку на сервере: сразу, если подключено, иначе после коннекта."""
        if self._connected and self.pane_remote.backend is not None:
            self._pending_remote = ""
            self.pane_remote.go(path)
            return
        self._pending_remote = path
        if self._creds:
            self.activate(force=not self._connected)
        else:
            self.set_status("Нет подключения")

    def _open_sftp(self):
        with self._conn_lock:
            ssh = self._ssh
        if ssh is None:
            raise paramiko.SSHException("Нет соединения с сервером")
        return ssh.open_sftp()

    # --- баннер и статус ---
    def _show_banner(self, text: str, error: bool) -> None:
        self.banner.setObjectName("bannerErr" if error else "banner")
        self.banner.setStyleSheet(FILES_QSS)  # переприменяем, чтобы подхватился новый objectName
        self.banner_text.setText(text)
        self.btn_reconnect.setVisible(True)
        self.banner.show()

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    # --- передачи ---
    def _on_transfer_requested(self, src_side: str, paths: list, _dst: str) -> None:
        if src_side == "local":
            if not self._connected:
                self.set_status("Нет подключения к серверу")
                self._show_banner("Нет соединения с сервером. Нажмите «Переподключить».", error=True)
                return
            dst = self.pane_remote.path or "/"
            if not paths:
                return
            display = paths[0].split("/")[-1].split("\\")[-1] if len(paths) == 1 \
                else f"{len(paths)} файлов"
            jid = self.manager.submit("up", display, paths, dst)
            self.queue.add_job(jid, f"↑ {display}", "up")
            self.set_status(f"Загрузка: {display} → {dst}")
        else:
            dst = self.pane_local.path or self._local.home()
            if not paths:
                return
            display = paths[0].rstrip("/").split("/")[-1] if len(paths) == 1 \
                else f"{len(paths)} файлов"
            jid = self.manager.submit("down", display, paths, dst)
            self.queue.add_job(jid, f"↓ {display}", "down")
            self.set_status(f"Скачивание: {display} → {dst}")

    def _on_job_done(self, job_id: int, ok: bool, message: str) -> None:
        self.queue.finish_job(job_id, ok, message)
        self.set_status(message)
        # обновляем панели, чтобы было видно результат
        try:
            self.pane_local.reload()
        except Exception:
            pass
        if self._connected:
            try:
                self.pane_remote.reload()
            except Exception:
                pass
        self._save_state()
