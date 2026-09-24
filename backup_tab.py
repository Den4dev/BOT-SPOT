#!/usr/bin/env python3
"""Вкладка «Бэкапы»: UI. НЕ импортирует botmanager."""
import copy
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from backup_core import (
    BackupEngine, BackupStore, DaemonRunner, FIND_CMD, new_job, ssh_exec,
    human_size,
)

__all__ = ["BackupTab"]


def fmt_last(last: dict) -> str:
    t = (last or {}).get("time") or ""
    if not t:
        return "—"
    try:
        return datetime.fromisoformat(t).strftime("%d.%m %H:%M")
    except ValueError:
        return t


class JobDialog(QDialog):
    """Добавление / редактирование задания."""

    def __init__(self, parent, job: dict, find_fn):
        super().__init__(parent)
        self.setWindowTitle("Задание бэкапа")
        self.setModal(True)
        self._find_fn = find_fn
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.e_name = QLineEdit(job.get("name", ""))
        self.e_name.setPlaceholderText("remnabot")
        self.e_remote = QLineEdit(job.get("remote", ""))
        self.e_remote.setPlaceholderText("/opt/remnabot/bot.db")
        self.btn_find = QPushButton("Найти на сервере…")
        self.btn_find.setObjectName("btnGhost")
        self.btn_find.setCursor(Qt.PointingHandCursor)
        self.btn_find.clicked.connect(self._on_find)
        remote_row = QHBoxLayout()
        remote_row.addWidget(self.e_remote, 1)
        remote_row.addWidget(self.btn_find)
        remote_wrap = QWidget()
        remote_wrap.setLayout(remote_row)
        self.c_type = QComboBox()
        self.c_type.addItem("sqlite")
        self.c_type.setCurrentText(job.get("dbtype") or "sqlite")
        self.s_keep = QSpinBox()
        self.s_keep.setRange(0, 1000)
        self.s_keep.setValue(int(job.get("keep") or 0))
        self.s_keep.setToolTip("0 = не удалять")
        self.c_zip = QCheckBox("Сжимать в zip")
        self.c_zip.setChecked(bool(job.get("compress")))
        self.e_root = QLineEdit(job.get("local_root") or "")
        self.e_root.setPlaceholderText("по умолчанию — глобальный корень")
        self.btn_browse = QPushButton("…")
        self.btn_browse.setObjectName("btnGhost")
        self.btn_browse.setFixedWidth(36)
        self.btn_browse.clicked.connect(self._on_browse)
        root_row = QHBoxLayout()
        root_row.addWidget(self.e_root, 1)
        root_row.addWidget(self.btn_browse)
        root_wrap = QWidget()
        root_wrap.setLayout(root_row)
        form.addRow("Название:", self.e_name)
        form.addRow("База на сервере:", remote_wrap)
        form.addRow("Тип БД:", self.c_type)
        form.addRow("Хранить копий (0 — все):", self.s_keep)
        form.addRow("", self.c_zip)
        form.addRow("Папка на компьютере:", root_wrap)
        lay.addLayout(form)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("Сохранить")
        box.button(QDialogButtonBox.Cancel).setText("Отмена")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self.setMinimumWidth(460)

    def _on_browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка бэкапов задания",
                                             self.e_root.text() or str(Path.home()))
        if d:
            self.e_root.setText(d)

    def _on_find(self) -> None:
        dlg = FindDialog(self, self._find_fn)
        if dlg.exec() == QDialog.Accepted and dlg.selected:
            self.e_remote.setText(dlg.selected)

    def result_job(self, base: dict) -> dict:
        job = dict(base)
        job["name"] = self.e_name.text().strip() or "job"
        job["remote"] = self.e_remote.text().strip()
        job["dbtype"] = self.c_type.currentText()
        job["keep"] = self.s_keep.value()
        job["compress"] = self.c_zip.isChecked()
        job["local_root"] = self.e_root.text().strip()
        return job


