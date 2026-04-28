# Time Tracker

A simple, no-fuss desktop app for tracking how many hours you spend on each project — with a fixed Break button, daily goal, history dashboard, multi-line notes per task, and CSV export.

Single Python file (`time_tracker.py`), runs as a real desktop app on **macOS** and **Windows**.

---

## Repository layout

```
time_tracker.py        — the source code (one file, no other modules)
mac/
  time_tracker.app     — prebuilt macOS app (double-click to launch)
windows/
  time_tracker.exe     — prebuilt Windows executable (double-click to launch)
```

You have two ways to use this app: grab the prebuilt binary for your OS, or run it from source.

---

## Option A — Just use it (no Python required)

### macOS
1. Download `mac/time_tracker.app` from this repo (clone the repo or use GitHub's "Download ZIP").
2. Drag `time_tracker.app` into your **Applications** folder.
3. **First launch only:** right-click the app → **Open** → click **Open** in the warning dialog. macOS blocks unsigned apps on the first launch; after that, double-click works normally.

### Windows
1. Download `windows/time_tracker.exe`.
2. Put it anywhere you like — `Documents`, `Desktop`, your own `C:\Apps\` folder.
3. Double-click to launch.
4. **First launch only:** Windows SmartScreen will show a warning. Click **More info** → **Run anyway**. Subsequent launches are normal.

That's it. The app creates its data folder automatically in your **Documents** folder (see [Where your data lives](#where-your-data-lives) below).

---

## Option B — Run from source (`time_tracker.py`)

Use this if you want to modify the code, or if you don't want a prebuilt binary on your machine.

### Prerequisites
- **Python 3.10 or newer** ([download](https://www.python.org/downloads/))
- One pip package: `customtkinter`

### macOS

```bash
# 1. Confirm Python is installed
python3 --version

# 2. Install the dependency
python3 -m pip install customtkinter

# 3. Run the app
cd /path/to/folder/with/time_tracker.py
python3 time_tracker.py
```

If pip complains about an "externally-managed-environment", add `--user`:
```bash
python3 -m pip install --user customtkinter
```

### Windows

During Python install, **check "Add Python to PATH"**. Then in PowerShell or Command Prompt:

```powershell
# 1. Confirm Python is installed
python --version

# 2. Install the dependency
python -m pip install customtkinter

# 3. Run the app
cd C:\path\to\folder\with\time_tracker.py
python time_tracker.py
```

When you run from source, your data goes into the **same folder** as `time_tracker.py` (handy for development).

---

## Option C — Build your own binary from source

If you change the code and want to rebuild the prebuilt binaries:

### macOS → `time_tracker.app`
```bash
python3 -m pip install pyinstaller
python3 -m PyInstaller --onefile --windowed \
  --name "time_tracker" \
  --collect-all customtkinter \
  time_tracker.py
# Output: dist/time_tracker.app
```

### Windows → `time_tracker.exe`
```powershell
python -m pip install pyinstaller
python -m PyInstaller --onefile --windowed `
  --name "time_tracker" `
  --collect-all customtkinter `
  time_tracker.py
# Output: dist\time_tracker.exe
```

You can't cross-build — a Mac can only produce a `.app`, a Windows machine can only produce an `.exe`. If you don't have access to both, GitHub Actions can build both for you on every push (ask if you want a workflow).

---

## Features

- **Add tasks (projects)** with one-click switching — only one timer runs at a time
- **Fixed Break button** always visible, separate from your project tasks
- **Card-level Pause/Resume** + **Global Pause** in the header
- **Daily work-hour goal** with a progress bar across the top — turns green when you hit it (excludes Break time)
- **Manual time edit** — click any timer to type in `HH:MM:SS` if you forgot to start it
- **Multi-line notes** per task per day — click "📝 Add Notes" to open a real text editor
- **History dashboard** with Day / Week / Month views, sortable by time
- **Edit or delete** any historical entry directly from the Day view
- **Quick re-add** of past projects via "Recent" chips below the input
- **Auto-save** every 5 minutes + on every state change (no progress lost on crash)
- **Today's data restored** automatically when you relaunch
- **Reset** clears all timers; **Finish Day** locks them in and shows a summary
- **Export CSV** to anywhere via a native Save As dialog

---

## Use cases

**Tracking work hours per project** — add a card per client/project, click to switch as you context-switch through the day. The daily goal bar shows how close you are to your target hours (excluding breaks). At end of day, click **Finish Day** for a summary.

**Project / client billing** — export the CSV at the end of the week or month, open in Numbers/Excel, sort/filter by task and date.

**Personal study time tracking** — one card per subject. Use notes to record what you covered. Use Week/Month dashboard views to see how your time is distributed.

**Honest break tracking** — the fixed Break card means you actually count your breaks instead of pretending they didn't happen. Helps you understand your real productive vs. recovery time.

**Returning to a forgotten project** — past project names appear as "Recent" chips at the top, so re-adding them next week takes one click — no retyping or copy-paste.

**Restart-safe sessions** — close the app accidentally, restart the laptop, whatever — open the app again and today's accumulated time is right there waiting for you to resume.

---

## Where your data lives

The app stores two files automatically:

- **`time_tracker_log.csv`** — every tracked second, by date and task, with notes
- **`time_tracker_settings.json`** — your daily goal hours

### Default locations

| How you launched it | macOS | Windows |
|---|---|---|
| Prebuilt `.app` / `.exe` | `~/Documents/TimeTracker/` | `C:\Users\<you>\Documents\TimeTracker\` |
| `python time_tracker.py` from source | next to the `.py` file | next to the `.py` file |

The app creates the folder automatically on first launch. Click the **📂 Data** button in the app's footer to open this folder in Finder / File Explorer.

### CSV format

```csv
date,task,seconds,hms,notes,exported_at
2026-04-28,Marketing Site,5400,01:30:00,"Wrote copy for landing page",2026-04-28 14:32:11
2026-04-28,☕  Break,1200,00:20:00,,2026-04-28 14:32:11
2026-04-28,Client Project,7200,02:00:00,"Bug fix in checkout flow",2026-04-28 14:32:11
```

Standard CSV — opens in Numbers, Excel, Google Sheets, any data tool.

### Export button — what it does

Clicking **⬇ Export** in the footer:

1. Saves the latest in-progress state to the canonical CSV at `~/Documents/TimeTracker/time_tracker_log.csv` (or wherever your data folder is).
2. Opens a native Save As dialog so you can save a **copy** anywhere — Downloads, Desktop, USB stick, iCloud, OneDrive — defaulting to a filename like `time_tracker_export_2026-04-28.csv`.

The original CSV in your data folder keeps growing day after day. Export is for sharing or backing up snapshots.

### Moving your history between machines

1. Get the app running on both machines (Option A or B).
2. Copy `time_tracker_log.csv` from the old machine's data folder to the same path on the new one.
3. Launch the app on the new machine — your full history appears in the dashboard immediately.

---

## Troubleshooting

**App won't launch on macOS — "Apple cannot check it for malicious software"**
Right-click the `.app` → **Open** → click **Open** in the warning. One-time thing. The app is unsigned because code-signing requires a paid Apple Developer account; opening it via right-click bypasses Gatekeeper.

**Windows SmartScreen blocks the .exe**
Click **More info** → **Run anyway**. Same situation — unsigned binaries trigger this once.

**Source build: `command not found: pyinstaller`**
Run it as a Python module instead: `python3 -m PyInstaller ...` (capital P, capital I in the module name).

**Source build: `ModuleNotFoundError: customtkinter` from the bundled app**
PyInstaller missed CTk's data files. The `--collect-all customtkinter` flag (already in the commands above) fixes this.

**Bundled app exits silently with no error**
`--windowed` hides stdout/stderr. Rebuild without `--windowed` once and run from a terminal to see the traceback.

**Harmless macOS warning when clicking Export from source**
`The class 'NSSavePanel' overrides the method identifier. This method is implemented by class 'NSWindow'` — printed by macOS itself, not the app. Cosmetic, ignore.

---

## License

Personal use. Modify freely.
