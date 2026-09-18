#!/usr/bin/env python3
"""
disk_analyzer.py - See where your disk space goes.

Visualizes, for any folder, which files/subfolders take the most space,
which files are exact duplicates, and which are "crumbs" (small, long-untouched
files worth deleting). Also flags well-known regenerable caches.

Outputs:
  * an interactive, self-contained HTML report (treemap + sortable tables)
  * a short colored summary in the terminal
  * a review-then-run.ps1 with every deletion COMMENTED OUT (and Recycle-Bin
    based, never a permanent delete) for you to inspect and run yourself.

The tool itself NEVER deletes anything.

Standard library only. Works on Python 3.8+.

Examples:
  python disk_analyzer.py
  python disk_analyzer.py "C:\\Users\\Me\\Documents"
  python disk_analyzer.py "C:\\Users\\Me" --top 40 --crumb-size 2MB --no-open
"""

import argparse
import bisect
import ctypes
import hashlib
import html
import json
import os
import pathlib
import re
import sys
import time
import webbrowser
from collections import defaultdict

FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# Directory basenames whose contents we never PROPOSE for deletion (they are
# still counted in size totals so the numbers stay honest).
DEFAULT_PROTECTED = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "AppData",
}

# Directory basenames that are typically safe-to-clear regenerable caches.
RECLAIMABLE_NAMES = {
    ".cache", "cache", "Cache", "Caches", "GPUCache", "Code Cache",
    ".bun", ".npm", ".yarn", ".gradle", ".nuget", ".pip", "pip-cache",
    ".chromium-browser-snapshots", "node_modules", "__pycache__",
    "Temp", "tmp", "Temporary Internet Files", "$WinREAgent",
}


# --------------------------------------------------------------------------- #
# size / time helpers
# --------------------------------------------------------------------------- #
def parse_size(text):
    """'1MB', '500KB', '4K', '2G', '1024' (bytes) -> int bytes."""
    s = str(text).strip().upper().replace(" ", "")
    table = [("KB", 1024), ("MB", 1024 ** 2), ("GB", 1024 ** 3),
             ("TB", 1024 ** 4), ("K", 1024), ("M", 1024 ** 2),
             ("G", 1024 ** 3), ("T", 1024 ** 4), ("B", 1)]
    mult = 1
    for suffix, m in table:
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            mult = m
            break
    if not s:
        s = "0"
    return int(float(s) * mult)


