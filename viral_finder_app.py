#!/usr/bin/env python3
"""
Viral Finder - десктопное приложение для Windows.
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
from datetime import datetime
from pathlib import Path

# В режиме --windowed у exe нет консоли: заглушки, чтобы библиотеки не падали
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import tkinter as tk
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
            views INTEGER, followers INTEGER, ratio REAL, found_at TEXT);
    """)
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
            con.execute("INSERT INTO reported VALUES (?,?,?,?,?,?,?)",
                        (vid, link, url, views, followers, ratio, found_at))
            on_found((url, views, followers, ratio, link, found_at))
            count += 1
        con.commit()
    if count == 0:
        log("   новых трендовых видео нет")
    return count


# ---------------- интерфейс ----------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Viral Finder - поиск трендовых видео")
        self.geometry("1020x760")
        self.minsize(860, 600)
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self._build()
        self._load_history()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)

    def _build(self):
        pad = {"padx": 10, "pady": 4}

        ttk.Label(self, text="Ссылки на аккаунты TikTok / каналы YouTube / профили Instagram "
                             "(по одной в строке; если нужно: ссылка | подписчики)"
                  ).pack(anchor="w", **pad)
        self.txt_accounts = tk.Text(self, height=7, wrap="none")
        self.txt_accounts.pack(fill="x", padx=10)
        if ACCOUNTS_PATH.exists():
            self.txt_accounts.insert("1.0", ACCOUNTS_PATH.read_text(encoding="utf-8"))

        opts = ttk.Frame(self)
        opts.pack(fill="x", **pad)
        self.var_mult = tk.StringVar(value="140")
        self.var_limit = tk.StringVar(value="50")
        self.var_full = tk.BooleanVar(value=False)
        self.var_browser = tk.StringVar(value="Нет")
        ttk.Label(opts, text="Просмотры ≥ подписчики ×").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=100000, width=7, textvariable=self.var_mult).pack(side="left", padx=(4, 14))
        ttk.Label(opts, text="Последних видео:").pack(side="left")
        ttk.Spinbox(opts, from_=5, to=500, width=5, textvariable=self.var_limit).pack(side="left", padx=(4, 14))
        ttk.Label(opts, text="Куки из браузера (для Instagram):").pack(side="left")
        ttk.Combobox(opts, values=list(BROWSERS), textvariable=self.var_browser,
                     width=9, state="readonly").pack(side="left", padx=(4, 14))
        ttk.Checkbutton(opts, text="Перепроверить всё заново",
                        variable=self.var_full).pack(side="left")

        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        self.btn_start = ttk.Button(btns, text="▶ Найти", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(btns, text="■ Стоп", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(btns, text="Копировать ссылки", command=self.copy_links).pack(side="left", padx=6)
        ttk.Button(btns, text="Экспорт в CSV", command=self.export_csv).pack(side="left")
        ttk.Button(btns, text="Забыть историю", command=self.reset_history).pack(side="right")

        cols = ("account", "views", "followers", "ratio", "link", "found")
        heads = ("Аккаунт", "Просмотры", "Подписчики", "×", "Ссылка на видео", "Найдено")
        widths = (220, 90, 90, 60, 360, 110)
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=12,
                                 selectmode="extended")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=4)
        self.tree.bind("<Double-1>", self._open_link)
        ttk.Label(self, text="Двойной клик по строке - открыть видео в браузере").pack(anchor="w", padx=10)

        self.log_box = tk.Text(self, height=8, state="disabled", bg="#f4f4f4")
        self.log_box.pack(fill="x", padx=10, pady=(4, 10))

    # ---- действия ----
    def log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _load_history(self):
        con = open_db()
        rows = con.execute("SELECT account, views, followers, ratio, url, found_at "
                           "FROM reported ORDER BY found_at DESC").fetchall()
        con.close()
        for r in rows:
            self._add_row(r, top=False)

    def _add_row(self, r, top=True):
        account, views, followers, ratio, link, found_at = r
        self.tree.insert("", 0 if top else "end",
                         values=(account, f"{views:,}", f"{followers:,}",
                                 f"{ratio:,.0f}", link, found_at))

    def start(self):
        accounts = parse_accounts(self.txt_accounts.get("1.0", "end"))
        if not accounts:
            messagebox.showinfo("Viral Finder", "Вставь хотя бы одну ссылку.")
            return
        try:
            opts = {"mult": float(self.var_mult.get()),
                    "limit": int(self.var_limit.get()),
                    "full": self.var_full.get(),
                    "browser": BROWSERS[self.var_browser.get()]}
        except ValueError:
            messagebox.showerror("Viral Finder", "Проверь числа в настройках.")
            return
        ACCOUNTS_PATH.write_text(self.txt_accounts.get("1.0", "end").strip(), encoding="utf-8")
        self.stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.log("Поиск запущен...")
        self.worker = threading.Thread(target=self._run, args=(accounts, opts), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
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
            for url, manual in accounts:
                if self.stop_event.is_set():
                    break
                try:
                    total += scan_account(con, url, manual, opts, flat, full,
                                          log, found, self.stop_event)
                except Exception as e:
                    log(f"   ! ошибка: {str(e)[:200]}")
        except Exception as e:
            self.q.put(("log", f"Ошибка запуска: {str(e)[:300]}"))
        finally:
            con.close()
            self.q.put(("done", total))

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.log(data)
                elif kind == "found":
                    self._add_row(data)
                elif kind == "done":
                    self.log(f"\nГотово. Новых трендовых видео: {data}")
                    self.btn_start.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _open_link(self, _event):
        sel = self.tree.selection()
        if sel:
            webbrowser.open(self.tree.item(sel[0], "values")[4])

    def _links(self):
        items = self.tree.selection() or self.tree.get_children()
        return [self.tree.item(i, "values")[4] for i in items]

    def copy_links(self):
        links = self._links()
        self.clipboard_clear()
        self.clipboard_append("\n".join(links))
        self.log(f"Скопировано ссылок: {len(links)}")

    def export_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")],
                                            initialfile="viral_videos.csv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Аккаунт", "Просмотры", "Подписчики", "x", "Ссылка", "Найдено"])
            for i in self.tree.get_children():
                w.writerow(self.tree.item(i, "values"))
        self.log(f"Сохранено: {path}")

    def reset_history(self):
        if not messagebox.askyesno("Viral Finder",
                                   "Забыть всё проверенное и найденное?\n"
                                   "После этого видео могут показаться повторно."):
            return
        con = open_db()
        con.executescript("DELETE FROM checked; DELETE FROM reported;")
        con.commit()
        con.close()
        self.tree.delete(*self.tree.get_children())
        self.log("История очищена.")

    def _on_close(self):
        try:
            ACCOUNTS_PATH.write_text(self.txt_accounts.get("1.0", "end").strip(), encoding="utf-8")
        except Exception:
            pass
        self.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
