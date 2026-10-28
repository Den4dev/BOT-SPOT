#!/usr/bin/env python3
"""Установщик BOT SPOT.

Кладёт программу в %LOCALAPPDATA%\\Programs\\BOT SPOT (без прав администратора),
делает ярлыки и запись в «Установка и удаление программ»; удаление — uninstall.ps1.

Режимы:
    bot-spot-setup.exe          окно установщика
    bot-spot-setup.exe /S       тихая установка (только меню «Пуск», без автозапуска)
"""
import os
import shutil
import subprocess
import sys
import winreg
from pathlib import Path

APP_NAME = "BOT SPOT"
APP_EXE = "bot-spot.exe"
VERSION = "1.0.0"
PUBLISHER = "Den4dev"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\BOT SPOT"
UNINSTALL_KEY_PS = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\BOT SPOT"


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def install_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / "Programs" / APP_NAME


def bundled_app() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / APP_EXE
    return Path(__file__).resolve().parent / "dist" / APP_EXE


def _powershell(script: str) -> str:
    res = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                         capture_output=True, text=True, creationflags=_no_window())
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or "powershell завершился с ошибкой").strip())
    return res.stdout


def _q(p: Path) -> str:
    return str(p).replace("'", "''")


def make_shortcut(lnk: Path, target: Path) -> None:
    lnk.parent.mkdir(parents=True, exist_ok=True)
    _powershell(
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{_q(lnk)}');"
        f"$s.TargetPath='{_q(target)}';$s.WorkingDirectory='{_q(target.parent)}';"
        f"$s.IconLocation='{_q(target)},0';$s.Description='{APP_NAME}';$s.Save()"
    )


def desktop_dir() -> Path:
    try:
        p = _powershell("[Environment]::GetFolderPath('Desktop')").strip()
        if p:
            return Path(p)
    except Exception:
        pass
    return Path.home() / "Desktop"


def start_menu_link() -> Path:
    appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME / f"{APP_NAME}.lnk"


def is_installed() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY):
            return True
    except OSError:
        return False


def write_uninstall_script(dest: Path) -> None:
    d = str(dest).replace("'", "''")
    lines = [
        "# Uninstall BOT SPOT (created by bot-spot-setup.exe)",
        f"$d = '{d}'",
        "$lnks = @(",
        f"  (Join-Path ([Environment]::GetFolderPath('Desktop')) '{APP_NAME}.lnk'),",
        f"  (Join-Path $env:APPDATA 'Microsoft\\Windows\\Start Menu\\Programs\\{APP_NAME}\\{APP_NAME}.lnk')",
        ")",
        "foreach ($f in $lnks) {",
        "  if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }",
        "}",
        f"$sm = Join-Path $env:APPDATA 'Microsoft\\Windows\\Start Menu\\Programs\\{APP_NAME}'",
        "if (Test-Path -LiteralPath $sm) { Remove-Item -LiteralPath $sm -Recurse -Force -ErrorAction SilentlyContinue }",
        f"Remove-Item -Path '{UNINSTALL_KEY_PS}' -Recurse -Force -ErrorAction SilentlyContinue",
        # папку нельзя стереть, пока приложение запущено или файл скрипта ещё у PS в руках —
        # cmd ретраит rmdir до ~30 сек и выходит, когда получится
        "Start-Process -WindowStyle Hidden cmd -ArgumentList ('/c for /L %i in (1,1,15) do "
        "(rmdir /s /q \"' + $d + '\" 2>nul && exit /b 0 || ping -n 2 127.0.0.1 >nul)')",
        "",
    ]
    (dest / "uninstall.ps1").write_text("\r\n".join(lines), encoding="ascii")


def register(dest: Path) -> None:
    exe = dest / APP_EXE
    size_kb = max(1, exe.stat().st_size // 1024) if exe.exists() else 0
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, VERSION)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(dest))
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(exe))
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ,
                          f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{dest / "uninstall.ps1"}"')
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)


