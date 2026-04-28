import customtkinter as ctk
import csv
import json
import os
import shutil
import subprocess
import sys
import time
import threading
from datetime import datetime, date, timedelta
from tkinter import filedialog
import calendar as cal_module

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

COLORS = {
    "bg":        "#0f1117",
    "panel":     "#1a1d27",
    "card":      "#21253a",
    "active":    "#3b82f6",
    "active_bg": "#1e3a5f",
    "break_":    "#10b981",
    "break_bg":  "#0d3325",
    "text":      "#e2e8f0",
    "muted":     "#64748b",
    "border":    "#2d3148",
    "danger":    "#ef4444",
    "success":   "#22c55e",
    "warning":   "#f59e0b",
}

def _data_dir() -> str:
    """Where to store the CSV + settings.

    - When running as a PyInstaller-bundled app, ``__file__`` lives in a
      temp extraction folder that's wiped on each launch — useless for
      persistence. So we use a stable per-user location instead.
    - When running the .py directly (development), we keep the existing
      behaviour: files live next to the script.

    The bundled-app location is ``~/Documents/TimeTracker`` — easy to find
    in Finder, easy to copy/back-up, easy to move between laptops.
    """
    if getattr(sys, "frozen", False):
        path = os.path.join(os.path.expanduser("~"), "Documents", "TimeTracker")
    else:
        path = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(path, exist_ok=True)
    return path


DATA_DIR = _data_dir()
CSV_FILE = os.path.join(DATA_DIR, "time_tracker_log.csv")
SETTINGS_FILE = os.path.join(DATA_DIR, "time_tracker_settings.json")
CSV_HEADERS = ["date", "task", "seconds", "hms", "notes", "exported_at"]
BREAK_NAME = "☕  Break"
AUTO_SAVE_MS = 5 * 60 * 1000
DEFAULT_TARGET_HOURS = 8.0


def _migrate_legacy_data() -> None:
    """One-time migration for users who upgrade from an older .py-only run.

    If the bundled app starts and ``DATA_DIR`` has no CSV, but a legacy CSV
    exists alongside the source script's typical locations, copy it in so
    the user's history survives the move to a packaged app.
    """
    if os.path.isfile(CSV_FILE):
        return
    # Common legacy locations: the timer/ folder where the script lived
    candidates = [
        os.path.join(os.path.expanduser("~"), "Desktop",
                     "python learning", "timer", "time_tracker_log.csv"),
    ]
    for src in candidates:
        if os.path.isfile(src):
            try:
                shutil.copy2(src, CSV_FILE)
                # Also copy settings if present
                src_settings = os.path.join(os.path.dirname(src),
                                            "time_tracker_settings.json")
                if os.path.isfile(src_settings) and not os.path.isfile(SETTINGS_FILE):
                    shutil.copy2(src_settings, SETTINGS_FILE)
            except OSError:
                pass
            break


_migrate_legacy_data()