def human(n):
    """Human-readable byte size."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return "%d B" % int(n)
            return "%.1f %s" % (n, unit)
        n /= 1024
    return "%.1f TB" % n


def _longpath(path):
    """Prefix Windows paths so files with >260-char paths can be opened."""
    if os.name == "nt":
        p = os.path.abspath(path)
        if not p.startswith("\\\\?\\"):
            p = "\\\\?\\" + p
        return p
    return path


# --------------------------------------------------------------------------- #
# scanning
# --------------------------------------------------------------------------- #
class FileInfo:
    __slots__ = ("path", "size", "mtime", "atime")

    def __init__(self, path, size, mtime, atime):
        self.path = path
        self.size = size
        self.mtime = mtime
        self.atime = atime


def _is_reparse(entry):
    try:
        st = entry.stat(follow_symlinks=False)
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return False


def scan(root, opts, progress=None):
    """
    Walk `root` WITHOUT following symlinks / junctions / reparse points.

    Returns: files (list[FileInfo]), dir_sizes (dict path->recursive bytes),
             skipped (list[(path, error)]), total_bytes (int).

    If `progress` is given it is called as progress(files_scanned) periodically
    (used by the GUI). When it is None the CLI prints progress to stderr instead.
    """
    root = os.path.abspath(root)
    files = []
    dir_sizes = {}
    skipped = []
    counter = [0]
    t_last = [time.time()]

    def walk(dirpath):
        total = 0
        try:
            scandir_it = os.scandir(dirpath)
        except OSError as exc:
            skipped.append((dirpath, str(exc)))
            dir_sizes[dirpath] = 0
            return 0
        with scandir_it:
            for entry in scandir_it:
                try:
                    is_link = entry.is_symlink()
                    if not opts.follow_symlinks and (is_link or _is_reparse(entry)):
                        continue
                    if entry.is_dir(follow_symlinks=opts.follow_symlinks):
                        total += walk(entry.path)
                    else:
                        st = entry.stat(follow_symlinks=opts.follow_symlinks)
                        size = st.st_size
                        files.append(FileInfo(entry.path, size,
                                              st.st_mtime, st.st_atime))
                        total += size
                        counter[0] += 1
                        if progress is not None:
                            if counter[0] % 2000 == 0:
                                progress(counter[0])
                        elif counter[0] % 20000 == 0 and sys.stderr is not None:
                            now = time.time()
                            rate = 20000 / max(now - t_last[0], 1e-6)
                            t_last[0] = now
                            sys.stderr.write(
                                "\r  scanned %s files (%s so far, %.0f/s)   "
                                % (f"{counter[0]:,}", human(_running_total(dir_sizes) + total),
                                   rate))
                            sys.stderr.flush()
                except OSError as exc:
                    skipped.append((entry.path, str(exc)))
        dir_sizes[dirpath] = total
        return total

    total_bytes = walk(root)
    if progress is None and counter[0] >= 20000 and sys.stderr is not None:
        sys.stderr.write("\r" + " " * 70 + "\r")
        sys.stderr.flush()
    return files, dir_sizes, skipped, total_bytes


def _running_total(dir_sizes):
    # cheap best-effort for the progress line only
    return 0


# --------------------------------------------------------------------------- #
# analysis
# --------------------------------------------------------------------------- #
def largest_files(files, top):
    return sorted(files, key=lambda f: f.size, reverse=True)[:top]


def largest_dirs(dir_sizes, root, top):
    items = [(p, s) for p, s in dir_sizes.items() if p != root]
    items.sort(key=lambda x: x[1], reverse=True)
    return items[:top]


def size_by_ext(files, top):
    agg = defaultdict(lambda: [0, 0])  # ext -> [bytes, count]
    for f in files:
        ext = os.path.splitext(f.path)[1].lower() or "(no ext)"
        agg[ext][0] += f.size
        agg[ext][1] += 1
    rows = [(ext, v[0], v[1]) for ext, v in agg.items()]
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:top]


def root_children(dir_sizes, files, root):
    """Immediate children of root with total sizes, for the treemap."""
    children = []
    for path, size in dir_sizes.items():
        if os.path.dirname(path) == root and path != root:
            children.append((os.path.basename(path), size, True, path))
    for f in files:
        if os.path.dirname(f.path) == root:
            children.append((os.path.basename(f.path), f.size, False, f.path))
    children.sort(key=lambda x: x[1], reverse=True)
    return children


def _quick_hash(path, chunk=65536):
    h = hashlib.blake2b(digest_size=16)
    with open(_longpath(path), "rb") as fh:
        h.update(fh.read(chunk))
    return h.digest()


def _full_hash(path, bufsize=1024 * 1024):
    h = hashlib.blake2b(digest_size=32)
    with open(_longpath(path), "rb") as fh:
        while True:
            block = fh.read(bufsize)
            if not block:
                break
            h.update(block)
    return h.digest()


def find_duplicates(files, opts, progress=None):
    """Return list of duplicate groups (each a list[FileInfo]), cheap->expensive.

    If `progress` is given it is called as progress(done, total) while hashing
    the size-collision candidates (used by the GUI for a determinate bar).
    """
    by_size = defaultdict(list)
    for f in files:
        if f.size > 0 and f.size >= opts.min_dup_size:
            by_size[f.size].append(f)

    candidates = sum(len(g) for g in by_size.values() if len(g) >= 2)
    done = [0]
    groups = []
    for size, group in by_size.items():
        if len(group) < 2:
            continue
        by_qh = defaultdict(list)
        for f in group:
            try:
                by_qh[_quick_hash(f.path)].append(f)
            except OSError:
                pass
            done[0] += 1
            if progress is not None and done[0] % 200 == 0:
                progress(done[0], candidates)
        for qgroup in by_qh.values():
            if len(qgroup) < 2:
                continue
            by_fh = defaultdict(list)
            for f in qgroup:
                try:
                    by_fh[_full_hash(f.path)].append(f)
                except OSError:
                    pass
            for fgroup in by_fh.values():
                if len(fgroup) >= 2:
                    groups.append(fgroup)
    if progress is not None:
        progress(candidates, candidates)
    groups.sort(key=lambda g: (len(g) - 1) * g[0].size, reverse=True)
    return groups


def find_crumbs(files, opts, now):
    crumbs = []
    for f in files:
        if f.size < opts.crumb_size:
            # mtime (last modified) is the reliable "last used" signal on Windows:
            # atime is often disabled, stale, or bumped to now just by scanning.
            last_used = f.mtime
            age_days = (now - last_used) / 86400.0
            if age_days > opts.crumb_age_days:
                crumbs.append((f, age_days))
    crumbs.sort(key=lambda x: x[0].size, reverse=True)
    return crumbs


def find_reclaimables(dir_sizes, root):
    """Dirs whose basename is a known cache; skip caches nested in bigger caches."""
    hits = []
    for path, size in dir_sizes.items():
        if path == root:
            continue
        if os.path.basename(path) in RECLAIMABLE_NAMES:
            hits.append((path, size))
    hits.sort(key=lambda x: x[1], reverse=True)
    pruned = []
    for path, size in hits:
        norm = path.replace("\\", "/").rstrip("/")
        if any(norm.startswith(kept.replace("\\", "/").rstrip("/") + "/")
               for kept, _ in pruned):
            continue
        pruned.append((path, size))
    return pruned


def is_protected(path, protected):
    parts = path.replace("\\", "/").split("/")
    return any(part in protected for part in parts)


# --------------------------------------------------------------------------- #
# unused versions / stale dependency detection (delete-with-caution)
# --------------------------------------------------------------------------- #
# app folder like  app-1.0.9041   discord-0.0.309   Update-2.3.1
_APPVER_RE = re.compile(r"^(?P<fam>.*?)[-_ ]?v?(?P<ver>\d+(?:\.\d+){1,})$", re.I)
# pip wheel:  numpy-1.26.4-cp312-cp312-win_amd64.whl
_WHEEL_RE = re.compile(r"^(?P<name>.+?)-(?P<ver>\d+(?:\.\d+)*(?:[.\-][0-9A-Za-z]+)*)-"
                       r"[^-]*-[^-]*-[^-]*\.whl$", re.I)
# nuget:  newtonsoft.json.13.0.3.nupkg
_NUPKG_RE = re.compile(r"^(?P<name>.+?)\.(?P<ver>\d+(?:\.\d+)+[0-9A-Za-z.\-]*)\.nupkg$", re.I)
DEP_DIR_NAMES = {"node_modules", ".venv", "venv", "env", ".tox", ".gradle"}


def _ver_key(v):
    key = []
    for p in re.split(r"[.\-+_]", v):
        key.append((0, int(p)) if p.isdigit() else (1, p.lower()))
    return key


def find_old_versions(dir_sizes, files, root, now=None, stale_days=365):
    """Cautiously flag superseded/stale items. Returns list of dicts:
    {cat, path, size, detail, risk, keep}. Never deletes; keeps newest/active.

    Categories: 'app-version' (older sibling app-<ver> folders),
    'lib-archive' (older cached .whl/.nupkg downloads — NOT installed libs),
    'stale-deps' (node_modules/.venv untouched, project-recency aware),
    'git-repo' (git clones long untouched)."""
    now = now or time.time()
    findings = []
    sfiles = sorted(files, key=lambda f: f.path)
    spaths = [f.path for f in sfiles]

    def max_mtime_under(prefix, exclude=None):
        pre = prefix.rstrip("\\/") + os.sep
        exc = (exclude.rstrip("\\/") + os.sep) if exclude else None
        i = bisect.bisect_left(spaths, pre)
        m = 0.0
        while i < len(spaths) and spaths[i].startswith(pre):
            if not (exc and spaths[i].startswith(exc)):
                if sfiles[i].mtime > m:
                    m = sfiles[i].mtime
            i += 1
        return m

    # 1) old app versions: sibling  <fam>-<ver>  folders, keep highest version
    by_fam = defaultdict(list)
    for path in dir_sizes:
        if path == root:
            continue
        mm = _APPVER_RE.match(os.path.basename(path))
        if mm:
            by_fam[(os.path.dirname(path), mm.group("fam").lower())].append(
                (mm.group("ver"), path))
    for (_parent, _fam), lst in by_fam.items():
        if len(lst) < 2:
            continue
        lst.sort(key=lambda t: _ver_key(t[0]))
        newest = lst[-1][0]
        for ver, path in lst[:-1]:
            findings.append(dict(cat="app-version", path=path,
                                 size=dir_sizes.get(path, 0),
                                 detail="old app version %s (newest kept: %s)" % (ver, newest),
                                 risk="", keep=False))

    # 2) duplicate library ARCHIVES (.whl/.nupkg) — safe: cached downloads only
    by_pkg = defaultdict(list)
    for f in files:
        base = os.path.basename(f.path)
        wm = _WHEEL_RE.match(base)
        if wm:
            by_pkg[("wheel", wm.group("name").lower())].append((wm.group("ver"), f))
            continue
        nm = _NUPKG_RE.match(base)
        if nm:
            by_pkg[("nupkg", nm.group("name").lower())].append((nm.group("ver"), f))
    for (kind, name), lst in by_pkg.items():
        if len(lst) < 2:
            continue
        lst.sort(key=lambda t: _ver_key(t[0]))
        newest = lst[-1][0]
        for ver, f in lst[:-1]:
            findings.append(dict(cat="lib-archive", path=f.path, size=f.size,
                                 detail="old %s archive of %s %s (newest: %s)"
                                 % (kind, name, ver, newest),
                                 risk="", keep=False))

    # 3) stale dependency folders (node_modules/.venv/...) — project-recency aware
    for path, size in dir_sizes.items():
        if os.path.basename(path) in DEP_DIR_NAMES and os.path.dirname(path) != path:
            proj = os.path.dirname(path)
            recent = max_mtime_under(proj, exclude=path)
            age_days = (now - recent) / 86400.0 if recent else 99999
            stale = age_days > stale_days
            findings.append(dict(cat="stale-deps", path=path, size=size,
                                 detail="%s — project last touched %.0f mo ago"
                                 % (os.path.basename(path), age_days / 30.0),
                                 risk=("" if stale else "project modified recently — likely in use"),
                                 keep=not stale))

    # 4) old git repo clones — flagged whole, only if long untouched
    for path in list(dir_sizes):
        if os.path.basename(path) == ".git":
            repo = os.path.dirname(path)
            recent = max_mtime_under(repo, exclude=path)
            age_days = (now - recent) / 86400.0 if recent else 99999
            if age_days > stale_days:
                findings.append(dict(cat="git-repo", path=repo,
                                     size=dir_sizes.get(repo, 0),
                                     detail="git repo — last touched %.0f mo ago" % (age_days / 30.0),
                                     risk="", keep=False))

    findings.sort(key=lambda d: d["size"], reverse=True)
    return findings


# --------------------------------------------------------------------------- #
# terminal report
# --------------------------------------------------------------------------- #
def _enable_ansi():
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception:
        return False


def render_terminal(root, total_bytes, files, dirs_top, files_top,
                    dup_groups, crumbs, reclaimables, skipped, use_color):
    def c(code, text):
        return "\033[%sm%s\033[0m" % (code, text) if use_color else text

    bold = lambda t: c("1", t)
    dim = lambda t: c("2", t)
    cyan = lambda t: c("36", t)
    yellow = lambda t: c("33", t)
    green = lambda t: c("32", t)

    print()
    print(bold("  DISK ANALYSIS  ") + dim(root))
    print("  " + "-" * 58)
    print("  Total size : %s across %s files"
          % (bold(human(total_bytes)), f"{len(files):,}"))
    dup_waste = sum((len(g) - 1) * g[0].size for g in dup_groups)
    crumb_bytes = sum(f.size for f, _ in crumbs)
    reclaim_bytes = sum(s for _, s in reclaimables)
    print("  Duplicates : %s wasted in %s groups"
          % (yellow(human(dup_waste)), len(dup_groups)))
    print("  Crumbs     : %s in %s small/old files"
          % (yellow(human(crumb_bytes)), f"{len(crumbs):,}"))
    if reclaimables:
        print("  Caches     : %s in %s regenerable cache folders"
              % (yellow(human(reclaim_bytes)), len(reclaimables)))
    if skipped:
        print("  " + dim("Skipped %d item(s) (permission / vanished)" % len(skipped)))
    print()

    if files_top:
        print(cyan("  Biggest files"))
        for f in files_top[:10]:
            print("    %10s  %s" % (human(f.size), _short(f.path, root)))
        print()
    if dirs_top:
        print(cyan("  Biggest folders"))
        for path, size in dirs_top[:10]:
            print("    %10s  %s" % (human(size), _short(path, root)))
        print()
    if reclaimables:
        print(green("  Safe-to-clear caches (regenerate automatically)"))
        for path, size in reclaimables[:8]:
            print("    %10s  %s" % (human(size), _short(path, root)))
        print()


def _short(path, root):
    try:
        rel = os.path.relpath(path, root)
        return rel if len(rel) < len(path) else path
    except ValueError:
        return path


def _file_uri(path):
    """file:// URI for a local path (percent-encoded; clickable in a browser)."""
    try:
        return pathlib.Path(os.path.abspath(path)).as_uri()
    except Exception:
        return "file:///" + os.path.abspath(path).replace("\\", "/")


