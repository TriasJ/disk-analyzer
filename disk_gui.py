#!/usr/bin/env python3
"""
disk_gui.py - native tkinter GUI for the disk analyzer.

A tab per section (Overview, Treemap, Biggest files, Biggest folders, By type,
Duplicates, Crumbs, Caches). Reuses the whole analysis backend in
disk_analyzer.py; the HTML report export is unchanged and still available.

Run:  python disk_analyzer.py --gui        (recommended)
  or: pythonw disk_gui.py ["C:\\folder"]
"""

import os
import sys
import time
import queue
import threading
import tempfile
import shutil
import subprocess
import webbrowser
from types import SimpleNamespace

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Make the sibling disk_analyzer importable no matter the working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disk_analyzer as da
from disk_analyzer import (
    scan, largest_files, largest_dirs, size_by_ext, root_children,
    find_duplicates, find_crumbs, find_reclaimables, find_old_versions,
    render_html, write_review_script, write_clear_caches_script,
    parse_size, human, _short, DEFAULT_PROTECTED,
)

VER_CATS = [("app-version", "Old app versions"),
            ("lib-archive", "Duplicate library archives (cached)"),
            ("stale-deps", "Stale dependency folders"),
            ("git-repo", "Old git repos")]

PALETTE = ['#3563e9', '#7b61ff', '#0ea5e9', '#059669', '#d97706', '#dc2626',
           '#db2777', '#6366f1', '#0891b2', '#65a30d', '#ca8a04', '#9333ea']
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def default_folder():
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return docs if os.path.isdir(docs) else os.path.expanduser("~")


def treemap_layout(items, x, y, w, h, out):
    """Recursive binary-split treemap (ported from the HTML report's layout())."""
    if not items:
        return
    if len(items) == 1:
        out.append({'d': items[0], 'x': x, 'y': y, 'w': w, 'h': h})
        return
    total = sum(it['s'] for it in items)
    if total <= 0:
        return
    acc, k, n = 0, 0, len(items)
    while k < n - 1:
        if acc + items[k]['s'] >= total / 2:
            break
        acc += items[k]['s']
        k += 1
    cut = min(max(k + 1, 1), n - 1)
    a, b = items[:cut], items[cut:]
    frac = sum(it['s'] for it in a) / total
    if w >= h:
        aw = w * frac
        treemap_layout(a, x, y, aw, h, out)
        treemap_layout(b, x + aw, y, w - aw, h, out)
    else:
        ah = h * frac
        treemap_layout(a, x, y, w, ah, out)
        treemap_layout(b, x, y + ah, w, h - ah, out)


