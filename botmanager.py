#!/usr/bin/env python3
"""Bot Manager: FileZilla для systemd-ботов. Подключился по SSH, видишь ботов, жмёшь кнопки."""
import json
import re
import shlex
import sys
import threading
from pathlib import Path, PurePosixPath

import paramiko
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from files_tab import FilesTab, SEG_QSS

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


THEME_QSS = """
* { outline: none; }
QMainWindow, QWidget#root { background: #0F1420; }
QLabel { color: #E8ECF4; }
QLabel#title { font-size: 19px; font-weight: 800; letter-spacing: 1px; color: #FFFFFF; }
QLabel#subtitle { color: #8B93A7; font-size: 11px; }
QLabel#h2 { font-size: 13px; font-weight: 700; color: #FFFFFF; }
QLabel#statsPill {
    background: rgba(108, 92, 231, 38); color: #C7CBFF;
    border: 1px solid #343B63; border-radius: 10px; padding: 6px 12px; font-weight: 600;
}
QFrame#card {
    background: #1A2233; border: 1px solid #2A3550; border-radius: 14px;
}
QLineEdit, QComboBox, QSpinBox {
    background: #0F1420; color: #E8ECF4; border: 1px solid #2A3550;
    border-radius: 9px; padding: 7px 11px; selection-background-color: #6C5CE7;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid #6C5CE7; }
QLineEdit::placeholder { color: #5B657D; }
QComboBox QAbstractItemView {
    background: #1A2233; color: #E8ECF4; border: 1px solid #2A3550;
    selection-background-color: #6C5CE7; outline: none;
}
QPushButton {
    border: none; border-radius: 9px; padding: 9px 18px;
    font-weight: 700; color: #FFFFFF; background: #2A3550;
}
QPushButton:hover { filter: brightness(115%); background: #33405F; }
QPushButton:pressed { background: #232C47; }
QPushButton:disabled { color: #7C86A0; background: #222A41; }
QPushButton#btnPrimary { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6C5CE7, stop:1 #00B8D4); }
QPushButton#btnPrimary:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7D6EF0, stop:1 #1AC6DE); }
QPushButton#btnSuccess { background: #16A34A; }
QPushButton#btnSuccess:hover { background: #22C55E; }
QPushButton#btnDanger { background: #DC2626; }
QPushButton#btnDanger:hover { background: #EF4444; }
QPushButton#btnWarning { background: #D97706; }
QPushButton#btnWarning:hover { background: #F59E0B; }
QPushButton#btnGhost { background: #232C47; color: #C9D4E8; border: 1px solid #2A3550; }
QPushButton#btnGhost:hover { background: #2B3658; }
QCheckBox { color: #C9D4E8; spacing: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 5px; border: 1px solid #3A4666; background: #0F1420; }
QCheckBox::indicator:checked { background: #6C5CE7; border: 1px solid #6C5CE7; }
QTableWidget {
    background: #151C2E; alternate-background-color: #182036;
    color: #E8ECF4; gridline-color: #232D47; border: 1px solid #2A3550; border-radius: 10px;
}
QTableWidget::item { padding: 4px 6px; border: none; }
QTableWidget::item:selected { background: rgba(108, 92, 231, 55); color: #FFFFFF; }
QHeaderView::section {
    background: #1E2942; color: #9AA3BB; border: none; border-bottom: 1px solid #2A3550;
    padding: 9px 6px; font-weight: 700; font-size: 11px; text-transform: uppercase;
}
QHeaderView::section:first { border-top-left-radius: 10px; }
QHeaderView::section:last { border-top-right-radius: 10px; }
QTableCornerButton::section { background: #1E2942; border: none; }
QPlainTextEdit {
    background: #0B0F1A; color: #C9D4E8; border: 1px solid #2A3550;
    border-radius: 10px; padding: 8px; selection-background-color: #6C5CE7;
}
QSpinBox::up-button, QSpinBox::down-button { width: 18px; border: none; background: transparent; }
QStatusBar { background: #121826; color: #8B93A7; border-top: 1px solid #232D47; }
QStatusBar::item { border: none; }
QSplitter::handle { background: transparent; }
QSplitter::handle:vertical { height: 8px; }
QScrollBar:vertical { background: transparent; width: 11px; margin: 2px; }
QScrollBar::handle:vertical { background: #2E3A5C; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #6C5CE7; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 2px; }
QScrollBar::handle:horizontal { background: #2E3A5C; border-radius: 5px; min-width: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QToolTip { background: #232C47; color: #E8ECF4; border: 1px solid #2A3550; padding: 5px; }
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

    rows = []
    for i, u in enumerate(units):
        mem, cpu = stats.get(u.get("MainPID", ""), (None, None))
        rows.append({
            "name": u["Id"], "kind": unit_kind(i, u, tg_hits, go_units),
            "active": u.get("ActiveState", ""), "sub": u.get("SubState", ""),
            "pid": u.get("MainPID", "0"), "mem": mem, "cpu": cpu,
            "since": u.get("ActiveEnterTimestamp", "") if u.get("ActiveState") == "active" else "",
        })
    for r in dock:
        if r["pid"]:
            r["mem"], r["cpu"] = stats.get(r["pid"], (None, None))
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
        self.setWindowTitle("Bot Manager")
        self.setWindowIcon(app_icon())
        self.resize(1180, 780)
        self.ssh = SSH()
        self.cfg = load_cfg()
        self.rows = []
        self.busy = False
        self.log_name = None
        self.shown = []

        # --- шапка с эмблемкой ---
        logo = QLabel()
        pix = QPixmap(resource_path("icon.png"))
        if not pix.isNull():
            logo.setPixmap(pix.scaled(46, 46, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        title = QLabel("BOT MANAGER")
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
        head = QHBoxLayout()
        head.setSpacing(12)
        head.addWidget(logo)
        head.addLayout(tcol)
        head.addStretch()
        head.addWidget(self.stats)

        # --- карточка подключения ---
        self.prof = QComboBox()
        self.prof.addItem("— новое подключение —")
        self.prof.addItems(self.cfg["profiles"].keys())
        self.prof.setMinimumWidth(170)
        self.btn_rename = QPushButton("✏️")
        self.btn_delete = QPushButton("🗑️")
        for b in (self.btn_rename, self.btn_delete):
            b.setObjectName("btnGhost")
            b.setFixedWidth(40)
            b.setCursor(Qt.PointingHandCursor)
        self.btn_rename.setToolTip("Переименовать выбранный профиль")
        self.btn_delete.setToolTip("Удалить выбранный профиль")
        prof_bar = QHBoxLayout()
        prof_bar.setContentsMargins(0, 0, 0, 0)
        prof_bar.setSpacing(6)
        prof_bar.addWidget(self.prof, 1)
        prof_bar.addWidget(self.btn_rename)
        prof_bar.addWidget(self.btn_delete)
        prof_wrap = QWidget()
        prof_wrap.setStyleSheet("background: transparent;")
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
        self.btn_conn = QPushButton("⚡ Подключиться")
        self.btn_conn.setObjectName("btnPrimary")
        self.btn_conn.setCursor(Qt.PointingHandCursor)

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
        conn_card.setObjectName("card")
        conn_card.setLayout(conn_grid)

        # --- панель действий + таблица ---
        self.only_tg = QCheckBox("Только Telegram-боты")
        self.only_tg.setChecked(True)
        self.btn_start = QPushButton("▶ Старт")
        self.btn_stop = QPushButton("■ Стоп")
        self.btn_restart = QPushButton("⟳ Рестарт")
        self.btn_refresh = QPushButton("↻ Обновить")
        self.btn_start.setObjectName("btnSuccess")
        self.btn_stop.setObjectName("btnDanger")
        self.btn_restart.setObjectName("btnWarning")
        self.btn_refresh.setObjectName("btnGhost")
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
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(33)
        h = self.table.horizontalHeader()
        h.setHighlightSections(False)
        h.setSectionResizeMode(QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)

        tw = QFrame()
        tw.setObjectName("card")
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
        self.lines = QSpinBox()
        self.lines.setRange(20, 5000)
        self.lines.setValue(200)
        self.live = QCheckBox("Live (2 сек)")
        self.log_title = QLabel("📜 Логи: выбери бота")
        self.log_title.setObjectName("h2")
        lbar = QHBoxLayout()
        lbar.addWidget(self.log_title)
        lbar.addStretch()
        lbar.addWidget(QLabel("строк:"))
        lbar.addWidget(self.lines)
        lbar.addWidget(self.live)

        lw = QFrame()
        lw.setObjectName("card")
        ll = QVBoxLayout(lw)
        ll.setContentsMargins(14, 12, 14, 14)
        ll.setSpacing(8)
        ll.addLayout(lbar)
        ll.addWidget(self.logs)

        split = QSplitter(Qt.Vertical)
        split.addWidget(tw)
        split.addWidget(lw)
        split.setSizes([400, 300])

        root = QWidget()
        root.setObjectName("root")
        rl = QVBoxLayout(root)
        rl.setContentsMargins(14, 12, 14, 12)
        rl.setSpacing(12)
        # --- переключатель режимов (Боты / Файлы) ---
        self.btn_mode_bots = QPushButton("🤖 Боты")
        self.btn_mode_files = QPushButton("📁 Файлы")
        mode_group = QButtonGroup(self)
        mode_group.setExclusive(True)
        seg = QHBoxLayout()
        seg.setSpacing(0)
        for i, b in enumerate((self.btn_mode_bots, self.btn_mode_files)):
            b.setCheckable(True)
            b.setObjectName("segLeft" if i == 0 else "segRight")
            b.setCursor(Qt.PointingHandCursor)
            mode_group.addButton(b, i)
            seg.addWidget(b)
        self.btn_mode_bots.setChecked(True)
        seg_wrap = QWidget()
        seg_wrap.setStyleSheet(SEG_QSS)
        seg_wrap.setLayout(seg)
        head.insertWidget(3, seg_wrap)  # между заголовком и pill со статистикой

        self.files_tab = FilesTab()
        self.pages = QStackedWidget()
        self.pages.addWidget(split)         # 0 — Боты: ровно тот же split, что и раньше
        self.pages.addWidget(self.files_tab)  # 1 — Файлы
        mode_group.idClicked.connect(self.pages.setCurrentIndex)
        mode_group.idClicked.connect(lambda i: i == 1 and self.files_tab.activate())

        rl.addLayout(head)
        rl.addWidget(conn_card)
        rl.addWidget(self.pages, 1)
        self.setCentralWidget(root)

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
        self.lines.editingFinished.connect(self.load_logs)

        self.t_refresh = QTimer(self, interval=5000, timeout=self.refresh)
        self.t_logs = QTimer(self, interval=2000, timeout=self.load_logs)
        self.live.toggled.connect(lambda on: self.t_logs.start() if on else self.t_logs.stop())

        last = self.cfg.get("last")
        if last in self.cfg["profiles"]:
            self.prof.setCurrentText(last)  # только заполняет поля; подключаемся по кнопке
        self._update_prof_buttons()

    # --- подключение ---
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
            self.refresh()
            self.files_tab.on_connected(dict(host=host, port=port, user=user, password=pw, key=key, profile=name))

        bg(job, done)

    # --- список ботов ---
    def refresh(self):
        if not self.ssh.client or self.busy:
            return
        self.busy = True

        def done(rows, err):
            self.busy = False
            if err:
                self.t_refresh.stop()
                return self.statusBar().showMessage(f"Связь потеряна: {err}")
            self.rows = rows
            self.render()

        bg(lambda: scan(self.ssh), done)

    def render(self):
        sel = self.current()
        rows = [r for r in self.rows if not self.only_tg.isChecked() or r["kind"] == "Telegram"]
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            if r["active"] == "active":
                dot = QColor("#34D399")
            elif r["active"] == "failed":
                dot = QColor("#F87171")
            else:
                dot = QColor("#8B93A7")
            kinds = {"Telegram": ("#7DD3FC", (56, 189, 248, 42)),
                     "Python": ("#FCD34D", (251, 191, 36, 38)),
                     "Go": ("#5EEAD4", (45, 212, 191, 38)),
                     "Docker": ("#93C5FD", (147, 197, 253, 38)),
                     "Node": ("#86EFAC", (134, 239, 172, 34))}
            fg, bg = kinds.get(r["kind"], ("#9CA3AF", (156, 163, 175, 30)))
            kind_fg, kind_bg = QColor(fg), QColor(*bg)
            vals = ["●", r["name"], r["kind"], f'{r["active"]} ({r["sub"]})', r["pid"] if r["pid"] != "0" else "",
                    f'{r["mem"]:.1f}' if r["mem"] is not None else "", f'{r["cpu"]:.1f}' if r["cpu"] is not None else "", r["since"]]
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
                if j == 2:
                    it.setForeground(kind_fg)
                    it.setBackground(kind_bg)
                    it.setTextAlignment(Qt.AlignCenter)
                if j == 3:
                    it.setForeground(dot)
                self.table.setItem(i, j, it)
            if r["name"] == sel:
                self.table.selectRow(i)
        self.table.blockSignals(False)
        self.shown = rows
        n_run = sum(1 for r in rows if r["active"] == "active")
        self.stats.setText(f"🤖 {len(rows)} ботов · 🟢 {n_run} запущено")
        self.statusBar().showMessage(f"Ботов: {len(rows)}, запущено: {n_run}")

    def current(self):
        r = self.table.currentRow()
        if r < 0 or r >= len(getattr(self, "shown", [])):
            return None
        return self.shown[r]["name"]

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
    def load_logs(self):
        name = self.current()
        if not name or not self.ssh.client:
            return
        self.log_name = name
        self.log_title.setText(f"📜 Логи: {name}")
        n = self.lines.value()

        def done(res, err):
            if err or name != self.log_name:
                return
            bar = self.logs.verticalScrollBar()
            at_bottom = bar.value() >= bar.maximum() - 4
            self.logs.setPlainText(res[1] or res[2])
            if at_bottom or not self.live.isChecked():
                bar.setValue(bar.maximum())

        kind, target = self._target(name)
        if kind == "docker":
            bg(lambda: self.docker_exec(f"logs --tail {n}", target), done)
        else:
            bg(lambda: self.ssh.run(f"journalctl -u {shlex.quote(target)} -n {n} --no-pager -o short-iso", sudo=True), done)


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
