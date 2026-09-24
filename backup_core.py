#!/usr/bin/env python3
"""Вкладка «Бэкапы» — часть 1: helpers, хранилище, SSH, движок."""
import copy
import itertools
import json
import logging
import os
import posixpath
import queue as queue_mod
import re
import shlex
import shutil
import sqlite3
import stat as statmod
import threading
import time
import traceback
import zipfile
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

import paramiko
from PySide6.QtCore import QObject, Signal

__all__ = ["BackupEngine", "BackupStore", "new_job", "sanitize_name", "tmp_tag",
           "build_filename", "rotation_re", "human_size", "friendly",
           "check_integrity", "snapshot_cmd_sqlite_cli", "snapshot_cmd_sqlite_py",
           "has_sqlite_cli", "DUMPERS", "STATE_PATH", "DEFAULT_ROOT", "FIND_CMD",
           "FIND_PY_CMD", "Cancelled", "DaemonRunner"]

FIND_PY_CMD = ("find /opt /root /home /srv -maxdepth 4 -name '*.py' "
               "-not -path '*/venv/*' -not -path '*/.venv/*' "
               "2>/dev/null | head -50")

STATE_PATH = Path.home() / ".botmanager_backups.json"
LOG_PATH = Path.home() / ".botmanager_backups.log"
DEFAULT_ROOT = str(Path.home() / "BotBackups")
FIND_CMD = ("find /opt /root /home /srv -maxdepth 4 "
            "\\( -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' \\) "
            "-size +0 2>/dev/null | head -50")

_log = logging.getLogger("botmanager_backups")
if not _log.handlers:
    try:
        h = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _log.addHandler(h)
        _log.setLevel(logging.INFO)
    except Exception:
        logging.basicConfig(level=logging.INFO)


def human_size(n) -> str:
    if n is None or n < 0:
        return ""
    f = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if f < 1024 or unit == "ТБ":
            return f"{f:.0f} {unit}" if unit == "Б" else f"{f:.1f} {unit}"
        f /= 1024
    return ""


def sanitize_name(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "_", name or "").strip().strip(". ")
    return s or "job"


def tmp_tag(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]", "_", name or "job")[:32] or "job"
    return s


def build_filename(job_dir: Path, safe: str, ext: str) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    cand = job_dir / f"{safe}_{ts}.{ext}"
    i = 2
    while cand.exists():
        cand = job_dir / f"{safe}_{ts}_{i}.{ext}"
        i += 1
    return cand


def rotation_re(safe: str):
    return re.compile(r"^" + re.escape(safe) + r"_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(_\d+)?\.(db|zip)$")


def friendly(e: BaseException) -> str:
    if isinstance(e, PermissionError) or getattr(e, "errno", None) == 13 \
            or "Permission denied" in str(e):
        return f"Нет прав: {e}"
    if isinstance(e, FileNotFoundError) or "No such file" in str(e):
        return f"Файл не найден: {e}"
    name = type(e).__name__
    s = str(e) or name
    if isinstance(e, (TimeoutError, ConnectionError, OSError)) or "SSH" in name \
            or "socket" in name.lower() or "timed out" in s.lower() \
            or "not connected" in s.lower():
        return f"Нет соединения: {s}"
    return s


class Cancelled(Exception):
    pass


_uid = itertools.count()


class DaemonRunner(QObject):
    arrived = Signal(object, object, object)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._q: "queue_mod.Queue[tuple[Callable, Callable]]" = queue_mod.Queue()
        self.arrived.connect(lambda cb, res, err: cb(res, err))
        threading.Thread(target=self._loop, daemon=True, name="backup-runner").start()

    def submit(self, fn: Callable, cb: Callable) -> None:
        self._q.put((fn, cb))

    def _loop(self) -> None:
        while True:
            fn, cb = self._q.get()
            try:
                res, err = fn(), None
            except Exception as e:  # noqa: BLE001
                _log.warning("backup task failed: %s\n%s", e, traceback.format_exc())
                res, err = None, e
            self.arrived.emit(cb, res, err)