def fmt(secs: int) -> str:
    h, r = divmod(abs(secs), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _clamp_scrollable(scroll_frame, *, axis: str = "y"):
    """Take over wheel-scroll handling on a CTkScrollableFrame.

    Solves four problems with the default behaviour:
      1. **Over-scroll bounce** at top/bottom (or left/right).
      2. **Scrollbar hover hijack**: trackpad scrolling while hovering on
         the scrollbar widget moves the page erratically.
      3. **No-op when content fits**: wheel events still nudge the canvas.
      4. **Shift+wheel** sometimes fires for horizontal trackpad gestures.

    We bind a custom handler on EVERY descendant of the scroll frame —
    crucially including the inner ``tk.Scrollbar`` whose class-level
    ``<MouseWheel>`` binding would otherwise fire first. Returning
    ``"break"`` then stops class-level handlers from also running.

    Pass ``axis="x"`` for horizontal scrollable frames.
    """
    try:
        canvas = scroll_frame._parent_canvas
    except AttributeError:
        return

    horizontal = axis == "x"

    def _view():
        try:
            return canvas.xview() if horizontal else canvas.yview()
        except Exception:
            return (0.0, 1.0)

    def _scroll(units: int):
        try:
            if horizontal:
                canvas.xview_scroll(units, "units")
            else:
                canvas.yview_scroll(units, "units")
        except Exception:
            pass

    def _direction_from_event(event):
        if getattr(event, "num", None) == 4:
            return -1
        if getattr(event, "num", None) == 5:
            return 1
        d = getattr(event, "delta", 0)
        if d == 0:
            return None
        # macOS gives small deltas (smooth trackpad); Windows gives ±120.
        return -1 if d > 0 else 1

    def _on_wheel(event):
        first, last = _view()
        if first <= 0.0 and last >= 1.0:
            # Content fits — there is nothing to scroll.
            return "break"
        direction = _direction_from_event(event)
        if direction is None:
            return "break"
        if direction < 0 and first <= 0.0001:
            return "break"
        if direction > 0 and last >= 0.9999:
            return "break"
        _scroll(direction)
        return "break"

    EVENTS = ("<MouseWheel>", "<Shift-MouseWheel>",
              "<Button-4>", "<Button-5>",
              "<Shift-Button-4>", "<Shift-Button-5>")
    # Events that on macOS get hijacked by CTkScrollbar's drag handler when
    # the trackpad is used over the scrollbar — we kill these on the
    # scrollbar widget tree only.
    DEAD_EVENTS = EVENTS + ("<Button-1>", "<B1-Motion>", "<ButtonRelease-1>")

    sb_widget = getattr(scroll_frame, "_scrollbar", None)

    def _is_in_scrollbar(widget) -> bool:
        if sb_widget is None:
            return False
        w = widget
        while w is not None:
            if w is sb_widget:
                return True
            try:
                w = w.master
            except Exception:
                break
        return False

    def _bind_recursive(widget):
        # Don't bind our scroll handler on widgets inside the scrollbar —
        # the scrollbar is killed below to prevent macOS trackpad hijack.
        if _is_in_scrollbar(widget):
            return
        try:
            for ev in EVENTS:
                widget.bind(ev, _on_wheel)
        except Exception:
            pass
        try:
            for child in widget.winfo_children():
                _bind_recursive(child)
        except Exception:
            pass

    def _kill_scrollbar_input(widget):
        """Make the scrollbar widget tree completely non-interactive.

        On macOS, hovering CTkScrollbar and using a two-finger trackpad
        gesture causes the inner canvas to interpret the gesture as a
        thumb drag, scrolling the page erratically. Killing all input
        events on the scrollbar prevents this. Trackpad scrolling on the
        content area continues to work via our handler above.
        """
        try:
            for ev in DEAD_EVENTS:
                widget.bind(ev, lambda _e: "break")
        except Exception:
            pass
        try:
            for child in widget.winfo_children():
                _kill_scrollbar_input(child)
        except Exception:
            pass

    # 1. Bind our wheel handler on the content side (canvas + content frame).
    _bind_recursive(scroll_frame)
    _bind_recursive(canvas)

    # 2. Neutralize the scrollbar widget tree so it can't hijack gestures.
    if sb_widget is not None:
        _kill_scrollbar_input(sb_widget)

    def _reclamp():
        _bind_recursive(scroll_frame)
        _bind_recursive(canvas)
        if sb_widget is not None:
            _kill_scrollbar_input(sb_widget)

    scroll_frame._reclamp = _reclamp


def parse_hms(s: str) -> int | None:
    """Parse 'HH:MM:SS' / 'MM:SS' / 'H' / float-hours into seconds.
    Returns None if unparseable or negative."""
    s = s.strip()
    if not s:
        return None
    # Colon form
    if ":" in s:
        parts = s.split(":")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return None
        if any(n < 0 for n in nums):
            return None
        if len(nums) == 3:
            h, m, sec = nums
        elif len(nums) == 2:
            h, m, sec = 0, nums[0], nums[1]
        else:
            return None
        if m >= 60 or sec >= 60:
            return None
        return h * 3600 + m * 60 + sec
    # Plain number → treat as hours (float)
    try:
        hours = float(s)
    except ValueError:
        return None
    if hours < 0:
        return None
    return int(round(hours * 3600))


def load_settings() -> dict:
    if not os.path.isfile(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(data: dict) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


# ── CSV helpers ────────────────────────────────────────────────────────────────

def load_all_csv() -> list[dict]:
    if not os.path.isfile(CSV_FILE):
        return []
    try:
        with open(CSV_FILE, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def load_today_from_csv() -> dict[str, dict]:
    """Return today's data as {task: {"seconds": int, "notes": str}}."""
    today = date.today().isoformat()
    out: dict[str, dict] = {}
    for row in load_all_csv():
        if row.get("date") == today:
            try:
                secs = int(row["seconds"])
            except (ValueError, KeyError, TypeError):
                continue
            out[row["task"]] = {
                "seconds": secs,
                "notes": (row.get("notes") or "").strip(),
            }
    return out


def rows_for_day(rows: list[dict], ref: date) -> list[dict]:
    """Raw CSV rows whose date matches ref."""
    target = ref.isoformat()
    return [r for r in rows if r.get("date") == target]


def get_history_task_names() -> list[str]:
    """Unique task names from history (newest first), excluding Break."""
    seen: set[str] = set()
    names: list[str] = []
    for row in reversed(load_all_csv()):
        name = row.get("task", "").strip()
        if name and name != BREAK_NAME and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def save_csv(rows: list[dict]):
    """Overwrite today's rows; preserve all other days."""
    today = date.today().isoformat()
    other_days: list[dict] = []
    if os.path.isfile(CSV_FILE):
        try:
            with open(CSV_FILE, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("date") != today:
                        other_days.append(row)
        except OSError:
            pass
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=CSV_HEADERS, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(other_days)
        writer.writerows(rows)


def save_full_csv(rows: list[dict]):
    """Overwrite the entire CSV with the given rows (used by dashboard edit/delete)."""
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=CSV_HEADERS, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)


# ── Dashboard helpers ──────────────────────────────────────────────────────────

def aggregate_for_period(rows: list[dict], period: str, ref: date) -> dict[str, int]:
    if period == "day":
        dates = {ref.isoformat()}
    elif period == "week":
        monday = ref - timedelta(days=ref.weekday())
        dates = {(monday + timedelta(days=i)).isoformat() for i in range(7)}
    else:
        days = cal_module.monthrange(ref.year, ref.month)[1]
        start = date(ref.year, ref.month, 1)
        dates = {(start + timedelta(days=i)).isoformat() for i in range(days)}

    # Deduplicate per (date, task): keep the largest value for that day
    # (seconds only grow within a day, so max == latest cumulative value).
    per_day_task: dict[tuple[str, str], int] = {}
    for row in rows:
        d = row.get("date")
        task = row.get("task", "")
        if d not in dates or not task:
            continue
        try:
            secs = int(row.get("seconds", 0))
        except (ValueError, TypeError):
            secs = 0
        key = (d, task)
        if secs > per_day_task.get(key, -1):
            per_day_task[key] = secs

    totals: dict[str, int] = {}
    for (_d, task), secs in per_day_task.items():
        totals[task] = totals.get(task, 0) + secs
    return totals


def period_label(period: str, ref: date) -> str:
    if period == "day":
        return ref.strftime("%a, %d %b %Y")
    if period == "week":
        monday = ref - timedelta(days=ref.weekday())
        sunday = monday + timedelta(days=6)
        if monday.month == sunday.month:
            return f"{monday.strftime('%d')}–{sunday.strftime('%d %b %Y')}"
        return f"{monday.strftime('%d %b')} – {sunday.strftime('%d %b %Y')}"
    return ref.strftime("%B %Y")


def advance_ref(period: str, ref: date, delta: int) -> date:
    if period == "day":
        return ref + timedelta(days=delta)
    if period == "week":
        return ref + timedelta(weeks=delta)
    month = ref.month + delta
    year = ref.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(ref.day, cal_module.monthrange(year, month)[1])
    return date(year, month, day)


# ── TrackerState ───────────────────────────────────────────────────────────────

class TrackerState:
    def __init__(self):
        self.tasks: dict[str, int] = {}
        self.notes: dict[str, str] = {}
        self.active: str | None = None
        self._tick_start: float | None = None
        self.lock = threading.Lock()

    def add_task(self, name: str, initial_secs: int = 0, initial_notes: str = ""):
        with self.lock:
            if name not in self.tasks:
                self.tasks[name] = initial_secs
            if initial_notes and not self.notes.get(name):
                self.notes[name] = initial_notes

    def set_seconds(self, name: str, secs: int):
        """Manually set a task's accumulated seconds, safely w.r.t. live ticking."""
        with self.lock:
            if name not in self.tasks:
                return
            if self.active == name:
                # Drop the in-progress segment and restart the clock from the new base
                self._tick_start = time.monotonic()
            self.tasks[name] = max(0, int(secs))

    def set_notes(self, name: str, text: str):
        with self.lock:
            if name in self.tasks:
                self.notes[name] = text

    def get_notes(self, name: str) -> str:
        with self.lock:
            return self.notes.get(name, "")

    def remove_task(self, name: str):
        with self.lock:
            if self.active == name:
                self._flush()
                self.active = None
            self.tasks.pop(name, None)
            self.notes.pop(name, None)

    def select(self, name: str):
        with self.lock:
            if name not in self.tasks or self.active == name:
                return
            self._flush()
            self.active = name
            self._tick_start = time.monotonic()

    def deselect(self):
        with self.lock:
            self._flush()
            self.active = None

    def pause_active(self):
        with self.lock:
            self._flush()  # commit elapsed; _tick_start becomes None → paused

    def resume_active(self):
        with self.lock:
            if self.active and self._tick_start is None:
                self._tick_start = time.monotonic()

    def is_ticking(self) -> bool:
        with self.lock:
            return self.active is not None and self._tick_start is not None

    def _flush(self):
        """Commit live elapsed to tasks dict and stop the clock."""
        if self.active and self._tick_start is not None:
            self.tasks[self.active] = (
                self.tasks.get(self.active, 0)
                + int(time.monotonic() - self._tick_start)
            )
            self._tick_start = None

    def elapsed(self, name: str) -> int:
        with self.lock:
            base = self.tasks.get(name, 0)
            if self.active == name and self._tick_start is not None:
                return base + int(time.monotonic() - self._tick_start)
            return base

    def reset_all(self):
        with self.lock:
            for k in self.tasks:
                self.tasks[k] = 0
            self.active = None
            self._tick_start = None

    def snapshot_rows(self) -> list[dict]:
        """Non-destructive snapshot — active timer keeps running."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today = date.today().isoformat()
        rows = []
        with self.lock:
            for name, base in self.tasks.items():
                secs = base
                if name == self.active and self._tick_start is not None:
                    secs += int(time.monotonic() - self._tick_start)
                rows.append({
                    "date": today,
                    "task": name,
                    "seconds": secs,
                    "hms": fmt(secs),
                    "notes": self.notes.get(name, ""),
                    "exported_at": now_str,
                })
        return rows


# ── Edit popups ────────────────────────────────────────────────────────────────

class EditTimePopup(ctk.CTkToplevel):
    """Shared popup: edit a task's time (and optionally rename it)."""
    def __init__(self, master, *, title: str, current_secs: int,
                 current_name: str | None = None, allow_rename: bool = False,
                 on_save):
        super().__init__(master)
        self.title(title)
        self.geometry("360x260" if allow_rename else "360x210")
        self.resizable(False, False)
        self.configure(fg_color=COLORS["bg"])
        self.lift()
        self.focus_force()
        self.grab_set()
        self._on_save = on_save
        self._allow_rename = allow_rename

        ctk.CTkLabel(self, text=title, font=("Outfit", 16, "bold"),
                     text_color=COLORS["text"]).pack(pady=(18, 12))

        if allow_rename:
            ctk.CTkLabel(self, text="Task name", font=("Outfit", 11),
                         text_color=COLORS["muted"], anchor="w").pack(
                fill="x", padx=24)
            self.name_entry = ctk.CTkEntry(
                self, font=("Outfit", 13),
                fg_color=COLORS["card"], border_color=COLORS["border"],
                text_color=COLORS["text"], height=34)
            self.name_entry.pack(fill="x", padx=24, pady=(2, 10))
            if current_name:
                self.name_entry.insert(0, current_name)
        else:
            self.name_entry = None

        ctk.CTkLabel(self, text="Time  (HH:MM:SS  or  hours)",
                     font=("Outfit", 11), text_color=COLORS["muted"],
                     anchor="w").pack(fill="x", padx=24)
        self.time_entry = ctk.CTkEntry(
            self, font=("JetBrains Mono", 14),
            fg_color=COLORS["card"], border_color=COLORS["border"],
            text_color=COLORS["text"], height=34)
        self.time_entry.pack(fill="x", padx=24, pady=(2, 4))
        self.time_entry.insert(0, fmt(current_secs))
        self.time_entry.bind("<Return>", lambda _: self._save())

        self.error_lbl = ctk.CTkLabel(self, text="", font=("Outfit", 10),
                                      text_color=COLORS["danger"])
        self.error_lbl.pack(pady=(0, 4))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(pady=(4, 16))
        ctk.CTkButton(btns, text="Cancel", width=110, height=34,
                      font=("Outfit", 12),
                      fg_color=COLORS["card"], border_color=COLORS["border"],
                      border_width=1, text_color=COLORS["muted"],
                      hover_color="#2d1f1f",
                      command=self.destroy).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="Save", width=110, height=34,
                      font=("Outfit", 12, "bold"),
                      fg_color=COLORS["active"],
                      command=self._save).pack(side="left", padx=6)

        self.after(50, self.time_entry.focus_set)

    def _save(self):
        secs = parse_hms(self.time_entry.get())
        if secs is None:
            self.error_lbl.configure(text="Invalid time. Use HH:MM:SS or hours.")
            return
        new_name = None
        if self._allow_rename:
            new_name = (self.name_entry.get() or "").strip()
            if not new_name:
                self.error_lbl.configure(text="Task name cannot be empty.")
                return
        try:
            self._on_save(secs, new_name)
        except Exception as e:
            self.error_lbl.configure(text=str(e)[:60])
            return
        self.destroy()


class NotesPopup(ctk.CTkToplevel):
    """Multi-line notes editor / viewer."""
    def __init__(self, master, *, title: str, current_text: str,
                 read_only: bool = False, on_save=None):
        super().__init__(master)
        self.title(title)
        self.geometry("500x420")
        self.minsize(380, 300)
        self.configure(fg_color=COLORS["bg"])
        self.lift()
        self.focus_force()
        self.grab_set()
        self._on_save = on_save
        self._read_only = read_only

        ctk.CTkLabel(self, text=title, font=("Outfit", 15, "bold"),
                     text_color=COLORS["text"]).pack(pady=(16, 4), padx=20, anchor="w")

        if not read_only:
            ctk.CTkLabel(self,
                         text="Write what you worked on — multiple lines OK.",
                         font=("Outfit", 10),
                         text_color=COLORS["muted"]).pack(padx=20, anchor="w")

        self.textbox = ctk.CTkTextbox(
            self, font=("Outfit", 12),
            fg_color=COLORS["card"], border_color=COLORS["border"],
            border_width=1, text_color=COLORS["text"],
            wrap="word")
        self.textbox.pack(fill="both", expand=True, padx=20, pady=10)
        if current_text:
            self.textbox.insert("1.0", current_text)
        if read_only:
            self.textbox.configure(state="disabled")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(pady=(0, 14))
        if read_only:
            ctk.CTkButton(btns, text="Close", width=140, height=36,
                          font=("Outfit", 12, "bold"),
                          fg_color=COLORS["active"],
                          command=self.destroy).pack(padx=4)
        else:
            ctk.CTkButton(btns, text="Cancel", width=110, height=34,
                          font=("Outfit", 12),
                          fg_color=COLORS["card"], border_color=COLORS["border"],
                          border_width=1, text_color=COLORS["muted"],
                          hover_color="#2d1f1f",
                          command=self.destroy).pack(side="left", padx=6)
            ctk.CTkButton(btns, text="Save", width=110, height=34,
                          font=("Outfit", 12, "bold"),
                          fg_color=COLORS["active"],
                          command=self._save).pack(side="left", padx=6)
            self.bind("<Control-Return>", lambda _: self._save())
            self.bind("<Command-Return>", lambda _: self._save())

        self.after(50, self.textbox.focus_set)

    def _save(self):
        text = self.textbox.get("1.0", "end").rstrip()
        if self._on_save:
            self._on_save(text)
        self.destroy()


class GoalEditPopup(ctk.CTkToplevel):
    """Edit the daily work-hour target."""
    def __init__(self, master, current_hours: float, on_save):
        super().__init__(master)
        self.title("Daily Goal")
        self.geometry("320x190")
        self.resizable(False, False)
        self.configure(fg_color=COLORS["bg"])
        self.lift()
        self.focus_force()
        self.grab_set()
        self._on_save = on_save

        ctk.CTkLabel(self, text="Daily work-hour target",
                     font=("Outfit", 14, "bold"),
                     text_color=COLORS["text"]).pack(pady=(18, 6))
        ctk.CTkLabel(self, text="(0.5 – 24 hours, e.g. 8 or 7.5)",
                     font=("Outfit", 10), text_color=COLORS["muted"]).pack()

        self.entry = ctk.CTkEntry(
            self, font=("JetBrains Mono", 14),
            fg_color=COLORS["card"], border_color=COLORS["border"],
            text_color=COLORS["text"], height=34, justify="center")
        self.entry.pack(fill="x", padx=40, pady=(8, 4))
        self.entry.insert(0, f"{current_hours:g}")
        self.entry.bind("<Return>", lambda _: self._save())

        self.error_lbl = ctk.CTkLabel(self, text="", font=("Outfit", 10),
                                      text_color=COLORS["danger"])
        self.error_lbl.pack()

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(pady=(4, 12))
        ctk.CTkButton(btns, text="Cancel", width=90, height=32,
                      font=("Outfit", 12),
                      fg_color=COLORS["card"], border_color=COLORS["border"],
                      border_width=1, text_color=COLORS["muted"],
                      hover_color="#2d1f1f",
                      command=self.destroy).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Save", width=90, height=32,
                      font=("Outfit", 12, "bold"),
                      fg_color=COLORS["active"],
                      command=self._save).pack(side="left", padx=4)

        self.after(50, self.entry.focus_set)

    def _save(self):
        try:
            hours = float(self.entry.get().strip())
        except ValueError:
            self.error_lbl.configure(text="Enter a number.")
            return
        if not (0.5 <= hours <= 24):
            self.error_lbl.configure(text="Must be between 0.5 and 24.")
            return
        self._on_save(hours)
        self.destroy()


# ── TaskCard ───────────────────────────────────────────────────────────────────

class TaskCard(ctk.CTkFrame):
    def __init__(self, master, name: str, tracker: TrackerState,
                 on_select, on_remove, on_edit_time, on_notes_change,
                 is_break=False, **kw):
        super().__init__(master, corner_radius=12,
                         fg_color=COLORS["card"], border_width=1,
                         border_color=COLORS["border"], **kw)
        self.name = name
        self.tracker = tracker
        self.on_select = on_select
        self.on_remove = on_remove
        self.on_edit_time = on_edit_time
        self.on_notes_change = on_notes_change
        self.is_break = is_break
        self._active = False
        self._paused = False

        self.columnconfigure(0, weight=1)
        accent = COLORS["break_"] if is_break else COLORS["active"]

        self.name_lbl = ctk.CTkLabel(self, text=name, font=("Outfit", 14, "bold"),
                                     text_color=COLORS["text"], anchor="w")
        self.name_lbl.grid(row=0, column=0, padx=14, pady=(12, 0), sticky="ew")

        self.timer_lbl = ctk.CTkLabel(self, text="00:00:00",
                                      font=("JetBrains Mono", 26, "bold"),
                                      text_color=accent, cursor="hand2")
        self.timer_lbl.grid(row=1, column=0, padx=14, pady=(2, 0), sticky="w")
        self.timer_lbl.bind("<Button-1>", lambda _: self.on_edit_time(self.name))

        self.status_lbl = ctk.CTkLabel(self, text="● idle  ·  click time to edit",
                                       font=("Outfit", 11),
                                       text_color=COLORS["muted"])
        self.status_lbl.grid(row=2, column=0, padx=14, pady=(0, 6), sticky="w")

        # Notes preview row
        notes_row = ctk.CTkFrame(self, fg_color="transparent")
        notes_row.grid(row=3, column=0, columnspan=2,
                       padx=14, pady=(0, 12), sticky="ew")
        notes_row.columnconfigure(0, weight=1)

        self.notes_preview_lbl = ctk.CTkLabel(
            notes_row, text="No notes yet",
            font=("Outfit", 11), text_color=COLORS["muted"],
            anchor="w", justify="left", wraplength=320)
        self.notes_preview_lbl.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.notes_btn = ctk.CTkButton(
            notes_row, text="📝  Add Notes", width=120, height=28,
            font=("Outfit", 11, "bold"),
            fg_color=COLORS["card"], border_color=COLORS["border"],
            border_width=1, text_color=COLORS["text"],
            hover_color=COLORS["active_bg"],
            command=self._open_notes_popup
        )
        self.notes_btn.grid(row=0, column=1, sticky="e")
        self._notes_text = ""

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=0, column=1, rowspan=3, padx=(0, 10), pady=10, sticky="e")

        self.action_btn = ctk.CTkButton(
            btn_frame, text="▶  Start", width=100, height=32,
            font=("Outfit", 12, "bold"),
            fg_color=accent, hover_color=accent,
            command=self._toggle
        )
        self.action_btn.pack(pady=(0, 6))

        if not is_break:
            ctk.CTkButton(
                btn_frame, text="✕", width=100, height=28,
                font=("Outfit", 11),
                fg_color=COLORS["card"], hover_color=COLORS["danger"],
                border_color=COLORS["border"], border_width=1,
                text_color=COLORS["muted"],
                command=lambda: on_remove(name)
            ).pack()

    def set_notes_text(self, text: str):
        text = (text or "").strip()
        self._notes_text = text
        if text:
            # Show first line as preview, hint at more lines
            first_line = text.splitlines()[0]
            n_lines = len(text.splitlines())
            preview = first_line if len(first_line) <= 70 else first_line[:67] + "…"
            if n_lines > 1:
                preview = f"{preview}  (+{n_lines - 1} more)"
            self.notes_preview_lbl.configure(text=preview, text_color=COLORS["text"])
            self.notes_btn.configure(text="📝  Edit Notes")
        else:
            self.notes_preview_lbl.configure(text="No notes yet",
                                              text_color=COLORS["muted"])
            self.notes_btn.configure(text="📝  Add Notes")

    def _open_notes_popup(self):
        title = (f"Notes — {self.name}" if not self.is_break
                 else "Break notes")

        def on_save(text: str):
            self.set_notes_text(text)
            self.on_notes_change(self.name, text)

        NotesPopup(self.winfo_toplevel(), title=title,
                   current_text=self._notes_text, on_save=on_save)

    def _toggle(self):
        if self._active and not self._paused:
            self.on_select(None)       # stop/deselect
        else:
            self.on_select(self.name)  # start or resume

    def set_active(self, yes: bool, paused: bool = False):
        self._active = yes
        self._paused = paused
        accent = COLORS["break_"] if self.is_break else COLORS["active"]
        bg     = COLORS["break_bg"] if self.is_break else COLORS["active_bg"]
        if yes and not paused:
            self.configure(fg_color=bg, border_color=accent)
            self.action_btn.configure(text="⏸  Pause")
            self.status_lbl.configure(text="● running", text_color=accent)
        elif yes and paused:
            self.configure(fg_color=bg, border_color=COLORS["warning"])
            self.action_btn.configure(text="▶  Resume")
            self.status_lbl.configure(text="⏸ paused", text_color=COLORS["warning"])
        else:
            self.configure(fg_color=COLORS["card"], border_color=COLORS["border"])
            self.action_btn.configure(text="▶  Start")
            self.status_lbl.configure(text="● idle  ·  click time to edit",
                                      text_color=COLORS["muted"])

    def tick(self):
        self.timer_lbl.configure(text=fmt(self.tracker.elapsed(self.name)))


