#!/usr/bin/env python3
"""Вкладка «Деплой»: установка Python-бота на Debian/Ubuntu одной кнопкой.

Часть 1: константы, хелперы, анализ проекта, движок.
НЕ импортирует botmanager. Свой SSH-клиент, свой фоновый runner.
"""
import logging
import os
import posixpath
import queue as queue_mod
import re
import shlex
import socket
import stat
import tempfile
import threading
import time
import traceback
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

import paramiko
from PySide6.QtCore import QObject, Signal

__all__ = ["DeployEngine", "DaemonRunner", "analyze_project", "check_dotenv",
           "sanitize_service_name", "valid_service_name", "build_unit",
           "MANAGED_MARKER", "DEFAULT_PROTECTED", "STATE_PATH", "Cancelled",
           "friendly", "human_size", "ssh_exec", "PACK_EXCLUDES"]

STATE_PATH = Path.home() / ".botmanager_deploy.json"
LOG_PATH = Path.home() / ".botmanager_deploy.log"
MANAGED_MARKER = "# Managed by Bot Manager"
DEFAULT_SERVER_ROOT = "/opt/bots"
DEFAULT_PROTECTED = [".env", "*.db", "*.sqlite", "*.sqlite3", "*.session", "logs/"]
PACK_EXCLUDES = {".venv", "venv", "env", "__pycache__", ".git", ".idea",
                 ".vscode", "node_modules", "build", "dist"}
ENTRY_CANDIDATES = ("main.py", "bot.py", "app.py", "run.py", "__main__.py")

_log = logging.getLogger("botmanager_deploy")
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


def _msg(parent, kind: str, title: str, text: str):
    """Диалог, который точно всплывает поверх (raise+activate против 'зависаний').

    kind: 'info' | 'warn' | 'q' (Да/Нет). Возвращает кнопку для 'q'.
    """
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    if kind == "q":
        box.setIcon(QMessageBox.Question)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.button(QMessageBox.Yes).setText("Да")
        box.button(QMessageBox.No).setText("Нет")
    elif kind == "warn":
        box.setIcon(QMessageBox.Warning)
        box.setStandardButtons(QMessageBox.Ok)
    else:
        box.setIcon(QMessageBox.Information)
        box.setStandardButtons(QMessageBox.Ok)
    box.raise_()
    box.activateWindow()
    return box.exec()


class DaemonRunner(QObject):
    arrived = Signal(object, object, object)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._q: "queue_mod.Queue[tuple[Callable, Callable]]" = queue_mod.Queue()
        self.arrived.connect(lambda cb, res, err: cb(res, err))
        threading.Thread(target=self._loop, daemon=True, name="deploy-runner").start()

    def submit(self, fn: Callable, cb: Callable) -> None:
        self._q.put((fn, cb))

    def _loop(self) -> None:
        while True:
            fn, cb = self._q.get()
            try:
                res, err = fn(), None
            except Exception as e:  # noqa: BLE001
                _log.warning("deploy task failed: %s\n%s", e, traceback.format_exc())
                res, err = None, e
            self.arrived.emit(cb, res, err)


# ---------- анализ проекта ----------
def sanitize_service_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9_-]", "_", (name or "").lower()).strip("_-") or "bot"
    return s[:40] or "bot"


def valid_service_name(name: str) -> bool:
    return re.match(r"^[a-z][a-z0-9_-]{1,40}$", name or "") is not None


def analyze_project(path: str) -> dict:
    """Сводка по папке: requirements, .env, точки входа, требования к Python."""
    root = Path(path)
    req = (root / "requirements.txt").is_file()
    env = (root / ".env").is_file()
    entries = sorted(p.name for p in root.glob("*.py") if p.is_file())
    preferred = [e for e in ENTRY_CANDIDATES if e in entries] + \
                [e for e in entries if e not in ENTRY_CANDIDATES]
    pyver = None
    pv = root / ".python-version"
    if pv.is_file():
        try:
            pyver = pv.read_text(encoding="utf-8", errors="replace").strip().split()[0]
        except OSError:
            pass
    requires = None
    pp = root / "pyproject.toml"
    if pp.is_file():
        try:
            m = re.search(r'requires-python\s*=\s*["\']([^"\']+)["\']',
                          pp.read_text(encoding="utf-8", errors="replace"))
            if m:
                requires = m.group(1)
        except OSError:
            pass
    return {"requirements": req, "env": env, "entries": entries,
            "preferred": preferred, "python_version": pyver, "requires_python": requires}


def check_dotenv(path: str) -> list:
    """Проверка .env под строгий systemd-парсер. Возвращает [(строка, ключ|None, проблема)].

    Значения никогда не возвращаются — только имена ключей и номера строк.
    """
    issues = []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [(0, None, f"не читается: {e}")]
    continued = False
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continued = False
            continue
        if continued or raw.rstrip().endswith("\\"):
            issues.append((n, None, "многострочное значение (systemd не поддерживает)"))
            continued = raw.rstrip().endswith("\\")
            continue
        continued = False
        body = line
        if body.startswith("export "):
            issues.append((n, None, "лишний 'export' (убрать)"))
            body = body[len("export "):].strip()
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", body)
        if not m:
            issues.append((n, None, "строка не вида KEY=VALUE"))
            continue
        key = m.group(1)
        if re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+=", body):
            issues.append((n, key, "пробелы вокруг '=' (убрать)"))
    return issues


def build_unit(name: str, server_dir: str, entry: str) -> str:
    return (f"{MANAGED_MARKER}\n"
            f"[Unit]\n"
            f"Description=Telegram bot {name}\n"
            f"After=network-online.target\n"
            f"Wants=network-online.target\n"
            f"\n"
            f"[Service]\n"
            f"Type=simple\n"
            f"WorkingDirectory={server_dir}\n"
            f"ExecStart={server_dir}/.venv/bin/python {entry}\n"
            f"EnvironmentFile=-{server_dir}/.env\n"
            f"Environment=PYTHONUNBUFFERED=1\n"
            f"Restart=always\n"
            f"RestartSec=5\n"
            f"\n"
            f"[Install]\n"
            f"WantedBy=multi-user.target\n")


def ssh_exec(client: "paramiko.SSHClient", cmd: str):
    stdin, out, err = client.exec_command(cmd)
    o = out.read().decode(errors="replace")
    e = err.read().decode(errors="replace")
    return out.channel.recv_exit_status(), o, e


