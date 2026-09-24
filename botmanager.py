#!/usr/bin/env python3
"""BOT SPOT: FileZilla для systemd-ботов. Подключился по SSH, видишь ботов, жмёшь кнопки."""
import base64
import json
import re
import shlex
import sys
import threading
import time
from pathlib import Path, PurePosixPath

import paramiko
from PySide6.QtCore import QByteArray, QObject, QProcess, QProcessEnvironment, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QIcon, QLinearGradient, QPainter,
                           QPainterPath, QPixmap, QRadialGradient, QSyntaxHighlighter,
                           QTextCharFormat)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QFrame, QGraphicsDropShadowEffect,
    QGridLayout, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QMenu, QFileDialog, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from files_tab import FilesTab, SEG_QSS, sanitized_env
from backup_tab import BackupTab
from deploy_tab import DeployTab
from env_editor import EnvDialog

try:
    import keyring  # пароль хранится в системном хранилище (Keychain / Credential Manager / Secret Service)
except Exception:
    keyring = None

CFG = Path.home() / ".botmanager.json"
KR_SERVICE = "botmanager"
TG_RE = "aiogram|telebot|telegram|pyrogram|telethon|tgbotapi|telego|telegraf|grammy|node-telegram|BOT_TOKEN|TG_TOKEN|TELEGRAM_BOT|api\\.telegram\\.org"
# системные префиксы — не папки проектов, пропускаем при угадывании каталога бота
SKIP_BIN_PREFIX = ("/usr/bin", "/usr/sbin", "/usr/lib", "/bin", "/sbin", "/lib", "/etc/systemd",
                   "/run", "/proc", "/sys", "/dev", "/var/lib/docker")
NEW_PROF = "— новое подключение —"
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


def load_icon(name: str, color: str = "#E0E0E0", size: int = 16) -> QIcon:
    """Монохромная SVG-иконка из icons/ с подстановкой currentColor. Только отображение."""
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
        p = QPainter(self)
        p.setPen(Qt.NoPen)
        r = self.rect()
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0.0, QColor("#091A2E"))
        g.setColorAt(0.55, QColor("#0B2138"))
        g.setColorAt(1.0, QColor("#072642"))
        p.fillRect(r, g)
        rad = max(r.width(), r.height()) * 0.45
        rg = QRadialGradient(r.width() * 0.65, r.height() * 0.35, rad)
        rg.setColorAt(0.0, QColor(26, 104, 163, 51))
        rg.setColorAt(1.0, QColor(26, 104, 163, 0))
        p.fillRect(r, rg)
        p.end()


class GradientPanel(QFrame):
    """Карточка области таблицы ботов: свой градиент + свечение справа + рамка."""

    R = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("gradCard")

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        path = QPainterPath()
        path.addRoundedRect(r.adjusted(0, 0, -1, -1), self.R, self.R)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0.0, QColor("#091E34"))
        g.setColorAt(0.5, QColor("#0C2945"))
        g.setColorAt(1.0, QColor("#0A2038"))
        p.fillPath(path, g)
        p.save()
        p.setClipPath(path)
        rad = max(r.width(), r.height()) * 0.55
        rg = QRadialGradient(r.width() * 0.70, r.height() * 0.40, rad)
        rg.setColorAt(0.0, QColor(31, 105, 163, 46))
        rg.setColorAt(1.0, QColor(31, 105, 163, 0))
        p.fillRect(r, rg)
        p.restore()
        p.setPen(QColor("#245679"))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.end()


class LogHighlighter(QSyntaxHighlighter):
    """Цвет только на токене уровня (п.4 ТЗ), остальная строка нейтральная."""

    LEVELS = {"INFO": "#6FB6E8", "SUCCESS": "#35D58A", "WARNING": "#FFCA45",
              "ERROR": "#FF5965", "DEBUG": "#8A9BAC"}

    def __init__(self, doc):
        super().__init__(doc)
        self._formats = {}
        for level, color in self.LEVELS.items():
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            fmt.setFontWeight(QFont.Bold)
            self._formats[level] = fmt
        self._rx = re.compile(r"\blevel\s*=\s*(INFO|SUCCESS|WARNING|ERROR|DEBUG)\b"
                              r"|\b(INFO|SUCCESS|WARNING|ERROR|DEBUG|Traceback)\b")

    def highlightBlock(self, text: str) -> None:
        for m in self._rx.finditer(text):
            level = m.group(1) or m.group(2)
            key = "ERROR" if level == "Traceback" else level
            self.setFormat(m.start(), m.end() - m.start(), self._formats[key])