# ── Dashboard ──────────────────────────────────────────────────────────────────

class DashboardWindow(ctk.CTkToplevel):
    def __init__(self, master, on_history_changed=None):
        super().__init__(master)
        self.title("Dashboard — History")
        self.geometry("760x600")
        self.minsize(620, 460)
        self.configure(fg_color=COLORS["bg"])
        self.lift()
        self.focus_force()

        self._period = "day"
        self._ref = date.today()
        self._on_history_changed = on_history_changed or (lambda: None)

        self._build_ui()
        self._refresh()

    def _build_ui(self):
        # Header
        header = ctk.CTkFrame(self, fg_color=COLORS["panel"], corner_radius=0, height=56)
        header.pack(fill="x")
        header.pack_propagate(False)

        ctk.CTkLabel(header, text="📊  Dashboard",
                     font=("Outfit", 18, "bold"),
                     text_color=COLORS["text"]).pack(side="left", padx=20)

        self.seg = ctk.CTkSegmentedButton(
            header, values=["Day", "Week", "Month"],
            font=("Outfit", 12, "bold"),
            command=self._on_period
        )
        self.seg.set("Day")
        self.seg.pack(side="right", padx=20)

        # Navigation
        nav = ctk.CTkFrame(self, fg_color="transparent")
        nav.pack(fill="x", padx=16, pady=10)

        ctk.CTkButton(nav, text="◀", width=36, height=32,
                      fg_color=COLORS["card"], border_color=COLORS["border"],
                      border_width=1, font=("Outfit", 14),
                      command=lambda: self._nav(-1)).pack(side="left")

        self.period_lbl = ctk.CTkLabel(nav, text="",
                                        font=("Outfit", 14, "bold"),
                                        text_color=COLORS["text"])
        self.period_lbl.pack(side="left", padx=16)

        ctk.CTkButton(nav, text="▶", width=36, height=32,
                      fg_color=COLORS["card"], border_color=COLORS["border"],
                      border_width=1, font=("Outfit", 14),
                      command=lambda: self._nav(1)).pack(side="left")

        ctk.CTkButton(nav, text="Today", width=72, height=32,
                      fg_color=COLORS["active"], font=("Outfit", 12, "bold"),
                      command=self._go_today).pack(side="right")

        # Column headers — fixed minsize so SHARE bars line up across rows
        col_hdr = ctk.CTkFrame(self, fg_color=COLORS["panel"], corner_radius=0, height=30)
        col_hdr.pack(fill="x", padx=16)
        col_hdr.pack_propagate(False)
        col_hdr.columnconfigure(0, weight=0, minsize=180)
        col_hdr.columnconfigure(1, weight=1)
        col_hdr.columnconfigure(2, weight=0, minsize=240)

        for col, text, anchor, sticky, pad in [
            (0, "TASK",  "w",      "w",  (14, 4)),
            (1, "SHARE", "center", "ew", (8, 4)),
            (2, "TIME",  "e",      "e",  (14, 4)),
        ]:
            ctk.CTkLabel(col_hdr, text=text, font=("Outfit", 10, "bold"),
                         text_color=COLORS["muted"], anchor=anchor).grid(
                row=0, column=col, padx=pad[0], pady=pad[1], sticky=sticky)

        # Results
        self.results = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_button_color=COLORS["border"])
        self.results.pack(fill="both", expand=True, padx=16, pady=(4, 0))
        self.results.columnconfigure(0, weight=1)
        _clamp_scrollable(self.results)

        # Footer
        footer = ctk.CTkFrame(self, fg_color=COLORS["panel"], corner_radius=0, height=44)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        self.tasks_lbl = ctk.CTkLabel(footer, text="",
                                       font=("Outfit", 11), text_color=COLORS["muted"])
        self.tasks_lbl.pack(side="left", padx=20)
        self.total_lbl = ctk.CTkLabel(footer, text="Total: 00:00:00",
                                       font=("JetBrains Mono", 14, "bold"),
                                       text_color=COLORS["text"])
        self.total_lbl.pack(side="right", padx=20)

    def _on_period(self, value: str):
        self._period = value.lower()
        self._refresh()

    def _nav(self, delta: int):
        self._ref = advance_ref(self._period, self._ref, delta)
        self._refresh()

    def _go_today(self):
        self._ref = date.today()
        self._refresh()

    def _refresh(self):
        self.period_lbl.configure(text=period_label(self._period, self._ref))
        for w in self.results.winfo_children():
            w.destroy()

        all_rows = load_all_csv()

        if self._period == "day":
            self._render_day(all_rows)
        else:
            self._render_aggregate(all_rows)

        # Re-bind wheel handlers on the freshly-created row widgets
        if hasattr(self.results, "_reclamp"):
            self.results._reclamp()

    def _render_aggregate(self, all_rows: list[dict]):
        agg = aggregate_for_period(all_rows, self._period, self._ref)
        if not agg:
            self._render_empty()
            return

        total = sum(agg.values())
        sorted_tasks = sorted(agg.items(), key=lambda x: x[1], reverse=True)

        for i, (name, secs) in enumerate(sorted_tasks):
            is_break = name == BREAK_NAME
            accent = COLORS["break_"] if is_break else COLORS["active"]
            bg = COLORS["card"] if i % 2 == 0 else "#1e2235"

            row = ctk.CTkFrame(self.results, fg_color=bg, corner_radius=8)
            row.grid(row=i, column=0, sticky="ew", pady=2)
            self.results.columnconfigure(0, weight=1)
            row.columnconfigure(0, weight=0, minsize=180)
            row.columnconfigure(1, weight=1)
            row.columnconfigure(2, weight=0, minsize=240)

            ctk.CTkLabel(row, text=name, font=("Outfit", 13),
                         text_color=COLORS["text"], anchor="w").grid(
                row=0, column=0, padx=14, pady=8, sticky="w")

            bar_wrap = ctk.CTkFrame(row, fg_color="transparent")
            bar_wrap.grid(row=0, column=1, pady=8, sticky="ew")
            bar_wrap.columnconfigure(0, weight=1)

            ratio = secs / total if total > 0 else 0
            bar = ctk.CTkProgressBar(bar_wrap, height=8,
                                      progress_color=accent,
                                      fg_color=COLORS["border"])
            bar.grid(row=0, column=0, padx=8, sticky="ew")
            bar.set(ratio)

            ctk.CTkLabel(row, text=f"{fmt(secs)}  {ratio*100:.0f}%",
                         font=("JetBrains Mono", 12),
                         text_color=COLORS["text"], anchor="e",
                         width=120).grid(
                row=0, column=2, padx=14, pady=8, sticky="e")

        n = len(sorted_tasks)
        self.tasks_lbl.configure(text=f"{n} task{'s' if n != 1 else ''}")
        self.total_lbl.configure(text=f"Total: {fmt(total)}")

    def _render_day(self, all_rows: list[dict]):
        # Day view shows the raw CSV rows for the date — 1:1 mapping so each
        # row can be edited or deleted.
        day_rows = rows_for_day(all_rows, self._ref)
        # Deduplicate: if multiple rows exist for the same task on this day,
        # keep the one with the largest seconds (legacy data may have dupes).
        best: dict[str, dict] = {}
        for r in day_rows:
            try:
                secs = int(r.get("seconds", 0))
            except (ValueError, TypeError):
                continue
            t = r.get("task", "")
            if not t:
                continue
            if t not in best or secs > int(best[t].get("seconds", 0)):
                best[t] = r
        rows = sorted(best.values(),
                      key=lambda r: int(r.get("seconds", 0)), reverse=True)

        if not rows:
            self._render_empty()
            return

        total = sum(int(r.get("seconds", 0)) for r in rows)

        for i, r in enumerate(rows):
            name = r["task"]
            secs = int(r.get("seconds", 0))
            notes = (r.get("notes") or "").strip()
            is_break = name == BREAK_NAME
            accent = COLORS["break_"] if is_break else COLORS["active"]
            bg = COLORS["card"] if i % 2 == 0 else "#1e2235"

            row = ctk.CTkFrame(self.results, fg_color=bg, corner_radius=8)
            row.grid(row=i, column=0, sticky="ew", pady=2)
            row.columnconfigure(0, weight=0, minsize=180)
            row.columnconfigure(1, weight=1)
            row.columnconfigure(2, weight=0, minsize=240)

            label_block = ctk.CTkFrame(row, fg_color="transparent")
            label_block.grid(row=0, column=0, padx=14, pady=6, sticky="w")
            ctk.CTkLabel(label_block, text=name, font=("Outfit", 13),
                         text_color=COLORS["text"], anchor="w").pack(anchor="w")
            if notes:
                first_line = notes.splitlines()[0] if notes else ""
                n_lines = len(notes.splitlines())
                preview = first_line if len(first_line) <= 70 else first_line[:67] + "…"
                if n_lines > 1:
                    preview = f"{preview}  (+{n_lines - 1} more)"
                ctk.CTkLabel(label_block, text=preview,
                             font=("Outfit", 10),
                             text_color=COLORS["muted"], anchor="w").pack(anchor="w")

            bar_wrap = ctk.CTkFrame(row, fg_color="transparent")
            bar_wrap.grid(row=0, column=1, pady=8, sticky="ew")
            bar_wrap.columnconfigure(0, weight=1)
            ratio = secs / total if total > 0 else 0
            bar = ctk.CTkProgressBar(bar_wrap, height=8,
                                      progress_color=accent,
                                      fg_color=COLORS["border"])
            bar.grid(row=0, column=0, padx=8, sticky="ew")
            bar.set(ratio)

            right = ctk.CTkFrame(row, fg_color="transparent")
            right.grid(row=0, column=2, padx=(0, 8), pady=6, sticky="e")
            ctk.CTkLabel(right, text=f"{fmt(secs)}  {ratio*100:.0f}%",
                         font=("JetBrains Mono", 12),
                         text_color=COLORS["text"], anchor="e").pack(side="left",
                                                                      padx=(0, 6))
            # Notes view button (always shown so user can also *add* notes
            # to a historical entry)
            note_color = COLORS["text"] if notes else COLORS["muted"]
            ctk.CTkButton(right, text="📝", width=30, height=26,
                          font=("Outfit", 12),
                          fg_color=COLORS["card"], border_color=COLORS["border"],
                          border_width=1, text_color=note_color,
                          hover_color=COLORS["active_bg"],
                          command=lambda rr=r: self._view_notes(rr)).pack(
                side="left", padx=(0, 4))
            ctk.CTkButton(right, text="✎", width=30, height=26,
                          font=("Outfit", 12),
                          fg_color=COLORS["card"], border_color=COLORS["border"],
                          border_width=1, text_color=COLORS["text"],
                          hover_color=COLORS["active_bg"],
                          command=lambda rr=r: self._edit_csv_row(rr)).pack(
                side="left", padx=(0, 4))
            ctk.CTkButton(right, text="🗑", width=30, height=26,
                          font=("Outfit", 12),
                          fg_color=COLORS["card"], border_color=COLORS["border"],
                          border_width=1, text_color=COLORS["muted"],
                          hover_color=COLORS["danger"],
                          command=lambda rr=r: self._delete_csv_row(rr)).pack(
                side="left")

        n = len(rows)
        self.tasks_lbl.configure(text=f"{n} task{'s' if n != 1 else ''}")
        self.total_lbl.configure(text=f"Total: {fmt(total)}")

    def _render_empty(self):
        ctk.CTkLabel(self.results, text="No data for this period.",
                     font=("Outfit", 13), text_color=COLORS["muted"]).grid(
            row=0, column=0, columnspan=3, pady=48)
        self.total_lbl.configure(text="Total: 00:00:00")
        self.tasks_lbl.configure(text="0 tasks")

    # ── View / edit notes for a CSV row (Day view) ─────────────────────────────

    def _view_notes(self, original: dict):
        notes = (original.get("notes") or "").strip()
        task = original.get("task", "")
        date_str = original.get("date", "")

        def on_save(text: str):
            all_rows = load_all_csv()
            target_date = original.get("date")
            target_task = original.get("task")
            target_secs = original.get("seconds")
            updated = False
            for r in all_rows:
                if (not updated
                        and r.get("date") == target_date
                        and r.get("task") == target_task
                        and r.get("seconds") == target_secs):
                    r["notes"] = text
                    updated = True
            save_full_csv(all_rows)
            original["notes"] = text
            self._on_history_changed()
            self._refresh()

        NotesPopup(self,
                   title=f"Notes — {task}  ({date_str})",
                   current_text=notes,
                   on_save=on_save)

    # ── Edit / delete CSV rows (Day view only) ─────────────────────────────────

    def _edit_csv_row(self, original: dict):
        try:
            current_secs = int(original.get("seconds", 0))
        except (ValueError, TypeError):
            current_secs = 0

        def on_save(new_secs: int, new_name: str | None):
            all_rows = load_all_csv()
            target_date = original.get("date")
            target_task = original.get("task")
            target_secs = original.get("seconds")
            updated = False
            for r in all_rows:
                if (not updated
                        and r.get("date") == target_date
                        and r.get("task") == target_task
                        and r.get("seconds") == target_secs):
                    if new_name:
                        r["task"] = new_name
                    r["seconds"] = str(new_secs)
                    r["hms"] = fmt(new_secs)
                    r["exported_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    updated = True
            save_full_csv(all_rows)
            self._on_history_changed()
            self._refresh()

        EditTimePopup(self,
                      title="Edit history entry",
                      current_secs=current_secs,
                      current_name=original.get("task", ""),
                      allow_rename=True,
                      on_save=on_save)

    def _delete_csv_row(self, original: dict):
        confirm = ctk.CTkToplevel(self)
        confirm.title("Confirm delete")
        confirm.geometry("360x170")
        confirm.resizable(False, False)
        confirm.configure(fg_color=COLORS["bg"])
        confirm.lift()
        confirm.focus_force()
        confirm.grab_set()

        ctk.CTkLabel(confirm, text="Delete this entry?",
                     font=("Outfit", 14, "bold"),
                     text_color=COLORS["text"]).pack(pady=(20, 4))
        ctk.CTkLabel(confirm,
                     text=f"{original.get('task', '')}   ·   "
                          f"{fmt(int(original.get('seconds', 0) or 0))}\n"
                          f"on {original.get('date', '')}",
                     font=("Outfit", 11), text_color=COLORS["muted"]).pack(pady=4)

        btns = ctk.CTkFrame(confirm, fg_color="transparent")
        btns.pack(pady=14)

        def do_delete():
            all_rows = load_all_csv()
            target_date = original.get("date")
            target_task = original.get("task")
            target_secs = original.get("seconds")
            removed = False
            kept: list[dict] = []
            for r in all_rows:
                if (not removed
                        and r.get("date") == target_date
                        and r.get("task") == target_task
                        and r.get("seconds") == target_secs):
                    removed = True
                    continue
                kept.append(r)
            save_full_csv(kept)
            confirm.destroy()
            self._on_history_changed()
            self._refresh()

        ctk.CTkButton(btns, text="Cancel", width=110, height=32,
                      font=("Outfit", 12),
                      fg_color=COLORS["card"], border_color=COLORS["border"],
                      border_width=1, text_color=COLORS["muted"],
                      command=confirm.destroy).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="Delete", width=110, height=32,
                      font=("Outfit", 12, "bold"),
                      fg_color=COLORS["danger"], hover_color="#b91c1c",
                      command=do_delete).pack(side="left", padx=6)