# ---------- движок (один воркер, всё строго по очереди) ----------
class DeployEngine(QObject):
    sig_step = Signal(str, str, str)   # key, label, state: run|ok|fail|warn
    sig_log = Signal(str)              # строка журнала / хвост вывода
    sig_progress = Signal(int, int)    # done_bytes, total_bytes (заливка)
    sig_result = Signal(bool, dict)    # ok, info

    def __init__(self, parent=None):
        super().__init__(parent)
        self._q: "queue_mod.Queue[Optional[Callable]]" = queue_mod.Queue()
        self._cancel = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="deploy-engine").start()

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

    # --- API ---
    def install(self, params: dict, creds: dict) -> None:
        self._cancel.clear()
        self.submit(lambda: self._do_install(dict(params), dict(creds)))

    def update_bot(self, params: dict, creds: dict) -> None:
        self._cancel.clear()
        self.submit(lambda: self._do_update(dict(params), dict(creds)))

    def delete_bot(self, params: dict, creds: dict) -> None:
        self._cancel.clear()
        self.submit(lambda: self._do_delete(dict(params), dict(creds)))

    # --- подключение ---
    def _connect(self, creds: dict):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(creds["host"], port=int(creds.get("port") or 22),
                       username=creds["user"], password=creds.get("password") or None,
                       key_filename=creds.get("key") or None, timeout=10,
                       allow_agent=True, look_for_keys=True)
        client.get_transport().set_keepalive(20)
        return client

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    # ---------- установка ----------
    def _do_install(self, p: dict, creds: dict) -> None:
        name = p["service"]
        self._step("connect", "Подключение", "run")
        try:
            client = self._connect(creds)
        except Exception as e:  # noqa: BLE001
            return self._abort("connect", f"Не подключилось: {friendly(e)}")
        created_dir = False
        created_unit = False
        zip_path = ""
        try:
            # 0. предпроверки
            self._step("pre", "Предпроверки", "run")
            pre = self._prechecks(client, p)
            if not pre["ok"]:
                return self._abort("pre", pre["msg"])
            for w in pre.get("warnings", []):
                self._step("pre", w, "warn")
            server_py = pre["server_py"]
            self.sig_log.emit(f"Сервер: {pre['os']}, {server_py}")
            self._step("pre", "Предпроверки", "ok")
            created_dir = pre["created_dir"]

            # 1. упаковка
            self._step("pack", "Упаковка проекта", "run")
            zip_path, size = self._pack(p["local_dir"], PACK_EXCLUDES, set())
            self.sig_log.emit(f"Архив: {human_size(size)}")
            if size > 100 * 1024 * 1024:
                self._step("pack", "Архив больше 100 МБ — заливка будет долгой", "warn")
            self._check_cancel()
            self._step("pack", "Упаковка проекта", "ok")

            # 2. .env уже проверен в UI; дублируем проверку построчно без значений
            for n, key, issue in check_dotenv(os.path.join(p["local_dir"], ".env")) \
                    if os.path.isfile(os.path.join(p["local_dir"], ".env")) else []:
                who = f" (строка {n}{', ' + key if key else ''})"
                self._step("env", f".env: {issue}{who}", "warn")

            # 3. системные пакеты
            self._step("apt", "Системные пакеты", "run")
            rc, _o, _e = ssh_exec(client, "python3 -c 'import venv, ensurepip'")
            if rc != 0:
                self.sig_log.emit("Нет venv — ставлю python3-venv через apt…")
                rc, _o, e = ssh_exec(
                    client, "DEBIAN_FRONTEND=noninteractive apt-get update "
                            "&& apt-get install -y python3-venv python3-pip")
                if rc != 0:
                    return self._abort("apt", f"apt не справился: {self._tail(e)}",
                                       created_dir, created_unit, p)
            self._step("apt", "Системные пакеты", "ok")

            # 4. заливка + распаковка
            self._step("upload", "Заливка на сервер", "run")
            remote_zip = f"/tmp/botmgr_{name}.zip"
            sftp = client.open_sftp()
            try:
                self._upload(sftp, zip_path, remote_zip)
                rc, _o, e = ssh_exec(
                    client, f"mkdir -p {shlex.quote(p['server_dir'])} && "
                            f"python3 -m zipfile -e {shlex.quote(remote_zip)} "
                            f"{shlex.quote(p['server_dir'])}")
                if rc != 0:
                    return self._abort("upload", f"Не распаковалось: {self._tail(e)}",
                                       created_dir, created_unit, p)
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass
                try:
                    ssh_exec(client, f"rm -f {shlex.quote(remote_zip)}")
                except Exception:
                    pass
            self._check_cancel()
            self._step("upload", "Заливка на сервер", "ok")

            # 5. права на .env
            rc, _o, _e = ssh_exec(client, f"test -f {shlex.quote(p['server_dir'] + '/.env')}")
            if rc == 0:
                ssh_exec(client, f"chmod 600 {shlex.quote(p['server_dir'] + '/.env')}")
                self.sig_log.emit(".env: права 600")

            # 6. окружение
            self._step("venv", "Виртуальное окружение", "run")
            vd = p["server_dir"]
            rc, _o, e = ssh_exec(client, f"python3 -m venv {shlex.quote(vd + '/.venv')}")
            if rc != 0:
                return self._abort("venv", f"venv не создался: {self._tail(e)}",
                                   created_dir, created_unit, p)
            self._check_cancel()
            rc, _o, e = self._pip(client, f"{vd}/.venv/bin/pip install --upgrade pip")
            if rc != 0:
                return self._abort("venv", f"pip не обновился: {self._tail(e)}",
                                   created_dir, created_unit, p)
            if p.get("has_requirements"):
                self.sig_log.emit("Ставлю зависимости из requirements.txt…")
                rc, o_r, e_r = self._pip(client, f"{vd}/.venv/bin/pip install -r {shlex.quote(vd + '/requirements.txt')}")
                if rc != 0:
                    tail = self._tail((o_r or "") + "\n" + (e_r or ""))
                    hint = ""
                    if "Python.h" in tail or "gcc" in tail.lower():
                        hint = " Подсказка: нужны build-essential и python3-dev (ставятся только с вашего согласия)."
                    return self._abort("venv", f"pip install -r не прошёл, хвост вывода:\n{tail}{hint}",
                                       created_dir, created_unit, p)
            self._step("venv", "Виртуальное окружение", "ok")

            # 7. юнит
            self._step("unit", "Сервис systemd", "run")
            unit_path = f"/etc/systemd/system/{name}.service"
            content = build_unit(name, p["server_dir"], p["entry"])
            sftp = client.open_sftp()
            try:
                with sftp.open(unit_path, "w") as f:
                    f.write(content)
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass
            created_unit = True
            self._step("unit", "Сервис systemd", "ok")

            # 8. запуск
            self._step("start", "Запуск", "run")
            rc, _o, e = ssh_exec(client, f"systemctl daemon-reload && systemctl enable --now {shlex.quote(name)}")
            if rc != 0:
                return self._abort("start", f"Не запустился: {self._tail(e)}",
                                   created_dir, created_unit, p, keep_all=True)
            # 9. проверка
            status, logs = self._verify(client, name)
            self._step("start", "Запуск", "ok" if status == "running" else "fail")
            info = {"status": status, "logs": logs, "service": name,
                    "server_dir": p["server_dir"], "created_dir": created_dir,
                    "created_unit": created_unit}
            self.sig_result.emit(status == "running", info)
        except Cancelled:
            self._abort("cancel", "Отменено пользователем", created_dir, created_unit, p)
        except Exception as e:  # noqa: BLE001
            _log.warning("install failed: %s\n%s", e, traceback.format_exc())
            self._abort("error", friendly(e), created_dir, created_unit, p)
        finally:
            try:
                client.close()
            except Exception:
                pass
            try:
                if zip_path and os.path.exists(zip_path):
                    os.remove(zip_path)
            except OSError:
                pass

    # ---------- обновление ----------
    def _do_update(self, p: dict, creds: dict) -> None:
        name = p["service"]
        try:
            client = self._connect(creds)
        except Exception as e:  # noqa: BLE001
            return self._abort("connect", f"Не подключилось: {friendly(e)}")
        zip_path = ""
        try:
            self._step("pack", "Упаковка новой версии", "run")
            protected = p.get("protected") or list(DEFAULT_PROTECTED)
            zip_path, size = self._pack(p["local_dir"], PACK_EXCLUDES, set(), protected)
            self.sig_log.emit(f"Архив: {human_size(size)} (защищённое исключено)")
            self._check_cancel()
            self._step("pack", "Упаковка новой версии", "ok")

            self._step("stop", "Остановка бота", "run")
            ssh_exec(client, f"systemctl stop {shlex.quote(name)}")
            self._step("stop", "Остановка бота", "ok")

            self._step("upload", "Заливка поверх", "run")
            remote_zip = f"/tmp/botmgr_{name}.zip"
            sftp = client.open_sftp()
            try:
                self._upload(sftp, zip_path, remote_zip)
                rc, _o, e = ssh_exec(
                    client, f"python3 -m zipfile -e {shlex.quote(remote_zip)} "
                            f"{shlex.quote(p['server_dir'])}")
                if rc != 0:
                    return self._abort("upload", f"Не распаковалось: {self._tail(e)}",
                                       False, True, p, keep_all=True)
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass
                try:
                    ssh_exec(client, f"rm -f {shlex.quote(remote_zip)}")
                except Exception:
                    pass
            self._check_cancel()
            self._step("upload", "Заливка поверх", "ok")

            # requirements изменился? — сравнить с серверным
            need_pip = False
            local_req = os.path.join(p["local_dir"], "requirements.txt")
            if os.path.isfile(local_req):
                try:
                    sftp = client.open_sftp()
                    try:
                        with sftp.open(p["server_dir"] + "/requirements.txt", "r") as f:
                            srv = f.read().decode(errors="replace")
                    finally:
                        sftp.close()
                    with open(local_req, encoding="utf-8", errors="replace") as f:
                        loc = f.read()
                    need_pip = (srv != loc)
                except OSError:
                    need_pip = True
            if need_pip:
                self._step("venv", "Обновление зависимостей", "run")
                rc, _o, e = self._pip(
                    client, f"{p['server_dir']}/.venv/bin/pip install -r "
                            f"{shlex.quote(p['server_dir'] + '/requirements.txt')}")
                if rc != 0:
                    return self._abort("venv", f"pip install -r не прошёл:\n{self._tail(e)}",
                                       False, True, p, keep_all=True)
                self._step("venv", "Обновление зависимостей", "ok")

            self._step("start", "Запуск", "run")
            rc, _o, e = ssh_exec(client, f"systemctl start {shlex.quote(name)}")
            if rc != 0:
                return self._abort("start", f"Не запустился: {self._tail(e)}",
                                   False, True, p, keep_all=True)
            status, logs = self._verify(client, name)
            self._step("start", "Запуск", "ok" if status == "running" else "fail")
            self.sig_result.emit(status == "running",
                                 {"status": status, "logs": logs, "service": name,
                                  "server_dir": p["server_dir"], "created_dir": False,
                                  "created_unit": True})
        except Cancelled:
            self._abort("cancel", "Отменено пользователем", False, True, p, keep_all=True)
        except Exception as e:  # noqa: BLE001
            _log.warning("update failed: %s", e)
            self._abort("error", friendly(e), False, True, p, keep_all=True)
        finally:
            try:
                client.close()
            except Exception:
                pass
            try:
                if zip_path and os.path.exists(zip_path):
                    os.remove(zip_path)
            except OSError:
                pass

    # ---------- удаление ----------
    def _do_delete(self, p: dict, creds: dict) -> None:
        name = p["service"]
        try:
            client = self._connect(creds)
        except Exception as e:  # noqa: BLE001
            return self._abort("connect", f"Не подключилось: {friendly(e)}")
        try:
            self._step("check", "Проверка маркера", "run")
            rc, o, _e = ssh_exec(client, f"head -1 /etc/systemd/system/{shlex.quote(name + '.service')}")
            if rc != 0 or o.strip() != MANAGED_MARKER:
                return self._abort("check", "Сервис без маркера — установлен вручную, удаляйте вручную")
            self._step("check", "Проверка маркера", "ok")
            self._step("stop", "Остановка и отключение", "run")
            ssh_exec(client, f"systemctl stop {shlex.quote(name)}")
            ssh_exec(client, f"systemctl disable {shlex.quote(name)}")
            ssh_exec(client, f"rm -f /etc/systemd/system/{shlex.quote(name + '.service')}")
            ssh_exec(client, "systemctl daemon-reload")
            self._step("stop", "Остановка и отключение", "ok")
            if p.get("delete_dir") and self._safe_dir(p.get("server_dir", "")):
                self._step("dir", "Удаление папки", "run")
                ssh_exec(client, f"rm -rf {shlex.quote(p['server_dir'])}")
                self._step("dir", "Удаление папки", "ok")
            self.sig_result.emit(True, {"status": "deleted", "logs": "", "service": name,
                                        "server_dir": p.get("server_dir", ""),
                                        "created_dir": False, "created_unit": False})
        except Cancelled:
            self._abort("cancel", "Отменено пользователем")
        except Exception as e:  # noqa: BLE001
            _log.warning("delete failed: %s", e)
            self._abort("error", friendly(e))
        finally:
            try:
                client.close()
            except Exception:
                pass

    @staticmethod
    def _safe_dir(d: str) -> bool:
        """Папку удаляем, только если это явно подкаталог проектов, а не корень системы."""
        if not d:
            return False
        norm = posixpath.normpath(d)
        if norm in ("", "/", "/opt", "/root", "/home", "/srv", "/etc", "/var", "/usr", "/tmp"):
            return False
        parts = [x for x in norm.split("/") if x]
        return len(parts) >= 3

    # ---------- шаги-помощники ----------
    def _step(self, key: str, label: str, state: str) -> None:
        self.sig_step.emit(key, label, state)
        if state == "fail":
            self.sig_log.emit(f"✖ {label}")
        elif state == "ok":
            self.sig_log.emit(f"✔ {label}")
        elif state == "warn":
            self.sig_log.emit(f"! {label}")

    def _abort(self, key: str, msg: str, created_dir: bool = False,
               created_unit: bool = False, p: Optional[dict] = None,
               keep_all: bool = False) -> None:
        self._step(key, msg.splitlines()[0] if msg else "Ошибка", "fail")
        if "\n" in msg:
            self.sig_log.emit(msg)
        info = {"status": "failed", "logs": msg, "service": (p or {}).get("service", ""),
                "server_dir": (p or {}).get("server_dir", ""),
                "created_dir": created_dir and not keep_all,
                "created_unit": created_unit}
        self.sig_result.emit(False, info)

    @staticmethod
    def _tail(s: str, n: int = 25) -> str:
        lines = (s or "").strip().splitlines()
        return "\n".join(lines[-n:]) or "(пустой вывод)"

    def _prechecks(self, client, p: dict) -> dict:
        """Шаг 0. Возвращает dict(ok, msg, warnings, os, server_py, created_dir)."""
        rc, o, _e = ssh_exec(client, "cat /etc/os-release")
        m = re.search(r"^ID=(\w+)", o or "", re.M)
        os_id = m.group(1) if m else "?"
        if os_id not in ("debian", "ubuntu"):
            return {"ok": False, "msg": f"Сервер не Debian/Ubuntu (ID={os_id}), стоп"}
        rc, _o, _e = ssh_exec(client, "command -v systemctl && command -v python3")
        if rc != 0:
            return {"ok": False, "msg": "На сервере нет systemctl или python3"}
        rc, o, _e = ssh_exec(client, "id -u")
        if rc != 0 or o.strip() != "0":
            return {"ok": False, "msg": "Нужен root (sudo в первой версии не поддерживается)"}
        rc, o, _e = ssh_exec(client, "python3 --version 2>&1")
        server_py = o.strip() or "python3 ?"
        warnings = []
        want = p.get("want_python")
        if want and want not in server_py:
            warnings.append(f"Проект хочет {want}, на сервере {server_py} — продолжаю, но проверьте")
        if not valid_service_name(p["service"]):
            return {"ok": False, "msg": f"Плохое имя сервиса: {p['service']!r}"}
        rc, _o, _e = ssh_exec(client, f"systemctl cat {shlex.quote(p['service'])}")
        if rc == 0:
            return {"ok": False, "msg": f"Сервис {p['service']} уже есть — обновите его вместо установки",
                    "exists": True}
        rc, _o, _e = ssh_exec(client, f"test -d {shlex.quote(p['server_dir'])}")
        created_dir = False
        if rc == 0:
            rc2, o2, _e2 = ssh_exec(client, f"ls -A {shlex.quote(p['server_dir'])} | head -1")
            if o2.strip():
                return {"ok": False, "msg": f"Папка {p['server_dir']} уже существует и не пуста",
                        "exists": True}
        else:
            created_dir = True
        return {"ok": True, "msg": "", "warnings": warnings, "os": os_id,
                "server_py": server_py, "created_dir": created_dir}

    def _pack(self, local_dir: str, dir_excludes: set, file_excludes: set,
              protected: Optional[list] = None):
        """Zip проекта во временный файл. Возвращает (путь, размер).

        Две фазы: быстрый сбор списка (сразу видно объём) + упаковка с
        прогрессом и проверкой отмены. Необычные файлы (ссылки в никуда,
        fifo/сокеты) пропускаются с пометкой в журнале, а не вешают процесс.
        """
        protected = protected or []
        root = Path(local_dir)
        items = []  # (full, rel, size)
        skipped = []
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            parts = set(Path(rel_dir).parts) if rel_dir != "." else set()
            if parts & dir_excludes:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in dir_excludes]
            for fn in filenames:
                if fn.endswith(".pyc"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = fn if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/" + fn
                if self._protected_match(rel, protected):
                    continue
                try:
                    lst = os.lstat(full)
                except OSError:
                    skipped.append(rel + " (не читается)")
                    continue
                if stat.S_ISLNK(lst.st_mode):
                    try:
                        tgt = os.stat(full)
                    except OSError:
                        skipped.append(rel + " (битая ссылка)")
                        continue
                    if not stat.S_ISREG(tgt.st_mode):
                        skipped.append(rel + " (ссылка не на файл)")
                        continue
                    size = tgt.st_size
                elif stat.S_ISREG(lst.st_mode):
                    size = lst.st_size
                else:
                    skipped.append(rel + " (не обычный файл)")
                    continue
                items.append((full, rel.replace(os.sep, "/"), size))
        total = sum(s for _, _, s in items)
        self.sig_log.emit(f"Упаковка: {len(items)} файлов, ~{human_size(total)}")
        if items:
            big = sorted(items, key=lambda t: t[2], reverse=True)[:5]
            self.sig_log.emit("Самые тяжёлые: " + ", ".join(
                f"{r} ({human_size(s)})" for _, r, s in big))
        for s in skipped[:5]:
            self.sig_log.emit(f"Пропущен: {s}")
        if len(skipped) > 5:
            self.sig_log.emit(f"…и ещё пропущено: {len(skipped) - 5}")
        fd, tmp = tempfile.mkstemp(prefix="botmgr_", suffix=".zip")
        os.close(fd)
        try:
            done_b = 0
            done_n = 0
            last_t = time.monotonic()
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for full, rel, size in items:
                    self._check_cancel()
                    z.write(full, rel)
                    done_b += size
                    done_n += 1
                    now = time.monotonic()
                    if now - last_t >= 0.2 or done_n == len(items):
                        last_t = now
                        self.sig_log.emit(f"Упаковка: {done_n}/{len(items)}…")
                    self.sig_progress.emit(done_b, total)
            return tmp, os.path.getsize(tmp)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _protected_match(rel: str, patterns: list) -> bool:
        import fnmatch
        base = rel.rsplit("/", 1)[-1]
        for pat in patterns:
            pat = pat.strip()
            if not pat:
                continue
            if pat.endswith("/"):
                if rel == pat[:-1] or rel.startswith(pat):
                    return True
                continue
            if fnmatch.fnmatch(base, pat) or fnmatch.fnmatch(rel, pat):
                return True
        return False

    def _upload(self, sftp, local: str, remote: str) -> None:
        total = os.path.getsize(local)
        state = {"last_t": time.monotonic()}

        def cb(sent: int, _total: int):
            if self._cancel.is_set():
                raise Cancelled()
            now = time.monotonic()
            if now - state["last_t"] >= 0.15:
                state["last_t"] = now
                self.sig_progress.emit(sent, total)

        sftp.put(local, remote, callback=cb, confirm=False)
        self.sig_progress.emit(total, total)

    def _pip(self, client, cmd: str):
        """pip со стримингом вывода построчно. Возвращает (rc, out, err)."""
        import socket as _socket
        stdin, out, err = client.exec_command(cmd)
        chan = out.channel
        chan.settimeout(5)
        buf_out, buf_err, acc = [], [], ""
        deadline = time.monotonic() + 600
        while True:
            if self._cancel.is_set():
                try:
                    chan.close()
                except Exception:
                    pass
                raise Cancelled()
            if time.monotonic() > deadline:
                try:
                    chan.close()
                except Exception:
                    pass
                raise RuntimeError("pip: превышен таймаут 10 минут")
            try:
                chunk = chan.recv(4096)
            except _socket.timeout:
                if chan.exit_status_ready():
                    break
                continue
            if not chunk:
                if chan.exit_status_ready():
                    break
                time.sleep(0.2)
                continue
            acc += chunk.decode(errors="replace")
            while "\n" in acc:
                line, acc = acc.split("\n", 1)
                buf_out.append(line)
                self.sig_log.emit("  pip| " + line[:300])
        if acc.strip():
            buf_out.append(acc.strip())
            self.sig_log.emit("  pip| " + acc.strip()[:300])
        rc = chan.recv_exit_status()
        try:
            e = err.read().decode(errors="replace")
        except Exception:
            e = ""
        return rc, "\n".join(buf_out), e

    def _verify(self, client, name: str, tries: int = 2):
        """Проверка: active + без рестартов по кругу + логи. Возвращает (status, logs)."""
        self._sleep_cancel(5)
        for attempt in range(tries):
            rc, o, _e = ssh_exec(client, f"systemctl is-active {shlex.quote(name)}")
            active = (o.strip() == "active")
            if active and attempt < tries - 1:
                self._sleep_cancel(10)
                continue
            break
        rc, o, _e = ssh_exec(client, f"systemctl show -p NRestarts {shlex.quote(name)}")
        m = re.search(r"NRestarts=(\d+)", o or "")
        restarts = int(m.group(1)) if m else 0
        rc, logs, _e = ssh_exec(client, f"journalctl -u {shlex.quote(name)} -n 30 --no-pager")
        hint = self._hint(logs or "")
        if hint:
            self.sig_log.emit(hint)
        if not active:
            return "failed", logs
        if restarts > 0:
            self.sig_log.emit(f"Сервис перезапускался {restarts} раз(а) — бот падает по кругу")
            return "crashing", logs
        return "running", logs

    def _sleep_cancel(self, sec: float) -> None:
        end = time.monotonic() + sec
        while time.monotonic() < end:
            if self._cancel.is_set():
                raise Cancelled()
            time.sleep(0.5)

    @staticmethod
    def _hint(logs: str) -> str:
        m = re.search(r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]", logs)
        if m:
            return (f"Подсказка: не хватает пакета '{m.group(1)}' — "
                    f"добавьте его в requirements.txt и нажмите «Обновить»")
        if re.search(r"(?i)(token|unauthori[sz]ed|\b401\b|forbidden)", logs):
            return "Подсказка: похоже, нет токена — проверьте переменные в .env"
        return ""

    # ---------- списки сервисов для обновления ----------
    def list_units(self, client):
        """(managed, others): managed — с маркером; others — [(name, working_dir)]."""
        rc, o, _e = ssh_exec(client, "grep -l 'Managed by Bot Manager' /etc/systemd/system/*.service 2>/dev/null")
        managed = []
        for line in o.splitlines():
            line = line.strip()
            if line.endswith(".service"):
                managed.append(posixpath.basename(line)[:-len(".service")])
        rc, o, _e = ssh_exec(client, "ls /etc/systemd/system/*.service 2>/dev/null")
        others = []
        man_set = set(managed)
        names = [posixpath.basename(l.strip())[:-len(".service")]
                 for l in o.splitlines() if l.strip().endswith(".service")]
        for n in names:
            if n in man_set or "@" in n:
                continue
            rc2, o2, _e2 = ssh_exec(client, f"systemctl show -p WorkingDirectory --value {shlex.quote(n)}")
            wd = (o2 or "").strip().lstrip("!-")
            others.append((n, wd))
        return managed, others


# ---------- UI ----------
import json as _json

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from ui_anim import AnimatedButton, derived

__all__ += ["DeployTab"]


class UpdateDialog(QDialog):
    """Обновление бота: сервис + локальная папка + защищённые файлы."""

    def __init__(self, parent, managed: list, others: list, local_dir: str, protected: list):
        super().__init__(parent)
        self.setWindowTitle("Обновить бота")
        self.setModal(True)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.c_svc = QComboBox()
        self.c_svc.addItems(managed)
        self.c_all = QCheckBox("показать и остальные")
        self.c_all.toggled.connect(lambda on: self._refill(managed, others, on))
        self._managed, self._others = managed, others
        svc_row = QHBoxLayout()
        svc_row.addWidget(self.c_svc, 1)
        svc_row.addWidget(self.c_all)
        svc_wrap = QWidget()
        svc_wrap.setLayout(svc_row)
        self.e_dir = QLineEdit(local_dir)
        self.btn_browse = QPushButton("…")
        self.btn_browse.setObjectName("btnGhost")
        self.btn_browse.setFixedWidth(36)
        self.btn_browse.clicked.connect(self._browse)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.e_dir, 1)
        dir_row.addWidget(self.btn_browse)
        dir_wrap = QWidget()
        dir_wrap.setLayout(dir_row)
        self.e_prot = QLineEdit(", ".join(protected))
        self.e_prot.setPlaceholderText(".env, *.db, …")
        form.addRow("Сервис:", svc_wrap)
        form.addRow("Папка с новой версией:", dir_wrap)
        form.addRow("Не затирать:", self.e_prot)
        hint = QLabel("Файлы, которых нет в архиве, на сервере остаются. "
                      "Удалённые локально файлы на сервере не удаляются.")
        hint.setWordWrap(True)
        hint.setObjectName("statusLine")
        lay.addLayout(form)
        lay.addWidget(hint)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("Обновить")
        box.button(QDialogButtonBox.Cancel).setText("Отмена")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self.setMinimumWidth(520)

    def _refill(self, managed: list, others: list, show_all: bool) -> None:
        cur = self.c_svc.currentText()
        self.c_svc.clear()
        self.c_svc.addItems(managed + ([f"{n}  [{wd}]" for n, wd in others] if show_all else []))
        i = self.c_svc.findText(cur)
        self.c_svc.setCurrentIndex(i if i >= 0 else 0)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка с новой версией", self.e_dir.text())
        if d:
            self.e_dir.setText(d)

    def service(self) -> str:
        return self.c_svc.currentText().split("  [")[0].strip()

    def protected(self) -> list:
        return [x.strip() for x in self.e_prot.text().split(",") if x.strip()]