class FindDialog(QDialog):
    """'Найти на сервере': список *.db/*.sqlite*. Выбор подставляет путь."""

    def __init__(self, parent, find_fn):
        super().__init__(parent)
        self.setWindowTitle("Найти базы на сервере")
        self.setModal(True)
        self.selected = ""
        lay = QVBoxLayout(self)
        self.lst = QListWidget()
        self.lst.addItem("Поиск…")
        self.lst.itemDoubleClicked.connect(lambda _it: self._accept_current())
        lay.addWidget(self.lst, 1)
        row = QHBoxLayout()
        self.btn_re = QPushButton("Обновить")
        self.btn_re.setObjectName("btnGhost")
        self.btn_re.clicked.connect(lambda: find_fn(self._fill))
        row.addWidget(self.btn_re)
        row.addStretch()
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("Выбрать")
        box.button(QDialogButtonBox.Cancel).setText("Отмена")
        box.accepted.connect(self._accept_current)
        box.rejected.connect(self.reject)
        row.addWidget(box)
        lay.addLayout(row)
        self.setMinimumSize(520, 340)
        find_fn(self._fill)

    def _fill(self, paths, err) -> None:
        self.lst.clear()
        if err:
            self.lst.addItem(f"Ошибка: {err}")
            return
        if not paths:
            self.lst.addItem("Ничего не найдено")
            return
        self.lst.addItems(paths)

    def _accept_current(self) -> None:
        it = self.lst.currentItem()
        if it and it.text() and not it.text().startswith("Ошибка") \
                and it.text() != "Ничего не найдено" and it.text() != "Поиск…":
            self.selected = it.text()
            self.accept()


class SettingsDialog(QDialog):
    def __init__(self, parent, root: str):
        super().__init__(parent)
        self.setWindowTitle("Настройки бэкапов")
        self.setModal(True)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.e_root = QLineEdit(root)
        self.btn_browse = QPushButton("…")
        self.btn_browse.setObjectName("btnGhost")
        self.btn_browse.setFixedWidth(36)
        self.btn_browse.clicked.connect(self._on_browse)
        row = QHBoxLayout()
        row.addWidget(self.e_root, 1)
        row.addWidget(self.btn_browse)
        wrap = QWidget()
        wrap.setLayout(row)
        form.addRow("Корень бэкапов:", wrap)
        lay.addLayout(form)
        hint = QLabel("Подсказка: в бэкапах лежат данные пользователей и, возможно, токены. "
                      "Не кладите корень в папки с автосинхронизацией в облако без необходимости.")
        hint.setWordWrap(True)
        hint.setObjectName("statusLine")
        lay.addWidget(hint)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("Сохранить")
        box.button(QDialogButtonBox.Cancel).setText("Отмена")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self.setMinimumWidth(460)

    def _on_browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Корень бэкапов", self.e_root.text())
        if d:
            self.e_root.setText(d)


