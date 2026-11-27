#!/usr/bin/env python3
"""
Viral Finder - десктопное приложение для Windows в стиле Apple Music.
Ищет видео (YouTube Shorts, TikTok, Instagram Reels), у которых
просмотров >= 140 x числа подписчиков. Помнит, что уже проверил и показал.
"""
import csv
import os
import queue
import re
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

# В режиме --windowed у exe нет консоли: заглушки, чтобы библиотеки не падали
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import requests
from yt_dlp import YoutubeDL

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "ViralFinder"
APP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = APP_DIR / "state.db"
ACCOUNTS_PATH = APP_DIR / "accounts.txt"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
STOP_AFTER_KNOWN = 5  # столько уже проверенных видео подряд = "дошли до старого"
BROWSERS = {"Нет": None, "Firefox": "firefox", "Edge": "edge",
            "Chrome": "chrome", "Brave": "brave"}

# ---------------- палитра (тёмная тема Apple Music) ----------------
BG = "#1c1c1e"        # фон контента
SIDEBAR = "#252527"   # боковая панель
CARD = "#2c2c2e"      # карточки и поля ввода
PLAYER = "#2a2a2c"    # нижняя панель
LINE = "#3a3a3c"      # разделители
FG = "#ffffff"
SUB = "#98989f"       # второстепенный текст
ACCENT = "#fa2d48"    # фирменный красно-розовый
ACCENT_HI = "#ff5368"

SCALE = 1.0  # масштаб экрана, выставляется при старте


def px(n):
    return int(round(n * SCALE))


class SilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