class DeleteDialog(QDialog):
    """Удаление бота: только с маркером, подтверждение именем."""

    def __init__(self, parent, managed: list):
        super().__init__(parent)
        self.setWindowTitle("Удалить бота")
        self.setModal(True)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.c_svc = QComboBox()
        self.c_svc.addItems(managed)
        self.e_confirm = QLineEdit()
        self.e_confirm.setPlaceholderText("введите имя сервиса для подтверждения")
        self.e_confirm.textChanged.connect(self._check)
        self.c_dir = QCheckBox("удалить также папку проекта (там база и сессии!)")
        form.addRow("Сервис:", self.c_svc)
        form.addRow("Подтверждение:", self.e_confirm)
        lay.addLayout(form)
        warn = QLabel("Папка проекта по умолчанию НЕ удаляется. Сервисы без маркера "
                      "«Managed by Bot Manager» удаляйте вручную.")
        warn.setWordWrap(True)
        warn.setObjectName("statusLine")
        lay.addWidget(warn)
        lay.addWidget(self.c_dir)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.btn_ok = box.button(QDialogButtonBox.Ok)
        self.btn_ok.setText("Удалить")
        self.btn_ok.setEnabled(False)
        box.button(QDialogButtonBox.Cancel).setText("Отмена")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self.setMinimumWidth(460)
        self.c_svc.currentTextChanged.connect(self._check)

    def _check(self) -> None:
        self.btn_ok.setEnabled(bool(self.c_svc.currentText())
                               and self.e_confirm.text().strip() == self.c_svc.currentText())


