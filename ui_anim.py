"""Темы и анимации BOT SPOT: токены палитр, AnimatedButton, общие хелперы.

QSS не умеет transition — всё плавное здесь делается QVariantAnimation/
QPropertyAnimation. Токены темы — единственный источник цветов для всех вкладок.
"""
from PySide6.QtCore import (QEasingCurve, QPropertyAnimation, QRect, Qt,
                            QVariantAnimation)
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QGraphicsDropShadowEffect, QGraphicsOpacityEffect,
                               QPushButton, QTextEdit, QWidget)

THEMES = {
    "graphite": {
        "bg": "#0e0e11", "pg": "#09090b", "sd": "#0e0e11", "sf": "#17171c",
        "ln": "rgba(255,255,255,0.08)", "tx": "#ececf1", "mu": "#8a8a9c",
        "ac": "#9b8cff", "ac2": "#7c6cf0", "ai": "#15102e",
        "ok": "#4ade80", "wn": "#f5b942", "er": "#ff6b7a",
        "r": 10, "rs": 6, "icon": "#c7c7d1",
    },
    "white": {
        "bg": "#f4f5f7", "pg": "#e9ebee", "sd": "#ffffff", "sf": "#ffffff",
        "ln": "rgba(20,30,40,0.09)", "tx": "#1c232b", "mu": "#6b7684",
        "ac": "#2f6fed", "ac2": "#2f6fed", "ai": "#ffffff",
        "ok": "#12a26a", "wn": "#c07f14", "er": "#d6455f",
        "r": 16, "rs": 10, "icon": "#4a5568",
    },
    "sky": {
        "bg": "#eaf2fb", "pg": "#dbe7f6", "sd": "#eef5fc", "sf": "#ffffff",
        "ln": "rgba(30,80,140,0.12)", "tx": "#12283d", "mu": "#5b7793",
        "ac": "#1e9bff", "ac2": "#3b6cf6", "ai": "#ffffff",
        "ok": "#0f9a6a", "wn": "#b7791f", "er": "#d64560",
        "r": 18, "rs": 12, "icon": "#3f6b93",
    },
}
THEME_TITLES = {"graphite": "Графит", "white": "Белая", "sky": "Голубая"}
DEFAULT_THEME = "graphite"

_state = {"name": DEFAULT_THEME}
_hooks = []
_derived_cache = {}


def theme_name() -> str:
    return _state["name"]


def tokens() -> dict:
    return THEMES[_state["name"]]


def set_theme(name: str, notify: bool = True) -> None:
    if name not in THEMES:
        name = DEFAULT_THEME
    _state["name"] = name
    if notify:
        for fn in list(_hooks):
            try:
                fn()
            except Exception:
                pass


def register_theme_hook(fn) -> None:
    _hooks.append(fn)


# ---------- цветовые утилиты ----------
def _rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(r, g, b) -> str:
    return "#{:02x}{:02x}{:02x}".format(max(0, min(255, int(r))),
                                         max(0, min(255, int(g))),
                                         max(0, min(255, int(b))))


def shade(h: str, f: float) -> str:
    r, g, b = _rgb(h)
    return _hex(r * f, g * f, b * f)


def mix(a: str, b: str, f: float) -> str:
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    return _hex(ra + (rb - ra) * f, ga + (gb - ga) * f, ba + (bb - ba) * f)


def alpha(h: str, a: int) -> str:
    """Полупрозрачный вариант цвета в #AARRGGBB — понимает и QSS, и QColor."""
    r, g, b = _rgb(h)
    return f"#{max(0, min(255, a)):02x}{r:02x}{g:02x}{b:02x}"


def on_color(h: str) -> str:
    r, g, b = _rgb(h)
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#101418" if lum > 0.55 else "#ffffff"