class BackupStore:
    """~/.botmanager_backups.json: {root, profiles: {name: {jobs: [...]}}}."""

    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict = self._load()

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("root", DEFAULT_ROOT)
                d.setdefault("profiles", {})
                return d
        except Exception:
            pass
        return {"root": DEFAULT_ROOT, "profiles": {}}

    def save(self) -> None:
        with self._lock:
            try:
                self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                _log.warning("save state failed: %s", e)

    def root(self) -> str:
        return self.data.get("root") or DEFAULT_ROOT

    def set_root(self, root: str) -> None:
        self.data["root"] = root
        self.save()

    def jobs(self, profile: str) -> list:
        return self.data.setdefault("profiles", {}).setdefault(profile, {}).setdefault("jobs", [])

    def save_job(self, profile: str, job: dict) -> None:
        jobs = self.jobs(profile)
        for i, j in enumerate(jobs):
            if j.get("id") == job.get("id"):
                jobs[i] = job
                break
        else:
            jobs.append(job)
        self.save()

    def delete_job(self, profile: str, job_id: str) -> None:
        jobs = self.jobs(profile)
        self.data["profiles"][profile]["jobs"] = [j for j in jobs if j.get("id") != job_id]
        self.save()


def new_job(name: str = "", remote: str = "") -> dict:
    return {"id": f"j{int(time.time() * 1000)}_{next(_uid)}", "name": name, "remote": remote,
            "dbtype": "sqlite", "keep": 30, "compress": False, "local_root": "",
            "code": [],
            "last": {"time": "", "size": None, "ok": None, "msg": ""}}


def ssh_exec(client: "paramiko.SSHClient", cmd: str):
    stdin, out, err = client.exec_command(cmd)
    o = out.read().decode(errors="replace")
    e = err.read().decode(errors="replace")
    return out.channel.recv_exit_status(), o, e


def has_sqlite_cli(client) -> bool:
    rc, _o, _e = ssh_exec(client, "command -v sqlite3")
    return rc == 0


def snapshot_cmd_sqlite_cli(remote: str, tmp: str) -> str:
    return f"sqlite3 {shlex.quote(remote)} \".backup {shlex.quote(tmp)}\""


def snapshot_cmd_sqlite_py(remote: str, tmp: str) -> str:
    py = ("import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); "
          "d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()")
    return f"python3 -c {shlex.quote(py)} {shlex.quote(remote)} {shlex.quote(tmp)}"


def _sqlite_snapshot(client, remote: str, tmp: str):
    if has_sqlite_cli(client):
        rc, _o, e = ssh_exec(client, snapshot_cmd_sqlite_cli(remote, tmp))
        if rc != 0:
            raise RuntimeError(f"sqlite3 .backup: {e.strip() or rc}")
        return "sqlite3"
    rc, _o, e = ssh_exec(client, snapshot_cmd_sqlite_py(remote, tmp))
    if rc != 0:
        raise RuntimeError(f"python3 backup: {e.strip() or rc}")
    return "python3"


DUMPERS = {"sqlite": _sqlite_snapshot}


def check_integrity(db_path: Path) -> bool:
    try:
        con = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return False
    try:
        con.execute("PRAGMA query_only=ON;")
        rows = con.execute("PRAGMA integrity_check;").fetchall()
        return rows == [("ok",)]
    except sqlite3.Error:
        return False
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