# ── Finish Summary Popup ───────────────────────────────────────────────────────

class FinishPopup(ctk.CTkToplevel):
    def __init__(self, master, rows: list[dict]):
        super().__init__(master)
        self.title("Day Summary")
        self.geometry("420x560")
        self.minsize(420, 480)
        self.configure(fg_color=COLORS["bg"])
        self.lift()
        self.focus_force()
        self.grab_set()

        ctk.CTkLabel(self, text="✓  Day Complete",
                     font=("Outfit", 20, "bold"),
                     text_color=COLORS["success"]).pack(pady=(24, 2))
        ctk.CTkLabel(self, text=date.today().strftime("%A, %d %B %Y"),
                     font=("Outfit", 11), text_color=COLORS["muted"]).pack(pady=(0, 14))

        # Bottom-anchored block first (Close button + total) — guarantees
        # they stay visible even if the table scrollable expands.
        ctk.CTkButton(self, text="Close", width=140, height=38,
                      font=("Outfit", 13, "bold"),
                      fg_color=COLORS["active"],
                      command=self.destroy).pack(side="bottom", pady=(8, 20))

        total = sum(int(r.get("seconds", 0)) for r in rows)
        ctk.CTkLabel(self, text=f"Total tracked:  {fmt(total)}",
                     font=("JetBrains Mono", 15, "bold"),
                     text_color=COLORS["text"]).pack(side="bottom", pady=(8, 4))
        ctk.CTkFrame(self, fg_color=COLORS["border"], height=1).pack(
            side="bottom", fill="x", padx=24, pady=(8, 0))

        # Scrollable table fills the remaining space
        table = ctk.CTkScrollableFrame(self, fg_color=COLORS["panel"],
                                       corner_radius=12)
        table.pack(fill="both", expand=True, padx=24, pady=4)
        table.columnconfigure(0, weight=1)
        table.columnconfigure(1, weight=0)

        sorted_rows = sorted(rows, key=lambda r: int(r.get("seconds", 0)), reverse=True)
        for i, row in enumerate(sorted_rows):
            secs = int(row.get("seconds", 0))
            notes = (row.get("notes") or "").strip()
            accent = COLORS["break_"] if row["task"] == BREAK_NAME else COLORS["active"]

            cell = ctk.CTkFrame(table, fg_color="transparent")
            cell.grid(row=i, column=0, padx=12, pady=6, sticky="w")
            ctk.CTkLabel(cell, text=row["task"], font=("Outfit", 13),
                         text_color=COLORS["text"], anchor="w").pack(anchor="w")
            if notes:
                first = notes.splitlines()[0]
                n = len(notes.splitlines())
                shown = first if len(first) <= 56 else first[:53] + "…"
                if n > 1:
                    shown = f"{shown}  (+{n - 1} more)"
                ctk.CTkLabel(cell, text=shown, font=("Outfit", 10),
                             text_color=COLORS["muted"], anchor="w").pack(anchor="w")

            ctk.CTkLabel(table, text=fmt(secs),
                         font=("JetBrains Mono", 13), text_color=accent).grid(
                row=i, column=1, padx=12, pady=6, sticky="e")