def derive(t: dict) -> dict:
    """Полный набор токенов: базовые + производные для QSS/градиентов."""
    key = (t["bg"], t["ac"], t["sf"], t["tx"])
    if key in _derived_cache:
        return _derived_cache[key]
    d = dict(t)
    bg, pg, sd, sf = t["bg"], t["pg"], t["sd"], t["sf"]
    tx, mu, ac, ai = t["tx"], t["mu"], t["ac"], t["ai"]
    ok, wn, er, ln = t["ok"], t["wn"], t["er"], t["ln"]
    lnb = "#ffffff" if bg in ("#0e0e11", "#09090b") else "#141e28"
    d["ln_solid"] = mix(bg, lnb, 0.14) if lnb != "#ffffff" else mix(bg, "#ffffff", 0.14)
    d["btn"] = mix(sf, tx, 0.08)
    d["btn_hover"] = mix(sf, tx, 0.16)
    d["btn_press"] = mix(sf, bg, 0.35)
    d["primary"] = ac
    d["primary_hover"] = shade(ac, 1.12)
    d["primary_press"] = shade(ac, 0.88)
    d["primary_fg"] = on_color(ac)
    d["success"], d["success_fg"] = ok, on_color(ok)
    d["success_hover"] = shade(ok, 1.1)
    d["danger"], d["danger_fg"] = er, on_color(er)
    d["danger_hover"] = shade(er, 1.1)
    d["warning"], d["warning_fg"] = wn, on_color(wn)
    d["warning_hover"] = shade(wn, 1.08)
    d["ghost_bg"] = ai
    d["ghost_border"] = ac
    d["ghost_fg"] = tx
    d["ghost_hover"] = mix(ai, ac, 0.18)
    d["inp_bg"] = mix(sf, bg, 0.45)
    d["inp_border"] = d["ln_solid"]
    d["inp_fg"] = tx
    d["placeholder"] = mu
    d["card"] = sd
    d["card_border"] = d["ln_solid"]
    d["glass"] = alpha(mix(sf, ac, 0.10), 200)
    d["glass_border"] = mix(sf, ac, 0.25)
    d["log_bg"] = mix(pg, bg, 0.5)
    d["item_hover"] = mix(ac, sf, 0.14)
    d["item_sel"] = mix(ac, sf, 0.42)
    d["item_sel_fg"] = on_color(d["item_sel"])
    d["row_alt"] = alpha(mix(sf, bg, 0.5), 60)
    d["grid"] = mix(sf, bg, 0.6)
    d["header_bg"] = mix(sf, bg, 0.25)
    d["header_fg"] = mu
    d["header_border"] = d["ln_solid"]
    d["scroll"] = mix(sf, tx, 0.18)
    d["scroll_hover"] = mix(sf, tx, 0.3)
    d["tooltip_bg"] = sd
    d["accent_soft"] = alpha(ac, 46)
    d["accent_soft2"] = alpha(ac, 64)
    d["banner_bg"] = mix(ai, ac, 0.12)
    d["banner_border"] = mix(d["ln_solid"], ac, 0.35)
    d["banner_err_border"] = mix(er, d["ln_solid"], 0.45)
    d["grad1"] = shade(bg, 1.25)
    d["grad2"] = bg
    d["grad3"] = mix(bg, ac, 0.12)
    d["glow"] = alpha(ac, 40)
    d["panel1"] = mix(sd, bg, 0.15)
    d["panel2"] = sd
    d["panel3"] = mix(sd, ac, 0.06)
    d["panel_border"] = mix(d["ln_solid"], ac, 0.25)
    d["flash"] = alpha(wn, 110)
    _derived_cache[key] = d
    return d


def derived() -> dict:
    return derive(tokens())


# ---------- анимации ----------
def animate_geometry(w: QWidget, rect: QRect, ms: int = 160) -> QPropertyAnimation:
    a = QPropertyAnimation(w, b"geometry", w)
    a.setDuration(ms)
    a.setStartValue(w.geometry())
    a.setEndValue(rect)
    a.setEasingCurve(QEasingCurve.OutCubic)
    a.start()
    return a


def fade_widget(w: QWidget, start: float, end: float, ms: int = 180, remove_after=True):
    """Короткий fade через QGraphicsOpacityEffect; эффект снимается по завершении."""
    eff = QGraphicsOpacityEffect(w)
    w.setGraphicsEffect(eff)
    eff.setOpacity(start)
    a = QPropertyAnimation(eff, b"opacity", w)
    a.setDuration(ms)
    a.setStartValue(start)
    a.setEndValue(end)
    a.setEasingCurve(QEasingCurve.OutCubic)
    if remove_after:
        a.finished.connect(lambda: w.setGraphicsEffect(None))
    a.start()
    return a


def flash_last_alert(plain, color_hex: str, ms: int = 900) -> None:
    """Гаснущая подсветка последней строки WARNING/ERROR в логах (extraSelections)."""
    doc = plain.document()
    block = doc.lastBlock()
    target = None
    for _ in range(300):
        if not block.isValid():
            break
        txt = block.text()
        if ("WARNING" in txt) or ("ERROR" in txt) or ("Traceback" in txt):
            target = block
            break
        block = block.previous()
    if target is None:
        return
    sel = QTextCharFormat()
    es = QTextEdit.ExtraSelection()
    es.cursor = plain.textCursor()
    es.cursor.setPosition(target.position())
    es.cursor.setPosition(target.position() + target.length() - 1, QTextCursor.KeepAnchor)
    es.format = sel
    plain.setExtraSelections([es])

    anim = QVariantAnimation(plain)
    anim.setDuration(ms)
    anim.setStartValue(110)
    anim.setEndValue(0)
    anim.setEasingCurve(QEasingCurve.OutCubic)

    def tick(v):
        f = QTextCharFormat()
        f.setBackground(QColor(alpha(color_hex, int(v))))
        es.format = f
        plain.setExtraSelections([es])

    anim.valueChanged.connect(tick)
    anim.finished.connect(lambda: plain.setExtraSelections([]))
    plain.setProperty("_flash_anim", anim)  # защита от сборки мусора
    anim.start()


