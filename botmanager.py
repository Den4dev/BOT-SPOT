#!/usr/bin/env python3
"""BOT SPOT: FileZilla для systemd-ботов. Подключился по SSH, видишь ботов, жмёшь кнопки."""
import base64
import json
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath

import paramiko
from PySide6.QtCore import QByteArray, QEvent, QObject, QProcess, QProcessEnvironment, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QIcon, QLinearGradient, QPainter,
                           QPainterPath, QPixmap, QRadialGradient, QSyntaxHighlighter,
                           QTextCharFormat, QTextCursor)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QFrame, QGraphicsDropShadowEffect,
    QGridLayout, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QMenu, QFileDialog, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox, QSplitter,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from files_tab import FilesTab, build_seg_qss, sanitized_env
from backup_tab import BackupTab
from deploy_tab import DeployTab
from env_editor import EnvDialog
from ui_anim import (DEFAULT_THEME, THEMES, THEME_TITLES, AnimatedButton,
                     RowHoverTable, alpha, animate_geometry, derived, fade_widget,
                     flash_last_alert, mix, on_color, register_theme_hook,
                     set_theme, shade, theme_name, tokens)

try:
    import keyring  # пароль хранится в системном хранилище (Keychain / Credential Manager / Secret Service)
except Exception:
    keyring = None

CFG = Path.home() / ".botmanager.json"
KR_SERVICE = "botmanager"
CONNECT_WATCHDOG_MS = 45000  # если коннект висит дольше — разблокировать кнопку
TG_RE = "aiogram|telebot|telegram|pyrogram|telethon|tgbotapi|telego|telegraf|grammy|node-telegram|BOT_TOKEN|TG_TOKEN|TELEGRAM_BOT|api\\.telegram\\.org"
# системные префиксы — не папки проектов, пропускаем при угадывании каталога бота
SKIP_BIN_PREFIX = ("/usr/bin", "/usr/sbin", "/usr/lib", "/bin", "/sbin", "/lib", "/etc/systemd",
                   "/run", "/proc", "/sys", "/dev", "/var/lib/docker")
NEW_PROF = "— новое подключение —"
# локальная пара ключей для беспарольного терминала (wt/ssh подставляет -i)
BOOT_KEY = Path.home() / ".ssh" / "botspot_terminal"
BOOT_COMMENT = "botspot-terminal"
FIELDS = "Id,ActiveState,SubState,MainPID,ExecStart,WorkingDirectory,FragmentPath,ActiveEnterTimestamp"

FONT_UI = "Segoe UI Variable"
FONT_MONO = "Cascadia Code"


def resource_path(name):
    """Путь к ресурсу: работает и в .py, и внутри exe (sys._MEIPASS)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    p = base / name
    if p.exists():
        return str(p)
    return str(Path(__file__).parent / name)


def app_icon():
    for n in ("icon.ico", "icon.png"):
        p = resource_path(n)
        if Path(p).exists():
            return QIcon(p)
    return QIcon()


_ICON_CACHE: dict[tuple[str, str, int], QIcon] = {}


def load_icon(name: str, color: str = None, size: int = 16) -> QIcon:
    """Монохромная SVG-иконка из icons/ с подстановкой currentColor. Только отображение."""
    if color is None:
        color = tokens()["icon"]
    key = (name, color, size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    icon = QIcon()
    try:
        data = Path(resource_path(f"icons/{name}.svg")).read_text(encoding="utf-8")
        data = data.replace("currentColor", color)
        rend = QSvgRenderer(QByteArray(data.encode("utf-8")))
        if rend.isValid():
            pm = QPixmap(size, size)
            pm.fill(Qt.transparent)
            with QPainter(pm) as p:
                rend.render(p)
            icon = QIcon(pm)
    except OSError:
        pass
    _ICON_CACHE[key] = icon
    return icon


def strip_docker_prefix(name: str) -> str:
    """Имя для отображения: без служебного префикса '🐳 '. Данные и логика не меняются."""
    return name[2:].strip() if name.startswith("🐳 ") else name


class GradientRoot(QWidget):
    """Фон окна (п.3 ТЗ): линейный градиент 135° + мягкое радиальное свечение."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("root")

    def paintEvent(self, ev) -> None:
        d = derived()
        p = QPainter(self)
        p.setPen(Qt.NoPen)
        r = self.rect()
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0.0, QColor(d["grad1"]))
        g.setColorAt(0.55, QColor(d["grad2"]))
        g.setColorAt(1.0, QColor(d["grad3"]))
        p.fillRect(r, g)
        rad = max(r.width(), r.height()) * 0.45
        rg = QRadialGradient(r.width() * 0.65, r.height() * 0.35, rad)
        rg.setColorAt(0.0, QColor(d["glow"]))
        rg.setColorAt(1.0, QColor(alpha(d["ac"], 0)))
        p.fillRect(r, rg)
        p.end()


class GradientPanel(QFrame):
    """Карточка области таблицы ботов: свой градиент + свечение справа + рамка."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("gradCard")

    def paintEvent(self, ev) -> None:
        d = derived()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        rad = d["r"] + 2
        path = QPainterPath()
        path.addRoundedRect(r.adjusted(0, 0, -1, -1), rad, rad)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0.0, QColor(d["panel1"]))
        g.setColorAt(0.5, QColor(d["panel2"]))
        g.setColorAt(1.0, QColor(d["panel3"]))
        p.fillPath(path, QBrush(g))
        p.save()
        p.setClipPath(path)
        rrad = max(r.width(), r.height()) * 0.55
        rg = QRadialGradient(r.width() * 0.70, r.height() * 0.40, rrad)
        rg.setColorAt(0.0, QColor(alpha(d["ac"], 46)))
        rg.setColorAt(1.0, QColor(alpha(d["ac"], 0)))
        p.fillRect(r, rg)
        p.restore()
        p.setPen(QColor(d["panel_border"]))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.end()


class LogHighlighter(QSyntaxHighlighter):
    """Цвет только на токене уровня (п.4 ТЗ), остальная строка нейтральная."""

    def __init__(self, doc):
        super().__init__(doc)
        self._formats = {}
        self._build_formats()
        self._rx = re.compile(r"\blevel\s*=\s*(INFO|SUCCESS|WARNING|ERROR|DEBUG)\b"
                              r"|\b(INFO|SUCCESS|WARNING|ERROR|DEBUG|Traceback)\b")

    def _build_formats(self):
        d = derived()
        levels = {"INFO": d["ac"], "SUCCESS": d["ok"], "WARNING": d["wn"],
                  "ERROR": d["er"], "DEBUG": d["mu"]}
        self._formats = {}
        for level, color in levels.items():
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            fmt.setFontWeight(QFont.Bold)
            self._formats[level] = fmt

    def retheme(self):
        self._build_formats()
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        for m in self._rx.finditer(text):
            level = m.group(1) or m.group(2)
            key = "ERROR" if level == "Traceback" else level
            self.setFormat(m.start(), m.end() - m.start(), self._formats[key])


def build_qss(d: dict) -> str:
    """Весь QSS приложения из токенов темы (d = ui_anim.derived())."""
    return f"""