def _plink(abspath, display):
    """Render a path as a clickable file:// link that shows `display`."""
    return ('<a class="path" href="%s" target="_blank" rel="noopener" title="%s">%s</a>'
            % (html.escape(_file_uri(abspath)),
               html.escape(os.path.abspath(abspath)),
               html.escape(display)))


def _dir_link(filepath, label="\U0001F4C2"):
    """Small icon link that opens the file's containing folder."""
    folder = os.path.dirname(os.path.abspath(filepath))
    return ('<a class="folder" href="%s" target="_blank" rel="noopener" '
            'title="Open containing folder: %s">%s</a>'
            % (html.escape(_file_uri(folder)), html.escape(folder), label))


# --------------------------------------------------------------------------- #
# HTML report
# --------------------------------------------------------------------------- #
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Disk Report - __ROOTNAME__</title>
<style>
:root{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#1c2430; --muted:#67707c;
  --line:#e5e8ec; --accent:#3563e9; --warn:#d97706; --good:#059669;
  --bar:#3563e9;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#12151a; --panel:#1a1f27; --ink:#e7ebf0; --muted:#94a0ad;
    --line:#2a313b; --accent:#6b8dff; --warn:#f0a63c; --good:#37c98a; --bar:#6b8dff;}
}
:root[data-theme="light"]{ --bg:#f6f7f9; --panel:#fff; --ink:#1c2430; --muted:#67707c;
  --line:#e5e8ec; --accent:#3563e9; --warn:#d97706; --good:#059669; --bar:#3563e9;}
:root[data-theme="dark"]{ --bg:#12151a; --panel:#1a1f27; --ink:#e7ebf0; --muted:#94a0ad;
  --line:#2a313b; --accent:#6b8dff; --warn:#f0a63c; --good:#37c98a; --bar:#6b8dff;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:28px 20px 80px}
h1{font-size:22px;margin:0 0 2px}
h2{font-size:16px;margin:34px 0 12px;font-weight:650}
.sub{color:var(--muted);font-size:13px;word-break:break-all}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0 6px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:14px 16px;min-width:150px;flex:1}
.card .n{font-size:22px;font-weight:700}
.card .l{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card.warn .n{color:var(--warn)} .card.good .n{color:var(--good)}
.treemap{position:relative;width:100%;height:340px;background:var(--panel);
  border:1px solid var(--line);border-radius:12px;overflow:hidden}
.tile{position:absolute;overflow:hidden;border:1px solid var(--bg);
  padding:6px 8px;font-size:12px;color:#fff;transition:filter .1s}
.tile:hover{filter:brightness(1.12)}
.tile .tn{font-weight:600;white-space:nowrap;text-overflow:ellipsis;overflow:hidden}
.tile .ts{opacity:.85;font-size:11px}
table{width:100%;border-collapse:collapse;background:var(--panel);
  border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:13.5px}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--line)}
th{background:transparent;color:var(--muted);font-weight:600;cursor:pointer;
  user-select:none;white-space:nowrap;position:sticky;top:0}
th.num,td.num{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(128,128,128,.06)}
.path{word-break:break-all;color:var(--ink)}
a.path{color:var(--accent);text-decoration:none}
a.path:hover{text-decoration:underline}
a.folder{text-decoration:none;margin-left:8px;opacity:.6;font-size:13px;cursor:pointer}
a.folder:hover{opacity:1}
.tile a{color:#fff;text-decoration:none}
.actions{margin:2px 0 12px}
.btn{background:var(--accent);color:#fff;border:none;border-radius:8px;
  padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer}
.btn:hover{filter:brightness(1.08)}
.btn.copied{background:var(--good)}
.muted{color:var(--muted)}
.bar{height:8px;border-radius:5px;background:var(--bar);opacity:.85}
.barcell{min-width:120px}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:20px;
  background:rgba(128,128,128,.16);color:var(--muted);margin-left:6px}
.note{color:var(--muted);font-size:12.5px;margin:6px 0 0}
.toggle{float:right;font-size:12px;color:var(--muted);cursor:pointer;
  border:1px solid var(--line);border-radius:20px;padding:3px 10px;background:var(--panel)}
.scroll{overflow-x:auto}
details{margin-top:10px}summary{cursor:pointer;color:var(--muted)}
</style>
</head>
<body>
<div class="wrap">
  <span class="toggle" onclick="toggleTheme()">toggle theme</span>
  <h1>Disk space report</h1>
  <div class="sub">__ROOT__</div>
  <div class="cards">__CARDS__</div>

  <h2>Where the space is</h2>
  <div class="note">Each tile is an item directly inside the scanned folder, sized by how much disk it uses. Hover for detail; click a tile to open that file or folder.</div>
  <div class="treemap" id="treemap"></div>

  __SECTIONS__

  <p class="note">Generated __GENERATED__ by disk_analyzer.py &middot; nothing was deleted. Review the accompanying <code>review-then-run.ps1</code> to act on suggestions.</p>
</div>
<script>
var TREEDATA = __TREEJSON__;
var CLEARCMD = __CLEARCMD__;

function copyClearCmd(btn){
  var t=CLEARCMD||'';
  if(!t){return;}
  var done=function(){
    if(!btn.getAttribute('data-label')){btn.setAttribute('data-label',btn.textContent);}
    btn.classList.add('copied');
    btn.textContent='Copied ✓  — paste into PowerShell and press Enter';
    setTimeout(function(){btn.textContent=btn.getAttribute('data-label');btn.classList.remove('copied');},2600);
  };
  try{
    var ta=document.createElement('textarea');ta.value=t;
    ta.style.position='fixed';ta.style.top='-1000px';ta.style.opacity='0';
    document.body.appendChild(ta);ta.focus();ta.select();
    var ok=document.execCommand('copy');document.body.removeChild(ta);
    if(ok){done();return;}
  }catch(e){}
  if(navigator.clipboard){navigator.clipboard.writeText(t).then(done,function(){});}
}

function human(n){var u=['B','KB','MB','GB','TB'],i=0;n=+n;
  while(n>=1024&&i<4){n/=1024;i++;} return (i===0? n.toFixed(0):n.toFixed(1))+' '+u[i];}

function layout(items,x,y,w,h,out){
  if(!items.length)return;
  if(items.length===1){var it=items[0];out.push({d:it,x:x,y:y,w:w,h:h});return;}
  var total=0,i;for(i=0;i<items.length;i++)total+=items[i].s;
  if(total<=0){return;}
  var acc=0,k=0;for(k=0;k<items.length-1;k++){if(acc+items[k].s>=total/2)break;acc+=items[k].s;}
  // Always keep both sides non-empty AND strictly smaller so recursion terminates.
  var cut=Math.min(Math.max(k+1,1),items.length-1);
  var a=items.slice(0,cut),b=items.slice(cut),aS=0;for(i=0;i<a.length;i++)aS+=a[i].s;
  var frac=aS/total;
  if(w>=h){var aw=w*frac;layout(a,x,y,aw,h,out);layout(b,x+aw,y,w-aw,h,out);}
  else{var ah=h*frac;layout(a,x,y,w,ah,out);layout(b,x,y+ah,w,h-ah,out);}
}
var PALETTE=['#3563e9','#7b61ff','#0ea5e9','#059669','#d97706','#dc2626',
  '#db2777','#6366f1','#0891b2','#65a30d','#ca8a04','#9333ea'];
function renderTree(){
  var el=document.getElementById('treemap');el.innerHTML='';
  var W=el.clientWidth,H=el.clientHeight;
  var data=TREEDATA.filter(function(d){return d.s>0;});
  if(!data.length){el.innerHTML='<div class="tile" style="left:0;top:0;right:0;bottom:0;color:var(--muted)">empty</div>';return;}
  var out=[];layout(data,0,0,W,H,out);
  out.forEach(function(r,idx){
    var t=document.createElement('div');t.className='tile';
    t.style.left=r.x+'px';t.style.top=r.y+'px';
    t.style.width=Math.max(r.w-1,0)+'px';t.style.height=Math.max(r.h-1,0)+'px';
    t.style.background=PALETTE[TREEDATA.indexOf(r.d)%PALETTE.length];
    t.title=r.d.n+'  '+human(r.d.s)+(r.d.u?'  (click to open)':'');
    if(r.d.u){t.style.cursor='pointer';
      (function(u){t.addEventListener('click',function(){window.open(u,'_blank');});})(r.d.u);}
    if(r.w>54&&r.h>26){
      t.innerHTML='<div class="tn">'+(r.d.dir?'&#128193; ':'')+r.d.n+'</div>'+
                  '<div class="ts">'+human(r.d.s)+'</div>';}
    el.appendChild(t);
  });
}
window.addEventListener('resize',renderTree);

function sortTable(table,idx,numeric){
  var tb=table.tBodies[0],rows=Array.prototype.slice.call(tb.rows);
  var dir=table.getAttribute('data-sd')==='asc'?-1:1;
  table.setAttribute('data-sd',dir===1?'desc':'asc');
  rows.sort(function(a,b){
    var x=a.cells[idx].getAttribute('data-sort'),y=b.cells[idx].getAttribute('data-sort');
    if(numeric){x=parseFloat(x)||0;y=parseFloat(y)||0;return (x-y)*dir;}
    return x.localeCompare(y)*dir;});
  rows.forEach(function(r){tb.appendChild(r);});
}
document.querySelectorAll('table.sortable').forEach(function(t){
  Array.prototype.forEach.call(t.tHead.rows[0].cells,function(th,i){
    var numeric=th.classList.contains('num');
    th.addEventListener('click',function(){sortTable(t,i,numeric);});
  });
});

function toggleTheme(){
  var r=document.documentElement;
  var cur=r.getAttribute('data-theme');
  if(!cur){var dark=window.matchMedia('(prefers-color-scheme: dark)').matches;cur=dark?'dark':'light';}
  r.setAttribute('data-theme',cur==='dark'?'light':'dark');
  renderTree();
}
renderTree();
</script>
</body>
</html>
"""


def _bar(size, maxsize):
    pct = 0 if maxsize <= 0 else max(1, round(100 * size / maxsize))
    return '<div class="bar" style="width:%d%%"></div>' % pct


def _table(title, note, headers, rows, extra=""):
    """headers: list of (label, is_num). rows: list of list of (display, sortval)."""
    thead = "".join(
        '<th class="%s">%s</th>' % ("num" if isnum else "", html.escape(lbl))
        for lbl, isnum in headers)
    body = []
    for row in rows:
        cells = []
        for (disp, sortval), (_, isnum) in zip(row, headers):
            cells.append('<td class="%s" data-sort="%s">%s</td>'
                         % ("num" if isnum else "path",
                            html.escape(str(sortval)), disp))
        body.append("<tr>" + "".join(cells) + "</tr>")
    note_html = ('<div class="note">%s</div>' % html.escape(note)) if note else ""
    return ("<h2>%s</h2>%s%s<div class=\"scroll\"><table class=\"sortable\" data-sd=\"desc\">"
            "<thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>"
            % (html.escape(title), note_html, extra, thead, "".join(body)))


def render_html(root, total_bytes, files, dirs_top, files_top, ext_top,
                children, dup_groups, crumbs, reclaimables, skipped,
                opts, out_path, clear_caches_path=None, oldversions=None):
    oldversions = oldversions or []
    dup_waste = sum((len(g) - 1) * g[0].size for g in dup_groups)
    crumb_bytes = sum(f.size for f, _ in crumbs)
    reclaim_bytes = sum(s for _, s in reclaimables)
    ver_flagged = sum(f['size'] for f in oldversions if not f['keep'])
    clearcmd = ('powershell -ExecutionPolicy Bypass -File "%s"'
                % os.path.abspath(clear_caches_path)) if (clear_caches_path and reclaimables) else ""

    cards = [
        ('<div class="card"><div class="n">%s</div><div class="l">total size</div></div>' % human(total_bytes)),
        ('<div class="card"><div class="n">%s</div><div class="l">files</div></div>' % f"{len(files):,}"),
        ('<div class="card warn"><div class="n">%s</div><div class="l">duplicate waste</div></div>' % human(dup_waste)),
        ('<div class="card warn"><div class="n">%s</div><div class="l">crumbs</div></div>' % human(crumb_bytes)),
    ]
    if reclaimables:
        cards.append('<div class="card good"><div class="n">%s</div><div class="l">clearable caches</div></div>' % human(reclaim_bytes))
    if oldversions:
        cards.append('<div class="card warn"><div class="n">%s</div><div class="l">old/stale versions</div></div>' % human(ver_flagged))

    sections = []

    # biggest files
    maxf = files_top[0].size if files_top else 0
    rows = []
    for f in files_top:
        rows.append([
            (_plink(f.path, _short(f.path, root)) + _dir_link(f.path), _short(f.path, root)),
            (human(f.size), f.size),
            ('<div class="barcell">%s</div>' % _bar(f.size, maxf), f.size),
            (_date(f.mtime), int(f.mtime)),
        ])
    sections.append(_table("Biggest files", "Click a filename to open it, or the \U0001F4C2 icon to open its folder.",
                           [("File", False), ("Size", True), ("", True), ("Modified", True)], rows))

    # biggest folders
    maxd = dirs_top[0][1] if dirs_top else 0
    rows = []
    for path, size in dirs_top:
        rows.append([
            (('<span class="path">%s</span>' % html.escape(_short(path, root))), _short(path, root)),
            (human(size), size),
            ('<div class="barcell">%s</div>' % _bar(size, maxd), size),
        ])
    sections.append(_table("Biggest folders", "Folders ranked by their total (recursive) size. Click a folder to open it.",
                           [("Folder", False), ("Size", True), ("", True)], rows))

    # by type
    maxe = ext_top[0][1] if ext_top else 0
    rows = []
    for ext, size, count in ext_top:
        rows.append([
            (html.escape(ext), ext),
            (human(size), size),
            (f"{count:,}", count),
            ('<div class="barcell">%s</div>' % _bar(size, maxe), size),
        ])
    sections.append(_table("Space by file type", "Which kinds of files add up to the most.",
                           [("Type", False), ("Size", True), ("Files", True), ("", True)], rows))

    # reclaimable caches
    if reclaimables:
        maxr = reclaimables[0][1]
        rows = []
        for path, size in reclaimables:
            rows.append([
                (_plink(path, _short(path, root)), _short(path, root)),
                (human(size), size),
                ('<div class="barcell">%s</div>' % _bar(size, maxr), size),
            ])
        clear_extra = ""
        if clearcmd:
            clear_extra = (
                '<div class="actions">'
                '<button class="btn" type="button" onclick="copyClearCmd(this)">'
                '\U0001F9F9 Copy “clear all safe caches” command</button>'
                '<span class="note" style="margin-left:10px">'
                'Sends ~%s in %d folder(s) to the Recycle Bin, asking you to confirm first. '
                'Paste the copied command into PowerShell, or right-click '
                '<code>%s</code> &rarr; Run with PowerShell.</span></div>'
            ) % (human(reclaim_bytes), len(reclaimables),
                 html.escape(os.path.basename(clear_caches_path or "clear-safe-caches.ps1")))
        sections.append(_table("Safe-to-clear caches",
                               "Well-known regenerable caches. These rebuild themselves; clearing them is usually safe.",
                               [("Cache folder", False), ("Size", True), ("", True)], rows,
                               extra=clear_extra))

    # duplicates
    if dup_groups:
        rows = []
        for gi, g in enumerate(dup_groups[:opts.top]):
            waste = (len(g) - 1) * g[0].size
            paths = "<br>".join(_plink(f.path, _short(f.path, root)) for f in g)
            rows.append([
                (('<span class="path">%s</span>' % paths), _short(g[0].path, root)),
                (f"{len(g)}", len(g)),
                (human(g[0].size), g[0].size),
                (human(waste), waste),
            ])
        sections.append(_table("Duplicate files", "Byte-for-byte identical copies. 'Wasted' = space freed if you keep one copy.",
                               [("Copies (identical content)", False), ("#", True), ("Each", True), ("Wasted", True)], rows))

    # crumbs
    if crumbs:
        rows = []
        for f, age in crumbs[:opts.top]:
            prot = is_protected(f.path, opts.protected)
            tag = '<span class="tag">protected</span>' if prot else ""
            rows.append([
                (('%s%s' % (_plink(f.path, _short(f.path, root)), tag)), _short(f.path, root)),
                (human(f.size), f.size),
                ("%.0f mo" % (age / 30.0), age),
                (_date(f.mtime), int(f.mtime)),
            ])
        note = ("Small (< %s), not modified in > %d days. 'protected' items live in managed folders and are NOT put in the deletion script."
                % (human(opts.crumb_size), opts.crumb_age_days))
        sections.append(_table("Crumbs (small & forgotten)", note,
                               [("File", False), ("Size", True), ("Age", True), ("Last modified", True)], rows))

    # old & stale versions
    if oldversions:
        catlabels = {"app-version": "Old app version",
                     "lib-archive": "Duplicate library archive (cached)",
                     "stale-deps": "Stale dependency folder", "git-repo": "Old git repo"}
        rows = []
        for fnd in oldversions[:opts.top * 3]:
            detail = fnd['detail'] + (("  ⚠ " + fnd['risk']) if fnd['risk'] else "")
            rows.append([
                (_plink(fnd['path'], _short(fnd['path'], root)), _short(fnd['path'], root)),
                (html.escape(catlabels.get(fnd['cat'], fnd['cat'])), fnd['cat']),
                (human(fnd['size']), fnd['size']),
                (html.escape(detail), fnd['detail']),
            ])
        note = ("Superseded / stale items — delete with caution. Keeps the newest/active "
                "one; duplicate libraries are cached DOWNLOAD archives only (.whl/.nupkg), "
                "never installed libraries an app runs from; stale deps warn if the project "
                "was touched recently.")
        sections.append(_table("Old & stale versions", note,
                               [("Item", False), ("Category", False), ("Size", True),
                                ("Detail", False)], rows))

    if skipped:
        skip_rows = "".join("<li class='muted'>%s <span class='muted'>(%s)</span></li>"
                            % (html.escape(_short(p, root)), html.escape(e)) for p, e in skipped[:200])
        sections.append("<details><summary>%d item(s) skipped (permission denied / vanished / unreadable)</summary><ul>%s</ul></details>"
                        % (len(skipped), skip_rows))

    tree_json = json.dumps([{"n": n, "s": s, "dir": d, "u": _file_uri(p)}
                            for n, s, d, p in children[:60]])

    doc = (HTML_TEMPLATE
           .replace("__ROOTNAME__", html.escape(os.path.basename(root) or root))
           .replace("__ROOT__", html.escape(root))
           .replace("__CARDS__", "".join(cards))
           .replace("__SECTIONS__", "".join(sections))
           .replace("__TREEJSON__", tree_json)
           .replace("__CLEARCMD__", json.dumps(clearcmd))
           .replace("__GENERATED__", time.strftime("%Y-%m-%d %H:%M")))

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return out_path


def _date(ts):
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    except (ValueError, OSError):
        return "?"


# --------------------------------------------------------------------------- #
# PowerShell review script
# --------------------------------------------------------------------------- #
def _ps_quote(path):
    return "'" + os.path.abspath(path).replace("'", "''") + "'"


def write_review_script(root, dup_groups, crumbs, opts, out_path):
    lines = []
    a = lines.append
    a("# ============================================================")
    a("#  review-then-run.ps1")
    a("#  Generated by disk_analyzer.py for:")
    a("#    %s" % root)
    a("#")
    a("#  NOTHING here runs until YOU uncomment it. Every delete below is")
    a("#  commented out (starts with '#'). Read each line, delete the '#'")
    a("#  in front of the ones you approve, save, then run this script.")
    a("#")
    a("#  Deletes go to the RECYCLE BIN (recoverable), not permanent.")
    a("#  Run in PowerShell:   powershell -ExecutionPolicy Bypass -File .\\review-then-run.ps1")
    a("# ============================================================")
    a("")
    a("Add-Type -AssemblyName Microsoft.VisualBasic")
    a("function Remove-ToRecycleBin($Path){")
    a("    if(Test-Path -LiteralPath $Path){")
    a("        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(")
    a("            $Path,'OnlyErrorDialogs','SendToRecycleBin')")
    a("        Write-Host \"Recycled: $Path\"")
    a("    } else { Write-Host \"Already gone: $Path\" }")
    a("}")
    a("")

    dup_reclaim = 0
    for g in dup_groups:
        deletable = [f for f in g if not is_protected(f.path, opts.protected)]
        if len(deletable) >= 2:
            dup_reclaim += (len(deletable) - 1) * g[0].size
    a("# ---------- DUPLICATES  (~%s reclaimable) ----------" % human(dup_reclaim))
    a("# For each set of identical files, ONE copy is kept (left as a comment for")
    a("# reference) and the extra copies are listed below it, ready to uncomment.")
    a("")
    kept_count = 0
    for g in dup_groups:
        deletable = [f for f in g if not is_protected(f.path, opts.protected)]
        if len(deletable) < 2:
            continue
        deletable.sort(key=lambda f: (f.path.count(os.sep), f.mtime))
        keep = deletable[0]
        a("#   KEEP : %s  (%s)" % (keep.path, human(keep.size)))
        for f in deletable[1:]:
            a("# Remove-ToRecycleBin %s" % _ps_quote(f.path))
        a("")
        kept_count += 1
    if kept_count == 0:
        a("#   (no duplicate sets outside protected folders)")
        a("")

    crumb_bytes = sum(f.size for f, _ in crumbs if not is_protected(f.path, opts.protected))
    a("# ---------- CRUMBS: small (< %s) & untouched > %d days  (~%s) ----------"
      % (human(opts.crumb_size), opts.crumb_age_days, human(crumb_bytes)))
    a("")
    listed = 0
    for f, age in crumbs:
        if is_protected(f.path, opts.protected):
            continue
        a("# Remove-ToRecycleBin %s   # %s, %.0f months old"
          % (_ps_quote(f.path), human(f.size), age / 30.0))
        listed += 1
    if listed == 0:
        a("#   (no crumbs outside protected folders)")
    a("")
    a("Write-Host 'Done. (If nothing was recycled, you had not uncommented any lines.)'")

    with open(out_path, "w", encoding="utf-8-sig") as fh:
        fh.write("\n".join(lines))
    return out_path


def write_clear_caches_script(root, reclaimables, opts, out_path):
    """Generate a PowerShell script that sends regenerable caches to the Recycle Bin."""
    total = sum(s for _, s in reclaimables)
    lines = []
    a = lines.append
    a("# ============================================================")
    a("#  clear-safe-caches.ps1")
    a("#  Generated by disk_analyzer.py for:")
    a("#    %s" % root)
    a("#")
    a("#  Sends regenerable CACHE folders to the RECYCLE BIN (recoverable).")
    a("#  These rebuild themselves the next time the relevant tool runs")
    a("#  (npm/pip caches, __pycache__, node_modules, browser caches, ...).")
    a("#  You are asked to CONFIRM before anything is deleted.")
    a("#")
    a("#  Run: right-click this file -> Run with PowerShell,  or:")
    a("#       powershell -ExecutionPolicy Bypass -File .\\%s" % os.path.basename(out_path))
    a("# ============================================================")
    a("")
    a("Add-Type -AssemblyName Microsoft.VisualBasic")
    a("$VB = [Microsoft.VisualBasic.FileIO.FileSystem]")
    a("")
    a("$targets = @(")
    for path, size in reclaimables:
        a("  @{ Path = %s; Size = %d }," % (_ps_quote(path), int(size)))
    a(")")
    a("")
    a("$totalMB = [math]::Round(%d / 1MB, 1)" % int(total))
    a("Write-Host \"\"")
    a("Write-Host \"These regenerable cache folders will be sent to the Recycle Bin:\" -ForegroundColor Cyan")
    a("foreach ($t in $targets) { \"{0,10:N1} MB  {1}\" -f ($t.Size/1MB), $t.Path }")
    a("Write-Host \"\"")
    a("Write-Host \"Total: ~$totalMB MB across $($targets.Count) folder(s).\" -ForegroundColor Yellow")
    a("$ans = Read-Host 'Send these to the Recycle Bin? (y/N)'")
    a("if ($ans -notmatch '^(y|Y|yes)$') { Write-Host 'Cancelled. Nothing was deleted.'; return }")
    a("")
    a("foreach ($t in $targets) {")
    a("  if (Test-Path -LiteralPath $t.Path) {")
    a("    try {")
    a("      $VB::DeleteDirectory($t.Path,'OnlyErrorDialogs','SendToRecycleBin')")
    a("      Write-Host \"Recycled: $($t.Path)\" -ForegroundColor Green")
    a("    } catch { Write-Host \"Skipped (in use / locked): $($t.Path)\" -ForegroundColor Red }")
    a("  } else { Write-Host \"Already gone: $($t.Path)\" }")
    a("}")
    a("Write-Host 'Done. Recycled items can be restored from the Recycle Bin.' -ForegroundColor Green")
    with open(out_path, "w", encoding="utf-8-sig") as fh:
        fh.write("\n".join(lines))
    return out_path


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        description="Visualize disk usage; find duplicates and small/old 'crumbs'. Never deletes.")
    p.add_argument("path", nargs="?", default=".", help="folder to analyze (default: current dir)")
    p.add_argument("--crumb-size", default="1MB", help="crumb size threshold (default 1MB)")
    p.add_argument("--crumb-age-days", type=int, default=365, help="crumb age threshold in days (default 365)")
    p.add_argument("--top", type=int, default=25, help="rows in ranked tables (default 25)")
    p.add_argument("--min-dup-size", default="4KB", help="ignore files smaller than this for dup detection (default 4KB)")
    p.add_argument("--out-dir", default=None, help="where to write report + ps1 (default: this script's folder)")
    p.add_argument("--no-open", action="store_true", help="do not open the HTML report in a browser")
    p.add_argument("--no-color", action="store_true", help="disable colored terminal output")
    p.add_argument("--no-duplicates", action="store_true", help="skip duplicate detection (faster on huge trees)")
    p.add_argument("--follow-symlinks", action="store_true", help="follow symlinks/junctions (off by default)")
    p.add_argument("--no-default-protect", action="store_true", help="do not protect .git/node_modules/AppData/etc.")
    p.add_argument("--protect", nargs="*", default=[], help="extra folder names to exclude from deletion suggestions")
    p.add_argument("--gui", action="store_true", help="launch the graphical (tkinter) interface")
    return p


def main(argv=None):
    opts = build_parser().parse_args(argv)

    if getattr(opts, "gui", False):
        import disk_gui  # lazy: only import tkinter when the GUI is requested
        return disk_gui.launch(None if opts.path == "." else opts.path)

    opts.crumb_size = parse_size(opts.crumb_size)
    opts.min_dup_size = parse_size(opts.min_dup_size)
    opts.protected = set() if opts.no_default_protect else set(DEFAULT_PROTECTED)
    opts.protected.update(opts.protect)

    root = os.path.abspath(opts.path)
    if not os.path.isdir(root):
        print("Not a folder: %s" % root, file=sys.stderr)
        return 2

    out_dir = os.path.abspath(opts.out_dir) if opts.out_dir else os.path.dirname(os.path.abspath(__file__))
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    tag = "".join(ch if ch.isalnum() else "_" for ch in os.path.basename(root))[:40] or "root"
    html_path = os.path.join(out_dir, "disk_report_%s_%s.html" % (tag, stamp))
    ps1_path = os.path.join(out_dir, "review-then-run_%s_%s.ps1" % (tag, stamp))
    clear_path = os.path.join(out_dir, "clear-safe-caches_%s_%s.ps1" % (tag, stamp))

    print("Scanning %s ..." % root, file=sys.stderr)
    t0 = time.time()
    files, dir_sizes, skipped, total_bytes = scan(root, opts)
    print("  scan done: %s files, %s, %.1fs"
          % (f"{len(files):,}", human(total_bytes), time.time() - t0), file=sys.stderr)

    dirs_top = largest_dirs(dir_sizes, root, opts.top)
    files_top = largest_files(files, opts.top)
    ext_top = size_by_ext(files, opts.top)
    children = root_children(dir_sizes, files, root)
    reclaimables = find_reclaimables(dir_sizes, root)

    now = time.time()
    crumbs = find_crumbs(files, opts, now)
    oldversions = find_old_versions(dir_sizes, files, root, now=now,
                                    stale_days=opts.crumb_age_days)

    if opts.no_duplicates:
        dup_groups = []
    else:
        print("  finding duplicates ...", file=sys.stderr)
        dup_groups = find_duplicates(files, opts)

    use_color = (not opts.no_color) and sys.stdout.isatty() and _enable_ansi()
    render_terminal(root, total_bytes, files, dirs_top, files_top,
                    dup_groups, crumbs, reclaimables, skipped, use_color)

    if reclaimables:
        write_clear_caches_script(root, reclaimables, opts, clear_path)
    else:
        clear_path = None

    render_html(root, total_bytes, files, dirs_top, files_top, ext_top,
                children, dup_groups, crumbs, reclaimables, skipped, opts,
                html_path, clear_caches_path=clear_path, oldversions=oldversions)
    write_review_script(root, dup_groups, crumbs, opts, ps1_path)

    print("  HTML report : %s" % html_path)
    print("  Review script: %s" % ps1_path)
    print("  (Open the .ps1 to review suggested deletions - all commented out, Recycle-Bin based.)")
    if clear_path:
        print("  Clear caches : %s" % clear_path)
        print("  (Run it, or use the 'clear all safe caches' button in the HTML report.)")

    if not opts.no_open:
        try:
            webbrowser.open("file:///" + html_path.replace("\\", "/"))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
