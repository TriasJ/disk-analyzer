# Disk Analyzer

> **Beta** — a small, dependency-free, Windows-only disk-space analyzer. Actively developed.

A small, dependency-free tool that shows **where your disk space goes**, finds
**duplicate files**, and flags **"crumbs"** (small, long-forgotten files) — plus
well-known **regenerable caches** you can safely clear.

It comes in two flavours that share the same engine: a **native desktop GUI**
(tabbed window) and a self-contained **HTML report** (treemap + sortable tables),
plus a **review-then-run.ps1** script listing suggested deletions.

> **The tool never deletes anything itself.** Every suggested deletion is written
> to `review-then-run.ps1` **commented out**, and set to move files to the
> **Recycle Bin** (recoverable). You decide what to uncomment and run.

## Requirements
- Windows with **Python 3.8+** (you have 3.12). Uses the standard library only —
  nothing to install.

## Graphical interface (GUI)
- **Easiest:** double-click **`DiskAnalyzer-GUI.bat`** (or run
  `python disk_analyzer.py --gui`).
- Pick a folder, press **Scan** (~seconds). Each section is a **tab**:
  **Overview**, **Free up space**, **Treemap** (double-click a folder to **zoom in**, a file to open it; ⬆ Up / ⌂ Root to navigate),
  **Biggest files / folders**, **By type** (double-click a type → all its files, sortable by size/date), **Duplicates** (a "Find duplicates"
  button runs the slower content-hashing on demand), **Crumbs**, **Caches**, and **Old versions**.
- Double-click any file/folder row to open it; right-click for
  **Open / Open containing folder / Copy path**.
- **Actionable tabs:** in **Duplicates** and **Crumbs**, multi-select rows
  (Ctrl/Shift) and **Recycle selected** or **Delete selected**. Duplicates has
  **"Select redundant (keep 1/group)"** to auto-pick the extra copies.
- **Free up space** tab — a guided cleanup assistant for the big system wins:
  clear caches, empty Recycle Bin, disable hibernation (admin), open paging-file
  settings, manage/remove **WSL** distros, check **System Restore** usage (admin),
  and launch **Windows Disk Cleanup**. "Refresh system info" reads current sizes.
- **Old versions** tab — flags *superseded / stale* items to review (delete with caution): old `app-<version>` folders (keeps newest), **cached** duplicate library archives (`.whl`/`.nupkg` downloads only — never installed libraries an app runs from), stale `node_modules`/`.venv` (warned in red if the project was touched recently), and long-untouched git repos. Multi-select → Recycle/Delete; “Select all flagged (safe)” skips anything in use.
- **Cleanup wizard** (Overview / Tools menu / Free-up-space tab) walks you
  step-by-step: caches → duplicates → crumbs → system space → finish.
- **Caches tab / Free up space:** "Clear caches → Recycle Bin" (with confirmation)
  and **"Empty Recycle Bin"** to actually reclaim the space.
- **Overview → Export HTML report** produces the same standalone HTML report.

> Protected folders (`.git`, `node_modules`, `AppData`, …) are never selected for
> deletion, and destructive actions always confirm first. Admin actions (hibernation,
> restore points) go through a normal Windows UAC prompt.

## Quick start (HTML / command line)
- **Easiest:** double-click **`analyze.bat`**, or **drag a folder onto it**.
- **Terminal:**
  ```
  python disk_analyzer.py "%USERPROFILE%\Documents"
  ```
  With no path, it analyzes the current folder.

When it finishes, the HTML report opens automatically in your browser, and a
timestamped report + `review-then-run_*.ps1` are saved in this folder.

## What the report shows
- **Where the space is** — a treemap of the biggest items in the folder.
  Click a tile to open that file/folder.
- **Biggest files** and **Biggest folders** — ranked, with bars. Click a name
  to open it; click the 📂 icon next to a file to open its containing folder.
- **Space by file type** — which kinds of files add up most.
- **Safe-to-clear caches** — regenerable cache folders (e.g. `.cache`,
  `node_modules`, `__pycache__`, browser caches) and their sizes. Includes a
  **"Copy 'clear all safe caches' command"** button (see below).
- **Duplicate files** — byte-for-byte identical copies and the space wasted.
- **Crumbs** — small files not modified in a long time.

All file/folder names in the report are clickable `file://` links (they work
when you open the report as a local file — which is how the tool opens it).

## Clearing the safe caches
Each run also writes **`clear-safe-caches_*.ps1`**, which sends the regenerable
cache folders to the **Recycle Bin** after asking you to confirm. Two ways to run it:
- In the report, click **"Copy 'clear all safe caches' command"**, then paste
  into PowerShell and press Enter, **or**
- Right-click `clear-safe-caches_*.ps1` → **Run with PowerShell**.

(A browser page can't delete files itself, so the button copies a ready command
rather than deleting in-page.)

## Acting on the results (safely)
1. Open the generated **`review-then-run_*.ps1`** in a text editor.
2. Read the suggested deletions. Nothing runs while they start with `#`.
3. Delete the `#` in front of **only** the lines you approve; save.
4. Run it:
   ```
   powershell -ExecutionPolicy Bypass -File .\review-then-run_YYYYMMDD_HHMMSS.ps1
   ```
   Deleted items go to the **Recycle Bin**, so you can restore anything.

## Useful options
```
python disk_analyzer.py [PATH] [options]

  --crumb-size 1MB        Size below which a file may be a "crumb" (KB/MB/GB ok)
  --crumb-age-days 365    A file is a crumb only if not modified in this many days
  --top 25                How many rows to show in ranked tables
  --min-dup-size 4KB      Ignore files smaller than this when finding duplicates
  --no-duplicates         Skip duplicate detection (much faster on huge trees)
  --out-dir <dir>         Where to save the report + script (default: this folder)
  --no-open               Don't auto-open the browser
  --follow-symlinks       Follow symlinks/junctions (OFF by default, on purpose)
  --protect NAME ...      Extra folder names to never suggest deleting from
  --no-default-protect    Turn off the built-in protected list
```

## Safety notes
- **Symlinks/junctions are not followed** by default (avoids double-counting and
  loops — e.g. the localized library-shortcut links Windows adds to your user folders).
- **Protected folders** (`.git`, `node_modules`, `__pycache__`, `.venv`/`venv`,
  `AppData`) are still measured but **never suggested for deletion**, so you
  can't accidentally break a Git repo or an app.
- Duplicate detection is exact (content hash), not name-based.
- "Last used" is based on **last-modified time** (`mtime`), which is the reliable
  signal on Windows; access-time is often disabled or misleading.

## Examples
```
# Analyze Documents, show top 40, treat <2MB & >18 months as crumbs
python disk_analyzer.py "%USERPROFILE%\Documents" --top 40 --crumb-size 2MB --crumb-age-days 540

# Quickly map a huge folder without the slower duplicate scan
python disk_analyzer.py "%USERPROFILE%\AppData\Local" --no-duplicates
```