* {{ outline: none; }}
QMainWindow {{ background: {d['pg']}; }}
QFrame#topbar {{ background: {d['pg']}; border: none; border-bottom: 1px solid {d['ln_solid']}; border-radius: 0; }}
QLabel {{ color: {d['tx']}; }}
QLabel#title {{ font-size: 19px; font-weight: 800; letter-spacing: 1px; color: {d['tx']}; }}
QLabel#subtitle {{ color: {d['mu']}; font-size: 11px; }}
QLabel#h2 {{ font-size: 13px; font-weight: 700; color: {d['tx']}; }}
QLabel#statsPill {{
    background: {mix(d['sf'], d['ac'], 0.10)}; color: {d['tx']};
    border: 1px solid {d['ln_solid']}; border-radius: {d['rs'] + 4}px; padding: 6px 12px; font-weight: 600;
}}
QFrame#card {{
    background: {d['card']}; border: 1px solid {d['card_border']}; border-radius: {d['r'] + 2}px;
}}
QFrame#glassCard {{
    background: {d['glass']}; border: 1px solid {d['glass_border']}; border-radius: {d['r'] + 2}px;
}}
QFrame#logCard {{
    background: {d['log_bg']}; border: 1px solid {d['card_border']}; border-radius: {d['r'] + 2}px;
}}
QLineEdit, QComboBox, QSpinBox {{
    background: {d['inp_bg']}; color: {d['inp_fg']}; border: 1px solid {d['inp_border']};
    border-radius: 8px; padding: 7px 11px; selection-background-color: {d['ac']};
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{ border: 1px solid {mix(d['ac'], d['ln_solid'], 0.5)}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {d['ac']}; }}
QLineEdit::placeholder {{ color: {d['placeholder']}; }}
QComboBox QAbstractItemView {{
    background: {d['sd']}; color: {d['tx']}; border: 1px solid {d['ln_solid']};
    selection-background-color: {d['item_sel']}; outline: none;
}}
QPushButton {{
    border: 1px solid transparent; border-radius: 8px; padding: 9px 18px;
    font-weight: 700; color: {d['tx']}; background: {d['btn']};
}}
QPushButton:hover {{ background: {d['btn_hover']}; }}
QPushButton:pressed {{ background: {d['btn_press']}; }}
QPushButton:disabled {{ color: {d['mu']}; background: {d['btn']}; }}
QPushButton#btnPrimary {{ background: {d['primary']}; color: {d['primary_fg']}; border: 1px solid {shade(d['ac'], 1.15)}; }}
QPushButton#btnPrimary:hover {{ background: {d['primary_hover']}; }}
QPushButton#btnPrimary:pressed {{ background: {d['primary_press']}; }}
QPushButton#btnSuccess {{ background: {d['success']}; color: {d['success_fg']}; }}
QPushButton#btnSuccess:hover {{ background: {d['success_hover']}; color: {d['success_fg']}; }}
QPushButton#btnDanger {{ background: {d['danger']}; color: {d['danger_fg']}; }}
QPushButton#btnDanger:hover {{ background: {d['danger_hover']}; }}
QPushButton#btnWarning {{ background: {d['warning']}; color: {d['warning_fg']}; }}
QPushButton#btnWarning:hover {{ background: {d['warning_hover']}; color: {d['warning_fg']}; }}
QPushButton#btnGhost {{ background: {d['ghost_bg']}; color: {d['ghost_fg']}; border: 1px solid {d['ghost_border']}; }}
QPushButton#btnGhost:hover {{ background: {d['ghost_hover']}; }}
QCheckBox {{ color: {d['tx']}; spacing: 7px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {d['ln_solid']}; background: {d['inp_bg']}; }}
QCheckBox::indicator:checked {{ background: {d['ac']}; border: 1px solid {d['ac']}; }}
QTableWidget {{
    background: transparent; alternate-background-color: {d['row_alt']};
    color: {d['tx']}; gridline-color: {d['grid']}; border: none; border-radius: 0;
}}
QTableWidget::item {{ padding: 4px 6px; border: none; }}
QTableWidget::item:selected {{ background: {d['item_sel']}; color: {d['item_sel_fg']}; }}
QHeaderView::section {{
    background: {d['header_bg']}; color: {d['header_fg']}; border: none; border-bottom: 1px solid {d['header_border']};
    padding: 9px 6px; font-weight: 700; font-size: 11px;
}}
QHeaderView::section:first {{ border-top-left-radius: 10px; }}
QHeaderView::section:last {{ border-top-right-radius: 10px; }}
QTableCornerButton::section {{ background: {d['header_bg']}; border: none; }}
QPlainTextEdit {{
    background: {d['log_bg']}; color: {d['mu']}; border: 1px solid {d['card_border']};
    border-radius: 10px; padding: 8px; selection-background-color: {d['ac']};
}}
QProgressBar {{
    background: {d['inp_bg']}; border: 1px solid {d['inp_border']}; border-radius: 6px;
    text-align: center; color: {d['tx']}; font-size: 10px; height: 14px;
}}
QProgressBar::chunk {{ background: {d['ac']}; border-radius: 5px; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 18px; border: none; background: transparent; }}
QStatusBar {{ background: {d['pg']}; color: {d['mu']}; border-top: 1px solid {d['ln_solid']}; }}
QStatusBar::item {{ border: none; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:vertical {{ height: 8px; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {d['scroll']}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {d['scroll_hover']}; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {d['scroll']}; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QToolTip {{ background: {d['tooltip_bg']}; color: {d['tx']}; border: 1px solid {d['ln_solid']}; padding: 5px; }}
"""


# ---------- SSH ----------
class SSH:
    def __init__(self):
        self.client = None
        self.user = ""
        self.password = ""
        self.lock = threading.Lock()

    def connect(self, host, port, user, password, key):
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(host, port=port, username=user, password=password or None,
                  key_filename=key or None, timeout=10, allow_agent=True, look_for_keys=True)
        c.get_transport().set_keepalive(20)
        old, self.client = self.client, c
        self.user, self.password = user, password
        if old is not None:  # не копим висящие сессии на сервере
            try:
                old.close()
            except Exception:
                pass

    def run(self, cmd, sudo=False, timeout=30):
        need_sudo = sudo and self.user != "root"
        if need_sudo:
            cmd = ("sudo -S -p '' " if self.password else "sudo -n ") + cmd
        with self.lock:
            # timeout ограничивает чтение канала: зависший сервер не копит треды вечно
            stdin, out, err = self.client.exec_command(cmd, timeout=timeout)
            if need_sudo and self.password:
                stdin.write(self.password + "\n")
                stdin.flush()
            o = out.read().decode(errors="replace")
            e = err.read().decode(errors="replace")
            code = out.channel.recv_exit_status()
        return code, o, e


def boot_key_pair():
    """Локальная пара ключей терминала: (приватный, публичным одной строкой).

    ed25519 через ssh-keygen: современные sshd отключили подпись rsa-sha1,
    с RSA Windows-клиент так и просил бы пароль.
    """
    pub_file = Path(str(BOOT_KEY) + ".pub")
    if not BOOT_KEY.exists():
        BOOT_KEY.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", str(BOOT_KEY),
                            "-N", "", "-C", BOOT_COMMENT, "-q"],
                           check=True, capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            k = paramiko.RSAKey.generate(2048)
            k.write_private_key_file(str(BOOT_KEY))
            pub_file.write_text(f"ssh-rsa {k.get_base64()} {BOOT_COMMENT}\n", encoding="ascii")
    if not pub_file.exists():
        for cls in (paramiko.Ed25519Key, paramiko.RSAKey):
            try:
                k = cls(filename=str(BOOT_KEY))
                pub_file.write_text(f"{k.get_name()} {k.get_base64()} {BOOT_COMMENT}\n",
                                    encoding="ascii")
                break
            except Exception:
                continue
    return str(BOOT_KEY), pub_file.read_text(encoding="ascii").strip()


def _exec_tokens(d):
    """Токены запуска юнита: сначала argv[] из systemctl show, иначе сам ExecStart."""
    m = re.search(r"argv\[\]=([^;]*)", d.get("ExecStart", ""))
    src = m.group(1) if m else d.get("ExecStart", "")
    try:
        return shlex.split(src)
    except ValueError:
        return []


def bot_dir(d):
    wd = d.get("WorkingDirectory", "")
    if wd:
        return wd.lstrip("!-")
    toks = _exec_tokens(d)
    for tok in toks:
        t = tok.strip().strip("'\"")
        if t.startswith("/") and (t.endswith((".py", ".go")) or t.endswith((".yml", ".yaml", ".env"))):
            return str(PurePosixPath(t).parent)
    for tok in toks:
        t = tok.strip().strip("'\"")
        if (t.startswith("/") and "/" in t[1:] and "." not in PurePosixPath(t).name
                and not t.startswith(SKIP_BIN_PREFIX) and ":" not in t and "=" not in t):
            return t  # похоже на каталог проекта (напр. бинарник /opt/gobot/bot)
    return ""


def unit_kind(i, u, tg_hits, go_units):
    """Telegram (любой язык/докер) → Go → Python → Node → Docker → Другое."""
    if tg_hits.get(i):
        return "Telegram"
    ex = u.get("ExecStart", "").lower()
    if go_units.get(i) or "go run" in ex or ".go" in ex or "go.mod" in ex:
        return "Go"
    if "python" in ex:
        return "Python"
    if "node" in ex or "bun" in ex or "deno" in ex:
        return "Node"
    if "docker" in ex:
        return "Docker"
    return "Другое"


def docker_rows(ssh):
    """Контейнеры docker (включая Go-ботов без systemd-юнита). Возвращает (rows, pids)."""
    rows, cpids = [], []
    rc, o, e = ssh.run("docker ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}'")
    sudo = False
    if rc != 0 or not o.strip():
        if "permission denied" in (e or "").lower() or (rc == 0 and not o.strip() and e):
            rc2, o2, e2 = ssh.run("docker ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}'", sudo=True)
            if rc2 == 0 and o2.strip():
                rc, o, sudo = rc2, o2, True
            else:
                return [], []
        elif rc != 0:
            return [], []  # docker нет на сервере
        else:
            return [], []  # контейнеров нет
    names = []
    info = {}
    for line in o.splitlines():
        p = line.split("|")
        if len(p) >= 3 and p[0].strip():
            cname, image, status = p[0].strip(), p[1].strip(), p[2].strip()
            names.append(cname)
            info[cname] = (image, status)
    if not names:
        return [], []
    fmt = ("{{.Name}}|{{.State.Status}}|{{.State.Pid}}|{{.State.StartedAt}}|{{.Config.Image}}|"
           "{{range .Config.Env}}{{println .}}{{end}}---END---")
    rc, o, _ = ssh.run("docker inspect " + " ".join(shlex.quote(n) for n in names) + f" --format '{fmt}'",
                        sudo=sudo)
    if rc != 0:
        # inspect не удался — покажем хотя бы то, что дал docker ps
        for cname in names:
            image, status = info[cname]
            up = status.startswith("Up")
            rows.append({"name": f"🐳 {cname}", "kind": "Telegram" if re.search(TG_RE, image, re.I) else "Docker",
                         "active": "active" if up else "inactive", "sub": "running" if up else status.split()[0].lower(),
                         "pid": "0", "mem": None, "cpu": None, "since": ""})
        return rows, []
    for block in o.split("---END---"):
        head, _, env = block.strip().partition("\n")
        p = head.split("|")
        if len(p) < 5:
            continue
        cname, state, pid, started, image = p[0].lstrip("/"), p[1], p[2], p[3], p[4]
        blob = image + "\n" + env
        kind = "Telegram" if re.search(TG_RE, blob, re.I) else "Docker"
        if state == "running":
            active, sub = "active", "running"
        elif state == "restarting":
            active, sub = "failed", "restarting"
        else:
            active, sub = "inactive", state
        if pid not in ("", "0"):
            cpids.append(pid)
        rows.append({"name": f"🐳 {cname}", "kind": kind, "active": active, "sub": sub,
                     "pid": pid if pid != "0" else "", "mem": None, "cpu": None,
                     "since": started[:19].replace("T", " ") if active == "active" else ""})
    return rows, cpids


def scan(ssh):
    """Свои сервисы из /etc/systemd/system + docker-контейнеры.
    Тип: Telegram (py/go/js/токены, включая compose/env) → Go → Python → Node → Docker → Другое."""
    _, o, _ = ssh.run("systemctl list-unit-files --type=service --no-legend --no-pager | awk '{print $1}'")
    names = [n for n in o.split() if n.endswith(".service") and "@" not in n]
    if not names:
        return []
    _, o, _ = ssh.run(f"systemctl show --no-pager -p {FIELDS} " + " ".join(shlex.quote(n) for n in names))
    units = []
    for block in o.strip().split("\n\n"):
        d = dict(l.split("=", 1) for l in block.splitlines() if "=" in l)
        if d.get("FragmentPath", "").startswith("/etc/systemd/system"):
            units.append(d)

    # ищем в папке бота телеграм-маркеры: код (py/go/js), go.mod, compose/env/Dockerfile
    dirs = [(i, bot_dir(u)) for i, u in enumerate(units)]
    dirs = [(i, p) for i, p in dirs if p]
    tg_hits, go_units = {}, {}
    if dirs:
        code = ("-name '*.py' -o -name '*.go' -o -name 'go.mod' -o -name 'go.sum' -o -name '*.js' "
                "-o -name '*.ts' -o -name '*.json' -o -name '*.y*ml' -o -name '*.env' -o -name 'Dockerfile*'")
        skip = "-not -path '*/venv/*' -not -path '*/.venv/*' -not -path '*/node_modules/*' -not -path '*/.git/*'"
        script = "; ".join(
            f"echo TG{i}:$(find {shlex.quote(p)} -maxdepth 3 \\( {code} \\) {skip} "
            f"2>/dev/null | head -80 | xargs -r grep -liE '{TG_RE}' 2>/dev/null | head -1)"
            f"; echo GO{i}:$(find {shlex.quote(p)} -maxdepth 2 -name 'go.mod' -print -quit 2>/dev/null)"
            for i, p in dirs
        )
        _, o, _ = ssh.run(script)
        for line in o.splitlines():
            if line.startswith("TG"):
                k, _, v = line[2:].partition(":")
                if k.isdigit() and v.strip():
                    tg_hits[int(k)] = True
            elif line.startswith("GO"):
                k, _, v = line[2:].partition(":")
                if k.isdigit() and v.strip():
                    go_units[int(k)] = True

    dock, dock_pids = docker_rows(ssh)
    pids = [u["MainPID"] for u in units if u.get("MainPID", "0") not in ("", "0")] + dock_pids
    stats = {}
    if pids:
        _, o, _ = ssh.run("ps -o pid=,rss=,pcpu= -p " + ",".join(pids))
        for line in o.splitlines():
            p = line.split()
            if len(p) == 3:
                try:
                    stats[p[0]] = (int(p[1]) / 1024, float(p[2]))
                except ValueError:
                    pass
    mem_total = None
    try:
        _, o, _ = ssh.run("grep MemTotal /proc/meminfo")
        m = re.search(r"(\d+)", o or "")
        if m:
            mem_total = int(m.group(1)) / 1024
    except Exception:
        pass

    rows = []
    for i, u in enumerate(units):
        mem, cpu = stats.get(u.get("MainPID", ""), (None, None))
        rows.append({
            "name": u["Id"], "kind": unit_kind(i, u, tg_hits, go_units),
            "active": u.get("ActiveState", ""), "sub": u.get("SubState", ""),
            "pid": u.get("MainPID", "0"), "mem": mem, "cpu": cpu,
            "mem_total": mem_total, "dir": bot_dir(u),
            "since": u.get("ActiveEnterTimestamp", "") if u.get("ActiveState") == "active" else "",
        })
    for r in dock:
        if r["pid"]:
            r["mem"], r["cpu"] = stats.get(r["pid"], (None, None))
        r.setdefault("dir", "")
        r["mem_total"] = mem_total
    rows += dock
    return sorted(rows, key=lambda r: r["name"])


# ---------- фоновые задачи ----------
class Bridge(QObject):
    done = Signal(object, object, object)


bridge = None


def bg(fn, cb):
    def worker():
        try:
            res, err = fn(), None
        except Exception as e:  # noqa: BLE001
            res, err = None, e
        bridge.done.emit(cb, res, err)
    try:
        threading.Thread(target=worker, daemon=True).start()
    except RuntimeError as e:
        # треды кончились (долгая сессия с кучей висящих) — не вешаем UI,
        # сразу отдаём ошибку в колбэк, он разблокирует кнопки
        cb(None, e)


def load_cfg():
    try:
        return json.loads(CFG.read_text())
    except Exception:
        return {"profiles": {}, "last": None}


# ---------- окно ----------
class Win(QMainWindow):
    COLS = ["", "Сервис", "Тип", "Статус", "PID", "RAM, МБ", "CPU, % (сред.)", "Запущен"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("BOT SPOT")
        self.setWindowIcon(app_icon())
        self.resize(1180, 780)
        self.ssh = SSH()
        self._term_keys = set()  # user@host:port, куда уже поставили ключ терминала
        self.cfg = load_cfg()
        self.rows = []
        self.busy = False
        self.log_name = None
        self.log_raw = ""
        self.shown = []
        self._busy_log = False

        # --- шапка с эмблемкой ---
        logo = QLabel()
        pix = QPixmap(resource_path("icon.png"))
        if not pix.isNull():
            logo.setPixmap(pix.scaled(46, 46, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        title = QLabel("BOT SPOT")
        title.setObjectName("title")
        subtitle = QLabel("панель управления ботами · SSH + systemd")
        subtitle.setObjectName("subtitle")
        tcol = QVBoxLayout()
        tcol.setContentsMargins(0, 0, 0, 0)
        tcol.setSpacing(1)
        tcol.addWidget(title)
        tcol.addWidget(subtitle)
        self.stats = QLabel("нет подключения")
        self.stats.setObjectName("statsPill")
        self.monitor = QLabel("—")
        self.monitor.setObjectName("statsPill")
        self.monitor.setToolTip("CPU · RAM · диск · сеть · load average")
        self._net_prev = None
        self._busy_mon = False
        head = QHBoxLayout()
        head.setSpacing(12)
        head.setContentsMargins(14, 10, 14, 10)
        head.addWidget(logo)
        head.addLayout(tcol)
        head.addStretch()
        head.addWidget(self.stats)

        # --- карточка подключения ---
        self.prof = QComboBox()
        self.prof.addItem("— новое подключение —")
        self.prof.addItems(self.cfg["profiles"].keys())
        self.prof.setMinimumWidth(170)
        self.btn_rename = AnimatedButton()
        self.btn_delete = AnimatedButton()
        for b in (self.btn_rename, self.btn_delete):
            b.setObjectName("btnGhost")
            b.setFixedWidth(40)
            b.setCursor(Qt.PointingHandCursor)
        self.btn_rename.setIcon(load_icon("pencil"))
        self.btn_delete.setIcon(load_icon("trash"))
        self.btn_rename.setToolTip("Переименовать выбранный профиль")
        self.btn_delete.setToolTip("Удалить выбранный профиль")
        prof_bar = QHBoxLayout()
        prof_bar.setContentsMargins(0, 0, 0, 0)
        prof_bar.setSpacing(6)
        prof_bar.addWidget(self.prof, 1)
        prof_bar.addWidget(self.btn_rename)
        prof_bar.addWidget(self.btn_delete)
        prof_wrap = QWidget()
        prof_wrap.setLayout(prof_bar)
        self.host, self.port, self.user = QLineEdit(), QLineEdit("22"), QLineEdit("root")
        self.pw, self.key = QLineEdit(), QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        self.host.setPlaceholderText("хост / IP")
        self.user.setPlaceholderText("пользователь")
        self.pw.setPlaceholderText("пароль")
        self.key.setPlaceholderText("путь к ключу (необязательно)")
        self.host.setMinimumWidth(150)
        self.key.setMinimumWidth(200)
        self.port.setFixedWidth(64)
        self.remember = QCheckBox("запомнить")
        self.remember.setChecked(bool(keyring))
        self.remember.setEnabled(bool(keyring))
        self.btn_conn = AnimatedButton("Подключиться")
        self.btn_conn.setObjectName("btnPrimary")
        self.btn_conn.setCursor(Qt.PointingHandCursor)
        self.btn_conn.setIcon(load_icon("plug", "#FFFFFF"))

        conn_grid = QGridLayout()
        conn_grid.setSpacing(8)
        conn_grid.setContentsMargins(14, 14, 14, 14)
        conn_grid.addWidget(prof_wrap, 0, 0, 1, 2)
        conn_grid.addWidget(self.host, 0, 2)
        conn_grid.addWidget(self.port, 0, 3)
        conn_grid.addWidget(self.user, 0, 4)
        conn_grid.addWidget(self.pw, 1, 0)
        conn_grid.addWidget(self.key, 1, 1, 1, 3)
        conn_grid.addWidget(self.remember, 1, 4)
        conn_grid.addWidget(self.btn_conn, 0, 5, 2, 1)
        conn_grid.setColumnStretch(1, 1)
        conn_grid.setColumnStretch(2, 1)
        conn_card = QFrame()
        conn_card.setObjectName("glassCard")
        conn_card.setLayout(conn_grid)

        # --- панель действий + таблица ---
        self.only_tg = QCheckBox("Только Telegram-боты")
        self.only_tg.setChecked(True)
        self.btn_start = AnimatedButton("Старт")
        self.btn_stop = AnimatedButton("Стоп")
        self.btn_restart = AnimatedButton("Рестарт")
        self.btn_refresh = AnimatedButton("Обновить")
        self.btn_start.setObjectName("btnSuccess")
        self.btn_stop.setObjectName("btnDanger")
        self.btn_restart.setObjectName("btnWarning")
        self.btn_refresh.setObjectName("btnGhost")
        self.btn_start.setIcon(load_icon("play", "#04210F"))
        self.btn_stop.setIcon(load_icon("stop", "#FFFFFF"))
        self.btn_restart.setIcon(load_icon("refresh", "#1A1300"))
        self.btn_refresh.setIcon(load_icon("refresh"))
        for b in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_refresh, self.btn_conn):
            b.setCursor(Qt.PointingHandCursor)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        for w in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_refresh):
            bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(self.only_tg)
        self.bots_bar = QWidget()
        self.bots_bar.setLayout(bar)
        self.bots_hint = QLabel("Подключитесь к серверу карточкой выше — управление ботами появится после подключения.")
        self.bots_hint.setWordWrap(True)
        self.bots_hint.setObjectName("statusLine")

        self.table = RowHoverTable(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.viewport().setAutoFillBackground(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(33)
        h = self.table.horizontalHeader()
        h.setHighlightSections(False)
        h.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        h.setSectionResizeMode(QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)

        tw = GradientPanel()
        tl = QVBoxLayout(tw)
        tl.setContentsMargins(14, 14, 14, 14)
        tl.setSpacing(10)
        tl.addWidget(self.bots_bar)
        tl.addWidget(self.bots_hint)
        tl.addWidget(self.table)

        # --- логи ---
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setFont(QFont(FONT_MONO, 10))
        self.logs.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.logs.setPlaceholderText("Выбери бота сверху — здесь появятся его логи (journalctl)…")
        self._log_hl = LogHighlighter(self.logs.document())
        self.lines = QSpinBox()
        self.lines.setRange(20, 5000)
        self.lines.setValue(200)
        self.live = QCheckBox("Live (2 сек)")
        self.log_title = QLabel("Логи: выбери бота")
        self.log_title.setObjectName("h2")
        self.log_icon = QLabel()
        self.log_icon.setPixmap(load_icon("scroll").pixmap(16, 16))
        lbar = QHBoxLayout()
        lbar.addWidget(self.log_icon)
        lbar.addWidget(self.log_title)
        lbar.addStretch()
        lbar.addWidget(QLabel("строк:"))
        lbar.addWidget(self.lines)
        lbar.addWidget(self.live)
        lbar2 = QHBoxLayout()
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText("поиск…")
        self.log_search.setClearButtonEnabled(True)
        self.log_search.textChanged.connect(lambda _t: self._render_logs())
        self.log_level = QComboBox()
        self.log_level.addItems(["Все строки", "Только ошибки", "Ошибки и предупреждения"])
        self.log_level.currentIndexChanged.connect(lambda _i: self._render_logs())
        self.btn_log_save = AnimatedButton("Сохранить")
        self.btn_log_save.setObjectName("btnGhost")
        self.btn_log_save.setCursor(Qt.PointingHandCursor)
        self.btn_log_save.clicked.connect(self.save_logs)
        self.btn_log_full = QPushButton("Весь журнал")
        self.btn_log_full.setObjectName("btnGhost")
        self.btn_log_full.setCursor(Qt.PointingHandCursor)
        self.btn_log_full.clicked.connect(lambda: self._load_logs_n(10000))
        lbar2.addWidget(self.log_search, 1)
        lbar2.addWidget(self.log_level)
        lbar2.addWidget(self.btn_log_save)
        lbar2.addWidget(self.btn_log_full)

        lw = QFrame()
        lw.setObjectName("logCard")
        ll = QVBoxLayout(lw)
        ll.setContentsMargins(14, 12, 14, 14)
        ll.setSpacing(8)
        ll.addLayout(lbar)
        ll.addLayout(lbar2)
        ll.addWidget(self.logs)

        split = QSplitter(Qt.Vertical)
        split.addWidget(tw)
        split.addWidget(lw)
        split.setSizes([400, 300])

        root = GradientRoot()
        rl = QVBoxLayout(root)
        rl.setContentsMargins(0, 0, 0, 12)
        rl.setSpacing(12)
        # --- переключатель режимов (Боты / Файлы / Бэкапы / Деплой) ---
        self.btn_mode_bots = QPushButton("Боты")
        self.btn_mode_files = QPushButton("Файлы")
        self.btn_mode_backup = QPushButton("Бэкапы")
        self.btn_mode_deploy = QPushButton("Деплой")
        mode_group = QButtonGroup(self)
        mode_group.setExclusive(True)
        seg = QHBoxLayout()
        seg.setSpacing(0)
        seg_names = ("segLeft", "segMid", "segMid2", "segRight")
        seg_btns = (self.btn_mode_bots, self.btn_mode_files,
                    self.btn_mode_backup, self.btn_mode_deploy)
        for i, b in enumerate(seg_btns):
            b.setCheckable(True)
            b.setObjectName(seg_names[i])
            b.setCursor(Qt.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            mode_group.addButton(b, i)
            seg.addWidget(b, 1)
        seg_wide = max(b.sizeHint().width() for b in seg_btns)
        for b in seg_btns:
            b.setMinimumWidth(seg_wide)
        self.btn_mode_bots.setChecked(True)
        seg_wrap = QWidget()
        seg_wrap.setObjectName("segWrap")
        seg_wrap.setStyleSheet(build_seg_qss(derived()))
        seg_wrap.setLayout(seg)
        # скользящий индикатор активной секции — подложка ПОД кнопками (ТЗ 5.2)
        self.seg_wrap = seg_wrap
        self._seg_indicator = QWidget(seg_wrap)
        self._seg_indicator.setObjectName("segIndicator")
        self._seg_indicator.lower()
        seg_wrap.installEventFilter(self)
        head.insertWidget(3, seg_wrap)  # между заголовком и pill со статистикой
        head.insertWidget(4, self.monitor)

        # --- переключатель тем (ТЗ 4): три свотча-превью в правом краю topbar ---
        self.theme_wrap = QWidget()
        tw = QHBoxLayout(self.theme_wrap)
        tw.setContentsMargins(0, 0, 0, 0)
        tw.setSpacing(6)
        self.theme_btns = {}
        for name in THEMES:
            b = QPushButton()
            b.setFixedSize(24, 24)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(THEME_TITLES[name])
            b.clicked.connect(lambda _=False, n=name: self.set_theme(n))
            tw.addWidget(b)
            self.theme_btns[name] = b
        head.insertWidget(5, self.theme_wrap)
        self._style_theme_swatches()
        register_theme_hook(self.apply_theme)

        self.files_tab = FilesTab()
        self.backup_tab = BackupTab()
        self.deploy_tab = DeployTab()
        self.pages = QStackedWidget()
        self.pages.addWidget(split)         # 0 — Боты: ровно тот же split, что и раньше
        self.pages.addWidget(self.files_tab)  # 1 — Файлы
        self.pages.addWidget(self.backup_tab)  # 2 — Бэкапы
        self.pages.addWidget(self.deploy_tab)  # 3 — Деплой
        self._set_server_ui(False)  # без подключения панели действий скрыты (как в «Файлах»)
        self._seg_buttons = (self.btn_mode_bots, self.btn_mode_files,
                               self.btn_mode_backup, self.btn_mode_deploy)
        QTimer.singleShot(0, lambda: self._move_seg_glow(0, animate=False))
        mode_group.idClicked.connect(self._move_seg_glow)
        mode_group.idClicked.connect(self.pages.setCurrentIndex)
        self.pages.currentChanged.connect(self._page_crossfade)
        mode_group.idClicked.connect(lambda i: i == 1 and self.files_tab.activate())
        self.deploy_tab.open_bots_requested.connect(lambda: self.pages.setCurrentIndex(0))

        topbar = QFrame()
        topbar.setObjectName("topbar")
        topbar.setLayout(head)
        mid = QWidget()
        midlay = QVBoxLayout(mid)
        midlay.setContentsMargins(14, 0, 14, 0)
        midlay.setSpacing(12)
        midlay.addWidget(conn_card)
        midlay.addWidget(self.pages, 1)
        rl.addWidget(topbar)
        rl.addWidget(mid, 1)
        self.setCentralWidget(root)

        conn_glow = QGraphicsDropShadowEffect(self)
        conn_glow.setBlurRadius(18)
        conn_glow.setOffset(0)
        conn_glow.setColor(QColor(alpha(derived()["ac"], 64)))
        self.btn_conn.setGraphicsEffect(conn_glow)
        self.conn_glow = conn_glow

        self.btn_conn.clicked.connect(self.connect_ssh)
        self.pw.returnPressed.connect(self.connect_ssh)
        self.prof.currentTextChanged.connect(self.pick_profile)
        self.btn_rename.clicked.connect(self.rename_profile)
        self.btn_delete.clicked.connect(self.delete_profile)
        self.btn_refresh.clicked.connect(self.refresh)
        self.only_tg.toggled.connect(self.render)
        self.btn_start.clicked.connect(lambda: self.act("start"))
        self.btn_stop.clicked.connect(lambda: self.act("stop"))
        self.btn_restart.clicked.connect(lambda: self.act("restart"))
        self.table.itemSelectionChanged.connect(self.load_logs)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._bot_menu)
        self.lines.editingFinished.connect(self.load_logs)

        self.t_refresh = QTimer(self, interval=5000, timeout=self.refresh)
        self.t_logs = QTimer(self, interval=2000, timeout=self.load_logs)
        self.t_monitor = QTimer(self, interval=10000, timeout=self.monitor_tick)
        self.live.toggled.connect(lambda on: self.t_logs.start() if on else self.t_logs.stop())

        last = self.cfg.get("last")
        if last in self.cfg["profiles"]:
            self.prof.setCurrentText(last)  # только заполняет поля; подключаемся по кнопке
        self._update_prof_buttons()

    # --- подключение ---
    def _set_server_ui(self, on: bool) -> None:
        """Панели действий видны только при подключении (п. «как в Файлах»)."""
        self.bots_bar.setVisible(on)
        self.bots_hint.setVisible(not on)
        self.backup_tab.set_actions_visible(on)
        self.deploy_tab._set_deploy_visible(on)

    def _handle_server_lost(self) -> None:
        self.t_refresh.stop()
        self.t_monitor.stop()
        self.t_logs.stop()
        self._busy_log = False
        c, self.ssh.client = self.ssh.client, None
        try:
            if c is not None:
                c.close()
        except Exception:
            pass
        self._set_server_ui(False)
        self.statusBar().showMessage("Связь с сервером потеряна — переподключитесь карточкой выше")

    def _move_seg_glow(self, i: int, animate: bool = True) -> None:
        """Индикатор активной секции скользит подложкой под кнопками (ТЗ 5.2)."""
        if not (0 <= i < len(self._seg_buttons)):
            return
        b = self._seg_buttons[i]
        target = QRect(b.pos(), b.size())
        if animate and self._seg_indicator.width() > 1:
            animate_geometry(self._seg_indicator, target, 170)
        else:
            self._seg_indicator.setGeometry(target)

    def _page_crossfade(self, i: int) -> None:
        """Короткий crossfade при переключении разделов (ТЗ 5.3)."""
        w = self.pages.widget(i)
        if w is not None:
            fade_widget(w, 0.0, 1.0, 120)

    def eventFilter(self, obj, ev):
        if obj is getattr(self, "seg_wrap", None) and ev.type() == QEvent.Resize:
            # при ресайзе окна индикатор просто прилипает к активной кнопке
            for b in self._seg_buttons:
                if b.isChecked():
                    self._seg_indicator.setGeometry(QRect(b.pos(), b.size()))
                    break
        return super().eventFilter(obj, ev)

    # --- темы (ТЗ 4-5) ---
    def set_theme(self, name: str) -> None:
        if name == theme_name():
            return
        set_theme(name)  # вызовет apply_theme через хук
        self.cfg["theme"] = name
        self._save_cfg()

    def apply_theme(self) -> None:
        d = derived()
        QApplication.instance().setStyleSheet(build_qss(d))
        self.seg_wrap.setStyleSheet(build_seg_qss(d))
        self.conn_glow.setColor(QColor(alpha(d["ac"], 64)))
        self._log_hl.retheme()
        _ICON_CACHE.clear()  # иконки нейтрального цвета пересоздадутся под новую тему
        self.files_tab.apply_theme()
        self.backup_tab.apply_theme()
        self.deploy_tab.apply_theme()
        self._retheme_icons()
        self._style_theme_swatches()
        self.render()  # перекрасить точки/чипы таблицы ботов
        self.centralWidget().update()  # GradientRoot рисует фон новыми токенами
        fade_widget(self.centralWidget(), 0.4, 1.0, 180)

    def _style_theme_swatches(self) -> None:
        d = derived()
        cur = theme_name()
        for name, b in self.theme_btns.items():
            t = THEMES[name]
            ring = d["ac"] if name == cur else d["ln_solid"]
            b.setChecked(name == cur)
            b.setStyleSheet(f"""
                QPushButton {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                        stop:0 {t['sf']}, stop:1 {t['ac']});
                    border: 2px solid {ring};
                    border-radius: 11px;
                }}
                QPushButton:hover {{ border: 2px solid {d['ac']}; }}
            """)

    def _retheme_icons(self) -> None:
        # нейтральные иконки берут цвет tokens()["icon"]; на цветных кнопках — не трогаем
        self.btn_rename.setIcon(load_icon("pencil"))
        self.btn_delete.setIcon(load_icon("trash"))
        self.btn_refresh.setIcon(load_icon("refresh"))
        self.log_icon.setPixmap(load_icon("scroll").pixmap(16, 16))

    def _update_prof_buttons(self):
        is_real = self.prof.currentText() in self.cfg["profiles"]
        self.btn_rename.setEnabled(is_real)
        self.btn_delete.setEnabled(is_real)

    def _save_cfg(self):
        CFG.write_text(json.dumps(self.cfg, indent=2))

    def _refresh_prof_list(self, select=None):
        self.prof.blockSignals(True)
        self.prof.clear()
        self.prof.addItem("— новое подключение —")
        self.prof.addItems(sorted(self.cfg["profiles"].keys()))
        if select in self.cfg["profiles"]:
            self.prof.setCurrentText(select)
        self.prof.blockSignals(False)
        self._update_prof_buttons()

    def pick_profile(self, name):
        self._update_prof_buttons()
        p = self.cfg["profiles"].get(name)
        if not p:
            return
        self.host.setText(p["host"])
        self.port.setText(str(p["port"]))
        self.user.setText(p["user"])
        self.key.setText(p.get("key", ""))
        pw = ""
        if keyring:
            try:
                pw = keyring.get_password(KR_SERVICE, name) or ""
            except Exception:
                pass
        self.pw.setText(pw)

    def rename_profile(self):
        old = self.prof.currentText()
        if old not in self.cfg["profiles"]:
            return self.statusBar().showMessage("Выбери сохранённый профиль для переименования")
        new, ok = QInputDialog.getText(self, "Переименовать профиль", "Новое имя:", text=old)
        if not ok:
            return
        new = new.strip()
        if not new or new == old:
            return
        if new == "— новое подключение —" or new in self.cfg["profiles"]:
            QMessageBox.warning(self, "Переименование", "Такое имя уже занято.")
            return
        self.cfg["profiles"][new] = self.cfg["profiles"].pop(old)
        if self.cfg.get("last") == old:
            self.cfg["last"] = new
        if keyring:
            try:
                pw = keyring.get_password(KR_SERVICE, old) or ""
                if pw:
                    keyring.set_password(KR_SERVICE, new, pw)
                try:
                    keyring.delete_password(KR_SERVICE, old)
                except Exception:
                    pass
            except Exception:
                pass
        self._save_cfg()
        self._refresh_prof_list(select=new)
        self.statusBar().showMessage(f"Профиль переименован: {old} → {new}")

    def delete_profile(self):
        old = self.prof.currentText()
        if old not in self.cfg["profiles"]:
            return self.statusBar().showMessage("Выбери сохранённый профиль для удаления")
        if QMessageBox.question(self, "Удалить профиль", f'Удалить профиль "{old}"?') != QMessageBox.Yes:
            return
        self.cfg["profiles"].pop(old, None)
        if self.cfg.get("last") == old:
            self.cfg["last"] = None
        if keyring:
            try:
                keyring.delete_password(KR_SERVICE, old)
            except Exception:
                pass
        self._save_cfg()
        self._refresh_prof_list()
        self.statusBar().showMessage(f"Профиль удалён: {old}")

    def connect_ssh(self):
        host, user, key, pw = self.host.text().strip(), self.user.text().strip(), self.key.text().strip(), self.pw.text()
        try:
            port = int(self.port.text() or 22)
        except ValueError:
            return self.statusBar().showMessage("Порт должен быть числом")
        if not host or not user:
            return self.statusBar().showMessage("Укажи хост и пользователя")
        self.btn_conn.setEnabled(False)
        self.statusBar().showMessage(f"Подключаюсь к {host}…")

        # выбран сохранённый профиль — подключаемся под его именем, нового не создаём
        selected = self.prof.currentText()
        reuse = selected if selected in self.cfg["profiles"] else None
        self._conn_seq = getattr(self, "_conn_seq", 0) + 1
        seq = self._conn_seq

        def job():
            self.ssh.connect(host, port, user, pw, key)
            return reuse or f"{user}@{host}:{port}"

        def done(name, err):
            self.btn_conn.setEnabled(True)
            if seq != self._conn_seq:
                return  # устаревший ответ от зависшего коннекта — игнорим
            if err:
                return self.statusBar().showMessage(f"Ошибка подключения: {err}")
            self.cfg["profiles"][name] = {"host": host, "port": port, "user": user, "key": key}
            self.cfg["last"] = name
            self._save_cfg()
            self._refresh_prof_list(select=name)
            if keyring and self.remember.isChecked() and pw:
                try:
                    keyring.set_password(KR_SERVICE, name, pw)
                except Exception:
                    pass
            self.statusBar().showMessage(f"Подключено: {name}")
            self._set_server_ui(True)
            self.t_refresh.start()
            self.t_monitor.start()
            self.refresh()
            self.monitor_tick()
            self.files_tab.on_connected(dict(host=host, port=port, user=user, password=pw, key=key, profile=name))
            self.backup_tab.on_connected(dict(host=host, port=port, user=user, password=pw, key=key, profile=name))
            self.deploy_tab.on_connected(dict(host=host, port=port, user=user, password=pw, key=key, profile=name))

        bg(job, done)
        QTimer.singleShot(CONNECT_WATCHDOG_MS,
                          lambda: self._connect_watchdog(seq))

    def _connect_watchdog(self, seq):
        """Коннект висит дольше CONNECT_WATCHDOG_MS — разблокировать кнопку,
        чтобы клики не игнорились. Поздний ответ станет stale и будет проигнорирован."""
        if seq == getattr(self, "_conn_seq", 0) and not self.btn_conn.isEnabled():
            self.btn_conn.setEnabled(True)
            self.statusBar().showMessage("Превышено время ожидания — проверь хост/сеть и жми ещё раз")

    # --- список ботов ---
    def monitor_tick(self):
        if not self.ssh.client or self._busy_mon:
            return
        self._busy_mon = True
        cmd = ("cat /proc/loadavg; echo ---; "
               "free -m | awk '/^Mem:/{print $2, $3}'; echo ---; "
               "df -m / | awk 'NR==2{print $2, $3}'; echo ---; "
               "awk -F'[: ]+' '!/lo/&&/:/{r+=$3;t+=$11}END{print r+0, t+0}' /proc/net/dev; echo ---; "
               "A=$(awk '/^cpu /{print $2+$3+$4+$6+$7+$8, $5}' /proc/stat); sleep 1; "
               "B=$(awk '/^cpu /{print $2+$3+$4+$6+$7+$8, $5}' /proc/stat); echo \"$A $B\"")

        def done(res, err):
            self._busy_mon = False
            if err:
                return
            try:
                self.monitor.setText(self._fmt_monitor(res[1]))
            except Exception:
                pass

        bg(lambda: self.ssh.run(cmd), done)

    @staticmethod
    def _gb(mb: float) -> str:
        return f"{mb / 1024:.1f}G" if mb >= 1024 else f"{mb:.0f}M"

    def _fmt_monitor(self, out: str) -> str:
        parts = [s.strip() for s in (out or "").split("---")]
        while len(parts) < 5:
            parts.append("")
        load = (parts[0].split() or ["—"])[0]
        mem = parts[1].split()
        disk = parts[2].split()
        net = parts[3].split()
        cpu = parts[4].split()
        cpu_s = "—"
        try:
            a_t, a_i, b_t, b_i = float(cpu[0]), float(cpu[1]), float(cpu[2]), float(cpu[3])
            if b_t > a_t:
                pct = (1 - (b_i - a_i) / (b_t - a_t)) * 100
                cpu_s = f"{max(0.0, min(100.0, pct)):.0f}%"
        except (ValueError, IndexError):
            pass
        try:
            mem_s = f"{self._gb(float(mem[1]))}/{self._gb(float(mem[0]))}"
        except (ValueError, IndexError):
            mem_s = "—"
        try:
            disk_s = f"{float(disk[1]) / float(disk[0]) * 100:.0f}%" if float(disk[0]) else "—"
        except (ValueError, IndexError, ZeroDivisionError):
            disk_s = "—"
        net_s = "—"
        try:
            rx, tx = float(net[0]), float(net[1])
            now = time.monotonic()
            if self._net_prev:
                (prx, ptx, pt) = self._net_prev
                dt = max(now - pt, 0.001)
                if rx >= prx and tx >= ptx:
                    net_s = (f"↓{self._gb((rx - prx) / dt / 1024)}/с "
                             f"↑{self._gb((tx - ptx) / dt / 1024)}/с")
            self._net_prev = (rx, tx, now)
        except (ValueError, IndexError):
            pass
        return f"CPU {cpu_s} · RAM {mem_s} · DISK {disk_s} · {net_s} · {load}"

    def refresh(self):
        if not self.ssh.client or self.busy:
            return
        self.busy = True

        def done(rows, err):
            self.busy = False
            if err:
                self._handle_server_lost()
                self.statusBar().showMessage(f"Связь потеряна: {err}")
                return
            self.rows = rows
            self.render()

        bg(lambda: scan(self.ssh), done)

    def render(self):
        sel = self.current()
        rows = [r for r in self.rows if not self.only_tg.isChecked() or r["kind"] == "Telegram"]
        docker_icon = load_icon("docker")
        d = derived()
        kind_fg, kind_bg = QColor(d["mu"]), QColor(alpha(d["tx"], 16))
        status_tx = QColor(d["tx"])
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            if r["active"] == "active":
                dot = QColor(d["ok"])
            elif r["active"] == "failed":
                dot = QColor(d["er"])
            else:
                dot = QColor(d["mu"])
            is_dock = r["name"].startswith("🐳 ")
            disp_name = strip_docker_prefix(r["name"])
            if r["mem"] is None:
                mem_s = ""
            elif r.get("mem_total"):
                used = f'{r["mem"]:.1f}'.rstrip("0").rstrip(".")
                mem_s = f"{used}/{r['mem_total']:.0f}"
            else:
                mem_s = f'{r["mem"]:.1f}'
            vals = ["●", disp_name, r["kind"], f'{r["active"]} ({r["sub"]})', r["pid"] if r["pid"] != "0" else "",
                    mem_s, f'{r["cpu"]:.1f}' if r["cpu"] is not None else "", r["since"]]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                f = QFont(FONT_UI, 10)
                if j == 1:
                    f.setWeight(QFont.DemiBold)
                it.setFont(f)
                if j == 0:
                    it.setForeground(dot)
                    ff = QFont(FONT_UI, 13)
                    it.setFont(ff)
                    it.setTextAlignment(Qt.AlignCenter)
                if j == 1 and is_dock:
                    it.setIcon(docker_icon)
                if j == 2:
                    it.setForeground(kind_fg)
                    it.setBackground(kind_bg)
                    it.setTextAlignment(Qt.AlignCenter)
                if j == 3:
                    it.setForeground(status_tx)
                self.table.setItem(i, j, it)
            if r["name"] == sel:
                self.table.selectRow(i)
        self.table.blockSignals(False)
        self.shown = rows
        # лёгкий fade-in при изменении содержимого (не на каждый тик — ТЗ 5.3/5.5)
        fp = tuple((r["name"], r["active"], r["sub"], r["pid"], r["cpu"], r["mem"]) for r in rows)
        if getattr(self, "_render_fp", None) is not None and fp != self._render_fp:
            fade_widget(self.table, 0.55, 1.0, 150)
        self._render_fp = fp
        n_run = sum(1 for r in rows if r["active"] == "active")
        self.stats.setText(f"{len(rows)} ботов · {n_run} запущено")
        self.statusBar().showMessage(f"Ботов: {len(rows)}, запущено: {n_run}")

    def current(self):
        r = self.table.currentRow()
        if r < 0 or r >= len(getattr(self, "shown", [])):
            return None
        return self.shown[r]["name"]

    def current_row(self):
        r = self.table.currentRow()
        if r < 0 or r >= len(getattr(self, "shown", [])):
            return None
        return self.shown[r]

    def _bot_menu(self, pos) -> None:
        idx = self.table.indexAt(pos)
        if idx.row() >= 0:
            self.table.selectRow(idx.row())
        row = self.current_row()
        if row is None:
            return
        m = self._build_bot_menu(row)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _build_bot_menu(self, row: dict):
        m = QMenu(self)
        a_files = m.addAction("Файлы бота")
        a_files.setEnabled(bool(row.get("dir")))
        a_files.triggered.connect(lambda: self.open_bot_files(row))
        a_env = m.addAction("Переменные (.env)")
        a_env.setEnabled(bool(row.get("dir")))
        a_env.triggered.connect(lambda: self.open_bot_env(row))
        a_term = m.addAction("Открыть терминал")
        a_term.triggered.connect(lambda: self.open_bot_terminal(row))
        m.addSeparator()
        a_pull = m.addAction("Git pull + рестарт")
        a_pull.setEnabled(bool(row.get("dir")))
        a_pull.triggered.connect(lambda: self.git_pull_bot(row))
        m.addSeparator()
        a_start = m.addAction("Старт")
        a_start.triggered.connect(lambda: self.act("start"))
        a_stop = m.addAction("Стоп")
        a_stop.triggered.connect(lambda: self.act("stop"))
        a_restart = m.addAction("Рестарт")
        a_restart.triggered.connect(lambda: self.act("restart"))
        return m

    def open_bot_files(self, row: dict) -> None:
        d = (row or {}).get("dir", "")
        if not d:
            return self.statusBar().showMessage("Папка бота неизвестна")
        self.pages.setCurrentIndex(1)
        try:
            self.btn_mode_files.setChecked(True)
        except Exception:
            pass
        self.files_tab.open_remote_dir(d)
        self.statusBar().showMessage(f"Файлы: {d}")

    def open_bot_env(self, row: dict) -> None:
        d = (row or {}).get("dir", "")
        name = strip_docker_prefix((row or {}).get("name", ""))
        if not d or not self.ssh.client:
            return self.statusBar().showMessage("Нет папки бота или подключения")
        remote = d.rstrip("/") + "/.env"
        self.statusBar().showMessage(f"Читаю {remote}…")

        def done(res, err):
            if err:
                return self.statusBar().showMessage(f"Ошибка: {err}")
            code, o, e = res
            if code != 0:
                return self.statusBar().showMessage(f"Нет .env: {e.strip() or remote}")
            dlg = EnvDialog(self, name, o)
            if dlg.exec() != EnvDialog.Accepted or not dlg.has_changes():
                return
            self._save_bot_env(row, remote, dlg.result_text())

        bg(lambda: self.ssh.run(f"cat {shlex.quote(remote)}", sudo=True), done)

    def _save_bot_env(self, row: dict, remote: str, text: str) -> None:
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        cmd = f"printf %s {b64} | base64 -d > {shlex.quote(remote)} && chmod 600 {shlex.quote(remote)}"
        self.statusBar().showMessage("Сохраняю .env…")

        def done(res, err):
            if err:
                return self.statusBar().showMessage(f"Ошибка: {err}")
            code, _, e = res
            if code != 0:
                return self.statusBar().showMessage(f"Не сохранилось: {e.strip()}")
            self.statusBar().showMessage(".env сохранён")
            if QMessageBox.question(self, "Переменные",
                                    "Перезапустить бота, чтобы применить?") == QMessageBox.Yes:
                # выбираем ту же строку и жмём рестарт
                for r in getattr(self, "shown", []):
                    if r["name"] == row["name"]:
                        i = self.shown.index(r)
                        self.table.selectRow(i)
                        break
                self.act("restart")

        bg(lambda: self.ssh.run(cmd, sudo=True), done)

    def git_pull_bot(self, row: dict) -> None:
        """git pull в папке бота на сервере, при успехе — обновить файлы и рестарт."""
        d = (row or {}).get("dir", "")
        name = strip_docker_prefix((row or {}).get("name", ""))
        if not d or not self.ssh.client:
            return self.statusBar().showMessage("Нет папки бота или подключения")
        dq = shlex.quote(d)
        self.statusBar().showMessage(f"git pull: {name}…")

        def job():
            code, o, e = self.ssh.run(f"git -C {dq} rev-parse --is-inside-work-tree", sudo=True)
            if code != 0:
                raise RuntimeError("в папке бота нет git-репозитория")
            code, o, e = self.ssh.run(f"git -C {dq} pull 2>&1", sudo=True)
            if code != 0 and "dubious ownership" in (o or "").lower():
                self.ssh.run(f"git config --global --add safe.directory {dq}", sudo=True)
                code, o, e = self.ssh.run(f"git -C {dq} pull 2>&1", sudo=True)
            return code, (o or "").strip()

        def done(res, err):
            if err:
                self.statusBar().showMessage(f"git pull {name}: ошибка")
                QMessageBox.warning(self, f"Git pull — {name}", str(err))
                return
            code, out = res
            if code != 0:
                self.statusBar().showMessage(f"git pull {name}: ошибка")
                QMessageBox.warning(self, f"Git pull — {name}", out or f"ошибка, код {code}")
                return
            self.statusBar().showMessage(f"git pull {name}: готово, перезапускаю…")
            self._refresh_bot_files(d)
            kind, target = self._target(row.get("name", ""))

            def restarted(r2, e2):
                if e2:
                    return self.statusBar().showMessage(f"Рестарт не удался: {e2}")
                c2, _, err2 = r2
                msg = "ок" if c2 == 0 else (err2 or "").strip() or "ошибка"
                self.statusBar().showMessage(f"git pull {name}: готово · рестарт: {msg}")
                self.refresh()
                self.load_logs()

            if kind == "docker":
                bg(lambda: self.docker_exec("restart", target), restarted)
            else:
                bg(lambda: self.ssh.run(f"systemctl restart {shlex.quote(target)}", sudo=True), restarted)

        bg(job, done)

    def _refresh_bot_files(self, d: str) -> None:
        """Если вкладка «Файлы» открыта в папке бота — перечитать её."""
        try:
            ft = self.files_tab
            pane = getattr(ft, "pane_remote", None)
            cur = (getattr(pane, "path", "") or "")
            if cur == d or cur.startswith(d.rstrip("/") + "/"):
                ft.open_remote_dir(cur)
        except Exception:
            pass

    def open_bot_terminal(self, row: dict) -> None:
        """Терминал без пароля: при первом вызове ставим на сервер локальный ключ и ходим с -i."""
        host, user = self.host.text().strip(), self.user.text().strip()
        try:
            port = str(int(self.port.text() or 22))
        except ValueError:
            port = "22"
        if not host or not user:
            return self.statusBar().showMessage("Укажи хост и пользователя")
        d = (row or {}).get("dir", "")
        remote = f"cd {shlex.quote(d)} && exec bash -l" if d else "exec bash -l"
        target = f"{user}@{host}:{port}"

        def clean_env():
            pe = QProcessEnvironment()
            for k, v in sanitized_env().items():
                pe.insert(k, v)
            return pe

        def launch(key_path):
            args = ["ssh", "-t"]
            if key_path:
                args += ["-i", key_path, "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=no"]
            args += ["-p", port, f"{user}@{host}", remote]
            proc = QProcess()
            proc.setProcessEnvironment(clean_env())
            proc.setProgram("wt.exe")
            proc.setArguments(args)
            if proc.startDetached():
                return self.statusBar().showMessage(f"Терминал: {user}@{host}")
            fb = QProcess()
            fb.setProcessEnvironment(clean_env())
            fb.setProgram("cmd")
            fb.setArguments(["/c", "start", ""] + args)
            if fb.startDetached():
                return self.statusBar().showMessage(f"Терминал: {user}@{host}")
            self.statusBar().showMessage("Не запустился ни wt.exe, ни ssh")

        if not self.ssh.client:
            return launch(None)
        if target in self._term_keys:
            return launch(str(BOOT_KEY))

        self.statusBar().showMessage("Готовлю беспарольный доступ для терминала…")

        def job():
            key_path, pub = boot_key_pair()
            # самоустановка: вычищаем любые строки с нашим маркером (в т.ч. устаревшие
            # от прежних ключей) и дописываем ровно текущий публичный ключ
            cmd = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys"
                   f" && {{ grep -vF {shlex.quote(BOOT_COMMENT)} ~/.ssh/authorized_keys > ~/.ssh/authorized_keys.tmp || true; }}"
                   " && mv ~/.ssh/authorized_keys.tmp ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
                   f" && echo {shlex.quote(pub)} >> ~/.ssh/authorized_keys")
            code, _, e = self.ssh.run(cmd)
            if code != 0:
                raise RuntimeError((e or "").strip() or f"код {code}")
            # проверка ровно тем способом, которым пойдёт ssh.exe: вход по ключу, без пароля/агента
            probe = paramiko.SSHClient()
            probe.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                probe.connect(host, port=int(port), username=user, key_filename=key_path,
                              allow_agent=False, look_for_keys=False, timeout=10)
            except Exception as ke:
                raise RuntimeError(f"сервер не принял ключ: {ke}")
            finally:
                try:
                    probe.close()
                except Exception:
                    pass
            return key_path

        def done(key_path, err):
            if err:
                self.statusBar().showMessage(f"Ключ терминала не поставился ({err}) — ssh спросит пароль")
                return launch(None)
            self._term_keys.add(target)
            launch(key_path)

        bg(job, done)

    # --- действия ---
    @staticmethod
    def _target(name):
        """('docker', container) для строк 🐳, иначе ('systemd', unit)."""
        if name.startswith("🐳 "):
            return ("docker", name[2:].strip())
        return ("systemd", name)

    def docker_exec(self, args, container):
        rc, o, e = self.ssh.run(f"docker {args} {shlex.quote(container)}")
        if rc != 0 and "permission denied" in (e or "").lower():
            return self.ssh.run(f"docker {args} {shlex.quote(container)}", sudo=True)
        return rc, o, e

    def act(self, verb):
        name = self.current()
        if not name or not self.ssh.client:
            return
        self.statusBar().showMessage(f"{verb} {name}…")

        def done(res, err):
            if err:
                return self.statusBar().showMessage(f"Ошибка: {err}")
            code, _, e = res
            self.statusBar().showMessage(f"{verb} {name}: {'ок' if code == 0 else e.strip() or 'ошибка'}")
            self.refresh()
            self.load_logs()

        kind, target = self._target(name)
        if kind == "docker":
            bg(lambda: self.docker_exec(verb, target), done)
        else:
            bg(lambda: self.ssh.run(f"systemctl {verb} {shlex.quote(target)}", sudo=True), done)

    # --- логи ---
    ERR_RE = re.compile(r"(?i)(error|fail|exception|traceback|critical|refused|denied|panic)")
    WARN_RE = re.compile(r"(?i)(warn|error|fail|exception|traceback|critical|refused|denied|panic)")

    def load_logs(self):
        self._load_logs_n(self.lines.value())

    def _load_logs_n(self, n: int):
        name = self.current()
        if not name or not self.ssh.client or self._busy_log:
            return  # прошлый запрос ещё висит — не плодим треды
        self.log_name = name
        self.log_title.setText(f"Логи: {strip_docker_prefix(name)}")
        self._busy_log = True

        def done(res, err):
            self._busy_log = False
            if err or name != self.log_name:
                return
            self.log_raw = res[1] or res[2]
            self._render_logs()

        kind, target = self._target(name)
        if kind == "docker":
            bg(lambda: self.docker_exec(f"logs --tail {n}", target), done)
        else:
            bg(lambda: self.ssh.run(f"journalctl -u {shlex.quote(target)} -n {n} --no-pager -o short-iso", sudo=True), done)

    def _render_logs(self) -> None:
        text = getattr(self, "log_raw", "")
        lvl = self.log_level.currentIndex() if hasattr(self, "log_level") else 0
        q = self.log_search.text().strip().lower() if hasattr(self, "log_search") else ""
        if lvl or q:
            out = []
            for line in text.splitlines():
                if lvl == 1 and not self.ERR_RE.search(line):
                    continue
                if lvl == 2 and not self.WARN_RE.search(line):
                    continue
                if q and q not in line.lower():
                    continue
                out.append(line)
            text = "\n".join(out)
        cur = self.logs.textCursor()
        # selectedText() отдаёт \u2029 вместо \n — нормализуем для поиска
        sel_text = cur.selectedText().replace("\u2029", "\n") if cur.hasSelection() else ""
        old_start, old_end = cur.selectionStart(), cur.selectionEnd()
        if text == self.logs.toPlainText():
            return  # ничего не изменилось — не сносим выделение и не дёргаем скролл
        bar = self.logs.verticalScrollBar()
        val = bar.value()
        at_bottom = val >= bar.maximum() - 4
        self.logs.setPlainText(text)
        if sel_text:
            # было выделение: ищем тот же фрагмент в новом тексте (строки
            # дописываются в конец, ищем вперёд от старой позиции), скролл держим
            idx = text.find(sel_text, max(0, old_start - len(sel_text)))
            if idx < 0:
                idx = text.find(sel_text)
            nc = self.logs.textCursor()
            if idx >= 0:
                nc.setPosition(idx)
                nc.setPosition(idx + len(sel_text), QTextCursor.KeepAnchor)
            else:
                n = len(text)
                nc.setPosition(min(old_start, n))
                nc.setPosition(min(old_end, n), QTextCursor.KeepAnchor)
            self.logs.setTextCursor(nc)
            bar.setValue(min(val, bar.maximum()))
        elif at_bottom or not self.live.isChecked():
            bar.setValue(bar.maximum())
        else:
            # пользователь листал вверх — не дёргаем его в начало/конец
            bar.setValue(min(val, bar.maximum()))
        # новая порция логов — короткая подсветка последних WARNING/ERROR (ТЗ 5.3)
        if text != getattr(self, "_logs_fp", ""):
            self._logs_fp = text
            flash_last_alert(self.logs, tokens()["wn"])

    def save_logs(self) -> None:
        name = self.current() or "logs"
        safe = re.sub(r'[\\/:*?"<>|]', "_", strip_docker_prefix(name))
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить логи", f"{safe}.log",
                                              "Log files (*.log);;All files (*)")
        if not path:
            return
        try:
            Path(path).write_text(self.logs.toPlainText(), encoding="utf-8")
            self.statusBar().showMessage(f"Логи сохранены: {path}")
        except OSError as e:
            self.statusBar().showMessage(f"Не сохранилось: {e}")


def main():
    global bridge
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(app_icon())
    base_font = QFont(FONT_UI, 10)
    base_font.setHintingPreference(QFont.PreferFullHinting)
    app.setFont(base_font)
    # сохранённая тема применяется до показа окна — без вспышки дефолтной (ТЗ 4)
    saved_theme = load_cfg().get("theme")
    if saved_theme in THEMES:
        set_theme(saved_theme, notify=False)
    app.setStyleSheet(build_qss(derived()))
    bridge = Bridge()
    bridge.done.connect(lambda cb, res, err: cb(res, err))
    w = Win()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