class BackupTab(QWidget):
    COLS = ["Задание", "База на сервере", "Последний бэкап", "Статус"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._creds = None
        self._profile = ""
        self._running = False
        self.store = BackupStore()
        self.runner = DaemonRunner(self)
        self.engine = BackupEngine(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 12)
        lay.setSpacing(10)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn_add = QPushButton("+ Добавить")
        self.btn_add.setObjectName("btnPrimary")
        self.btn_edit = QPushButton("Изменить")
        self.btn_edit.setObjectName("btnGhost")
        self.btn_del = QPushButton("Удалить")
        self.btn_del.setObjectName("btnDanger")
        self.btn_one = QPushButton("Бэкап выбранного")
        self.btn_one.setObjectName("btnSuccess")
        self.btn_all = QPushButton("Бэкап всех")
        self.btn_all.setObjectName("btnSuccess")
        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.setObjectName("btnGhost")
        self.btn_cancel.setEnabled(False)
        self.btn_folder = QPushButton("Папка")
        self.btn_folder.setObjectName("btnGhost")
        self.btn_settings = QPushButton("Настройки")
        self.btn_settings.setObjectName("btnGhost")
        for b in (self.btn_add, self.btn_edit, self.btn_del, self.btn_one,
                  self.btn_all, self.btn_cancel, self.btn_folder, self.btn_settings):
            b.setCursor(Qt.PointingHandCursor)
            bar.addWidget(b)
        bar.addStretch()
        lay.addLayout(bar)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setObjectName("fileTable")
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        h = self.table.horizontalHeader()
        h.setHighlightSections(False)
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.itemDoubleClicked.connect(lambda _it: self.backup_selected())
        lay.addWidget(self.table, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("Готов")
        lay.addWidget(self.progress)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(150)
        lay.addWidget(self.log)

        self.status = QLabel("Выберите профиль и нажмите «Подключиться»")
        self.status.setObjectName("statusLine")
        lay.addWidget(self.status)

        self.btn_add.clicked.connect(self.add_job)
        self.btn_edit.clicked.connect(self.edit_job)
        self.btn_del.clicked.connect(self.delete_job)
        self.btn_one.clicked.connect(self.backup_selected)
        self.btn_all.clicked.connect(self.backup_all)
        self.btn_cancel.clicked.connect(self.cancel_run)
        self.btn_folder.clicked.connect(self.open_folder)
        self.btn_settings.clicked.connect(self.open_settings)
        self.engine.sig_log.connect(self._on_log)
        self.engine.sig_progress.connect(self._on_progress)
        self.engine.sig_job_done.connect(self._on_job_done)
        self.engine.sig_all_done.connect(self._on_all_done)
        self.engine.sig_refresh.connect(self.render)
        self._jobs_cache = []
        self.render()

    # --- подключение ---
    def on_connected(self, creds: dict) -> None:
        self._creds = dict(creds)
        self._profile = creds.get("profile", "")
        self.render()
        self._log(f"Профиль: {self._profile}. Заданий: {len(self._jobs_cache)}")

    def _need(self) -> bool:
        if not self._creds:
            self._log("Нет подключения: выберите профиль и нажмите «Подключиться»")
            QMessageBox.information(self, "Бэкапы", "Сначала подключитесь к серверу.")
            return False
        return True

    def _selected(self):
        r = self.table.currentRow()
        if r < 0 or r >= len(self._jobs_cache):
            return None
        return copy.deepcopy(self._jobs_cache[r])

    # --- таблица ---
    def render(self) -> None:
        jobs = self.store.jobs(self._profile) if self._profile else []
        self._jobs_cache = jobs
        self.table.setRowCount(len(jobs))
        for i, j in enumerate(jobs):
            last = j.get("last") or {}
            vals = [j.get("name", ""), j.get("remote", ""), fmt_last(last), self._status_text(last)]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 0:
                    f = QFont()
                    f.setBold(True)
                    it.setFont(f)
                if c == 3:
                    ok = last.get("ok")
                    it.setForeground(QColor("#29D17D") if ok is True
                                     else QColor("#FF4D59") if ok is False else QColor("#8496A8"))
                self.table.setItem(i, c, it)

    @staticmethod
    def _status_text(last: dict) -> str:
        ok = (last or {}).get("ok")
        if ok is True:
            size = human_size((last or {}).get("size"))
            return f"● ок {size}".strip()
        if ok is False:
            msg = (last or {}).get("msg") or "ошибка"
            return f"● {msg[:40]}"
        return "○ не было"

    # --- задания ---
    def add_job(self) -> None:
        if not self._need():
            return
        dlg = JobDialog(self, new_job(), self._run_find)
        if dlg.exec() == QDialog.Accepted:
            job = dlg.result_job(new_job())
            self.store.save_job(self._profile, job)
            self.render()
            self._log(f"Задание добавлено: {job['name']}")

    def edit_job(self) -> None:
        sel = self._selected()
        if sel is None:
            return self._log("Выберите задание")
        dlg = JobDialog(self, sel, self._run_find)
        if dlg.exec() == QDialog.Accepted:
            self.store.save_job(self._profile, dlg.result_job(sel))
            self.render()

    def delete_job(self) -> None:
        sel = self._selected()
        if sel is None:
            return self._log("Выберите задание")
        if QMessageBox.question(self, "Удалить задание",
                                f'Удалить задание "{sel.get("name")}"? Файлы бэкапов останутся.') \
                != QMessageBox.Yes:
            return
        self.store.delete_job(self._profile, sel.get("id", ""))
        self.render()
        self._log(f"Задание удалено: {sel.get('name')}")

    def _run_find(self, cb) -> None:
        """find на сервере в фоне, результат — в UI-поток."""
        if not self._creds:
            cb([], "Нет подключения")
            return
        creds = dict(self._creds)

        def work():
            import paramiko
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(creds["host"], port=int(creds.get("port") or 22),
                           username=creds["user"], password=creds.get("password") or None,
                           key_filename=creds.get("key") or None, timeout=10,
                           allow_agent=True, look_for_keys=True)
            try:
                from backup_core import ssh_exec
                rc, o, e = ssh_exec(client, FIND_CMD)
                if rc != 0:
                    return [], (e.strip() or f"find: rc={rc}")
                return [l.strip() for l in o.splitlines() if l.strip()], None
            finally:
                try:
                    client.close()
                except Exception:
                    pass

        self.runner.submit(work, lambda res, err: cb(*res) if not err else cb([], err))

    # --- запуск ---
    def _set_running(self, on: bool) -> None:
        self._running = on
        for b in (self.btn_add, self.btn_edit, self.btn_del, self.btn_one, self.btn_all):
            b.setEnabled(not on)
        self.btn_cancel.setEnabled(on)
        if on:
            self.progress.setValue(0)
            self.progress.setFormat("Работаю…")

    def backup_selected(self) -> None:
        if self._running or not self._need():
            return
        sel = self._selected()
        if sel is None:
            return self._log("Выберите задание")
        self._set_running(True)
        self._log(f"Старт: {sel.get('name')}")
        self.engine.run_job(self.store, self._profile, sel, dict(self._creds))

    def backup_all(self) -> None:
        if self._running or not self._need():
            return
        jobs = copy.deepcopy(self.store.jobs(self._profile))
        if not jobs:
            return self._log("Нет заданий")
        self._set_running(True)
        self._log(f"Бэкап всех ({len(jobs)})…")
        self.engine.run_all(self.store, self._profile, jobs, dict(self._creds))

    def cancel_run(self) -> None:
        self.engine.cancel()
        self._log("Отмена запрошена…")

    # --- слоты движка ---
    def _on_log(self, text: str) -> None:
        self.log.appendPlainText(text)
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_progress(self, done_b: int, total_b: int) -> None:
        if total_b > 0:
            self.progress.setValue(int(done_b * 100 / total_b))
            self.progress.setFormat(f"{human_size(done_b)} / {human_size(total_b)}")

    def _on_job_done(self, _jid: str, ok: bool, msg: str) -> None:
        self.progress.setValue(100 if ok else 0)
        self.progress.setFormat("Готово" if ok else msg[:60])
        self._set_running(False)

    def _on_all_done(self, ok_count: int, total: int) -> None:
        self.progress.setValue(100 if ok_count == total else 0)
        self.progress.setFormat(f"{ok_count} из {total} успешно")
        self.status.setText(f"Бэкап всех: {ok_count} из {total} успешно")
        self._set_running(False)

    # --- прочее ---
    def _log(self, text: str) -> None:
        self.status.setText(text)
        self.log.appendPlainText(text)

    def open_folder(self) -> None:
        sel = self._selected()
        from backup_core import sanitize_name
        if sel:
            root = sel.get("local_root") or self.store.root()
            path = str(Path(root) / sanitize_name(self._profile) / sanitize_name(sel.get("name", "")))
        else:
            path = self.store.root()
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_settings(self) -> None:
        dlg = SettingsDialog(self, self.store.root())
        if dlg.exec() == QDialog.Accepted and dlg.e_root.text().strip():
            self.store.set_root(dlg.e_root.text().strip())
            self._log(f"Корень бэкапов: {self.store.root()}")