class DiskAnalyzerGUI:
    def __init__(self, root, initial=None):
        self.root = root
        self.q = queue.Queue()
        self.results = None
        self.dup_groups = None
        self.tm_children = None
        self.tm_root = None
        self.scan_root = None
        self.busy = False
        self.dup_busy = False
        self.recycle_busy = False
        self._menu_target = None

        root.title("Disk Analyzer")
        root.geometry("1060x710")
        root.minsize(840, 560)

        # option vars
        self.opt_top = tk.IntVar(value=30)
        self.opt_crumb_size = tk.StringVar(value="1MB")
        self.opt_crumb_age = tk.IntVar(value=365)
        self.opt_min_dup = tk.StringVar(value="4KB")
        self.opt_follow = tk.BooleanVar(value=False)

        self.folder_var = tk.StringVar(value=initial or default_folder())
        self.status = tk.StringVar(value="Choose a folder and press Scan.")

        self._build_menu()
        self._build_topbar()
        self._build_tabs()
        self.root.after(120, self._poll)

    # ---------------------------------------------------------------- layout
    def _build_menu(self):
        m = tk.Menu(self.root)
        fm = tk.Menu(m, tearoff=0)
        fm.add_command(label="Open folder\u2026", command=self.browse)
        fm.add_command(label="Export HTML report", command=self.export_html)
        fm.add_command(label="Save review script\u2026", command=self.save_review)
        fm.add_separator()
        fm.add_command(label="Exit", command=self.root.destroy)
        m.add_cascade(label="File", menu=fm)
        om = tk.Menu(m, tearoff=0)
        om.add_command(label="Scan options\u2026", command=self.open_options)
        m.add_cascade(label="Options", menu=om)
        tm = tk.Menu(m, tearoff=0)
        tm.add_command(label="Recommended cleanup\u2026", command=self._open_recommended)
        tm.add_command(label="Cleanup wizard\u2026", command=self._open_wizard)
        tm.add_command(label="Refresh system info", command=self._refresh_system)
        m.add_cascade(label="Tools", menu=tm)
        hm = tk.Menu(m, tearoff=0)
        hm.add_command(label="About", command=lambda: messagebox.showinfo(
            "About", "Disk Analyzer \u2014 tkinter GUI\nReuses disk_analyzer.py; "
            "the HTML report is still available via File \u2192 Export."))
        m.add_cascade(label="Help", menu=hm)
        self.root.config(menu=m)

    def _build_topbar(self):
        bar = ttk.Frame(self.root, padding=(10, 8))
        bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(bar, text="Folder:").pack(side=tk.LEFT)
        ttk.Entry(bar, textvariable=self.folder_var, width=58).pack(
            side=tk.LEFT, padx=6)
        ttk.Button(bar, text="Browse\u2026", command=self.browse).pack(side=tk.LEFT)
        self.scan_btn = ttk.Button(bar, text="Scan", command=self.start_scan)
        self.scan_btn.pack(side=tk.LEFT, padx=6)
        self.progress = ttk.Progressbar(bar, mode="determinate", length=170)
        self.progress.pack(side=tk.LEFT, padx=6)
        ttk.Label(bar, textvariable=self.status).pack(side=tk.LEFT, padx=6)

    def _build_tabs(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.nb = nb
        self._build_overview(nb)
        self._build_freeup(nb)
        self._build_treemap(nb)
        self.tv_files = self._table_tab(nb, "Biggest files",
                                        ("File", "Size", "Modified"),
                                        numeric=("Size",), openable=True)
        self.tv_dirs = self._table_tab(nb, "Biggest folders",
                                       ("Folder", "Size"),
                                       numeric=("Size",), openable=True)
        self.tv_types = self._table_tab(
            nb, "By type", ("Type", "Size", "Files"),
            numeric=("Size", "Files"), openable=False,
            hint="Double-click a type to list all its files (sortable by size or date).")
        self.tv_types.bind("<Double-1>", self._open_type_detail)
        self._build_duplicates(nb)
        self._build_crumbs(nb)
        self._build_caches(nb)
        self._build_versions(nb)
        # Redraw the treemap when its tab becomes visible (its canvas only gets a
        # real size once mapped).
        nb.bind("<<NotebookTabChanged>>", lambda e: self._draw_treemap())

    def _build_overview(self, nb):
        f = ttk.Frame(nb, padding=16)
        nb.add(f, text="Overview")
        cards = ttk.Frame(f)
        cards.pack(fill=tk.X)
        self.card_vars = {}
        specs = [("total", "Total size"), ("files", "Files"),
                 ("dupe", "Duplicate waste"), ("crumbs", "Crumbs"),
                 ("caches", "Clearable caches"), ("versions", "Old/stale")]
        for i, (key, label) in enumerate(specs):
            card = ttk.Frame(cards, relief="solid", borderwidth=1, padding=12)
            card.grid(row=0, column=i, padx=6, sticky="nsew")
            cards.columnconfigure(i, weight=1)
            var = tk.StringVar(value="\u2014")
            self.card_vars[key] = var
            ttk.Label(card, textvariable=var, font=("Segoe UI", 16, "bold")).pack(anchor="w")
            ttk.Label(card, text=label, foreground="#777").pack(anchor="w")
        self.ov_meta = tk.StringVar(value="No scan yet.")
        ttk.Label(f, textvariable=self.ov_meta, foreground="#777",
                  justify="left").pack(anchor="w", pady=(16, 8))
        btns = ttk.Frame(f)
        btns.pack(anchor="w", pady=8)
        ttk.Button(btns, text="\u26a1 Recommended cleanup",
                   command=self._open_recommended).pack(side=tk.LEFT)
        ttk.Button(btns, text="Cleanup wizard\u2026",
                   command=self._open_wizard).pack(side=tk.LEFT, padx=8)
        ttk.Button(btns, text="Export HTML report",
                   command=self.export_html).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btns, text="Save review script\u2026",
                   command=self.save_review).pack(side=tk.LEFT)

    def _build_treemap(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="Treemap")
        bar = ttk.Frame(f)
        bar.pack(fill=tk.X, padx=6, pady=(6, 0))
        self.tm_up_btn = ttk.Button(bar, text="⬆ Up", width=7,
                                    command=self._treemap_up, state=tk.DISABLED)
        self.tm_up_btn.pack(side=tk.LEFT)
        ttk.Button(bar, text="⌂ Root", width=8,
                   command=self._treemap_root).pack(side=tk.LEFT, padx=(6, 10))
        self.tm_crumb = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.tm_crumb, foreground="#555").pack(side=tk.LEFT)
        ttk.Label(f, foreground="#999", padding=(6, 2),
                  text="Double-click a folder to zoom in · double-click a file to open "
                       "it · right-click for more.").pack(anchor="w")
        self.canvas = tk.Canvas(f, background="#12151a", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.canvas.bind("<Configure>", lambda e: self._draw_treemap())
        self._tm_menu = tk.Menu(self.canvas, tearoff=0)

    def _build_duplicates(self, nb):
        f = ttk.Frame(nb, padding=6)
        nb.add(f, text="Duplicates")
        top = ttk.Frame(f)
        top.pack(fill=tk.X, pady=(0, 6))
        self.dup_btn = ttk.Button(top, text="Find duplicates",
                                  command=self.find_dupes)
        self.dup_btn.pack(side=tk.LEFT)
        self.dup_progress = ttk.Progressbar(top, mode="determinate", length=220)
        self.dup_progress.pack(side=tk.LEFT, padx=8)
        ttk.Label(top, text="(reads file contents \u2014 can take a few minutes)",
                  foreground="#777").pack(side=tk.LEFT)
        act = ttk.Frame(f)
        act.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(act, text="Select redundant (keep 1/group)",
                   command=self._dup_select_redundant).pack(side=tk.LEFT)
        ttk.Button(act, text="Recycle selected",
                   command=lambda: self._dupes_action("recycle")).pack(side=tk.LEFT, padx=8)
        ttk.Button(act, text="Delete selected",
                   command=lambda: self._dupes_action("delete")).pack(side=tk.LEFT)
        wrap = ttk.Frame(f)
        wrap.pack(fill=tk.BOTH, expand=True)
        self.tv_dupes = ttk.Treeview(wrap, columns=("wasted",),
                                     show="tree headings", selectmode="extended")
        self.tv_dupes.heading("#0", text="Duplicate group / files")
        self.tv_dupes.heading("wasted", text="Wasted")
        self.tv_dupes.column("#0", width=720, stretch=True)
        self.tv_dupes.column("wasted", width=110, anchor="e", stretch=False)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tv_dupes.yview)
        self.tv_dupes.configure(yscrollcommand=vsb.set)
        self.tv_dupes.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.dup_path = {}
        self.tv_dupes.bind("<Double-1>", self._open_dup)

    def _build_caches(self, nb):
        f = ttk.Frame(nb, padding=6)
        nb.add(f, text="Caches")
        top = ttk.Frame(f)
        top.pack(fill=tk.X, pady=(0, 6))
        self.cache_total = tk.StringVar(value="No scan yet.")
        ttk.Label(top, textvariable=self.cache_total).pack(side=tk.LEFT)
        ttk.Button(top, text="Clear caches \u2192 Recycle Bin",
                   command=self.clear_caches).pack(side=tk.RIGHT)
        ttk.Button(top, text="Empty Recycle Bin",
                   command=self.empty_recycle_bin).pack(side=tk.RIGHT, padx=8)
        self.tv_caches = self._make_tree(f, ("Cache folder", "Size"),
                                         numeric=("Size",))
        self._attach_open(self.tv_caches)

    def _build_versions(self, nb):
        f = ttk.Frame(nb, padding=6)
        nb.add(f, text="Old versions")
        ttk.Label(f, foreground="#777", justify="left", padding=(2, 0, 2, 4),
                  text="Superseded / stale items (delete with caution). Keeps the newest, "
                       "excludes what's in use. Duplicate libraries = cached DOWNLOAD "
                       "archives only (.whl/.nupkg) — never installed libraries an app runs "
                       "from. Everything goes to the Recycle Bin (recoverable).").pack(anchor="w")
        top = ttk.Frame(f)
        top.pack(fill=tk.X, pady=(0, 6))
        self.ver_total = tk.StringVar(value="No scan yet.")
        ttk.Label(top, textvariable=self.ver_total).pack(side=tk.LEFT)
        ttk.Button(top, text="Delete selected",
                   command=lambda: self._versions_action("delete")).pack(side=tk.RIGHT)
        ttk.Button(top, text="Recycle selected",
                   command=lambda: self._versions_action("recycle")).pack(side=tk.RIGHT, padx=8)
        ttk.Button(top, text="Select all flagged (safe)",
                   command=self._versions_select_flagged).pack(side=tk.RIGHT, padx=8)
        wrap = ttk.Frame(f)
        wrap.pack(fill=tk.BOTH, expand=True)
        self.tv_versions = ttk.Treeview(wrap, columns=("size", "detail"),
                                        show="tree headings", selectmode="extended")
        self.tv_versions.heading("#0", text="Item")
        self.tv_versions.heading("size", text="Size")
        self.tv_versions.heading("detail", text="Detail")
        self.tv_versions.column("#0", width=380, stretch=True)
        self.tv_versions.column("size", width=90, anchor="e", stretch=False)
        self.tv_versions.column("detail", width=420, stretch=False)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tv_versions.yview)
        self.tv_versions.configure(yscrollcommand=vsb.set)
        self.tv_versions.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tv_versions.tag_configure("risk", foreground="#c0392b")
        self.ver_path = {}
        self.ver_flagged = []
        self.tv_versions.bind("<Double-1>", self._open_ver)

    def _fill_versions(self, findings, root):
        t = self.tv_versions
        t.delete(*t.get_children(''))
        self.ver_path = {}
        self.ver_flagged = []
        by_cat = {}
        for fnd in findings:
            by_cat.setdefault(fnd['cat'], []).append(fnd)
        grand = 0
        for cat, label in VER_CATS:
            items = by_cat.get(cat)
            if not items:
                continue
            csize = sum(i['size'] for i in items)
            grand += sum(i['size'] for i in items if not i['keep'])
            pid = "cat_" + cat
            t.insert('', 'end', iid=pid, open=True,
                     text="%s  (%d)" % (label, len(items)), values=(human(csize), ""))
            for j, fnd in enumerate(items):
                cid = "%s_%d" % (pid, j)
                tags = ("risk",) if fnd['risk'] else ()
                detail = fnd['detail'] + (("  ⚠ " + fnd['risk']) if fnd['risk'] else "")
                t.insert(pid, 'end', iid=cid, text=_short(fnd['path'], root),
                         values=(human(fnd['size']), detail), tags=tags)
                self.ver_path[cid] = fnd['path']
                if not fnd['keep']:
                    self.ver_flagged.append(cid)
        n_flag = len(self.ver_flagged)
        self.ver_total.set("%d item(s) · %s flagged as safe to remove"
                           % (len(findings), human(grand)) if findings
                           else "No superseded/stale items found.")
        return grand

    def _versions_select_flagged(self):
        self.tv_versions.selection_set(self.ver_flagged)
        if self.ver_flagged:
            self.tv_versions.see(self.ver_flagged[0])

    def _versions_action(self, mode):
        iids, paths = [], []
        for i in self.tv_versions.selection():
            p = self.ver_path.get(i)
            if p and os.path.exists(p):
                iids.append(i)
                paths.append(p)
        self._apply_removal(self.tv_versions, iids, paths, mode, "old/stale item(s)")

    def _open_ver(self, event):
        p = self.ver_path.get(self.tv_versions.focus())
        if p:
            self._safe_open(p)

    # -------------------------------------------------------------- table helpers
    def _make_tree(self, parent, columns, numeric=()):
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True)
        tree = ttk.Treeview(wrap, columns=columns, show="headings",
                            selectmode="browse")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        tree._numeric = set(numeric)
        tree._sort = {}
        tree._lastsort = (None, False)
        for c in columns:
            tree.heading(c, text=c, command=lambda cc=c: self._sort_by(tree, cc))
            tree.column(c, anchor=("e" if c in numeric else "w"),
                        width=(90 if c in numeric else 260),
                        stretch=(c not in numeric))
        return tree

    def _table_tab(self, nb, title, columns, numeric=(), openable=False, hint=None):
        f = ttk.Frame(nb, padding=6)
        nb.add(f, text=title)
        if hint:
            ttk.Label(f, text=hint, foreground="#999",
                      padding=(2, 0, 2, 4)).pack(anchor="w")
        tree = self._make_tree(f, columns, numeric)
        if openable:
            self._attach_open(tree)
        return tree

    def _attach_open(self, tree):
        tree.bind("<Double-1>", lambda e: self._open_selected(tree))
        menu = tk.Menu(tree, tearoff=0)
        menu.add_command(label="Open", command=lambda: self._safe_open(self._menu_target))
        menu.add_command(label="Open containing folder",
                         command=lambda: self.reveal(self._menu_target))
        menu.add_command(label="Copy path",
                         command=lambda: self.copy_path(self._menu_target))

        def popup(e):
            iid = tree.identify_row(e.y)
            if iid:
                tree.selection_set(iid)
                tree.focus(iid)
                self._menu_target = iid
                menu.tk_popup(e.x_root, e.y_root)
        tree.bind("<Button-3>", popup)

    def _sort_by(self, tree, col):
        items = list(tree.get_children(''))
        numeric = col in tree._numeric
        def key(iid):
            raw = tree._sort.get(iid, {})
            if col in raw:
                return raw[col]
            v = tree.set(iid, col)
            return v.lower() if isinstance(v, str) else v
        last_col, last_rev = tree._lastsort
        reverse = (not last_rev) if last_col == col else numeric
        try:
            items.sort(key=key, reverse=reverse)
        except TypeError:
            items.sort(key=lambda i: str(key(i)), reverse=reverse)
        for i, iid in enumerate(items):
            tree.move(iid, '', i)
        tree._lastsort = (col, reverse)

    # ---------------------------------------------------------------- actions
    def browse(self):
        d = filedialog.askdirectory(initialdir=self.folder_var.get() or default_folder())
        if d:
            self.folder_var.set(d)

    def _current_opts(self):
        try:
            top = max(1, int(self.opt_top.get()))
        except Exception:
            top = 30
        try:
            age = max(0, int(self.opt_crumb_age.get()))
        except Exception:
            age = 365
        return SimpleNamespace(
            top=top,
            crumb_size=parse_size(self.opt_crumb_size.get() or "1MB"),
            crumb_age_days=age,
            min_dup_size=parse_size(self.opt_min_dup.get() or "4KB"),
            follow_symlinks=bool(self.opt_follow.get()),
            no_default_protect=False, protect=[],
            protected=set(DEFAULT_PROTECTED),
            out_dir=os.path.dirname(os.path.abspath(__file__)),
        )

    def start_scan(self):
        if self.busy:
            return
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning("Disk Analyzer", "Please choose a valid folder.")
            return
        self.opts = self._current_opts()
        self.busy = True
        self.scan_btn.config(state=tk.DISABLED)
        self.progress.config(mode="indeterminate")
        self.progress.start(12)
        self.status.set("Scanning\u2026")
        root_abs = os.path.abspath(folder)
        opts = self.opts
        t0 = time.time()

        def worker():
            try:
                files, dir_sizes, skipped, total = scan(
                    root_abs, opts,
                    progress=lambda n: self.q.put(("scan_progress", n)))
                res = {
                    'root': root_abs, 'files': files, 'dir_sizes': dir_sizes,
                    'skipped': skipped, 'total': total,
                    'files_top': largest_files(files, opts.top),
                    'dirs_top': largest_dirs(dir_sizes, root_abs, opts.top),
                    'ext_top': size_by_ext(files, opts.top),
                    'children': root_children(dir_sizes, files, root_abs),
                    'crumbs': find_crumbs(files, opts, time.time()),
                    'reclaimables': find_reclaimables(dir_sizes, root_abs),
                    'oldversions': find_old_versions(dir_sizes, files, root_abs,
                                                     stale_days=opts.crumb_age_days),
                    'elapsed': time.time() - t0,
                }
                self.q.put(("scan_done", res))
            except Exception as e:  # noqa
                self.q.put(("error", "Scan failed:\n%s" % e))

        threading.Thread(target=worker, daemon=True).start()

    def find_dupes(self):
        if not self.results:
            messagebox.showinfo("Duplicates", "Scan a folder first.")
            return
        if self.dup_busy:
            return
        self.dup_busy = True
        self.dup_btn.config(state=tk.DISABLED)
        self.dup_progress.config(mode="determinate", maximum=100, value=0)
        self.status.set("Finding duplicates\u2026")
        files = self.results['files']
        opts = self.opts

        def worker():
            try:
                groups = find_duplicates(
                    files, opts,
                    progress=lambda d, t: self.q.put(("dupe_progress", (d, t))))
                self.q.put(("dupe_done", groups))
            except Exception as e:  # noqa
                self.q.put(("error", "Duplicate scan failed:\n%s" % e))

        threading.Thread(target=worker, daemon=True).start()

    def clear_caches(self):
        if not self.results or not self.results['reclaimables']:
            messagebox.showinfo("Caches", "No cache folders found. Scan first.")
            return
        recl = self.results['reclaimables']
        total = sum(s for _, s in recl)
        if not messagebox.askyesno(
                "Clear caches",
                "Send %d cache folder(s) (~%s) to the Recycle Bin?\n\n"
                "They regenerate automatically. Afterwards use "
                "\u201cEmpty Recycle Bin\u201d to actually reclaim the space."
                % (len(recl), human(total))):
            return
        paths = [p for p, _ in recl]
        self.recycle_busy = True
        self.progress.stop()
        self.progress.config(mode="determinate", maximum=len(paths), value=0)
        self.status.set("Recycling caches\u2026")

        def worker():
            try:
                self._recycle(paths,
                              progress=lambda d, t: self.q.put(("recycle_progress", (d, t))))
                self.q.put(("recycle_done", None))
            except Exception as e:  # noqa
                self.q.put(("error", "Recycle failed:\n%s" % e))

        threading.Thread(target=worker, daemon=True).start()

    def _recycle(self, paths, progress=None):
        fd, listfile = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(paths))
        ps = (
            "$ErrorActionPreference='SilentlyContinue';"
            "Add-Type -AssemblyName Microsoft.VisualBasic;"
            "$vb=[Microsoft.VisualBasic.FileIO.FileSystem];"
            "$paths=Get-Content -LiteralPath $env:DA_LIST -Encoding UTF8;"
            "$i=0; foreach($p in $paths){ if($p -and (Test-Path -LiteralPath $p)){"
            " try{ $vb::DeleteDirectory($p,'OnlyErrorDialogs','SendToRecycleBin') }catch{} }"
            " $i++; Write-Output $i }"
        )
        env = dict(os.environ, DA_LIST=listfile)
        total = len(paths)
        try:
            p = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=env, creationflags=_NO_WINDOW, text=True)
            for line in p.stdout:
                line = line.strip()
                if line.isdigit() and progress:
                    n = int(line)
                    if n % 15 == 0 or n == total:
                        progress(n, total)
            p.wait()
            if progress:
                progress(total, total)
        finally:
            try:
                os.remove(listfile)
            except OSError:
                pass

    def empty_recycle_bin(self):
        if self.recycle_busy:
            return
        if not messagebox.askyesno(
                "Empty Recycle Bin",
                "Permanently empty the Recycle Bin now?\nThis cannot be undone."):
            return
        self.recycle_busy = True
        self.status.set("Emptying Recycle Bin\u2026")

        def worker():
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-Command", "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=_NO_WINDOW)
                self.q.put(("info", "Recycle Bin emptied."))
            except Exception as e:  # noqa
                self.q.put(("error", "Could not empty Recycle Bin:\n%s" % e))

        threading.Thread(target=worker, daemon=True).start()

    def export_html(self):
        if not self.results:
            messagebox.showinfo("Export", "Scan a folder first.")
            return
        r = self.results
        out_dir = self.opts.out_dir
        stamp = time.strftime("%Y%m%d_%H%M%S")
        tag = "".join(ch if ch.isalnum() else "_"
                      for ch in os.path.basename(r['root']))[:40] or "root"
        html_path = os.path.join(out_dir, "disk_report_%s_%s.html" % (tag, stamp))
        clear_path = None
        if r['reclaimables']:
            clear_path = os.path.join(out_dir, "clear-safe-caches_%s_%s.ps1" % (tag, stamp))
            write_clear_caches_script(r['root'], r['reclaimables'], self.opts, clear_path)
        render_html(r['root'], r['total'], r['files'], r['dirs_top'],
                    r['files_top'], r['ext_top'], r['children'],
                    self.dup_groups or [], r['crumbs'], r['reclaimables'],
                    r['skipped'], self.opts, html_path, clear_caches_path=clear_path,
                    oldversions=r.get('oldversions'))
        webbrowser.open("file:///" + html_path.replace("\\", "/"))
        self.status.set("Exported " + os.path.basename(html_path))

    def save_review(self):
        if not self.results:
            messagebox.showinfo("Review script", "Scan a folder first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".ps1", initialdir=self.opts.out_dir,
            initialfile="review-then-run.ps1",
            filetypes=[("PowerShell script", "*.ps1")])
        if not path:
            return
        write_review_script(self.results['root'], self.dup_groups or [],
                            self.results['crumbs'], self.opts, path)
        self.status.set("Saved " + os.path.basename(path))

    def open_options(self):
        win = tk.Toplevel(self.root)
        win.title("Scan options")
        win.transient(self.root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill=tk.BOTH, expand=True)
        rows = [("Rows per table (Top N):", self.opt_top),
                ("Crumb size threshold (e.g. 1MB):", self.opt_crumb_size),
                ("Crumb age (days):", self.opt_crumb_age),
                ("Min file size for duplicates:", self.opt_min_dup)]
        for i, (label, var) in enumerate(rows):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", pady=4)
            ttk.Entry(frm, textvariable=var, width=14).grid(row=i, column=1, pady=4, padx=6)
        ttk.Checkbutton(frm, text="Follow symlinks / junctions",
                        variable=self.opt_follow).grid(row=len(rows), column=0,
                                                       columnspan=2, sticky="w", pady=6)
        ttk.Label(frm, text="Applied on the next Scan.", foreground="#777").grid(
            row=len(rows) + 1, column=0, columnspan=2, sticky="w")
        ttk.Button(frm, text="Close", command=win.destroy).grid(
            row=len(rows) + 2, column=0, columnspan=2, pady=(10, 0))

    def _open_type_detail(self, event=None):
        if not self.results:
            return
        iid = self.tv_types.focus()
        if not iid:
            return
        ext = self.tv_types.set(iid, "Type")
        if not ext:
            return

        def ext_of(p):
            return os.path.splitext(p)[1].lower() or "(no ext)"

        matches = [f for f in self.results['files'] if ext_of(f.path) == ext]
        matches.sort(key=lambda f: f.size, reverse=True)
        total = sum(f.size for f in matches)
        cap = 5000
        shown = matches[:cap]
        root = self.results['root']

        win = tk.Toplevel(self.root)
        win.title("%s files" % ext)
        win.geometry("820x520")
        note = ("%d %s file(s) · %s · click a header to sort by Size or "
                "Modified · double-click to open" % (len(matches), ext, human(total)))
        if len(matches) > cap:
            note += "  (showing the largest %d)" % cap
        ttk.Label(win, text=note, padding=(10, 8), foreground="#555").pack(anchor="w")
        tree = self._make_tree(win, ("File", "Size", "Modified"),
                               numeric=("Size", "Modified"))
        for f in shown:
            tree.insert('', 'end', iid=f.path,
                        values=(_short(f.path, root), human(f.size), da._date(f.mtime)))
            tree._sort[f.path] = {"Size": f.size, "Modified": f.mtime}
        self._attach_open(tree)
        win.transient(self.root)

    # ---------------------------------------------------------------- open/reveal
    def _open_selected(self, tree):
        iid = tree.focus()
        self._safe_open(iid)

    def _open_dup(self, event):
        iid = self.tv_dupes.focus()
        p = self.dup_path.get(iid)
        if p:
            self._safe_open(p)

    def _safe_open(self, path):
        if path and os.path.exists(path):
            self.open_path(path)

    def open_path(self, path):
        try:
            os.startfile(path)  # noqa - Windows
        except Exception as e:  # noqa
            messagebox.showerror("Open", "Could not open:\n%s\n\n%s" % (path, e))

    def reveal(self, path):
        if not path:
            return
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except Exception:
            try:
                os.startfile(os.path.dirname(path))  # noqa
            except Exception:
                pass

    def copy_path(self, path):
        if path:
            self.root.clipboard_clear()
            self.root.clipboard_append(path)
            self.status.set("Copied path.")

    # ---------------------------------------------------------------- queue poll
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _handle(self, kind, payload):
        if kind == "scan_progress":
            self.status.set("Scanning\u2026 %s files" % f"{payload:,}")
        elif kind == "scan_done":
            self._on_scan_done(payload)
        elif kind == "dupe_progress":
            d, t = payload
            if t > 0:
                self.dup_progress.config(maximum=t, value=d)
            self.status.set("Hashing files\u2026 %s / %s" % (f"{d:,}", f"{t:,}"))
        elif kind == "dupe_done":
            self._on_dupe_done(payload)
        elif kind == "recycle_progress":
            d, t = payload
            self.progress.config(maximum=t, value=d)
            self.status.set("Recycling caches\u2026 %d / %d" % (d, t))
        elif kind == "recycle_done":
            self.recycle_busy = False
            self.progress.config(value=0)
            self.status.set("Caches sent to Recycle Bin. Use \u201cEmpty Recycle Bin\u201d to reclaim.")
            messagebox.showinfo("Caches", "Caches sent to the Recycle Bin.\n\n"
                                "Click \u201cEmpty Recycle Bin\u201d to actually free the space.")
        elif kind == "info":
            self.recycle_busy = False
            self.status.set(payload)
            messagebox.showinfo("Disk Analyzer", payload)
        elif kind == "removal_done":
            mode, n = payload
            self.recycle_busy = False
            self.progress.config(value=0)
            tree, iids = getattr(self, "_pending_remove", (None, []))
            if tree is not None:
                for iid in iids:
                    try:
                        tree.delete(iid)
                    except Exception:
                        pass
            self._pending_remove = (None, [])
            verb = "Recycled" if mode == "recycle" else "Deleted"
            extra = " Empty the Recycle Bin to reclaim." if mode == "recycle" else ""
            self.status.set("%s %d item(s).%s" % (verb, n, extra))
        elif kind == "sysinfo":
            for k, v in payload.items():
                if k in self.sys_vars:
                    self.sys_vars[k].set(v)
            self.status.set("System info updated.")
        elif kind == "recommended_done":
            n, emptied = payload
            self.recycle_busy = False
            self.progress.config(value=0)
            msg = "Recommended cleanup: %d item(s) recycled." % n
            msg += " Recycle Bin emptied." if emptied else " Empty the Recycle Bin to reclaim."
            self.status.set(msg)
            messagebox.showinfo("Recommended cleanup", msg + "\n\nPress Scan to refresh totals.")
        elif kind == "error":
            self.busy = self.dup_busy = self.recycle_busy = False
            self.progress.stop()
            self.progress.config(mode="determinate", value=0)
            self.scan_btn.config(state=tk.NORMAL)
            self.dup_btn.config(state=tk.NORMAL)
            self.status.set("Error.")
            messagebox.showerror("Disk Analyzer", payload)

    # ---------------------------------------------------------------- populate
    def _on_scan_done(self, res):
        self.results = res
        self.dup_groups = None
        self.busy = False
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self.scan_btn.config(state=tk.NORMAL)
        root = res['root']
        self.card_vars['total'].set(human(res['total']))
        self.card_vars['files'].set(f"{len(res['files']):,}")
        self.card_vars['dupe'].set("\u2014 run Duplicates")
        self.card_vars['crumbs'].set(human(sum(f.size for f, _ in res['crumbs'])))
        self.card_vars['caches'].set(human(sum(s for _, s in res['reclaimables'])))
        self.ov_meta.set("%s\n%s files \u00b7 scan %.1fs \u00b7 %d skipped"
                         % (root, f"{len(res['files']):,}", res['elapsed'],
                            len(res['skipped'])))
        self._fill_files(res['files_top'], root)
        self._fill_dirs(res['dirs_top'], root)
        self._fill_types(res['ext_top'])
        self._fill_crumbs(res['crumbs'], root)
        self._fill_caches(res['reclaimables'], root)
        ver_flagged = self._fill_versions(res.get('oldversions', []), root)
        self.card_vars['versions'].set(human(ver_flagged))
        # reset duplicates view
        self.tv_dupes.delete(*self.tv_dupes.get_children(''))
        self.dup_path = {}
        self.dup_progress.config(value=0)
        self.scan_root = root
        self._set_tm_root(root, res['children'])
        self.status.set("Done \u00b7 %s in %s files \u00b7 %.1fs"
                        % (human(res['total']), f"{len(res['files']):,}", res['elapsed']))

    def _fill_files(self, files_top, root):
        t = self.tv_files
        t.delete(*t.get_children(''))
        t._sort = {}
        for f in files_top:
            t.insert('', 'end', iid=f.path,
                     values=(_short(f.path, root), human(f.size), da._date(f.mtime)))
            t._sort[f.path] = {"Size": f.size}

    def _fill_dirs(self, dirs_top, root):
        t = self.tv_dirs
        t.delete(*t.get_children(''))
        t._sort = {}
        for path, size in dirs_top:
            t.insert('', 'end', iid=path, values=(_short(path, root), human(size)))
            t._sort[path] = {"Size": size}

    def _fill_types(self, ext_top):
        t = self.tv_types
        t.delete(*t.get_children(''))
        t._sort = {}
        for i, (ext, size, count) in enumerate(ext_top):
            iid = "type%d" % i
            t.insert('', 'end', iid=iid, values=(ext, human(size), f"{count:,}"))
            t._sort[iid] = {"Size": size, "Files": count}

    def _fill_crumbs(self, crumbs, root):
        t = self.tv_crumbs
        t.delete(*t.get_children(''))
        t._sort = {}
        for f, age in crumbs[:self.opts.top]:
            t.insert('', 'end', iid=f.path,
                     values=(_short(f.path, root), human(f.size),
                             "%.0f mo" % (age / 30.0), da._date(f.mtime)))
            t._sort[f.path] = {"Size": f.size, "Age": age}

    def _fill_caches(self, reclaimables, root):
        t = self.tv_caches
        t.delete(*t.get_children(''))
        t._sort = {}
        for path, size in reclaimables:
            t.insert('', 'end', iid=path, values=(_short(path, root), human(size)))
            t._sort[path] = {"Size": size}
        total = sum(s for _, s in reclaimables)
        self.cache_total.set("%d cache folder(s) \u00b7 %s reclaimable"
                             % (len(reclaimables), human(total)))

    def _on_dupe_done(self, groups):
        self.dup_busy = False
        self.dup_btn.config(state=tk.NORMAL)
        self.dup_groups = groups
        t = self.tv_dupes
        t.delete(*t.get_children(''))
        self.dup_path = {}
        root = self.results['root']
        waste_total = 0
        for gi, g in enumerate(groups):
            waste = (len(g) - 1) * g[0].size
            waste_total += waste
            pid = "g%d" % gi
            t.insert('', 'end', iid=pid, open=False,
                     text="%d copies \u00d7 %s" % (len(g), human(g[0].size)),
                     values=(human(waste),))
            for fj, f in enumerate(g):
                cid = "%s:%d" % (pid, fj)
                t.insert(pid, 'end', iid=cid, text=_short(f.path, root), values=("",))
                self.dup_path[cid] = f.path
        self.dup_progress.config(value=self.dup_progress['maximum'])
        self.card_vars['dupe'].set(human(waste_total))
        self.status.set("Duplicates: %s wasted in %d groups."
                        % (human(waste_total), len(groups)))

    # ---------------------------------------------------------------- treemap
    def _draw_treemap(self):
        c = self.canvas
        c.delete('all')
        data = self.tm_children
        if not data:
            return
        W, H = c.winfo_width(), c.winfo_height()
        if W < 20 or H < 20:
            return
        items = []
        for i, (n, s, d, p) in enumerate([x for x in data if x[1] > 0][:80]):
            items.append({'n': n, 's': s, 'dir': d, 'p': p,
                          'color': PALETTE[i % len(PALETTE)]})
        if not items:
            c.create_text(W // 2, H // 2, text="(nothing to show here)",
                          fill="#666", font=("Segoe UI", 10))
            return
        out = []
        treemap_layout(items, 0, 0, W, H, out)
        for idx, r in enumerate(out):
            it = r['d']
            x0, y0 = r['x'], r['y']
            x1, y1 = x0 + r['w'], y0 + r['h']
            tag = "tile%d" % idx
            c.create_rectangle(x0, y0, x1, y1, fill=it['color'],
                               outline="#12151a", tags=(tag,))
            if r['w'] > 62 and r['h'] > 30:
                label = ("\U0001F4C1 " if it['dir'] else "\U0001F4C4 ") + it['n']
                c.create_text(x0 + 6, y0 + 6, anchor="nw", text=label,
                              fill="white", font=("Segoe UI", 9, "bold"),
                              width=max(r['w'] - 12, 10), tags=(tag,))
                c.create_text(x0 + 6, y0 + 24, anchor="nw", text=human(it['s']),
                              fill="#eeeeee", font=("Segoe UI", 8), tags=(tag,))
            c.tag_bind(tag, "<Double-1>", lambda e, i=it: self._treemap_activate(i))
            c.tag_bind(tag, "<Button-3>", lambda e, i=it: self._treemap_menu(e, i))
            c.tag_bind(tag, "<Enter>", lambda e: c.config(cursor="hand2"))
            c.tag_bind(tag, "<Leave>", lambda e: c.config(cursor=""))

    # ---- treemap drill-down navigation ----
    def _set_tm_root(self, path, children=None):
        self.tm_root = path
        if children is None:
            children = root_children(self.results['dir_sizes'],
                                     self.results['files'], path)
        self.tm_children = children
        top = os.path.basename(self.scan_root) or self.scan_root
        try:
            rel = os.path.relpath(path, self.scan_root)
        except (ValueError, TypeError):
            rel = "."
        crumb = top if (not rel or rel == ".") else top + " › " + rel.replace(os.sep, " › ")
        self.tm_crumb.set(crumb)
        at_root = os.path.normpath(path) == os.path.normpath(self.scan_root or path)
        self.tm_up_btn.config(state=(tk.DISABLED if at_root else tk.NORMAL))
        self._draw_treemap()

    def _treemap_activate(self, item):
        p = item['p']
        if item.get('dir'):
            kids = root_children(self.results['dir_sizes'], self.results['files'], p)
            if any(s > 0 for _, s, _, _ in kids):
                self._set_tm_root(p, kids)
                return
        self._safe_open(p)

    def _treemap_up(self):
        if not self.tm_root or not self.scan_root:
            return
        if os.path.normpath(self.tm_root) == os.path.normpath(self.scan_root):
            return
        self._set_tm_root(os.path.dirname(self.tm_root))

    def _treemap_root(self):
        if self.scan_root:
            self._set_tm_root(self.scan_root)

    def _treemap_menu(self, event, item):
        m = self._tm_menu
        m.delete(0, "end")
        p = item['p']
        if item.get('dir'):
            m.add_command(label="Zoom in", command=lambda: self._treemap_activate(item))
        m.add_command(label="Open", command=lambda: self._safe_open(p))
        m.add_command(label="Open containing folder", command=lambda: self.reveal(p))
        m.add_command(label="Copy path", command=lambda: self.copy_path(p))
        m.tk_popup(event.x_root, event.y_root)

    # ================= cleanup pipeline =================
    def _apply_removal(self, tree, iids, paths, mode, label):
        """Recycle or permanently delete `paths`; on success drop `iids` rows."""
        paths = [p for p in paths if p]
        if not paths:
            messagebox.showinfo("Nothing selected", "Select one or more rows first.")
            return
        if self.recycle_busy:
            return
        if mode == "recycle":
            msg = ("Send %d %s to the Recycle Bin?\n\nRecoverable; use \u201cEmpty "
                   "Recycle Bin\u201d afterwards to reclaim the space." % (len(paths), label))
        else:
            msg = ("PERMANENTLY delete %d %s?\n\nThis cannot be undone." % (len(paths), label))
        if not messagebox.askyesno("Confirm", msg):
            return
        self._pending_remove = (tree, list(iids))
        self.recycle_busy = True
        self.progress.stop()
        self.progress.config(mode="determinate", maximum=len(paths), value=0)
        self.status.set("Working\u2026")

        def worker():
            try:
                if mode == "recycle":
                    self._recycle(paths, progress=lambda d, t: self.q.put(("recycle_progress", (d, t))))
                else:
                    self._perm_delete(paths, progress=lambda d, t: self.q.put(("recycle_progress", (d, t))))
                self.q.put(("removal_done", (mode, len(paths))))
            except Exception as e:  # noqa
                self.q.put(("error", "Removal failed:\n%s" % e))

        threading.Thread(target=worker, daemon=True).start()

    def _perm_delete(self, paths, progress=None):
        total, done = len(paths), 0
        for p in paths:
            try:
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
            done += 1
            if progress and (done % 10 == 0 or done == total):
                progress(done, total)
        if progress:
            progress(total, total)

    # ---- duplicates actions ----
    def _dup_select_redundant(self):
        sel = []
        for pid in self.tv_dupes.get_children(''):
            kept = False
            for cid in self.tv_dupes.get_children(pid):
                p = self.dup_path.get(cid)
                if not p or da.is_protected(p, self.opts.protected):
                    continue
                if not kept:
                    kept = True
                else:
                    sel.append(cid)
        self.tv_dupes.selection_set(sel)
        self.status.set("Selected %d redundant copies (one kept per group)." % len(sel))

    def _dupes_action(self, mode):
        iids, paths = [], []
        for i in self.tv_dupes.selection():
            p = self.dup_path.get(i)
            if p and not da.is_protected(p, self.opts.protected):
                iids.append(i)
                paths.append(p)
        self._apply_removal(self.tv_dupes, iids, paths, mode, "duplicate file(s)")

    # ---- crumbs tab (actionable) ----
    def _build_crumbs(self, nb):
        f = ttk.Frame(nb, padding=6)
        nb.add(f, text="Crumbs")
        top = ttk.Frame(f)
        top.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(top, text="Small, long-untouched files. Select rows, then act.",
                  foreground="#777").pack(side=tk.LEFT)
        ttk.Button(top, text="Delete selected",
                   command=lambda: self._crumbs_action("delete")).pack(side=tk.RIGHT)
        ttk.Button(top, text="Recycle selected",
                   command=lambda: self._crumbs_action("recycle")).pack(side=tk.RIGHT, padx=8)
        ttk.Button(top, text="Select all",
                   command=lambda: self.tv_crumbs.selection_set(
                       self.tv_crumbs.get_children(''))).pack(side=tk.RIGHT, padx=8)
        self.tv_crumbs = self._make_tree(f, ("File", "Size", "Age", "Modified"),
                                         numeric=("Size", "Age"))
        self.tv_crumbs.configure(selectmode="extended")
        self._attach_open(self.tv_crumbs)

    def _crumbs_action(self, mode):
        iids, paths = [], []
        for i in self.tv_crumbs.selection():
            if os.path.exists(i) and not da.is_protected(i, self.opts.protected):
                iids.append(i)
                paths.append(i)
        self._apply_removal(self.tv_crumbs, iids, paths, mode, "crumb file(s)")

    # ---- free up space (system assistant) ----
    def _build_freeup(self, nb):
        f = ttk.Frame(nb, padding=14)
        nb.add(f, text="Free up space")
        ttk.Label(f, text="Guided space cleanup. Some actions ask for admin approval (UAC).",
                  foreground="#777").pack(anchor="w", pady=(0, 10))
        self.sys_vars = {}
        grid = ttk.Frame(f)
        grid.pack(fill=tk.X)
        rows = [
            ("caches", "Regenerable caches (this scan)", "scan a folder", "Clear \u2192 Recycle Bin", self.clear_caches),
            ("recyclebin", "Recycle Bin", "\u2014", "Empty now", self.empty_recycle_bin),
            ("hibernate", "Hibernation file (hiberfil.sys)", "refresh to read", "Disable (admin)", self._sys_hibernate_off),
            ("pagefile", "Paging file (pagefile.sys)", "refresh to read", "Open VM settings", self._sys_open_vm),
            ("wsl", "WSL Linux distributions", "refresh to read", "Manage\u2026", self._sys_wsl_manage),
            ("restore", "System Restore (shadow storage)", "needs admin", "Check (admin)", self._sys_restore_check),
            ("cleanmgr", "Windows Disk Cleanup", "\u2014", "Launch", self._sys_cleanmgr),
        ]
        for i, (key, name, detail, btn, cmd) in enumerate(rows):
            ttk.Label(grid, text=name).grid(row=i, column=0, sticky="w", pady=5)
            v = tk.StringVar(value=detail)
            self.sys_vars[key] = v
            ttk.Label(grid, textvariable=v, foreground="#777", width=24).grid(
                row=i, column=1, sticky="w", padx=14)
            ttk.Button(grid, text=btn, width=20,
                       command=cmd).grid(row=i, column=2, sticky="w")
        grid.columnconfigure(1, weight=1)
        bottom = ttk.Frame(f)
        bottom.pack(anchor="w", pady=14)
        ttk.Button(bottom, text="\u26a1 Recommended cleanup",
                   command=self._open_recommended).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Refresh system info",
                   command=self._refresh_system).pack(side=tk.LEFT, padx=10)
        ttk.Button(bottom, text="Cleanup wizard\u2026",
                   command=self._open_wizard).pack(side=tk.LEFT)

    def _refresh_system(self):
        if self.results:
            recl = self.results['reclaimables']
            self.sys_vars['caches'].set("%s in %d folders"
                                        % (human(sum(s for _, s in recl)), len(recl)))

        def worker():
            info = {}
            for key, path in (("hibernate", r"C:\hiberfil.sys"),
                              ("pagefile", r"C:\pagefile.sys")):
                try:
                    info[key] = human(os.path.getsize(path))
                except OSError:
                    info[key] = "not present / no access"
            try:
                out = subprocess.run(
                    ["wsl", "--list", "--quiet"], capture_output=True, text=True,
                    env=dict(os.environ, WSL_UTF8="1"),
                    creationflags=_NO_WINDOW, timeout=20)
                names = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
                info["wsl"] = ", ".join(names) if names else "none installed"
            except Exception:
                info["wsl"] = "unknown"
            self.q.put(("sysinfo", info))

        threading.Thread(target=worker, daemon=True).start()
        self.status.set("Reading system info\u2026")

    def _elevated(self, exe, args):
        cmd = "Start-Process -FilePath '%s' -Verb RunAs" % exe
        if args:
            cmd += " -ArgumentList " + ",".join("'%s'" % a.replace("'", "''") for a in args)
        subprocess.Popen(["powershell", "-NoProfile", "-Command", cmd],
                         creationflags=_NO_WINDOW)

    def _sys_hibernate_off(self):
        if not messagebox.askyesno(
                "Disable hibernation",
                "Disable hibernation and delete hiberfil.sys?\n\nFrees the "
                "hibernation file (often 3+ GB). Approve the UAC prompt.\n"
                "Re-enable later with: powercfg /h on"):
            return
        self._elevated("powercfg", ["/h", "off"])
        self.status.set("Requested hibernation off \u2014 approve UAC, then Refresh.")

    def _sys_open_vm(self):
        messagebox.showinfo(
            "Paging file",
            "Opening Performance Options.\n\nAdvanced tab \u2192 Virtual memory "
            "\u2192 Change. Uncheck \u2018automatically manage\u2019, set a Custom "
            "size (e.g. 2048 / 8192 MB), then reboot. Don\u2019t set it too small.")
        for exe in ("SystemPropertiesPerformance.exe", "SystemPropertiesAdvanced.exe"):
            try:
                subprocess.Popen([exe], creationflags=_NO_WINDOW)
                return
            except Exception:
                continue

    def _sys_cleanmgr(self):
        try:
            subprocess.Popen(["cleanmgr"], creationflags=_NO_WINDOW)
        except Exception as e:  # noqa
            messagebox.showerror("Disk Cleanup", str(e))

    def _sys_restore_check(self):
        cmd = ("Start-Process powershell -Verb RunAs -ArgumentList "
               "'-NoExit','-Command','vssadmin list shadowstorage'")
        subprocess.Popen(["powershell", "-NoProfile", "-Command", cmd],
                         creationflags=_NO_WINDOW)
        self.status.set("Opening an admin window with restore-point usage\u2026")

    def _sys_wsl_manage(self):
        try:
            out = subprocess.run(
                ["wsl", "--list", "--quiet"], capture_output=True, text=True,
                env=dict(os.environ, WSL_UTF8="1"),
                creationflags=_NO_WINDOW, timeout=20)
            names = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        except Exception:
            names = []
        if not names:
            messagebox.showinfo("WSL", "No WSL distributions are installed.")
            return
        win = tk.Toplevel(self.root)
        win.title("WSL distributions")
        win.transient(self.root)
        ttk.Label(win, padding=10, justify="left",
                  text="Unregistering a distro permanently deletes its Linux files\n"
                       "(its virtual disk), reclaiming that space. Not recoverable."
                  ).pack(anchor="w")
        for n in names:
            row = ttk.Frame(win, padding=(10, 2))
            row.pack(fill=tk.X)
            ttk.Label(row, text=n, width=24).pack(side=tk.LEFT)
            ttk.Button(row, text="Unregister\u2026",
                       command=lambda nn=n: self._wsl_unregister(nn, win)).pack(side=tk.LEFT)
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=8)

    def _wsl_unregister(self, name, win):
        if not messagebox.askyesno(
                "Unregister %s" % name,
                "Permanently delete the WSL distro '%s' and its virtual disk?\n\n"
                "This CANNOT be undone." % name, parent=win):
            return
        try:
            subprocess.run(["wsl", "--unregister", name],
                          env=dict(os.environ, WSL_UTF8="1"),
                          creationflags=_NO_WINDOW, timeout=180)
            messagebox.showinfo("WSL", "Unregistered %s." % name, parent=win)
        except Exception as e:  # noqa
            messagebox.showerror("WSL", str(e), parent=win)
        win.destroy()

    # ---- cleanup wizard ----
    def _open_wizard(self):
        if not self.results:
            messagebox.showinfo("Cleanup wizard", "Scan a folder first (press Scan).")
            return
        win = tk.Toplevel(self.root)
        win.title("Cleanup wizard")
        win.transient(self.root)
        win.geometry("580x360")
        body = ttk.Frame(win, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        nav = ttk.Frame(win, padding=(16, 0, 16, 12))
        nav.pack(fill=tk.X)
        back = ttk.Button(nav, text="\u2190 Back", command=lambda: self._wiz_go(-1))
        back.pack(side=tk.LEFT)
        ttk.Button(nav, text="Close", command=win.destroy).pack(side=tk.RIGHT)
        nxt = ttk.Button(nav, text="Next \u2192", command=lambda: self._wiz_go(1))
        nxt.pack(side=tk.RIGHT, padx=8)
        self._wiz = SimpleNamespace(win=win, step=0, body=body, back=back, next=nxt)
        self._wiz_render()

    def _wiz_go(self, delta):
        self._wiz.step = max(0, min(4, self._wiz.step + delta))
        self._wiz_render()

    def _wiz_render(self):
        w = self._wiz
        for child in w.body.winfo_children():
            child.destroy()
        b, step, r = w.body, w.step, self.results
        titles = ["1. Regenerable caches", "2. Duplicate files",
                  "3. Crumbs (small & old)", "4. System space", "5. Finish"]
        ttk.Label(b, text=titles[step], font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(0, 10))
        if step == 0:
            recl = r['reclaimables']
            ttk.Label(b, justify="left",
                      text="Found %s of regenerable caches in %d folders.\nThey rebuild "
                           "automatically." % (human(sum(s for _, s in recl)), len(recl))).pack(anchor="w")
            ttk.Button(b, text="Clear caches \u2192 Recycle Bin",
                       command=self.clear_caches).pack(anchor="w", pady=10)
        elif step == 1:
            if self.dup_groups is None:
                ttk.Label(b, text="Duplicates not scanned yet.").pack(anchor="w")
                ttk.Button(b, text="Find duplicates now",
                           command=self.find_dupes).pack(anchor="w", pady=10)
            else:
                waste = sum((len(g) - 1) * g[0].size for g in self.dup_groups)
                ttk.Label(b, text="%s wasted across %d duplicate groups."
                          % (human(waste), len(self.dup_groups))).pack(anchor="w")
                ttk.Button(b, text="Select & recycle redundant copies",
                           command=self._wiz_recycle_dupes).pack(anchor="w", pady=10)
        elif step == 2:
            cb = sum(f.size for f, _ in r['crumbs']
                     if not da.is_protected(f.path, self.opts.protected))
            ttk.Label(b, text="%d crumb files (%s), not modified in a long time."
                      % (len(r['crumbs']), human(cb))).pack(anchor="w")
            ttk.Button(b, text="Recycle all crumbs",
                       command=self._wiz_recycle_crumbs).pack(anchor="w", pady=10)
        elif step == 3:
            ttk.Label(b, text="System-level space (some need admin):",
                      justify="left").pack(anchor="w", pady=(0, 6))
            for text, cmd in [("Empty Recycle Bin", self.empty_recycle_bin),
                              ("Disable hibernation (admin)", self._sys_hibernate_off),
                              ("Paging file settings", self._sys_open_vm),
                              ("Manage WSL distros", self._sys_wsl_manage),
                              ("Windows Disk Cleanup", self._sys_cleanmgr)]:
                ttk.Button(b, text=text, width=32, command=cmd).pack(anchor="w", pady=3)
        else:
            ttk.Label(b, justify="left",
                      text="All set. Export a full HTML report, or re-scan to see "
                           "updated totals.").pack(anchor="w")
            ttk.Button(b, text="Export HTML report",
                       command=self.export_html).pack(anchor="w", pady=6)
            ttk.Button(b, text="Re-scan", command=self.start_scan).pack(anchor="w")
        w.back.config(state=(tk.NORMAL if step > 0 else tk.DISABLED))
        w.next.config(state=(tk.NORMAL if step < 4 else tk.DISABLED))

    def _wiz_recycle_dupes(self):
        self._dup_select_redundant()
        self._dupes_action("recycle")

    def _wiz_recycle_crumbs(self):
        paths = [f.path for f, _ in self.results['crumbs']
                 if not da.is_protected(f.path, self.opts.protected)]
        self._apply_removal(self.tv_crumbs, [], paths, "recycle", "crumb file(s)")

    # ---- one-click recommended cleanup ----
    def _redundant_dupe_paths(self):
        """Redundant copies (all but one kept per group), skipping protected."""
        paths = []
        for g in (self.dup_groups or []):
            deletable = [f for f in g if not da.is_protected(f.path, self.opts.protected)]
            if len(deletable) < 2:
                continue
            deletable.sort(key=lambda f: (f.path.count(os.sep), f.mtime))
            for f in deletable[1:]:
                paths.append(f.path)
        return paths

    def _open_recommended(self):
        if not self.results:
            messagebox.showinfo("Recommended cleanup", "Scan a folder first (press Scan).")
            return
        r = self.results
        cache_paths = [p for p, _ in r['reclaimables']]
        cache_sz = sum(s for _, s in r['reclaimables'])
        dupe_paths = self._redundant_dupe_paths()
        dupe_waste = sum((len(g) - 1) * g[0].size for g in (self.dup_groups or []))
        crumb_paths = [f.path for f, _ in r['crumbs']
                       if not da.is_protected(f.path, self.opts.protected)]
        crumb_sz = sum(f.size for f, _ in r['crumbs']
                       if not da.is_protected(f.path, self.opts.protected))

        win = tk.Toplevel(self.root)
        win.title("Recommended cleanup")
        win.transient(self.root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, foreground="#555", justify="left",
                  text="Safe cleanups — everything goes to the Recycle Bin "
                       "(recoverable).\nPick what to include, then Apply.").pack(anchor="w", pady=(0, 10))

        v_cache = tk.BooleanVar(value=bool(cache_paths))
        v_dupe = tk.BooleanVar(value=bool(dupe_paths))
        v_crumb = tk.BooleanVar(value=False)
        v_empty = tk.BooleanVar(value=False)

        ttk.Checkbutton(frm, variable=v_cache,
                        text="Clear regenerable caches  —  %s in %d folders"
                        % (human(cache_sz), len(cache_paths)),
                        state=(tk.NORMAL if cache_paths else tk.DISABLED)).pack(anchor="w", pady=2)
        if self.dup_groups is None:
            ttk.Label(frm, foreground="#999",
                      text="   (run “Find duplicates” first to include duplicate copies)"
                      ).pack(anchor="w")
        else:
            ttk.Checkbutton(frm, variable=v_dupe,
                            text="Recycle redundant duplicate copies  —  %s (keeps 1 per group)"
                            % human(dupe_waste),
                            state=(tk.NORMAL if dupe_paths else tk.DISABLED)).pack(anchor="w", pady=2)
        ttk.Checkbutton(frm, variable=v_crumb,
                        text="Recycle crumbs (small, long-unused files)  —  %s in %d files"
                        % (human(crumb_sz), len(crumb_paths)),
                        state=(tk.NORMAL if crumb_paths else tk.DISABLED)).pack(anchor="w", pady=2)
        ttk.Separator(frm).pack(fill=tk.X, pady=10)
        ttk.Checkbutton(frm, variable=v_empty,
                        text="Empty the Recycle Bin afterwards (permanent — reclaims the "
                             "space now)").pack(anchor="w")

        btns = ttk.Frame(frm)
        btns.pack(anchor="e", pady=(14, 0))
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Apply", command=lambda: self._apply_recommended(
            win, v_cache.get(), v_dupe.get(), v_crumb.get(), v_empty.get(),
            cache_paths, dupe_paths, crumb_paths)).pack(side=tk.RIGHT, padx=8)

    def _apply_recommended(self, win, do_cache, do_dupe, do_crumb, do_empty,
                           cache_paths, dupe_paths, crumb_paths):
        paths = []
        if do_cache:
            paths += cache_paths
        if do_dupe:
            paths += dupe_paths
        if do_crumb:
            paths += crumb_paths
        paths = list(dict.fromkeys(p for p in paths if p))  # de-dupe, keep order
        if not paths and not do_empty:
            messagebox.showinfo("Recommended cleanup", "Nothing selected.", parent=win)
            return
        extra = "\nThen the Recycle Bin will be emptied (permanent)." if do_empty else ""
        if not messagebox.askyesno(
                "Recommended cleanup",
                "Send %d item(s) to the Recycle Bin?%s" % (len(paths), extra), parent=win):
            return
        win.destroy()
        if self.recycle_busy:
            return
        self.recycle_busy = True
        self.progress.stop()
        self.progress.config(mode="determinate", maximum=max(len(paths), 1), value=0)
        self.status.set("Recommended cleanup…")
        n = len(paths)

        def worker():
            try:
                if paths:
                    self._recycle(paths, progress=lambda d, t: self.q.put(("recycle_progress", (d, t))))
                if do_empty:
                    subprocess.run(
                        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                         "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=_NO_WINDOW)
                self.q.put(("recommended_done", (n, do_empty)))
            except Exception as e:  # noqa
                self.q.put(("error", "Cleanup failed:\n%s" % e))

        threading.Thread(target=worker, daemon=True).start()


def launch(initial=None):
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    DiskAnalyzerGUI(root, initial)
    root.mainloop()
    return 0


if __name__ == "__main__":
    launch(sys.argv[1] if len(sys.argv) > 1 else None)