class AnimatedButton(QPushButton):
    """QPushButton с плавным hover/press и свечением цветных вариантов."""

    VARIANTS = {"btnPrimary": "primary", "btnSuccess": "success",
                "btnDanger": "danger", "btnWarning": "warning", "btnGhost": "ghost"}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._anim = None
        self._glow_anim = None
        self._press_anim = None
        self._base_geo = None
        self.setMouseTracking(True)

    # --- цвета варианта ---
    def _colors(self):
        d = derived()
        v = self.VARIANTS.get(self.objectName(), "base")
        if v == "primary":
            return d["primary"], d["primary_hover"], d["primary_press"], d["primary"], True
        if v == "success":
            return d["success"], d["success_hover"], shade(d["ok"], 0.85), d["ok"], True
        if v == "danger":
            return d["danger"], d["danger_hover"], shade(d["er"], 0.85), d["er"], True
        if v == "warning":
            return d["warning"], d["warning_hover"], shade(d["wn"], 0.85), d["wn"], True
        if v == "ghost":
            return d["ghost_bg"], d["ghost_hover"], d["ghost_bg"], d["ac"], False
        return d["btn"], d["btn_hover"], d["btn_press"], d["ac"], False

    def _set_bg(self, color: QColor) -> None:
        self.setStyleSheet(f"QPushButton {{ background-color: {color.name()}; }}")

    def _animate_bg(self, target: str, ms: int = 120) -> None:
        if self._anim is not None:
            self._anim.stop()
        try:
            start = QColor(self.property("_cur_bg") or target)
            if not start.isValid():
                start = QColor(target)
        except Exception:
            start = QColor(target)
        a = QVariantAnimation(self)
        a.setDuration(ms)
        a.setStartValue(start)
        a.setEndValue(QColor(target))
        a.setEasingCurve(QEasingCurve.OutCubic)

        def tick(v):
            self.setProperty("_cur_bg", v.name())
            self._set_bg(v)

        a.valueChanged.connect(tick)
        a.finished.connect(lambda: self.setProperty("_cur_bg", target))
        self._anim = a
        self.setProperty("_cur_bg", start.name())
        a.start()

    def _set_glow(self, on: bool) -> None:
        base, hover, press, glow_color, has_glow = self._colors()
        if not has_glow or self.graphicsEffect() is not None:
            return  # не трогаем чужие эффекты (напр. conn_glow)
        eff = QGraphicsDropShadowEffect(self)
        eff.setOffset(0)
        eff.setColor(QColor(alpha(glow_color, 0)))
        eff.setBlurRadius(8)
        self.setGraphicsEffect(eff)
        if self._glow_anim is not None:
            self._glow_anim.stop()
        a = QPropertyAnimation(eff, b"blurRadius", self)
        a.setDuration(140)
        a.setStartValue(eff.blurRadius())
        a.setEndValue(16.0 if on else 8.0)
        a.setEasingCurve(QEasingCurve.OutCubic)
        a.finished.connect(lambda: eff.setColor(QColor(alpha(glow_color, 90 if on else 0))))
        eff.setColor(QColor(alpha(glow_color, 90 if on else 0)))
        self._glow_anim = a
        a.start()

    # --- события ---
    def enterEvent(self, ev):
        if self.isEnabled():
            _b, hover, _p, _g, _has = self._colors()
            self._animate_bg(hover)
            self._set_glow(True)
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        if self.isEnabled():
            base, _h, _p, _g, _has = self._colors()
            self._animate_bg(base if not self.isChecked() else base)
            self._set_glow(False)
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):
        if self.isEnabled():
            _b, _h, press, _g, _has = self._colors()
            self._animate_bg(press, 90)
            if self._base_geo is None:
                self._base_geo = QRect(self.geometry())
            if self._press_anim is not None:
                self._press_anim.stop()
            self._press_anim = animate_geometry(
                self, self._base_geo.adjusted(2, 2, -2, -2), 90)
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self.isEnabled() and self._base_geo is not None:
            if self._press_anim is not None:
                self._press_anim.stop()
            self._press_anim = animate_geometry(self, QRect(self._base_geo), 90)
            hover = self.rect().contains(ev.position().toPoint())
            _b, h, base, _g, _has = self._colors()
            self._animate_bg(h if hover else base, 120)
        super().mouseReleaseEvent(ev)

    def retheme(self) -> None:
        """После смены темы: сбросить кэш фона и свечение."""
        self.setProperty("_cur_bg", None)
        self.setStyleSheet("")
        eff = self.graphicsEffect()
        if isinstance(eff, QGraphicsDropShadowEffect) and eff.parent() is self:
            pass  # эффект пересоздастся при следующем hover