# ---------------- память (SQLite) ----------------
def open_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS checked  (video_id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS reported (
            video_id TEXT PRIMARY KEY, url TEXT, account TEXT,
            views INTEGER, followers INTEGER, ratio REAL, found_at TEXT,
            session_at TEXT);
        CREATE TABLE IF NOT EXISTS searches (
            session_at TEXT PRIMARY KEY, accounts INTEGER,
            found INTEGER, stopped INTEGER);
    """)
    try:  # миграция старой базы без колонки session_at
        con.execute("ALTER TABLE reported ADD COLUMN session_at TEXT")
        con.commit()
    except sqlite3.OperationalError:
        pass
    return con


# ---------------- вспомогательное ----------------
def parse_count(s):
    s = s.replace(",", "").strip()
    m = re.match(r"([\d.]+)\s*([KMB]?)", s, re.I)
    if not m:
        return None
    mult = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()]
    return int(float(m.group(1)) * mult)


def platform_of(url):
    u = url.lower()
    if "tiktok.com" in u:
        return "tiktok"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "instagram.com" in u:
        return "instagram"
    return None


def parse_accounts(text):
    """Строки:  ссылка   или   ссылка | подписчики"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url, _, manual = [p.strip() for p in line.partition("|")]
        out.append((url, parse_count(manual) if manual else None))
    return out


def short_name(url):
    m = re.search(r"@([\w.\-]+)", url)
    if m:
        return "@" + m.group(1)
    m = re.search(r"instagram\.com/([\w.]+)", url)
    if m:
        return "@" + m.group(1)
    return url[:40]


def fmt(n):
    return f"{int(n):,}".replace(",", "\u00a0")


MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


def plural(n, one, few, many):
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def session_title(key, accounts=None):
    """'2026-11-27 14:32:05' -> 'Сегодня, 14:32 · 5 аккаунтов'"""
    try:
        dt = datetime.strptime(key, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(key)
    today = datetime.now().date()
    if dt.date() == today:
        day = "Сегодня"
    elif dt.date() == today - timedelta(days=1):
        day = "Вчера"
    else:
        day = f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}"
    title = f"{day}, {dt:%H:%M}"
    if accounts:
        title += f" · {accounts} {plural(accounts, 'аккаунт', 'аккаунта', 'аккаунтов')}"
    return title


def video_url(entry, platform):
    if platform == "youtube":
        return f"https://www.youtube.com/shorts/{entry['id']}"
    return entry.get("webpage_url") or entry.get("url")


def get_followers(url, platform, flat, log):
    try:
        if platform == "youtube":
            info = flat.extract_info(url, download=False)
            n = info.get("channel_follower_count")
            if n:
                return n
            for e in (info.get("entries") or []):
                vid = YoutubeDL({"quiet": True, "logger": SilentLogger()}).extract_info(
                    f"https://www.youtube.com/watch?v={e['id']}", download=False)
                return vid.get("channel_follower_count")
        elif platform == "tiktok":
            html = requests.get(url, headers={"User-Agent": UA}, timeout=20).text
            m = re.search(r'"followerCount":(\d+)', html)
            return int(m.group(1)) if m else None
        elif platform == "instagram":
            html = requests.get(url, headers={"User-Agent": UA}, timeout=20).text
            m = re.search(r"([\d.,]+[KMB]?)\s+Followers", html, re.I)
            return parse_count(m.group(1)) if m else None
    except Exception as e:
        log(f"   ! не удалось получить подписчиков: {e}")
    return None


# ---------------- поиск ----------------
def scan_account(con, url, manual, opts, flat, full, log, on_found, stop):
    platform = platform_of(url)
    if not platform:
        log(f"? Не знаю платформу: {url}")
        return 0
    if platform == "youtube" and not re.search(r"/(shorts|videos|streams)/?$", url):
        url = url.rstrip("/") + "/shorts"

    log(f"\n== {url}")
    followers = manual or get_followers(url, platform, flat, log)
    if not followers:
        log("   Не нашёл число подписчиков. Допиши его в строке: ссылка | 12500")
        return 0
    threshold = followers * opts["mult"]
    log(f"   подписчиков: {followers:,} -> порог: {int(threshold):,} просмотров")

    try:
        info = flat.extract_info(url, download=False)
    except Exception as e:
        log(f"   ! ошибка загрузки списка видео: {str(e)[:200]}")
        return 0

    count, known_streak = 0, 0
    for entry in (info.get("entries") or [])[: opts["limit"]]:
        if stop.is_set():
            break
        vid = entry.get("id")
        if not vid:
            continue

        seen = con.execute("SELECT 1 FROM checked WHERE video_id=?", (vid,)).fetchone()
        if seen and not opts["full"]:
            known_streak += 1
            if known_streak >= STOP_AFTER_KNOWN:
                log("   дошёл до уже проверенных видео, останавливаюсь")
                break
            continue
        known_streak = 0

        views = entry.get("view_count")
        if views is None:  # в списке нет просмотров - берём детально
            try:
                views = full.extract_info(video_url(entry, platform),
                                          download=False).get("view_count")
            except Exception:
                views = None
        if views is None:
            continue
        con.execute("INSERT OR IGNORE INTO checked VALUES (?)", (vid,))

        if views >= threshold:
            if con.execute("SELECT 1 FROM reported WHERE video_id=?", (vid,)).fetchone():
                continue
            link = video_url(entry, platform)
            ratio = views / followers
            found_at = datetime.now().strftime("%Y-%m-%d %H:%M")
            con.execute("INSERT INTO reported (video_id, url, account, views, followers, "
                        "ratio, found_at, session_at) VALUES (?,?,?,?,?,?,?,?)",
                        (vid, link, url, views, followers, ratio, found_at,
                         opts.get("session_at")))
            on_found((url, views, followers, ratio, link, found_at))
            count += 1
        con.commit()
    if count == 0:
        log("   новых трендовых видео нет")
    return count


# ---------------- рисованные элементы интерфейса ----------------
def round_rect(c, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def draw_icon(c, kind, cx, cy, color):
    s = SCALE
    w = max(2, int(round(2 * s)))
    if kind == "search":
        c.create_oval(cx - 7 * s, cy - 7 * s, cx + 3 * s, cy + 3 * s, outline=color, width=w)
        c.create_line(cx + 2 * s, cy + 2 * s, cx + 8 * s, cy + 8 * s, fill=color,
                      width=w, capstyle="round")
    elif kind == "list":
        for dy in (-6, 0, 6):
            c.create_line(cx - 8 * s, cy + dy * s, cx + 8 * s, cy + dy * s, fill=color,
                          width=w, capstyle="round")
    elif kind == "sliders":
        c.create_line(cx - 8 * s, cy - 4 * s, cx + 8 * s, cy - 4 * s, fill=color, width=w,
                      capstyle="round")
        c.create_line(cx - 8 * s, cy + 4 * s, cx + 8 * s, cy + 4 * s, fill=color, width=w,
                      capstyle="round")
        c.create_oval(cx - 5 * s, cy - 7 * s, cx + 1 * s, cy - 1 * s, fill=BG, outline=color, width=w)
        c.create_oval(cx + 1 * s, cy + 1 * s, cx + 7 * s, cy + 7 * s, fill=BG, outline=color, width=w)


def draw_logo(c, size, bg):
    """Красный скруглённый квадрат с белой буквой V."""
    c.delete("all")
    round_rect(c, 1, 1, size - 1, size - 1, size * 0.26, fill=ACCENT, outline=ACCENT)
    a, b = size * 0.30, size * 0.70
    c.create_line(a, size * 0.32, size / 2, size * 0.70, b, size * 0.32, fill="white",
                  width=max(2, int(size * 0.11)), capstyle="round", joinstyle="round")


class Pill(tk.Canvas):
    """Кнопка-«таблетка» со скруглёнными краями."""

    def __init__(self, parent, text, command, font, parent_bg,
                 fill=CARD, hover=LINE, fg=FG, padx=18, height=34):
        self.text, self.command, self.font = text, command, font
        self.fill, self.hover_fill, self.fg = fill, hover, fg
        self.w = font.measure(text) + px(padx) * 2
        self.h = px(height)
        super().__init__(parent, width=self.w, height=self.h, bg=parent_bg,
                         highlightthickness=0, cursor="hand2")
        self._draw(fill)
        self.bind("<Enter>", lambda e: self._draw(self.hover_fill))
        self.bind("<Leave>", lambda e: self._draw(self.fill))
        self.bind("<ButtonRelease-1>", lambda e: self.command())

    def _draw(self, color):
        self.delete("all")
        round_rect(self, 1, 1, self.w - 1, self.h - 1, self.h // 2, fill=color, outline=color)
        self.create_text(self.w // 2, self.h // 2, text=self.text, fill=self.fg, font=self.font)


class NavItem(tk.Canvas):
    """Пункт боковой панели с иконкой."""

    def __init__(self, parent, text, icon, on_click, font):
        super().__init__(parent, height=px(38), bg=SIDEBAR, highlightthickness=0, cursor="hand2")
        self.text, self.icon, self.font = text, icon, font
        self.active = self.hovered = False
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<ButtonRelease-1>", lambda e: on_click())

    def _hover(self, v):
        self.hovered = v
        self.redraw()

    def set_active(self, v):
        self.active = v
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if self.active or self.hovered:
            col = LINE if self.active else "#303032"
            round_rect(self, 0, 0, w, h, px(8), fill=col, outline=col)
        draw_icon(self, self.icon, px(22), h // 2, ACCENT)
        self.create_text(px(46), h // 2, text=self.text, anchor="w", fill=FG, font=self.font)


class Toggle(tk.Canvas):
    """Переключатель в стиле iOS."""

    def __init__(self, parent, var, bg):
        self.w, self.h = px(46), px(28)
        super().__init__(parent, width=self.w, height=self.h, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self.var = var
        self.bind("<ButtonRelease-1>", lambda e: self.var.set(not self.var.get()))
        var.trace_add("write", lambda *a: self.draw())
        self.draw()

    def draw(self):
        self.delete("all")
        on = self.var.get()
        col = ACCENT if on else "#48484a"
        round_rect(self, 1, 1, self.w - 1, self.h - 1, self.h // 2, fill=col, outline=col)
        r = self.h // 2 - px(3)
        cx = self.w - self.h // 2 if on else self.h // 2
        self.create_oval(cx - r, self.h // 2 - r, cx + r, self.h // 2 + r,
                         fill="white", outline="white")


class PlayButton(tk.Canvas):
    """Большая круглая кнопка: ▶ запустить / ■ остановить."""

    def __init__(self, parent, command, bg):
        self.d = px(48)
        super().__init__(parent, width=self.d, height=self.d, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self.command, self.running, self.hovered = command, False, False
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<ButtonRelease-1>", lambda e: self.command())
        self.draw()

    def _hover(self, v):
        self.hovered = v
        self.draw()

    def set_running(self, v):
        self.running = v
        self.draw()

    def draw(self):
        self.delete("all")
        d = self.d
        col = "#e5e5ea" if self.hovered else "#ffffff"
        self.create_oval(2, 2, d - 2, d - 2, fill=col, outline=col)
        c = d / 2
        if self.running:
            s = d * 0.17
            round_rect(self, c - s, c - s, c + s, c + s, px(3), fill=BG, outline=BG)
        else:
            s = d * 0.18
            self.create_polygon(c - s * 0.7, c - s * 1.15, c - s * 0.7, c + s * 1.15,
                                c + s * 1.3, c, fill=BG, outline=BG)


# ---------------- главное окно ----------------
class App(tk.Tk):
    def __init__(self):
        global SCALE
        super().__init__()
        SCALE = self.winfo_fpixels("1i") / 96.0
        self.title("Viral Finder")
        self.geometry(f"{px(1120)}x{px(740)}")
        self.minsize(px(940), px(600))
        self.configure(bg=BG)

        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.session_found = 0
        self.session_at = None
        self.running = False
        self._dirty = False
        self.group_iids = {}

        self._setup_fonts()
        self._setup_styles()
        self._setup_icon()

        self.var_mult = tk.StringVar(value="140")
        self.var_limit = tk.StringVar(value="50")
        self.var_full = tk.BooleanVar(value=False)
        self.var_browser = tk.StringVar(value="Нет")
        self.status_title = tk.StringVar(value="Готов к поиску")
        self.status_sub = tk.StringVar(value="Добавь ссылки на аккаунты и нажми ▶")
        self.total_text = tk.StringVar(value="")

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_pages()
        self._build_player()
        self.show_page("search")

        self._rebuild_list()
        self._dark_titlebar()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)

    # ---- оформление ----
    def _setup_fonts(self):
        fams = set(tkfont.families())
        body = next((f for f in ("Segoe UI Variable Text", "Segoe UI") if f in fams), "Segoe UI")
        disp = next((f for f in ("Segoe UI Variable Display", "Segoe UI") if f in fams), "Segoe UI")
        self.fam = body
        self.f_title = tkfont.Font(family=disp, size=24, weight="bold")
        self.f_h2 = tkfont.Font(family=disp, size=13, weight="bold")
        self.f_body = tkfont.Font(family=body, size=11)
        self.f_body_b = tkfont.Font(family=body, size=11, weight="bold")
        self.f_small = tkfont.Font(family=body, size=10)
        self.f_nav = tkfont.Font(family=body, size=11)
        self.f_mono = tkfont.Font(family="Consolas", size=9)

    def _setup_styles(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("Treeview", background=BG, fieldbackground=BG, foreground=FG,
                     rowheight=px(42), borderwidth=0, relief="flat", font=self.f_body)
        st.map("Treeview", background=[("selected", LINE)], foreground=[("selected", FG)])
        st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        st.configure("Treeview.Heading", background=BG, foreground=SUB, relief="flat",
                     borderwidth=0, bordercolor=BG, lightcolor=BG, darkcolor=BG,
                     font=(self.fam, 10, "bold"), padding=(px(8), px(8)))
        st.map("Treeview.Heading", background=[("active", BG)])
        st.layout("Vertical.TScrollbar",
                  [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
                      ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        st.configure("Vertical.TScrollbar", background="#48484a", troughcolor=BG,
                     bordercolor=BG, lightcolor="#48484a", darkcolor="#48484a",
                     relief="flat", width=px(10))
        st.map("Vertical.TScrollbar", background=[("active", "#636366")])
        st.configure("TSpinbox", fieldbackground=LINE, foreground=FG, background=LINE,
                     arrowcolor=FG, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                     insertcolor=FG, padding=px(4))
        st.configure("TCombobox", fieldbackground=LINE, background=LINE, foreground=FG,
                     arrowcolor=FG, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                     selectbackground=LINE, selectforeground=FG, padding=px(4))
        st.map("TCombobox", fieldbackground=[("readonly", LINE)],
               foreground=[("readonly", FG)], selectbackground=[("readonly", LINE)],
               selectforeground=[("readonly", FG)])
        self.option_add("*TCombobox*Listbox.background", CARD)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "white")
        self.option_add("*TCombobox*Listbox.font", self.f_body)

    def _setup_icon(self):
        try:
            img = tk.PhotoImage(width=64, height=64)
            r = 14
            for y in range(64):
                inset = 0
                if y < r:
                    inset = r - int((r * r - (r - y) ** 2) ** 0.5)
                elif y >= 64 - r:
                    inset = r - int((r * r - (y - (63 - r)) ** 2) ** 0.5)
                img.put(ACCENT, to=(inset, y, 64 - inset, y + 1))
            for y in range(16, 49):
                t = (y - 16) / 32.0
                for xc in (18 + 14 * t, 46 - 14 * t):
                    img.put("#ffffff", to=(int(xc) - 3, y, int(xc) + 4, y + 1))
            self.iconphoto(True, img)
            self._icon = img
        except Exception:
            pass

    def _dark_titlebar(self):
        """Тёмный заголовок окна (Windows 10/11)."""
        try:
            import ctypes
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            dwm = ctypes.windll.dwmapi
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
                dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_int(1)), 4)
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(ctypes.c_int(0x001E1C1C)), 4)  # цвет заголовка
            self.withdraw()
            self.deiconify()
        except Exception:
            pass

    # ---- боковая панель ----
    def _build_sidebar(self):
        side = tk.Frame(self, bg=SIDEBAR, width=px(230))
        side.grid(row=0, column=0, sticky="ns")
        side.grid_propagate(False)
        side.pack_propagate(False)

        brand = tk.Frame(side, bg=SIDEBAR)
        brand.pack(fill="x", padx=px(16), pady=(px(22), px(18)))
        logo = tk.Canvas(brand, width=px(30), height=px(30), bg=SIDEBAR, highlightthickness=0)
        logo.pack(side="left")
        draw_logo(logo, px(30), SIDEBAR)
        tk.Label(brand, text="Viral Finder", bg=SIDEBAR, fg=FG,
                 font=self.f_h2).pack(side="left", padx=px(10))

        self.nav = {}
        for key, text, icon in (("search", "Поиск", "search"),
                                ("results", "Находки", "list"),
                                ("settings", "Настройки", "sliders")):
            item = NavItem(side, text, icon, lambda k=key: self.show_page(k), self.f_nav)
            item.pack(fill="x", padx=px(10), pady=px(2))
            self.nav[key] = item

        tk.Label(side, text="Трендовые видео:\nпросмотры ≥ подписчики × порог",
                 bg=SIDEBAR, fg=SUB, font=self.f_small, justify="left",
                 anchor="w").pack(side="bottom", fill="x", padx=px(18), pady=px(16))

    # ---- страницы ----
    def _build_pages(self):
        self.content = tk.Frame(self, bg=BG)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.rowconfigure(0, weight=1)
        self.content.columnconfigure(0, weight=1)
        self.pages = {}
        self._page_search()
        self._page_results()
        self._page_settings()

    def _page_search(self):
        page = self._new_page("search")
        tk.Label(page, text="Поиск", bg=BG, fg=FG, font=self.f_title).pack(anchor="w")
        tk.Label(page, bg=BG, fg=SUB, font=self.f_body, justify="left", anchor="w",
                 text="Вставь ссылки на аккаунты TikTok, каналы YouTube и профили Instagram — "
                      "по одной в строке.\nЕсли число подписчиков не определилось, допиши его: "
                      "ссылка | 12500"
                 ).pack(anchor="w", pady=(px(4), px(14)))

        card = tk.Frame(page, bg=CARD, padx=px(4), pady=px(4))
        card.pack(fill="both", expand=True)
        self.txt_accounts = tk.Text(card, wrap="none", bg=CARD, fg=FG, insertbackground=FG,
                                    relief="flat", bd=0, highlightthickness=0, font=self.f_body,
                                    padx=px(12), pady=px(10), selectbackground=ACCENT,
                                    selectforeground="white", height=8)
        self.txt_accounts.pack(fill="both", expand=True)
        if ACCOUNTS_PATH.exists():
            self.txt_accounts.insert("1.0", ACCOUNTS_PATH.read_text(encoding="utf-8"))
        self._enable_clipboard(self.txt_accounts)

        tk.Label(page, text="Журнал", bg=BG, fg=FG, font=self.f_h2).pack(anchor="w", pady=(px(18), px(6)))
        logcard = tk.Frame(page, bg=SIDEBAR, padx=px(4), pady=px(4))
        logcard.pack(fill="x")
        self.log_box = tk.Text(logcard, height=7, state="disabled", bg=SIDEBAR, fg=SUB,
                               relief="flat", bd=0, highlightthickness=0, font=self.f_mono,
                               padx=px(10), pady=px(8), wrap="word")
        self.log_box.pack(fill="x")

    def _page_results(self):
        page = self._new_page("results")
        head = tk.Frame(page, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="Находки", bg=BG, fg=FG, font=self.f_title).pack(side="left")
        Pill(head, "Экспорт CSV", self.export_csv, self.f_small, BG).pack(side="right")
        Pill(head, "Копировать ссылки", self.copy_links, self.f_small, BG).pack(side="right", padx=px(8))
        tk.Label(page, bg=BG, fg=SUB, font=self.f_body, anchor="w", justify="left",
                 text="История поисков: находки сгруппированы по дате и времени запуска. "
                      "Двойной клик по видео открывает его в браузере."
                 ).pack(anchor="w", pady=(px(4), px(12)))

        wrap = tk.Frame(page, bg=BG)
        wrap.pack(fill="both", expand=True)
        cols = ("views", "followers", "ratio", "link", "found")
        heads = ("Просмотры", "Подписчики", "×", "Ссылка на видео", "Время")
        widths = (110, 110, 70, 280, 120)
        self.tree = ttk.Treeview(wrap, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Поиск и аккаунт", anchor="w")
        self.tree.column("#0", width=px(340), anchor="w", stretch=False)
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h, anchor="w")
            self.tree.column(c, width=px(w), anchor="w", stretch=(c == "link"))
        self.tree.tag_configure("group", background=CARD, font=self.f_body_b)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", self._open_link)
        self.empty_lbl = tk.Label(wrap, bg=BG, fg=SUB, font=self.f_body, justify="center",
                                  text="Пока ничего не найдено\nЗапусти поиск — история поисков появится здесь")

    def _page_settings(self):
        page = self._new_page("settings")
        tk.Label(page, text="Настройки", bg=BG, fg=FG, font=self.f_title).pack(anchor="w", pady=(0, px(14)))

        card = tk.Frame(page, bg=CARD)
        card.pack(fill="x")
        r = self._setting_row(card, "Порог просмотров",
                              "Видео считается трендовым, если просмотров не меньше, "
                              "чем подписчики × это число.", first=True)
        ttk.Spinbox(r, from_=1, to=100000, width=8, textvariable=self.var_mult).pack()
        r = self._setting_row(card, "Глубина проверки",
                              "Сколько последних видео смотреть у каждого аккаунта.")
        ttk.Spinbox(r, from_=5, to=500, width=8, textvariable=self.var_limit).pack()
        r = self._setting_row(card, "Куки из браузера",
                              "Нужны для Instagram и иногда для TikTok. Выбери браузер, где ты "
                              "вошёл в аккаунт (надёжнее всего — Firefox) и закрой его перед поиском.")
        ttk.Combobox(r, values=list(BROWSERS), textvariable=self.var_browser, width=10,
                     state="readonly").pack()
        r = self._setting_row(card, "Перепроверить всё заново",
                              "Игнорировать память о проверенных видео. "
                              "Уже показанные ссылки всё равно не повторяются.")
        Toggle(r, self.var_full, CARD).pack()

        tk.Label(page, text="Данные", bg=BG, fg=FG, font=self.f_h2).pack(anchor="w", pady=(px(22), px(8)))
        Pill(page, "Забыть историю", self.reset_history, self.f_body, BG,
             fill=CARD, hover=LINE, fg=ACCENT, height=38).pack(anchor="w")

    def _new_page(self, key):
        page = tk.Frame(self.content, bg=BG, padx=px(34), pady=px(28))
        page.grid(row=0, column=0, sticky="nsew")
        self.pages[key] = page
        return page

    def _setting_row(self, parent, title, sub, first=False):
        if not first:
            tk.Frame(parent, bg=LINE, height=1).pack(fill="x", padx=px(16))
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x")
        right = tk.Frame(row, bg=CARD)
        right.pack(side="right", padx=px(18))
        left = tk.Frame(row, bg=CARD)
        left.pack(side="left", fill="x", expand=True, padx=px(18), pady=px(13))
        tk.Label(left, text=title, bg=CARD, fg=FG, font=self.f_body_b, anchor="w").pack(anchor="w")
        tk.Label(left, text=sub, bg=CARD, fg=SUB, font=self.f_small, anchor="w",
                 justify="left", wraplength=px(480)).pack(anchor="w", pady=(px(2), 0))
        return right

    def show_page(self, name):
        self.pages[name].tkraise()
        for k, item in self.nav.items():
            item.set_active(k == name)

    # ---- нижняя панель (как плеер в Apple Music) ----
    def _build_player(self):
        bar = tk.Frame(self, bg=PLAYER)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.progress = tk.Canvas(bar, height=px(3), bg=LINE, highlightthickness=0)
        self.progress.pack(fill="x")

        inner = tk.Frame(bar, bg=PLAYER)
        inner.pack(fill="x", padx=px(18), pady=px(10))
        inner.columnconfigure(0, weight=1, uniform="p")
        inner.columnconfigure(1, weight=0)
        inner.columnconfigure(2, weight=1, uniform="p")

        left = tk.Frame(inner, bg=PLAYER)
        left.grid(row=0, column=0, sticky="w")
        logo = tk.Canvas(left, width=px(46), height=px(46), bg=PLAYER, highlightthickness=0)
        logo.pack(side="left")
        draw_logo(logo, px(46), PLAYER)
        txt = tk.Frame(left, bg=PLAYER)
        txt.pack(side="left", padx=px(12))
        tk.Label(txt, textvariable=self.status_title, bg=PLAYER, fg=FG,
                 font=self.f_body_b, anchor="w").pack(anchor="w")
        tk.Label(txt, textvariable=self.status_sub, bg=PLAYER, fg=SUB,
                 font=self.f_small, anchor="w").pack(anchor="w")

        self.play = PlayButton(inner, self.toggle_run, PLAYER)
        self.play.grid(row=0, column=1, padx=px(20))

        tk.Label(inner, textvariable=self.total_text, bg=PLAYER, fg=SUB,
                 font=self.f_small, anchor="e").grid(row=0, column=2, sticky="e")

    def _set_progress(self, frac):
        self.progress.delete("all")
        w = self.progress.winfo_width()
        if frac > 0:
            self.progress.create_rectangle(0, 0, int(w * frac), px(3), fill=ACCENT, outline=ACCENT)

    # ---- буфер обмена ----
    def _enable_clipboard(self, widget):
        """Ctrl+V/C/X/A работают при любой раскладке (в т.ч. русской) + меню по правой кнопке."""
        def on_key(event):
            if event.state & 0x4:  # зажат Ctrl
                code = event.keycode  # на Windows: V=86, C=67, X=88, A=65
                if code == 86:
                    widget.event_generate("<<Paste>>")
                    return "break"
                if code == 67:
                    widget.event_generate("<<Copy>>")
                    return "break"
                if code == 88:
                    widget.event_generate("<<Cut>>")
                    return "break"
                if code == 65:
                    widget.tag_add("sel", "1.0", "end-1c")
                    return "break"
        widget.bind("<Key>", on_key)

        menu = tk.Menu(widget, tearoff=0, bg=CARD, fg=FG, activebackground=ACCENT,
                       activeforeground="white", bd=0, font=self.f_body)
        menu.add_command(label="Вставить", command=lambda: widget.event_generate("<<Paste>>"))
        menu.add_command(label="Копировать", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="Вырезать", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_separator()
        menu.add_command(label="Выделить всё",
                         command=lambda: widget.tag_add("sel", "1.0", "end-1c"))
        widget.bind("<Button-3>",
                    lambda e: (widget.focus_set(), menu.tk_popup(e.x_root, e.y_root)))

    # ---- действия ----
    def log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _is_open(self, iid):
        try:
            return self.tree.item(iid, "open") in (1, True, "1", "true", "True")
        except tk.TclError:
            return True

    def _refresh_empty(self, has_sessions, total):
        if has_sessions:
            self.empty_lbl.place_forget()
        else:
            self.empty_lbl.place(relx=0.5, rely=0.4, anchor="center")
        self.total_text.set(f"Всего находок: {total}" if total else "")

    def _rebuild_list(self):
        """Перестраивает список «Находки» из базы: группы по дате и времени поиска."""
        con = open_db()
        rows = con.execute(
            "SELECT account, views, followers, ratio, url, found_at, "
            "COALESCE(session_at, found_at || ':00') FROM reported ORDER BY ratio DESC").fetchall()
        sessions = dict(con.execute("SELECT session_at, accounts FROM searches").fetchall())
        con.close()

        was_open = {k: self._is_open(iid) for k, iid in self.group_iids.items()}
        y = self.tree.yview()[0]
        self.tree.delete(*self.tree.get_children())
        self.group_iids = {}

        groups = {k: [] for k in sessions}
        for r in rows:
            groups.setdefault(r[6], []).append(r)

        for key in sorted(groups, reverse=True):
            items = groups[key]
            if items:
                summary = f"{len(items)} видео"
            elif self.running and key == self.session_at:
                summary = "идёт поиск…"
            else:
                summary = "ничего нового"
            iid = self.tree.insert("", "end", text=session_title(key, sessions.get(key)),
                                   values=("", "", "", "", summary), tags=("group",),
                                   open=was_open.get(key, True))
            self.group_iids[key] = iid
            for acc, views, followers, ratio, link, found_at, _k in items:
                self.tree.insert(iid, "end", text=short_name(acc),
                                 values=(fmt(views), fmt(followers),
                                         f"×{ratio:,.0f}".replace(",", "\u00a0"),
                                         link, (found_at or "")[11:16]))
        self.tree.yview_moveto(y)
        self._refresh_empty(bool(groups), len(rows))

    def toggle_run(self):
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self):
        accounts = parse_accounts(self.txt_accounts.get("1.0", "end"))
        if not accounts:
            messagebox.showinfo("Viral Finder", "Вставь хотя бы одну ссылку на странице «Поиск».")
            self.show_page("search")
            return
        try:
            opts = {"mult": float(self.var_mult.get()),
                    "limit": int(self.var_limit.get()),
                    "full": self.var_full.get(),
                    "browser": BROWSERS[self.var_browser.get()]}
        except ValueError:
            messagebox.showerror("Viral Finder", "Проверь числа на странице «Настройки».")
            return
        ACCOUNTS_PATH.write_text(self.txt_accounts.get("1.0", "end").strip(), encoding="utf-8")

        self.session_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        opts["session_at"] = self.session_at
        con = open_db()
        con.execute("INSERT OR IGNORE INTO searches (session_at, accounts, found, stopped) "
                    "VALUES (?,?,0,0)", (self.session_at, len(accounts)))
        con.commit()
        con.close()

        self.stop_event.clear()
        self.session_found = 0
        self.running = True
        self.play.set_running(True)
        self.status_title.set("Ищу трендовые видео")
        self.status_sub.set("Подготовка…")
        self._set_progress(0.02)
        self.log("Поиск запущен...")
        self._rebuild_list()
        self.worker = threading.Thread(target=self._run, args=(accounts, opts), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.status_sub.set("Останавливаю…")
        self.log("Останавливаю...")

    def _run(self, accounts, opts):
        total = 0
        con = open_db()
        try:
            base = {"quiet": True, "no_warnings": True, "skip_download": True,
                    "logger": SilentLogger()}
            if opts["browser"]:
                base["cookiesfrombrowser"] = (opts["browser"],)
            flat = YoutubeDL({**base, "extract_flat": True, "playlistend": opts["limit"]})
            full = YoutubeDL(base)
            log = lambda m: self.q.put(("log", m))
            found = lambda r: self.q.put(("found", r))
            n = len(accounts)
            for i, (url, manual) in enumerate(accounts, 1):
                if self.stop_event.is_set():
                    break
                self.q.put(("status", (short_name(url), i, n)))
                try:
                    total += scan_account(con, url, manual, opts, flat, full,
                                          log, found, self.stop_event)
                except Exception as e:
                    log(f"   ! ошибка: {str(e)[:200]}")
                self.q.put(("progress", i / n))
        except Exception as e:
            self.q.put(("log", f"Ошибка запуска: {str(e)[:300]}"))
        finally:
            try:
                con.execute("UPDATE searches SET found=?, stopped=? WHERE session_at=?",
                            (total, int(self.stop_event.is_set()), opts["session_at"]))
                con.commit()
            except Exception:
                pass
            con.close()
            self.q.put(("done", total))

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.log(data)
                elif kind == "found":
                    self.session_found += 1
                    self._dirty = True
                    self.status_sub.set(f"Найдено новых: {self.session_found}")
                elif kind == "status":
                    name, i, n = data
                    self.status_title.set("Ищу трендовые видео")
                    self.status_sub.set(f"Проверяю {name} · {i} из {n} · найдено: {self.session_found}")
                elif kind == "progress":
                    self._set_progress(data)
                elif kind == "done":
                    stopped = self.stop_event.is_set()
                    self.running = False
                    self._dirty = True
                    self.log(f"\nГотово. Новых трендовых видео: {data}")
                    self.status_title.set("Поиск остановлен" if stopped else "Поиск завершён")
                    self.status_sub.set(f"Новых находок: {data}")
                    self.play.set_running(False)
                    self._set_progress(0 if stopped else 1)
                    if data and not stopped:
                        self.show_page("results")
        except queue.Empty:
            pass
        if self._dirty:
            self._dirty = False
            self._rebuild_list()
        self.after(100, self._poll)

    def _is_group(self, iid):
        return "group" in self.tree.item(iid, "tags")

    def _open_link(self, _event):
        sel = self.tree.selection()
        if sel and not self._is_group(sel[0]):
            webbrowser.open(str(self.tree.item(sel[0], "values")[3]))

    def _links(self):
        out = []

        def collect(iid):
            if self._is_group(iid):
                for ch in self.tree.get_children(iid):
                    collect(ch)
            else:
                link = str(self.tree.item(iid, "values")[3])
                if link and link not in out:
                    out.append(link)

        for iid in (self.tree.selection() or self.tree.get_children()):
            collect(iid)
        return out

    def copy_links(self):
        links = self._links()
        self.clipboard_clear()
        self.clipboard_append("\n".join(links))
        self.status_title.set("Ссылки скопированы")
        self.status_sub.set(f"Скопировано ссылок: {len(links)}")

    def export_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")],
                                            initialfile="viral_videos.csv")
        if not path:
            return
        con = open_db()
        rows = con.execute(
            "SELECT COALESCE(session_at, found_at || ':00') AS s, account, url, views, "
            "followers, ratio, found_at FROM reported ORDER BY s DESC, ratio DESC").fetchall()
        con.close()
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Поиск (дата и время)", "Аккаунт", "Ссылка на видео",
                        "Просмотры", "Подписчики", "x", "Найдено"])
            for s_at, acc, link, views, followers, ratio, found_at in rows:
                w.writerow([s_at, short_name(acc), link, views, followers,
                            f"{ratio:.1f}", found_at])
        self.status_title.set("Файл сохранён")
        self.status_sub.set(path)

    def reset_history(self):
        if not messagebox.askyesno("Viral Finder",
                                   "Удалить всю историю поисков и находок?\n"
                                   "После этого видео могут показаться повторно."):
            return
        con = open_db()
        con.executescript("DELETE FROM checked; DELETE FROM reported; DELETE FROM searches;")
        con.commit()
        con.close()
        self.group_iids = {}
        self._rebuild_list()
        self.status_title.set("История очищена")
        self.status_sub.set("Память о проверенных видео сброшена")

    def _on_close(self):
        try:
            ACCOUNTS_PATH.write_text(self.txt_accounts.get("1.0", "end").strip(), encoding="utf-8")
        except Exception:
            pass
        self.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    try:  # чёткий текст на экранах с масштабированием
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()