class DeployTab(QWidget):
    open_bots_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._creds = None
        self._profile = ""
        self._running = False
        self._checking = False
        self._last_info = {}
        self._state = self._load_state()
        self.runner = DaemonRunner(self)
        # быстрые независимые операции (проверка сервера, списки): свой поток,
        # чтобы не вставать за долгой задачей (напр. стриминг pip) в runner
        self.runner_fast = DaemonRunner(self)
        self.engine = DeployEngine(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 12)
        lay.setSpacing(10)

        # 1. проект
        card1 = QFrame()
        card1.setObjectName("card")
        c1 = QVBoxLayout(card1)
        c1.setContentsMargins(14, 12, 14, 12)
        c1.setSpacing(8)
        t1 = QLabel("1. Проект")
        t1.setObjectName("paneTitle")
        c1.addWidget(t1)
        row = QHBoxLayout()
        self.btn_folder = AnimatedButton("Выбрать папку…")
        self.btn_folder.setObjectName("btnGhost")
        self.btn_folder.setCursor(Qt.PointingHandCursor)
        self.btn_folder.clicked.connect(self.choose_folder)
        self.lbl_path = QLabel("папка не выбрана")
        self.lbl_path.setObjectName("statusLine")
        row.addWidget(self.btn_folder)
        row.addWidget(self.lbl_path, 1)
        c1.addLayout(row)
        self.lbl_summary = QLabel("")
        self.lbl_summary.setWordWrap(True)
        c1.addWidget(self.lbl_summary)
        lay.addWidget(card1)

        # 2. параметры
        card2 = QFrame()
        card2.setObjectName("card")
        c2 = QVBoxLayout(card2)
        c2.setContentsMargins(14, 12, 14, 12)
        c2.setSpacing(8)
        t2 = QLabel("2. Параметры")
        t2.setObjectName("paneTitle")
        c2.addWidget(t2)
        form = QFormLayout()
        self.e_service = QLineEdit()
        self.e_service.setPlaceholderText("mybot")
        self.e_service.textChanged.connect(lambda _t: self._sync_server_dir(auto=True))
        self.c_entry = QComboBox()
        self.c_entry.setEditable(True)
        self.e_dir = QLineEdit()
        self.e_dir.setPlaceholderText("/opt/bots/mybot")
        self.e_dir.textChanged.connect(lambda _t: setattr(self, "_dir_touched", True))
        self.lbl_py = QLabel("Python на сервере: —")
        self.lbl_py.setObjectName("statusLine")
        self.btn_check = AnimatedButton("Проверить сервер")
        self.btn_check.setObjectName("btnGhost")
        self.btn_check.setCursor(Qt.PointingHandCursor)
        self.btn_check.clicked.connect(self.check_server)
        form.addRow("Имя сервиса:", self.e_service)
        form.addRow("Точка входа:", self.c_entry)
        form.addRow("Папка на сервере:", self.e_dir)
        c2.addLayout(form)
        c2.addWidget(self.lbl_py)
        c2.addWidget(self.btn_check)
        lay.addWidget(card2)

        # 3. установка
        card3 = QFrame()
        card3.setObjectName("card")
        c3 = QVBoxLayout(card3)
        c3.setContentsMargins(14, 12, 14, 12)
        c3.setSpacing(8)
        t3 = QLabel("3. Установка")
        t3.setObjectName("paneTitle")
        c3.addWidget(t3)
        brow = QHBoxLayout()
        brow.setSpacing(8)
        self.btn_install = AnimatedButton("Установить и запустить")
        self.btn_install.setObjectName("btnSuccess")
        self.btn_cancel = AnimatedButton("Отмена")
        self.btn_cancel.setObjectName("btnGhost")
        self.btn_cancel.setEnabled(False)
        self.btn_update = AnimatedButton("Обновить бота…")
        self.btn_update.setObjectName("btnGhost")
        self.btn_delete = AnimatedButton("Удалить бота…")
        self.btn_delete.setObjectName("btnDanger")
        for b in (self.btn_install, self.btn_cancel, self.btn_update, self.btn_delete):
            b.setCursor(Qt.PointingHandCursor)
            brow.addWidget(b)
        brow.addStretch()
        c3.addLayout(brow)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("Готов")
        c3.addWidget(self.progress)
        self.journal = QPlainTextEdit()
        self.journal.setReadOnly(True)
        self.journal.setMaximumHeight(170)
        c3.addWidget(self.journal)
        lay.addWidget(card3)

        # 4. результат
        card4 = QFrame()
        card4.setObjectName("card")
        c4 = QVBoxLayout(card4)
        c4.setContentsMargins(14, 12, 14, 12)
        c4.setSpacing(8)
        t4 = QLabel("4. Результат")
        t4.setObjectName("paneTitle")
        c4.addWidget(t4)
        rrow = QHBoxLayout()
        self.lbl_status = QLabel("—")
        self.lbl_status.setObjectName("h2")
        self.btn_bots = AnimatedButton("Открыть в Ботах")
        self.btn_bots.setObjectName("btnGhost")
        self.btn_bots.setCursor(Qt.PointingHandCursor)
        self.btn_bots.clicked.connect(self.open_bots_requested.emit)
        self.btn_rmdir = AnimatedButton("Удалить папку")
        self.btn_rmdir.setObjectName("btnDanger")
        self.btn_rmdir.setCursor(Qt.PointingHandCursor)
        self.btn_rmdir.clicked.connect(self.remove_created_dir)
        self.btn_rmdir.hide()
        rrow.addWidget(self.lbl_status, 1)
        rrow.addWidget(self.btn_rmdir)
        rrow.addWidget(self.btn_bots)
        c4.addLayout(rrow)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumHeight(130)
        self.logs.setPlaceholderText("Здесь появятся последние 30 строк логов бота…")
        c4.addWidget(self.logs)
        lay.addWidget(card4)
        lay.addStretch()

        self._cards = [card1, card2, card3, card4]
        self.deploy_hint = QLabel("Подключитесь к серверу карточкой выше — "
                                  "здесь появятся проект, параметры и установка")
        self.deploy_hint.setWordWrap(True)
        self.deploy_hint.setObjectName("statusLine")
        lay.insertWidget(0, self.deploy_hint)
        self._set_deploy_visible(False)

        self.btn_install.clicked.connect(self.start_install)
        self.btn_cancel.clicked.connect(self.engine.cancel)
        self.btn_update.clicked.connect(self.open_update)
        self.btn_delete.clicked.connect(self.open_delete)
        self.engine.sig_step.connect(self._on_step)
        self.engine.sig_log.connect(self._on_log)
        self.engine.sig_progress.connect(self._on_progress)
        self.engine.sig_result.connect(self._on_result)
        self._local_dir = self._state.get("last_dir", "")
        if self._local_dir:
            self.set_folder(self._local_dir)

    # --- состояние ---
    def _load_state(self) -> dict:
        try:
            d = _json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            STATE_PATH.write_text(_json.dumps(
                {"last_dir": self._local_dir,
                 "protected": self._state.get("protected", list(DEFAULT_PROTECTED))},
                ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # --- подключение ---
    def on_connected(self, creds: dict) -> None:
        self._creds = dict(creds)
        self._profile = creds.get("profile", "")
        self._set_deploy_visible(True)
        self._log(f"Профиль: {self._profile}")

    def _set_deploy_visible(self, on: bool) -> None:
        for c in getattr(self, "_cards", []):
            c.setVisible(on)
        if hasattr(self, "deploy_hint"):
            self.deploy_hint.setVisible(not on)

    def _apply_status_color(self) -> None:
        d = derived()
        kind = getattr(self, "_status_kind", None)
        self.lbl_status.setStyleSheet(f"color: {d[kind]};" if kind in ("ok", "er") else "")

    def apply_theme(self) -> None:
        for b in self.findChildren(QPushButton):
            if isinstance(b, AnimatedButton):
                b.retheme()
        self._apply_status_color()

    def _need(self) -> bool:
        if not self._creds:
            _msg(self, "info", "Деплой", "Сначала подключитесь к серверу.")
            return False
        return True

    # --- шаг 1-2: проект ---
    def choose_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка с ботом", self._local_dir or str(Path.home()))
        if d:
            self.set_folder(d)

    def set_folder(self, d: str) -> None:
        self._local_dir = d
        self._state["last_dir"] = d
        self._save_state()
        self.lbl_path.setText(d)
        info = analyze_project(d)
        self.e_service.setText(sanitize_service_name(Path(d).name))
        self.c_entry.clear()
        self.c_entry.addItems(info["preferred"] or info["entries"])
        self._sync_server_dir(auto=True)
        parts = []
        parts.append("requirements.txt: " + ("найден" if info["requirements"] else "НЕ НАЙДЕН"))
        parts.append(".env: " + ("найден" if info["env"] else "не найден"))
        parts.append("точки входа: " + (", ".join(info["preferred"][:5]) if info["preferred"] else "нет .py в корне!"))
        if info["python_version"]:
            parts.append(f".python-version: {info['python_version']}")
        if info["requires_python"]:
            parts.append(f"requires-python: {info['requires_python']}")
        self._proj_info = info
        self.lbl_summary.setText(" · ".join(parts))

    def _sync_server_dir(self, auto: bool = False) -> None:
        if auto and getattr(self, "_dir_touched", False):
            return
        svc = sanitize_service_name(self.e_service.text())
        self.e_dir.blockSignals(True)
        self.e_dir.setText(f"{DEFAULT_SERVER_ROOT}/{svc}")
        self.e_dir.blockSignals(False)

    def _params(self) -> dict:
        info = getattr(self, "_proj_info", {}) or analyze_project(self._local_dir)
        return {"service": self.e_service.text().strip(),
                "entry": self.c_entry.currentText().strip(),
                "server_dir": self.e_dir.text().strip(),
                "local_dir": self._local_dir,
                "has_requirements": bool(info.get("requirements")),
                "want_python": info.get("python_version") or info.get("requires_python") or ""}

    # --- установка ---
    def start_install(self) -> None:
        if self._running or not self._need():
            return
        if not self._local_dir or not os.path.isdir(self._local_dir):
            _msg(self, "warn", "Деплой", "Выберите папку с ботом.")
            return
        p = self._params()
        if not valid_service_name(p["service"]):
            _msg(self, "warn", "Деплой",
                 "Имя сервиса: латиница, цифры, _ и -, с буквы, до 40 символов.")
            return
        if not p["entry"].endswith(".py"):
            _msg(self, "warn", "Деплой", "Точка входа — .py файл из корня проекта.")
            return
        if not p["server_dir"].startswith("/"):
            _msg(self, "warn", "Деплой", "Папка на сервере — абсолютный путь.")
            return
        info = analyze_project(self._local_dir)
        if not info["requirements"]:
            if _msg(self, "q", "Деплой",
                    "Нет requirements.txt — зависимости не поставятся. Продолжить?") \
                    != QMessageBox.Yes:
                return
        if not info["env"]:
            if _msg(self, "q", "Деплой",
                    "Нет .env — боту может не хватить настроек. Продолжить?") \
                    != QMessageBox.Yes:
                return
        else:
            issues = check_dotenv(os.path.join(self._local_dir, ".env"))
            if issues:
                lines = "\n".join(f"строка {n}: {msg}" for n, _k, msg in issues[:8])
                if _msg(self, "q", "Деплой",
                        f".env с подозрительным форматом:\n{lines}\nПродолжить?") \
                        != QMessageBox.Yes:
                    return
        self._set_running(True)
        self.journal.clear()
        self.logs.clear()
        self.lbl_status.setText("Установка…")
        self.btn_rmdir.hide()
        self.engine.install(p, dict(self._creds))

    def check_server(self) -> None:
        if self._running or self._checking or not self._need():
            return
        creds = dict(self._creds)
        self._checking = True
        self.btn_check.setEnabled(False)
        self.progress.setFormat("Проверка сервера…")
        self._log("Проверка сервера…")

        def work():
            import paramiko
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(creds["host"], port=int(creds.get("port") or 22),
                           username=creds["user"], password=creds.get("password") or None,
                           key_filename=creds.get("key") or None, timeout=10,
                           allow_agent=True, look_for_keys=True)
            try:
                rc, o, _e = ssh_exec(client, "cat /etc/os-release; echo ---; python3 --version 2>&1; echo ---; id -u")
                return o
            finally:
                try:
                    client.close()
                except Exception:
                    pass

        def done(res, err):
            self._checking = False
            self.btn_check.setEnabled(True)
            self.progress.setFormat("Готов")
            if err:
                self._log(f"Проверка не удалась: {err}")
                return
            self.lbl_py.setText("Сервер: " + " | ".join(
                [l for l in (res or '').splitlines() if l][:6]).replace("---", "·")[:160])
            self._log("Проверка сервера готова")

        self.runner_fast.submit(work, done)

    # --- обновление / удаление ---
    def _fetch_units(self, cb) -> None:
        if not self._creds:
            cb(([], []), "Нет подключения")
            return
        creds = dict(self._creds)
        eng = self.engine

        def work():
            import paramiko
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(creds["host"], port=int(creds.get("port") or 22),
                           username=creds["user"], password=creds.get("password") or None,
                           key_filename=creds.get("key") or None, timeout=10,
                           allow_agent=True, look_for_keys=True)
            try:
                return eng.list_units(client)
            finally:
                try:
                    client.close()
                except Exception:
                    pass

        self.runner_fast.submit(work, lambda res, err: cb(res, err))

    def open_update(self) -> None:
        if self._running or not self._need():
            return

        def done(res, err):
            if err:
                return self._log(f"Список сервисов не получен: {err}")
            managed, others = res
            if not managed and not others:
                return self._log("Своих сервисов на сервере не видно")
            dlg = UpdateDialog(self, managed, others, self._local_dir or "",
                               self._state.get("protected", list(DEFAULT_PROTECTED)))
            if dlg.exec() != QDialog.Accepted:
                return
            svc = dlg.service()
            if not svc:
                return
            local_dir = dlg.e_dir.text().strip()
            if not local_dir or not os.path.isdir(local_dir):
                _msg(self, "warn", "Деплой", "Выберите папку с новой версией.")
                return
            self._state["protected"] = dlg.protected()
            self._save_state()
            server_dir = ""
            for n, wd in others:
                if n == svc:
                    server_dir = wd
                    break
            if not server_dir:
                # свой сервис: папка из WorkingDirectory юнита
                server_dir = self._working_dir_of(svc)
                if not server_dir:
                    return self._log("Не знаю папку бота на сервере")
            self._set_running(True)
            self.journal.clear()
            self.logs.clear()
            self.engine.update_bot({"service": svc, "server_dir": server_dir,
                                    "local_dir": local_dir,
                                    "protected": dlg.protected()}, dict(self._creds))

        self._log("Читаю список сервисов…")
        self._fetch_units(done)

    def _working_dir_of(self, svc: str):
        import paramiko
        if not self._creds:
            return ""
        c = dict(self._creds)
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(c["host"], port=int(c.get("port") or 22),
                           username=c["user"], password=c.get("password") or None,
                           key_filename=c.get("key") or None, timeout=10,
                           allow_agent=True, look_for_keys=True)
            try:
                rc, o, _e = ssh_exec(
                    client, f"systemctl show -p WorkingDirectory --value {svc}")
                return (o or "").strip().lstrip("!-")
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        except Exception:
            return ""

    def open_delete(self) -> None:
        if self._running or not self._need():
            return

        def done(res, err):
            if err:
                return self._log(f"Список сервисов не получен: {err}")
            managed, _others = res
            if not managed:
                return self._log("Сервисов с маркером нет — удалять нечего")
            dlg = DeleteDialog(self, managed)
            if dlg.exec() != QDialog.Accepted:
                return
            self._set_running(True)
            self.journal.clear()
            self.engine.delete_bot({"service": dlg.c_svc.currentText(),
                                    "server_dir": self._working_dir_of(dlg.c_svc.currentText()),
                                    "delete_dir": dlg.c_dir.isChecked()}, dict(self._creds))

        self._fetch_units(done)

    def remove_created_dir(self) -> None:
        info = self._last_info
        if not info.get("created_dir") or not info.get("server_dir") or not self._need():
            return
        if _msg(self, "q", "Деплой",
                 f'Удалить папку {info["server_dir"]} на сервере?') != QMessageBox.Yes:
            return
        self._set_running(True)
        eng = self.engine
        creds = dict(self._creds)
        server_dir = info["server_dir"]

        def work():
            import paramiko
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(creds["host"], port=int(creds.get("port") or 22),
                           username=creds["user"], password=creds.get("password") or None,
                           key_filename=creds.get("key") or None, timeout=10,
                           allow_agent=True, look_for_keys=True)
            try:
                if not eng._safe_dir(server_dir):
                    return False
                import shlex as _sh
                rc, _o, _e = ssh_exec(client, f"rm -rf {_sh.quote(server_dir)}")
                return rc == 0
            finally:
                try:
                    client.close()
                except Exception:
                    pass

        def done(res, err):
            self._set_running(False)
            if err or not res:
                self._log("Папку удалить не удалось")
            else:
                self._log(f"Папка удалена: {server_dir}")
                self.btn_rmdir.hide()

        self.runner_fast.submit(work, done)

    # --- слоты движка ---
    def _set_running(self, on: bool) -> None:
        self._running = on
        for b in (self.btn_install, self.btn_update, self.btn_delete,
                  self.btn_folder, self.btn_check):
            b.setEnabled(not on)
        self.btn_cancel.setEnabled(on)
        if on:
            self.progress.setValue(0)
            self.progress.setFormat("Работаю…")

    def _on_step(self, _key: str, label: str, state: str) -> None:
        mark = {"run": "…", "ok": "✔", "fail": "✖", "warn": "!"}.get(state, "")
        # движок уже пишет ✔/✖/! сам; дублируем только старты шагов
        if state == "run":
            self.journal.appendPlainText(f"{mark} {label}")

    def _on_log(self, text: str) -> None:
        for line in str(text).splitlines():
            self.journal.appendPlainText(line)
        bar = self.journal.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_progress(self, done_b: int, total_b: int) -> None:
        if total_b > 0:
            self.progress.setValue(int(done_b * 100 / total_b))
            self.progress.setFormat(f"{human_size(done_b)} / {human_size(total_b)}")
        else:
            self.progress.setFormat("Заливка…")

    def _on_result(self, ok: bool, info: dict) -> None:
        self._last_info = dict(info or {})
        self._set_running(False)
        status = (info or {}).get("status", "")
        self.progress.setValue(100 if ok else 0)
        if status == "running":
            self.lbl_status.setText("Бот работает")
            self._status_kind = "ok"
        elif status == "crashing":
            self.lbl_status.setText("Бот падает (рестарты по кругу)")
            self._status_kind = "er"
        elif status == "failed":
            self.lbl_status.setText("Не получилось — смотри журнал")
            self._status_kind = "er"
        elif status == "deleted":
            self.lbl_status.setText("Бот удалён")
            self._status_kind = None
        else:
            self.lbl_status.setText(status or "—")
            self._status_kind = None
        self._apply_status_color()
        logs = (info or {}).get("logs", "")
        if logs:
            self.logs.setPlainText(logs)
        show_rm = bool((info or {}).get("created_dir")) and not ok
        self.btn_rmdir.setVisible(show_rm)

    def _log(self, text: str) -> None:
        self.journal.appendPlainText(text)