THEME_QSS = """
* { outline: none; }
QMainWindow { background: #081726; }
QFrame#topbar { background: #081726; border: none; border-bottom: 1px solid #1A3A59; border-radius: 0; }
QLabel { color: #F1F6FC; }
QLabel#title { font-size: 19px; font-weight: 800; letter-spacing: 1px; color: #F1F6FC; }
QLabel#subtitle { color: #839AAF; font-size: 11px; }
QLabel#h2 { font-size: 13px; font-weight: 700; color: #F1F6FC; }
QLabel#statsPill {
    background: #102B45; color: #D6E2ED;
    border: 1px solid #234A6B; border-radius: 10px; padding: 6px 12px; font-weight: 600;
}
QFrame#card {
    background: #0D2943; border: 1px solid #245679; border-radius: 12px;
}
QFrame#glassCard {
    background: rgba(20, 65, 100, 140); border: 1px solid #26587E; border-radius: 12px;
}
QFrame#logCard {
    background: #071A2D; border: 1px solid #214D6D; border-radius: 12px;
}
QLineEdit, QComboBox, QSpinBox {
    background: #0A2036; color: #E9F3FC; border: 1px solid #214968;
    border-radius: 8px; padding: 7px 11px; selection-background-color: #168AF5;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover { border: 1px solid #2877AA; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid #1593FF; }
QLineEdit::placeholder { color: #71899E; }
QComboBox QAbstractItemView {
    background: #0D2943; color: #F1F6FC; border: 1px solid #245679;
    selection-background-color: #168AF5; outline: none;
}
QPushButton {
    border: 1px solid transparent; border-radius: 8px; padding: 9px 18px;
    font-weight: 700; color: #FFFFFF; background: #12304D;
}
QPushButton:hover { background: #1A4066; }
QPushButton:pressed { background: #1A4066; }
QPushButton:disabled { color: #71899E; background: #12304D; }
QPushButton#btnPrimary { background: #168BF4; border: 1px solid #48AEFF; }
QPushButton#btnPrimary:hover { background: #249AFF; }
QPushButton#btnPrimary:pressed { background: #0E70CC; }
QPushButton#btnSuccess { background: #16B86A; color: #04210F; }
QPushButton#btnSuccess:hover { background: #20CF7B; color: #04210F; }
QPushButton#btnDanger { background: #DC2F3C; }
QPushButton#btnDanger:hover { background: #EF3E4A; }
QPushButton#btnWarning { background: #F5B514; color: #1A1300; }
QPushButton#btnWarning:hover { background: #FFC52C; color: #1A1300; }
QPushButton#btnGhost { background: #102F4A; color: #DDEEFF; border: 1px solid #168AF5; }
QPushButton#btnGhost:hover { background: #164063; }
QCheckBox { color: #B6CCE0; spacing: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px; border: 1px solid #3C617D; background: #102A42; }
QCheckBox::indicator:checked { background: #168AF5; border: 1px solid #168AF5; }
QTableWidget {
    background: transparent; alternate-background-color: rgba(12, 41, 66, 150);
    color: #F1F6FC; gridline-color: #0C2942; border: none; border-radius: 0;
}
QTableWidget::item { padding: 4px 6px; border: none; }
QTableWidget::item:hover { background: #123A5C; }
QTableWidget::item:selected { background: #154C77; color: #FFFFFF; }
QHeaderView::section {
    background: #123554; color: #B6CCE0; border: none; border-bottom: 1px solid #285B7D;
    padding: 9px 6px; font-weight: 700; font-size: 11px;
}
QHeaderView::section:first { border-top-left-radius: 10px; }
QHeaderView::section:last { border-top-right-radius: 10px; }
QTableCornerButton::section { background: #123554; border: none; }
QPlainTextEdit {
    background: #061521; color: #7F9AB0; border: 1px solid #214D6D;
    border-radius: 10px; padding: 8px; selection-background-color: #168AF5;
}
QProgressBar {
    background: #0A2036; border: 1px solid #214968; border-radius: 6px;
    text-align: center; color: #D6E2ED; font-size: 10px; height: 14px;
}
QProgressBar::chunk { background: #168AF5; border-radius: 5px; }
QSpinBox::up-button, QSpinBox::down-button { width: 18px; border: none; background: transparent; }
QStatusBar { background: #081726; color: #B6CCE0; border-top: 1px solid #1A3A59; }
QStatusBar::item { border: none; }
QSplitter::handle { background: transparent; }
QSplitter::handle:vertical { height: 8px; }
QScrollBar:vertical { background: transparent; width: 11px; margin: 2px; }
QScrollBar::handle:vertical { background: #245679; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #2C6387; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 2px; }
QScrollBar::handle:horizontal { background: #245679; border-radius: 5px; min-width: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QToolTip { background: #0D2943; color: #F1F6FC; border: 1px solid #245679; padding: 5px; }
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
        self.client, self.user, self.password = c, user, password

    def run(self, cmd, sudo=False):
        need_sudo = sudo and self.user != "root"
        if need_sudo:
            cmd = ("sudo -S -p '' " if self.password else "sudo -n ") + cmd
        with self.lock:
            stdin, out, err = self.client.exec_command(cmd)
            if need_sudo and self.password:
                stdin.write(self.password + "\n")
                stdin.flush()
            o = out.read().decode(errors="replace")
            e = err.read().decode(errors="replace")
            code = out.channel.recv_exit_status()
        return code, o, e


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
    threading.Thread(target=worker, daemon=True).start()


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
        self.cfg = load_cfg()
        self.rows = []
        self.busy = False
        self.log_name = None
        self.log_raw = ""
        self.shown = []

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
        self.btn_rename = QPushButton()
        self.btn_delete = QPushButton()
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
        self.btn_conn = QPushButton("Подключиться")
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
        self.btn_start = QPushButton("Старт")
        self.btn_stop = QPushButton("Стоп")
        self.btn_restart = QPushButton("Рестарт")
        self.btn_refresh = QPushButton("Обновить")
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

        self.table = QTableWidget(0, len(self.COLS))
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
        tl.addLayout(bar)
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
        self.btn_log_save = QPushButton("Сохранить")
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
        for i, b in enumerate((self.btn_mode_bots, self.btn_mode_files,
                               self.btn_mode_backup, self.btn_mode_deploy)):
            b.setCheckable(True)
            b.setObjectName(seg_names[i])
            b.setCursor(Qt.PointingHandCursor)
            mode_group.addButton(b, i)
            seg.addWidget(b)
        self.btn_mode_bots.setChecked(True)
        seg_wrap = QWidget()
        seg_wrap.setStyleSheet(SEG_QSS)
        seg_wrap.setLayout(seg)
        head.insertWidget(3, seg_wrap)  # между заголовком и pill со статистикой
        head.insertWidget(4, self.monitor)

        self.files_tab = FilesTab()
        self.backup_tab = BackupTab()
        self.deploy_tab = DeployTab()
        self.pages = QStackedWidget()
        self.pages.addWidget(split)         # 0 — Боты: ровно тот же split, что и раньше
        self.pages.addWidget(self.files_tab)  # 1 — Файлы
        self.pages.addWidget(self.backup_tab)  # 2 — Бэкапы
        self.pages.addWidget(self.deploy_tab)  # 3 — Деплой
        self._seg_buttons = (self.btn_mode_bots, self.btn_mode_files,
                               self.btn_mode_backup, self.btn_mode_deploy)
        self._move_seg_glow(0)
        mode_group.idClicked.connect(self._move_seg_glow)
        mode_group.idClicked.connect(self.pages.setCurrentIndex)
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
        conn_glow.setColor(QColor(20, 145, 255, 64))
        self.btn_conn.setGraphicsEffect(conn_glow)

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
    def _move_seg_glow(self, i: int) -> None:
        """Свечение — только на активной кнопке переключателя (п.4 ТЗ).

        Эффект каждый раз новый: Qt удаляет старый при setGraphicsEffect(None),
        переиспользовать один инстанс нельзя.
        """
        for b in self._seg_buttons:
            b.setGraphicsEffect(None)
        if 0 <= i < len(self._seg_buttons):
            eff = QGraphicsDropShadowEffect(self._seg_buttons[i])
            eff.setBlurRadius(18)
            eff.setOffset(0)
            eff.setColor(QColor(20, 139, 244, 46))
            self._seg_buttons[i].setGraphicsEffect(eff)

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

        def job():
            self.ssh.connect(host, port, user, pw, key)
            return reuse or f"{user}@{host}:{port}"

        def done(name, err):
            self.btn_conn.setEnabled(True)
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
            self.t_refresh.start()
            self.t_monitor.start()
            self.refresh()
            self.monitor_tick()
            self.files_tab.on_connected(dict(host=host, port=port, user=user, password=pw, key=key, profile=name))
            self.backup_tab.on_connected(dict(host=host, port=port, user=user, password=pw, key=key, profile=name))
            self.deploy_tab.on_connected(dict(host=host, port=port, user=user, password=pw, key=key, profile=name))

        bg(job, done)

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
                self.t_refresh.stop()
                self.t_monitor.stop()
                return self.statusBar().showMessage(f"Связь потеряна: {err}")
            self.rows = rows
            self.render()

        bg(lambda: scan(self.ssh), done)

    def render(self):
        sel = self.current()
        rows = [r for r in self.rows if not self.only_tg.isChecked() or r["kind"] == "Telegram"]
        docker_icon = load_icon("docker")
        kind_fg, kind_bg = QColor("#B6CCE0"), QColor(255, 255, 255, 14)
        status_tx = QColor("#F1F6FC")
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            if r["active"] == "active":
                dot = QColor("#29D17D")
            elif r["active"] == "failed":
                dot = QColor("#FF4D59")
            else:
                dot = QColor("#8496A8")
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

    def open_bot_terminal(self, row: dict) -> None:
        host, user = self.host.text().strip(), self.user.text().strip()
        try:
            port = str(int(self.port.text() or 22))
        except ValueError:
            port = "22"
        if not host or not user:
            return self.statusBar().showMessage("Укажи хост и пользователя")
        d = (row or {}).get("dir", "")
        remote = f"cd {shlex.quote(d)} && exec bash -l" if d else "exec bash -l"
        args = ["ssh", "-t", "-p", port, f"{user}@{host}", remote]

        def clean_env():
            pe = QProcessEnvironment()
            for k, v in sanitized_env().items():
                pe.insert(k, v)
            return pe

        proc = QProcess()
        proc.setProcessEnvironment(clean_env())
        proc.setProgram("wt.exe")
        proc.setArguments(args)
        if proc.startDetached():
            return self.statusBar().showMessage(f"Терминал: {user}@{host}")
        fb = QProcess()
        fb.setProcessEnvironment(clean_env())
        fb.setProgram("cmd")
        fb.setArguments(["/c", "start", "", "ssh", "-t", "-p", port,
                         f"{user}@{host}", remote])
        if fb.startDetached():
            return self.statusBar().showMessage(f"Терминал: {user}@{host}")
        self.statusBar().showMessage("Не запустился ни wt.exe, ни ssh")

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
        if not name or not self.ssh.client:
            return
        self.log_name = name
        self.log_title.setText(f"Логи: {strip_docker_prefix(name)}")

        def done(res, err):
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
        bar = self.logs.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        self.logs.setPlainText(text)
        if at_bottom or not self.live.isChecked():
            bar.setValue(bar.maximum())

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
    app.setStyleSheet(THEME_QSS)
    bridge = Bridge()
    bridge.done.connect(lambda cb, res, err: cb(res, err))
    w = Win()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