# ---------- движок (один воркер-поток, задания строго по очереди) ----------
class BackupEngine(QObject):
    sig_log = Signal(str)
    sig_progress = Signal(int, int)        # done_bytes, total_bytes
    sig_job_done = Signal(str, bool, str)  # job_id, ok, message
    sig_all_done = Signal(int, int)        # ok_count, total
    sig_refresh = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._q: "queue_mod.Queue[Optional[Callable]]" = queue_mod.Queue()
        self._cancel = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="backup-engine").start()

    def submit(self, fn: Callable) -> None:
        self._q.put(fn)

    def cancel(self) -> None:
        self._cancel.set()

    def _loop(self) -> None:
        while True:
            fn = self._q.get()
            if fn is None:
                return
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                _log.warning("engine failed: %s\n%s", e, traceback.format_exc())
                self.sig_log.emit(f"Ошибка движка: {friendly(e)}")

    def run_job(self, store: BackupStore, profile: str, job: dict, creds: dict) -> None:
        self._cancel.clear()
        self.submit(lambda: self._do_job(store, profile, copy.deepcopy(job), creds))

    def run_all(self, store: BackupStore, profile: str, jobs: list, creds: dict) -> None:
        self._cancel.clear()
        self.submit(lambda: self._do_all(store, profile, copy.deepcopy(jobs), creds))

    def _connect(self, creds: dict):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(creds["host"], port=int(creds.get("port") or 22),
                       username=creds["user"], password=creds.get("password") or None,
                       key_filename=creds.get("key") or None, timeout=10,
                       allow_agent=True, look_for_keys=True)
        client.get_transport().set_keepalive(20)
        return client

    def _do_all(self, store: BackupStore, profile: str, jobs: list, creds: dict) -> None:
        if not jobs:
            return
        ok = 0
        try:
            client = self._connect(creds)
        except Exception as e:  # noqa: BLE001
            self.sig_log.emit(f"Не подключилось: {friendly(e)}")
            for j in jobs:
                self._fail(store, profile, j, "Нет соединения")
            self.sig_all_done.emit(0, len(jobs))
            return
        try:
            for j in jobs:
                if self._cancel.is_set():
                    self._fail(store, profile, j, "Отменено")
                    continue
                if self._one(client, store, profile, j):
                    ok += 1
        finally:
            try:
                client.close()
            except Exception:
                pass
        self.sig_all_done.emit(ok, len(jobs))
        self.sig_log.emit(f"Готово: {ok} из {len(jobs)} успешно")
        self.sig_refresh.emit()

    def _do_job(self, store: BackupStore, profile: str, job: dict, creds: dict) -> None:
        try:
            client = self._connect(creds)
        except Exception as e:  # noqa: BLE001
            self.sig_log.emit(f"Не подключилось: {friendly(e)}")
            self._fail(store, profile, job, "Нет соединения")
            return
        try:
            self._one(client, store, profile, job)
        finally:
            try:
                client.close()
            except Exception:
                pass
        self.sig_refresh.emit()

    def _one(self, client, store: BackupStore, profile: str, job: dict) -> bool:
        name = job.get("name") or "job"
        remote = (job.get("remote") or "").strip()
        code_list = [c.strip() for c in (job.get("code") or []) if c.strip()]
        self.sig_log.emit(f"—— {name}: старт ——")
        if not remote and not code_list:
            self._fail(store, profile, job, "Нечего бэкапить: ни базы, ни кода")
            return False
        if remote:
            dumper = DUMPERS.get(job.get("dbtype") or "sqlite")
            if dumper is None:
                self._fail(store, profile, job, f'Неизвестный тип БД: {job.get("dbtype")}')
                return False
        safe = sanitize_name(name)
        root = job.get("local_root") or store.root()
        job_dir = Path(root) / sanitize_name(profile) / safe
        backup_dir = job_dir / "backup"
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._fail(store, profile, job, f"Нет доступа к папке: {e}")
            return False
        # переезд со старой схемы (датированные файлы лежали рядом) в backup/
        self._migrate_history(job_dir, backup_dir, safe)

        sftp = None
        try:
            sftp = client.open_sftp()
            final = None
            if remote:
                final = self._backup_db(client, sftp, job, name, remote, safe,
                                        job_dir, backup_dir)
            code_note = ""
            code_bytes = 0
            if code_list:
                n_files, code_bytes = self._backup_code(sftp, code_list, job_dir / "code")
                code_note = f", код: {n_files}"
                self.sig_log.emit(f"{name}: код скачан ({n_files} файлов)")
            # ротация истории — только после успеха
            self._rotate(backup_dir, safe, int(job.get("keep") or 0))
            size = final.stat().st_size if final else code_bytes
            msg = (final.name if final else "только код") + code_note
            job["last"] = {"time": datetime.now().isoformat(timespec="seconds"),
                           "size": size, "ok": True, "msg": msg}
            store.save_job(profile, job)
            self.sig_job_done.emit(job.get("id", ""), True,
                                   f"ок · {human_size(size)}{code_note}")
            self.sig_log.emit(f"{name}: ок ({human_size(size)}) → {msg}")
            return True
        except Cancelled:
            self._fail(store, profile, job, "Отменено", log=True)
            return False
        except Exception as e:  # noqa: BLE001
            _log.warning("backup %s failed: %s", name, e)
            self._fail(store, profile, job, friendly(e), log=True)
            return False
        finally:
            try:
                if sftp is not None:
                    sftp.close()
            except Exception:
                pass

    @staticmethod
    def _migrate_history(job_dir: Path, backup_dir: Path, safe: str) -> None:
        rx = rotation_re(safe)
        try:
            for p in job_dir.iterdir():
                if p.is_file() and rx.match(p.name):
                    try:
                        shutil.move(str(p), str(backup_dir / p.name))
                    except OSError:
                        pass
        except OSError:
            pass

    def _backup_db(self, client, sftp, job: dict, name: str, remote: str,
                   safe: str, job_dir: Path, backup_dir: Path) -> Path:
        # 1. проверка
        try:
            st = sftp.stat(remote)
            remote_size = st.st_size or 0
        except OSError:
            raise FileNotFoundError(f"Файл не найден: {remote}")
        self.sig_log.emit(f"{name}: файл на сервере есть ({human_size(remote_size)}), делаю снапшот…")
        # 2. временная копия на сервере
        rc, o, _e = ssh_exec(client, f"mktemp /tmp/botmgr_{tmp_tag(name)}_XXXXXX")
        if rc != 0 or not o.strip():
            raise RuntimeError("Не создался tmp на сервере")
        tmp = o.strip().splitlines()[0].strip()
        try:
            dumper = DUMPERS[job.get("dbtype") or "sqlite"]
            method = dumper(client, remote, tmp)
            self.sig_log.emit(f"{name}: снапшот через {method}, скачиваю…")
            tmp_size = sftp.stat(tmp).st_size or 0
            # 3-5. скачивание, проверка, сжатие (при ошибке — зачистить локальное)
            dest = None
            part = None
            try:
                dest = build_filename(backup_dir, safe, "db")
                part = dest.with_name(dest.name + ".part")
                self._download(sftp, tmp, part, tmp_size)
                if part.stat().st_size != tmp_size:
                    raise RuntimeError(
                        f"Размер не сошёлся: скачано {part.stat().st_size}, на сервере {tmp_size}")
                os.replace(part, dest)
                part = None
                # 4. целостность
                if not check_integrity(dest):
                    raise RuntimeError("PRAGMA integrity_check != ok")
                # актуальная копия всегда рядом
                try:
                    shutil.copyfile(dest, job_dir / f"{safe}.db")
                except OSError as e:
                    self.sig_log.emit(f"{name}: latest не записался: {e}")
                final = dest
                if job.get("compress"):
                    final = self._zip_one(dest)
                    dest = None
                return final
            except Exception:
                for p in (part, dest):
                    try:
                        if p is not None and p.exists() and p.is_file():
                            p.unlink()
                    except OSError:
                        pass
                raise
        finally:
            # 6. очистка tmp всегда
            if tmp:
                try:
                    ssh_exec(client, f"rm -f {shlex.quote(tmp)}")
                except Exception:
                    pass

    def _backup_code(self, sftp, code_list: list, code_dir: Path):
        """Резерв кода в code/. Возвращает (файлов, байт)."""
        code_dir.mkdir(parents=True, exist_ok=True)
        pairs = []  # (remote, size)
        for entry in code_list:
            try:
                st = sftp.stat(entry)
            except OSError:
                raise FileNotFoundError(f"Файл кода не найден: {entry}")
            mode = st.st_mode or 0
            if statmod.S_ISDIR(mode):
                pairs.extend(self._remote_py_files(sftp, entry))
            else:
                pairs.append((entry, st.st_size or 0))
        if not pairs:
            raise FileNotFoundError("По списку кода ничего не найдено")
        total = sum(s for _, s in pairs)
        done = 0
        for remote, size in pairs:
            if self._cancel.is_set():
                raise Cancelled()
            base = posixpath.basename(remote.rstrip("/")) or "file"
            dst = code_dir / base
            i = 2
            while dst.exists():
                stem, dot, ext = base.rpartition(".")
                if not dot or "/" in stem:
                    stem, ext = base, ""
                dst = code_dir / f"{stem} ({i}){('.' + ext) if ext else ''}"
                i += 1
            part = dst.with_name(dst.name + ".part")
            self._download(sftp, remote, part, size)
            os.replace(part, dst)
            done += size
            self.sig_log.emit(f"код: {base} ({human_size(size)})")
        return len(pairs), total

    def _remote_py_files(self, sftp, root: str):
        out = []
        stack = [root.rstrip("/") or "/"]
        while stack:
            d = stack.pop()
            try:
                attrs = sftp.listdir_attr(d)
            except OSError as e:
                raise e
            for a in attrs:
                full = d.rstrip("/") + "/" + a.filename
                mode = a.st_mode or 0
                if statmod.S_ISLNK(mode):
                    continue
                elif statmod.S_ISDIR(mode):
                    stack.append(full)
                elif a.filename.endswith(".py"):
                    out.append((full, a.st_size or 0))
        return out

    def _download(self, sftp, remote: str, part: Path, total: int) -> None:
        if part.exists():
            part.unlink()
        state = {"last_t": time.monotonic()}

        def cb(sent: int, _total: int):
            if self._cancel.is_set():
                raise Cancelled()
            now = time.monotonic()
            if now - state["last_t"] >= 0.15:
                state["last_t"] = now
                self.sig_progress.emit(sent, total or sent)

        try:
            sftp.get(remote, str(part), callback=cb)
        except Exception:
            try:
                if part.exists():
                    part.unlink()
            except OSError:
                pass
            raise
        self.sig_progress.emit(total, total)

    def _zip_one(self, dest: Path) -> Path:
        zpath = dest.with_suffix(".zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(dest, dest.name)
        dest.unlink()
        return zpath

    def _rotate(self, job_dir: Path, safe: str, keep: int) -> None:
        if keep <= 0:
            return
        rx = rotation_re(safe)
        files = sorted([p for p in job_dir.iterdir() if p.is_file() and rx.match(p.name)],
                       key=lambda p: p.name)
        if len(files) > keep:
            for old in files[:-keep]:
                try:
                    old.unlink()
                    self.sig_log.emit(f"Ротация: удалён {old.name}")
                except OSError as e:
                    self.sig_log.emit(f"Ротация: не удалился {old.name}: {e}")

    def _fail(self, store: BackupStore, profile: str, job: dict, msg: str, log: bool = False) -> bool:
        if log:
            self.sig_log.emit(f"{job.get('name')}: {msg}")
        job["last"] = {"time": datetime.now().isoformat(timespec="seconds"),
                       "size": None, "ok": False, "msg": msg}
        store.save_job(profile, job)
        self.sig_job_done.emit(job.get("id", ""), False, msg)
        return False