# ── Main App ───────────────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Time Tracker")
        self.geometry("600x820")
        self.minsize(500, 620)
        self.configure(fg_color=COLORS["bg"])

        # Named 'tracker' (not 'state') — avoids shadowing tkinter's .state() method
        self.tracker = TrackerState()
        self.cards: dict[str, TaskCard] = {}
        self._globally_paused = False
        self._dashboard_win: DashboardWindow | None = None

        # Settings (daily goal)
        settings = load_settings()
        try:
            self.target_hours = float(settings.get("target_hours", DEFAULT_TARGET_HOURS))
        except (TypeError, ValueError):
            self.target_hours = DEFAULT_TARGET_HOURS
        if not (0.5 <= self.target_hours <= 24):
            self.target_hours = DEFAULT_TARGET_HOURS

        self._build_ui()
        self._add_break_card()
        self._load_today()
        self._refresh_history_chips()
        self._tick()
        self._auto_save()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        header = ctk.CTkFrame(self, fg_color=COLORS["panel"], corner_radius=0, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)

        ctk.CTkLabel(header, text="⏱  Time Tracker",
                     font=("Outfit", 20, "bold"),
                     text_color=COLORS["text"]).pack(side="left", padx=20)

        right = ctk.CTkFrame(header, fg_color="transparent")
        right.pack(side="right", padx=20)
        self.total_lbl = ctk.CTkLabel(right, text="Total: 00:00:00",
                                      font=("JetBrains Mono", 13),
                                      text_color=COLORS["muted"])
        self.total_lbl.pack(anchor="e")
        ctk.CTkLabel(right,
                     text=f"Session started {datetime.now().strftime('%H:%M')}",
                     font=("Outfit", 10), text_color=COLORS["muted"]).pack(anchor="e")

        self.global_pause_btn = ctk.CTkButton(
            header, text="⏸  Pause", width=110, height=36,
            font=("Outfit", 12, "bold"),
            fg_color=COLORS["card"], border_color=COLORS["border"], border_width=1,
            text_color=COLORS["muted"],
            command=self._toggle_global_pause
        )
        self.global_pause_btn.pack(side="right", padx=(0, 12))

        # Goal strip — work-time progress vs daily target
        goal_strip = ctk.CTkFrame(self, fg_color=COLORS["panel"],
                                  corner_radius=0, height=40)
        goal_strip.pack(fill="x")
        goal_strip.pack_propagate(False)
        goal_strip.columnconfigure(1, weight=1)

        self.goal_lbl = ctk.CTkButton(
            goal_strip, text="🎯  Goal: 00:00 / 08:00",
            font=("Outfit", 11, "bold"),
            fg_color="transparent", hover_color=COLORS["card"],
            text_color=COLORS["text"], height=28, width=190,
            anchor="w", command=self._edit_goal
        )
        self.goal_lbl.grid(row=0, column=0, padx=(16, 8), pady=6, sticky="w")

        self.goal_bar = ctk.CTkProgressBar(
            goal_strip, height=8,
            progress_color=COLORS["active"], fg_color=COLORS["border"])
        self.goal_bar.grid(row=0, column=1, padx=(0, 16), pady=6, sticky="ew")
        self.goal_bar.set(0)

        # Add task bar
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(14, 4))
        self.task_entry = ctk.CTkEntry(
            bar, placeholder_text="New task name…",
            font=("Outfit", 13), fg_color=COLORS["card"],
            border_color=COLORS["border"], text_color=COLORS["text"], height=40)
        self.task_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.task_entry.bind("<Return>", lambda _: self._add_task())
        ctk.CTkButton(bar, text="+ Add Task", width=110, height=40,
                      font=("Outfit", 13, "bold"),
                      fg_color=COLORS["active"],
                      command=self._add_task).pack(side="left")

        # Date label (packed early so chips_row can use `before=` to slot above it)
        self.date_lbl = ctk.CTkLabel(
            self,
            text=f"Today  ·  {date.today().strftime('%A, %d %B %Y')}",
            font=("Outfit", 11), text_color=COLORS["muted"])
        self.date_lbl.pack(anchor="w", padx=18, pady=(8, 4))

        # History chips row — packed/unpacked dynamically so no empty gap
        self.chips_row = ctk.CTkFrame(self, fg_color="transparent")
        self.chips_label = ctk.CTkLabel(
            self.chips_row, text="Recent:", font=("Outfit", 10),
            text_color=COLORS["muted"])
        self.chips_scroll = ctk.CTkScrollableFrame(
            self.chips_row, fg_color="transparent",
            orientation="horizontal", height=36,
            scrollbar_button_color=COLORS["border"])
        _clamp_scrollable(self.chips_scroll, axis="x")
        self._chips_visible = False

        # Task list
        self.scroll = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_button_color=COLORS["border"])
        self.scroll.pack(fill="both", expand=True, padx=16, pady=4)
        self.scroll.columnconfigure(0, weight=1)
        _clamp_scrollable(self.scroll)

        # Footer
        footer = ctk.CTkFrame(self, fg_color=COLORS["panel"], corner_radius=0, height=58)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)

        ctk.CTkButton(footer, text="↺  Reset", width=90, height=36,
                      font=("Outfit", 12, "bold"),
                      fg_color=COLORS["card"], border_color=COLORS["border"],
                      border_width=1, text_color=COLORS["muted"],
                      hover_color="#2d1f1f",
                      command=self._reset_all).pack(side="left", padx=(14, 6), pady=10)

        ctk.CTkButton(footer, text="📊  Dashboard", width=120, height=36,
                      font=("Outfit", 12, "bold"),
                      fg_color=COLORS["card"], border_color=COLORS["border"],
                      border_width=1, text_color=COLORS["text"],
                      command=self._open_dashboard).pack(side="left", padx=6, pady=10)

        ctk.CTkButton(footer, text="📂  Data", width=80, height=36,
                      font=("Outfit", 12),
                      fg_color=COLORS["card"], border_color=COLORS["border"],
                      border_width=1, text_color=COLORS["muted"],
                      command=self._show_data_folder).pack(side="left", padx=6, pady=10)

        ctk.CTkButton(footer, text="✓  Finish Day", width=120, height=36,
                      font=("Outfit", 12, "bold"),
                      fg_color=COLORS["success"],
                      command=self._finish_day).pack(side="right", padx=14, pady=10)

        ctk.CTkButton(footer, text="⬇  Export", width=96, height=36,
                      font=("Outfit", 12, "bold"),
                      fg_color=COLORS["active"],
                      command=self._export).pack(side="right", padx=6, pady=10)

        self.status_bar = ctk.CTkLabel(footer, text="",
                                       font=("Outfit", 11),
                                       text_color=COLORS["break_"])
        self.status_bar.pack(side="right", padx=6)

    # ── Break card ─────────────────────────────────────────────────────────────

    def _add_break_card(self):
        self.tracker.add_task(BREAK_NAME)
        card = TaskCard(self.scroll, BREAK_NAME, self.tracker,
                        on_select=self._select, on_remove=lambda _: None,
                        on_edit_time=self._edit_time,
                        on_notes_change=self._on_notes_change,
                        is_break=True)
        card.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.cards[BREAK_NAME] = card

    # ── Restore today's data ───────────────────────────────────────────────────

    def _load_today(self):
        today_data = load_today_from_csv()
        if not today_data:
            return
        for name, info in today_data.items():
            secs = info["seconds"]
            notes = info["notes"]
            if name == BREAK_NAME:
                self.tracker.tasks[BREAK_NAME] = secs
                if notes:
                    self.tracker.notes[BREAK_NAME] = notes
                if BREAK_NAME in self.cards:
                    self.cards[BREAK_NAME].set_notes_text(notes)
            elif name not in self.cards:
                self._add_task_with_name(name, initial_secs=secs,
                                         initial_notes=notes)
            else:
                self.tracker.tasks[name] = secs
                if notes:
                    self.tracker.notes[name] = notes
                self.cards[name].set_notes_text(notes)
        self._flash_status("Today's data restored from CSV")

    # ── History chips ──────────────────────────────────────────────────────────

    def _refresh_history_chips(self):
        for w in self.chips_scroll.winfo_children():
            w.destroy()

        available = [n for n in get_history_task_names() if n not in self.cards]

        if not available:
            if self._chips_visible:
                self.chips_row.pack_forget()
                self._chips_visible = False
            return

        if not self._chips_visible:
            # Slot the chips row above the date label
            self.chips_row.pack(fill="x", padx=16, pady=(2, 0),
                                before=self.date_lbl)
            self.chips_label.pack(side="left", padx=(0, 6), pady=4)
            self.chips_scroll.pack(side="left", fill="x", expand=True)
            self._chips_visible = True

        for name in available[:15]:
            ctk.CTkButton(
                self.chips_scroll, text=f"+ {name}",
                width=0, height=26, font=("Outfit", 11),
                fg_color=COLORS["card"], border_color=COLORS["border"],
                border_width=1, text_color=COLORS["text"],
                hover_color=COLORS["active_bg"],
                command=lambda n=name: self._quick_add(n)
            ).pack(side="left", padx=(0, 6))

        # Re-bind wheel handlers on the new chip widgets too
        if hasattr(self.chips_scroll, "_reclamp"):
            self.chips_scroll._reclamp()

    def _quick_add(self, name: str):
        self._add_task_with_name(name)
        self._refresh_history_chips()

    # ── Add / Remove ───────────────────────────────────────────────────────────

    def _add_task(self):
        name = self.task_entry.get().strip()
        if not name or name in self.cards:
            return
        self.task_entry.delete(0, "end")
        self._add_task_with_name(name)
        self._refresh_history_chips()

    def _add_task_with_name(self, name: str, initial_secs: int = 0,
                            initial_notes: str = ""):
        self.tracker.add_task(name, initial_secs, initial_notes)
        row = len(self.cards)
        card = TaskCard(self.scroll, name, self.tracker,
                        on_select=self._select, on_remove=self._remove_task,
                        on_edit_time=self._edit_time,
                        on_notes_change=self._on_notes_change)
        card.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        if initial_notes:
            card.set_notes_text(initial_notes)
        self.cards[name] = card
        # New card has descendant widgets (timer label, notes button, etc.)
        # — re-apply wheel bindings so hovering them while scrolling works.
        if hasattr(self.scroll, "_reclamp"):
            self.scroll._reclamp()

    # ── Edit a card's time ─────────────────────────────────────────────────────

    def _edit_time(self, name: str):
        if name not in self.cards:
            return
        current = self.tracker.elapsed(name)

        def on_save(new_secs: int, _new_name):
            self.tracker.set_seconds(name, new_secs)
            self._sync_cards()
            # Persist immediately
            rows = self.tracker.snapshot_rows()
            save_csv(rows)
            self._flash_status(f"{name}: time set to {fmt(new_secs)}")

        EditTimePopup(self,
                      title=f"Edit time — {name}",
                      current_secs=current,
                      on_save=on_save)

    # ── Notes change ───────────────────────────────────────────────────────────

    def _on_notes_change(self, name: str, text: str):
        if self.tracker.get_notes(name) == text:
            return
        self.tracker.set_notes(name, text)
        # Save quietly so notes persist; no status flash to avoid noise
        rows = self.tracker.snapshot_rows()
        save_csv(rows)

    def _remove_task(self, name: str):
        card = self.cards.pop(name, None)
        if card:
            card.destroy()
        self.tracker.remove_task(name)
        for i, c in enumerate(self.cards.values()):
            c.grid(row=i, column=0, sticky="ew", pady=(0, 8))
        self._refresh_history_chips()

    # ── Selection ──────────────────────────────────────────────────────────────

    def _select(self, name: str | None):
        if name is None:
            # Card's Pause button — deselect regardless of global pause state
            self.tracker.deselect()
            self._globally_paused = False
            self.global_pause_btn.configure(text="⏸  Pause",
                                             text_color=COLORS["muted"])
        elif self._globally_paused and name == self.tracker.active:
            # Card's Resume button while globally paused → resume
            self._globally_paused = False
            self.tracker.resume_active()
            self.global_pause_btn.configure(text="⏸  Pause",
                                             text_color=COLORS["muted"])
        else:
            if self._globally_paused:
                self._globally_paused = False
                self.global_pause_btn.configure(text="⏸  Pause",
                                                 text_color=COLORS["muted"])
            self.tracker.select(name)
        self._sync_cards()
        # Persist immediately so a crash mid-session doesn't lose committed time
        self._quiet_save()

    def _sync_cards(self):
        ticking = self.tracker.is_ticking()
        for n, c in self.cards.items():
            active = n == self.tracker.active
            c.set_active(active, paused=active and not ticking)

    # ── Global Pause ───────────────────────────────────────────────────────────

    def _toggle_global_pause(self):
        if not self.tracker.active:
            return
        if self._globally_paused:
            self._globally_paused = False
            self.tracker.resume_active()
            self.global_pause_btn.configure(text="⏸  Pause",
                                             text_color=COLORS["muted"])
        else:
            self._globally_paused = True
            self.tracker.pause_active()
            self.global_pause_btn.configure(text="▶  Resume",
                                             text_color=COLORS["warning"])
        self._sync_cards()
        self._quiet_save()

    # ── Reset ──────────────────────────────────────────────────────────────────

    def _reset_all(self):
        self._globally_paused = False
        self.tracker.reset_all()
        self.global_pause_btn.configure(text="⏸  Pause", text_color=COLORS["muted"])
        for c in self.cards.values():
            c.set_active(False)
        self._flash_status("All timers reset to 00:00:00")

    # ── Finish Day ─────────────────────────────────────────────────────────────

    def _finish_day(self):
        self._globally_paused = False
        self.tracker.deselect()
        self.global_pause_btn.configure(text="⏸  Pause", text_color=COLORS["muted"])
        self._sync_cards()
        rows = self.tracker.snapshot_rows()
        save_csv(rows)
        FinishPopup(self, rows)

    # ── Dashboard ──────────────────────────────────────────────────────────────

    def _open_dashboard(self):
        # Persist live in-progress data so the dashboard reflects the
        # current session, not just whatever was last auto-saved.
        rows = self.tracker.snapshot_rows()
        if any(r["seconds"] > 0 for r in rows):
            save_csv(rows)

        if self._dashboard_win and self._dashboard_win.winfo_exists():
            self._dashboard_win._refresh()
            self._dashboard_win.lift()
            self._dashboard_win.focus_force()
            return
        self._dashboard_win = DashboardWindow(
            self, on_history_changed=self._on_history_changed)

    def _on_history_changed(self):
        """Called when the dashboard edits/deletes CSV rows. If today's data
        was touched, reload the live tracker so the main window reflects it."""
        today_data = load_today_from_csv()
        # Update existing cards' seconds + notes to match CSV
        for name, info in today_data.items():
            secs = info["seconds"]
            notes = info["notes"]
            with self.tracker.lock:
                if name in self.tracker.tasks:
                    # Don't disturb a live-ticking timer's clock — only adjust base
                    if self.tracker.active == name and self.tracker._tick_start is not None:
                        self.tracker._tick_start = time.monotonic()
                    self.tracker.tasks[name] = secs
                    self.tracker.notes[name] = notes
            if name in self.cards:
                self.cards[name].set_notes_text(notes)
        # Drop today-tasks that were deleted from CSV
        existing_today = set(today_data.keys())
        for name in list(self.tracker.tasks.keys()):
            if name == BREAK_NAME:
                if BREAK_NAME not in existing_today:
                    self.tracker.tasks[BREAK_NAME] = 0
                continue
            if name not in existing_today and name in self.cards:
                # User removed the row from history → reset locally to 0
                with self.tracker.lock:
                    self.tracker.tasks[name] = 0
                    self.tracker.notes[name] = ""
                self.cards[name].set_notes_text("")

    # ── Export & data folder ───────────────────────────────────────────────────

    def _export(self):
        # Always commit current state to the canonical CSV first.
        rows = self.tracker.snapshot_rows()
        save_csv(rows)

        # Then ask the user where to save a *copy*.
        default_name = f"time_tracker_export_{date.today().isoformat()}.csv"
        target = filedialog.asksaveasfilename(
            parent=self,
            title="Export CSV — choose location",
            defaultextension=".csv",
            initialfile=default_name,
            initialdir=os.path.expanduser("~/Downloads"),
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not target:
            self._flash_status("Export cancelled")
            return
        try:
            shutil.copy2(CSV_FILE, target)
            self._flash_status(f"Exported → {os.path.basename(target)}")
        except OSError as e:
            self._flash_status(f"Export failed: {e}")

    def _show_data_folder(self):
        """Open the data folder in Finder / Explorer / file manager."""
        path = DATA_DIR
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", path])
            self._flash_status(f"Opened {path}")
        except Exception as e:
            self._flash_status(f"Could not open: {e}")

    def _flash_status(self, msg: str):
        self.status_bar.configure(text=msg)
        self.after(3000, lambda: self.status_bar.configure(text=""))

    def _quiet_save(self):
        """Snapshot + save without status bar noise. Best-effort — never raises."""
        try:
            rows = self.tracker.snapshot_rows()
            save_csv(rows)
        except Exception:
            pass

    # ── Goal ───────────────────────────────────────────────────────────────────

    def _work_seconds(self) -> int:
        return sum(self.tracker.elapsed(n)
                   for n in self.cards if n != BREAK_NAME)

    def _edit_goal(self):
        def on_save(hours: float):
            self.target_hours = hours
            save_settings({"target_hours": hours})
            self._flash_status(f"Daily goal set to {hours:g}h")

        GoalEditPopup(self, current_hours=self.target_hours, on_save=on_save)

    def _update_goal_display(self, work_secs: int):
        target_secs = max(1, int(self.target_hours * 3600))
        ratio = min(1.0, work_secs / target_secs)
        # HH:MM display (drop seconds for the goal label — cleaner)
        def hm(s: int) -> str:
            h, r = divmod(max(0, s), 3600)
            return f"{h:02d}:{r // 60:02d}"
        reached = work_secs >= target_secs
        color = COLORS["success"] if reached else COLORS["active"]
        prefix = "✓  Goal reached!  " if reached else "🎯  Goal: "
        self.goal_lbl.configure(
            text=f"{prefix}{hm(work_secs)} / {hm(target_secs)}",
            text_color=COLORS["success"] if reached else COLORS["text"])
        self.goal_bar.configure(progress_color=color)
        self.goal_bar.set(ratio)

    # ── Tick ───────────────────────────────────────────────────────────────────

    def _tick(self):
        total = 0
        work = 0
        for name, card in self.cards.items():
            card.tick()
            elapsed = self.tracker.elapsed(name)
            total += elapsed
            if name != BREAK_NAME:
                work += elapsed
        self.total_lbl.configure(text=f"Total: {fmt(total)}")
        self._update_goal_display(work)
        self.after(500, self._tick)

    # ── Auto-save ──────────────────────────────────────────────────────────────

    def _auto_save(self):
        rows = self.tracker.snapshot_rows()
        if any(r["seconds"] > 0 for r in rows):
            save_csv(rows)
        self.after(AUTO_SAVE_MS, self._auto_save)

    # ── Close ──────────────────────────────────────────────────────────────────

    def _on_close(self):
        rows = self.tracker.snapshot_rows()
        if rows:
            save_csv(rows)
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