def do_install(desktop_shortcut: bool = True) -> Path:
    src = bundled_app()
    if not src.exists():
        raise RuntimeError(f"внутри установщика нет {APP_EXE}")
    dest = install_dir()
    dest.mkdir(parents=True, exist_ok=True)
    exe = dest / APP_EXE
    tmp = dest / (APP_EXE + ".new")
    shutil.copyfile(src, tmp)
    try:
        os.replace(tmp, exe)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise RuntimeError("BOT SPOT сейчас запущен — закрой его и повтори установку")
    write_uninstall_script(dest)
    make_shortcut(start_menu_link(), exe)
    if desktop_shortcut:
        make_shortcut(desktop_dir() / f"{APP_NAME}.lnk", exe)
    register(dest)
    return dest


def launch(exe: Path) -> None:
    flags = 0
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=flags)


def run_gui() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QHBoxLayout,
                                   QLabel, QMessageBox, QPushButton, QVBoxLayout)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    icon_path = Path(sys._MEIPASS) / "icon.ico" if getattr(sys, "frozen", False) else Path(__file__).parent / "icon.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    app.setStyleSheet("""
        QDialog { background: #0e0e11; }
        QLabel { color: #c9c9d4; font-size: 10pt; }
        QLabel#title { color: #ececf1; font-size: 16pt; font-weight: 700; }
        QLabel#dest { color: #8a8a9c; }
        QCheckBox { color: #ececf1; spacing: 8px; font-size: 10pt; }
        QPushButton {
            background: #1d1d24; color: #ececf1; border: 1px solid #2c2c36;
            border-radius: 8px; padding: 8px 16px; font-size: 10pt;
        }
        QPushButton:hover { background: #26262f; }
        QPushButton#primary { background: #4c6ef5; border: 1px solid #4c6ef5; color: #ffffff; font-weight: 600; }
        QPushButton#primary:hover { background: #5b7cff; }
    """)

    installed = is_installed()
    dlg = QDialog()
    dlg.setWindowTitle("Установка BOT SPOT" if not installed else "Обновление BOT SPOT")
    dlg.setMinimumWidth(500)
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(22, 20, 22, 18)
    lay.setSpacing(10)

    title = QLabel(f"BOT SPOT {VERSION}")
    title.setObjectName("title")
    lay.addWidget(title)
    sub = QLabel("Менеджер ботов на удалённом сервере по SSH.\n"
                 "Установка для текущего пользователя — права администратора не нужны.")
    lay.addWidget(sub)
    dest_lbl = QLabel(f"Папка установки: {install_dir()}")
    dest_lbl.setObjectName("dest")
    dest_lbl.setWordWrap(True)
    lay.addWidget(dest_lbl)
    if installed:
        note = QLabel("Программа уже установлена. Установщик заменит её на эту версию — "
                      "профили и настройки сохранятся.")
        note.setWordWrap(True)
        lay.addWidget(note)

    c_desk = QCheckBox("Ярлык на рабочем столе")
    c_desk.setChecked(True)
    c_run = QCheckBox("Запустить после установки")
    c_run.setChecked(True)
    lay.addWidget(c_desk)
    lay.addWidget(c_run)

    row = QHBoxLayout()
    row.addStretch()
    btn_cancel = QPushButton("Отмена")
    btn_ok = QPushButton("Обновить" if installed else "Установить")
    btn_ok.setObjectName("primary")
    btn_ok.setDefault(True)
    row.addWidget(btn_cancel)
    row.addWidget(btn_ok)
    lay.addLayout(row)

    def do() -> None:
        btn_ok.setEnabled(False)
        btn_cancel.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            dest = do_install(c_desk.isChecked())
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(dlg, "Установка", f"Не удалось установить:\n{e}")
            btn_ok.setEnabled(True)
            btn_cancel.setEnabled(True)
            return
        QApplication.restoreOverrideCursor()
        if c_run.isChecked():
            launch(dest / APP_EXE)
        dlg.accept()

    btn_cancel.clicked.connect(dlg.reject)
    btn_ok.clicked.connect(do)
    dlg.exec()
    return 0


def main() -> int:
    args = {a.lower() for a in sys.argv[1:]}
    if "/s" in args or "--silent" in args:
        try:
            do_install(desktop_shortcut=False)
            return 0
        except Exception:
            return 1
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
