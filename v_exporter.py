#!/usr/bin/env python3
"""V-Exporter - export Cyberpunk 2077 NPV looks as GLB / FBX / OBJ.

GUI usage:      python v_exporter.py
CLI usage:      python v_exporter.py --help
Examples:       python v_exporter.py --list
                python v_exporter.py --export --npv MyV
Self-test:      python v_exporter.py --selftest

Standard library only (tkinter, subprocess, json, pathlib, shutil).
See README.md for the one-time prerequisites and EXPORT-RESEARCH.md for the export pipeline.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

import pipeline as pl

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception as _tk_error:  # pragma: no cover - selftest must still work
    tk = None  # type: ignore[assignment]
    filedialog = messagebox = ttk = None  # type: ignore[assignment]
    _TK_ERROR = _tk_error
else:
    _TK_ERROR = None

if ttk is not None:
    _FrameBase = ttk.Frame
else:  # placeholder base so this module imports without tkinter installed
    class _FrameBase:  # type: ignore[no-redef]
        pass


# ---------------------------------------------------------------------------
# reusable checklist widget
# ---------------------------------------------------------------------------

class CheckList(_FrameBase):
    """Scrollable list of real ttk checkboxes (NPVs, looks)."""

    def __init__(self, master, height: int = 160, on_change=None):
        super().__init__(master)
        self.on_change = on_change
        self._vars: dict[str, tk.BooleanVar] = {}
        self._order: list[str] = []
        self._enabled: dict[str, bool] = {}
        self._height = height
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(self, height=height, highlightthickness=1,
                                borderwidth=0, background="#ffffff")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.inner = ttk.Frame(self.canvas, padding=(4, 2))
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind(
            "<Configure>",
            lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda event: self.canvas.itemconfigure(self._window, width=event.width),
        )
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    # -- mouse wheel ------------------------------------------------------
    def _bind_wheel(self, _event=None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self, _event=None) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event) -> None:
        try:
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass

    # -- content ----------------------------------------------------------
    def set_items(self, items) -> None:
        """items: iterable of (key, label) or (key, label, enabled)."""
        for child in list(self.inner.children.values()):
            child.destroy()
        self._vars.clear()
        self._enabled.clear()
        self._order = []
        for item in items:
            key, label = item[0], item[1]
            enabled = bool(item[2]) if len(item) > 2 else True
            variable = tk.BooleanVar(value=False)
            state = "normal" if enabled else "disabled"
            try:
                widget = ttk.Checkbutton(
                    self.inner,
                    text=label,
                    variable=variable,
                    state=state,
                    command=lambda k=key: self._notify(k),
                    wraplength=max(300, self.winfo_width() - 60),
                )
            except tk.TclError:
                widget = ttk.Checkbutton(
                    self.inner, text=label, variable=variable, state=state,
                    command=lambda k=key: self._notify(k),
                )
            widget.pack(anchor="w", fill="x", padx=4, pady=1)
            self._vars[key] = variable
            self._enabled[key] = enabled
            self._order.append(key)
        self.canvas.yview_moveto(0)

    def set_checked(self, keys) -> None:
        wanted = {str(key) for key in keys}
        for key, variable in self._vars.items():
            if not self._enabled.get(key, True):
                continue
            variable.set(key in wanted)

    def set_all(self, value: bool) -> None:
        for key, variable in self._vars.items():
            if self._enabled.get(key, True):
                variable.set(bool(value))
        self._notify(None)

    def checked(self) -> list[str]:
        return [
            key for key in self._order
            if self._enabled.get(key, True) and self._vars[key].get()
        ]

    def _notify(self, key) -> None:
        if self.on_change is not None:
            value = self._vars[key].get() if key is not None and key in self._vars else None
            self.on_change(key, value)


# ---------------------------------------------------------------------------
# main window
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root, settings=None):
        self.root = root
        self.settings = settings or pl.load_settings()
        self._ui_queue: "queue.Queue" = queue.Queue()
        self._log_queue: "queue.Queue" = queue.Queue()
        self.logger = pl.Logger(echo=lambda level, msg: self._log_queue.put((level, msg)))

        self.npv_infos: list[pl.NpvInfo] = []
        self.look_state: dict[str, dict] = {}
        self.active_npv: str | None = None
        self.worker: threading.Thread | None = None
        self._busy = False
        self._look_scan_busy: set[str] = set()
        self._closing = False
        self._refresh_pending = False
        self._npv_signature = None

        root.title(pl.APP_TITLE)
        root.geometry("1180x880")
        root.minsize(980, 680)
        self._configure_styles()
        self._build_ui()
        self._poll()

        self.logger.step(f"{pl.APP_TITLE} - {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info(f"Log file: {pl.LOG_PATH}")
        if not pl.SETTINGS_PATH.exists():
            pl.save_settings(self.settings, logger=self.logger)
            self.logger.info("First run: settings.json created.")
        self.refresh_npv_list()
        self._auto_detect_missing()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ ui
    def _configure_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Bad.TLabel", foreground="#b00020")
        style.configure("Good.TLabel", foreground="#1a7f37")

    def _build_ui(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=2)
        root.rowconfigure(4, weight=3)

        header = ttk.Frame(root, padding=(12, 10, 12, 0))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        tk.Label(header, text="V-Exporter", font=("Segoe UI", 17, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(
            header,
            text="Cyberpunk 2077 NPV looks → GLB (materials) + FBX + OBJ",
            font=("Segoe UI", 9), fg="#666666",
        ).grid(row=1, column=0, sticky="w")

        # ---- setup -----------------------------------------------------
        setup = ttk.LabelFrame(root, text="One-time setup", padding=(6, 4))
        setup.grid(row=1, column=0, sticky="ew", padx=12, pady=(8, 4))
        setup.columnconfigure(1, weight=1)

        self.var_wk = tk.StringVar(value=self.settings.wolvenkit_cli)
        self.var_blender = tk.StringVar(value=self.settings.blender)
        self.var_game = tk.StringVar(value=self.settings.game_root)
        self.var_npv = tk.StringVar(value=self.settings.npv_root)
        self.var_output = tk.StringVar(value=self.settings.output_root)

        self._add_row(setup, 0, "WolvenKit.CLI.exe", self.var_wk,
                      lambda: self._browse_file(self.var_wk, "Select WolvenKit.CLI.exe", "WolvenKit.CLI.exe"),
                      "Auto-detect", lambda: self._detect_async("wolvenkit"))
        self._add_row(setup, 1, "Blender (blender.exe)", self.var_blender,
                      lambda: self._browse_file(self.var_blender, "Select blender.exe", "blender.exe"),
                      "Auto-detect", lambda: self._detect_async("blender"))
        self._add_row(setup, 2, "Game folder", self.var_game,
                      lambda: self._browse_folder(self.var_game, "Select the Cyberpunk 2077 game folder"),
                      "Auto-detect", lambda: self._detect_async("game"))
        self._add_row(setup, 3, "NPV folder (override)", self.var_npv,
                      lambda: self._browse_folder(self.var_npv, "Select ...\\AppearanceCreatorMod\\NPV"),
                      "From game", lambda: self._detect_async("npv"))
        self._add_row(setup, 4, "Output folder", self.var_output,
                      lambda: self._browse_folder(self.var_output, "Select the export destination"),
                      "Open", self._open_output)

        # ---- NPV + looks ----------------------------------------------
        middle = ttk.Frame(root)
        middle.grid(row=2, column=0, sticky="nsew", padx=12, pady=4)
        middle.columnconfigure(0, weight=3)
        middle.columnconfigure(1, weight=4)
        middle.rowconfigure(0, weight=1)

        npv_panel = ttk.LabelFrame(middle, text="NPV folders", padding=(6, 4))
        npv_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        npv_panel.columnconfigure(0, weight=1)
        npv_panel.rowconfigure(1, weight=1)

        npv_bar = ttk.Frame(npv_panel)
        npv_bar.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        npv_bar.columnconfigure(0, weight=1)
        self.npv_root_label = tk.Label(npv_bar, text="NPV folder: (not set)", anchor="w", fg="#666666")
        self.npv_root_label.grid(row=0, column=0, sticky="ew")
        ttk.Button(npv_bar, text="Rescan", command=self.refresh_npv_list).grid(row=0, column=1)

        self.npv_list = CheckList(npv_panel, height=8, on_change=self._npv_toggled)
        self.npv_list.grid(row=1, column=0, sticky="nsew")

        ttk.Label(
            npv_panel,
            text="Tick one or more NPVs. A tick also loads that NPV's looks.",
            style="Hint.TLabel",
        ).grid(row=2, column=0, sticky="w", pady=(4, 0))

        look_panel = ttk.LabelFrame(middle, text="Looks (appearances) for the selected NPV", padding=(6, 4))
        look_panel.grid(row=0, column=1, sticky="nsew")
        look_panel.columnconfigure(0, weight=1)
        look_panel.rowconfigure(2, weight=1)

        self.look_title = tk.Label(look_panel, text="No NPV selected", anchor="w",
                                   font=("Segoe UI", 9, "bold"))
        self.look_title.grid(row=0, column=0, sticky="ew")
        self.look_hint = tk.Label(
            look_panel,
            text="Tick an NPV on the left. Scan looks uncooks the archive (needs WolvenKit).",
            anchor="w", justify="left", fg="#666666",
        )
        self.look_hint.grid(row=1, column=0, sticky="ew", pady=(0, 4))

        self.look_list = CheckList(look_panel, height=8, on_change=self._looks_changed)
        self.look_list.grid(row=2, column=0, sticky="nsew")

        look_buttons = ttk.Frame(look_panel)
        look_buttons.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self.btn_scan = ttk.Button(look_buttons, text="Scan looks (uncook)", command=self._scan_looks)
        self.btn_scan.pack(side="left")
        ttk.Button(look_buttons, text="Select all", command=lambda: self.look_list.set_all(True)).pack(side="left", padx=4)
        ttk.Button(look_buttons, text="Select none", command=lambda: self.look_list.set_all(False)).pack(side="left")

        # ---- actions ---------------------------------------------------
        actions = ttk.Frame(root, padding=(12, 2, 12, 2))
        actions.grid(row=3, column=0, sticky="ew")
        self.btn_export = tk.Button(
            actions, text="EXPORT SELECTED NPVs", font=("Segoe UI", 11, "bold"),
            bg="#1f6feb", fg="white", activebackground="#1558c0", activeforeground="white",
            relief="flat", padx=18, pady=7, command=self._start_export,
        )
        self.btn_export.pack(side="left")
        self.var_force = tk.BooleanVar(value=False)
        ttk.Checkbutton(actions, text="Force re-uncook", variable=self.var_force).pack(side="left", padx=10)
        self.var_proofs = tk.BooleanVar(value=bool(self.settings.render_proofs))
        ttk.Checkbutton(
            actions, text="Render 1024×1024 proofs", variable=self.var_proofs,
            command=self._after_option_change,
        ).pack(side="left")
        self.btn_open_output = ttk.Button(actions, text="Open output folder", command=self._open_output)
        self.btn_open_output.pack(side="left")
        self.btn_selftest = ttk.Button(actions, text="Self-test", command=self._run_selftest_ui)
        self.btn_selftest.pack(side="right")

        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=140)
        self.progress.pack(side="right", padx=8)
        self.status_label = tk.Label(actions, text="Ready", fg="#666666")
        self.status_label.pack(side="right")

        # ---- log -------------------------------------------------------
        log_frame = ttk.LabelFrame(root, text="Log", padding=(4, 4))
        log_frame.grid(row=4, column=0, sticky="nsew", padx=12, pady=(2, 10))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_frame, wrap="word", height=12, font=("Consolas", 9),
            background="#fbfbfb", foreground="#222222", state="disabled",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.tag_configure("info", foreground="#222222")
        self.log_text.tag_configure("step", foreground="#0b5cad", font=("Consolas", 9, "bold"))
        self.log_text.tag_configure("warn", foreground="#a15c00")
        self.log_text.tag_configure("error", foreground="#b00020")
        self.log_text.tag_configure("raw", foreground="#555555")

    def _add_row(self, parent, row, label, variable, browse, action_text, action) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(6, 8), pady=2)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=2)
        ttk.Button(parent, text="Browse…", command=browse).grid(row=row, column=2, padx=4, pady=2)
        ttk.Button(parent, text=action_text, command=action).grid(row=row, column=3, padx=(0, 6), pady=2)

    # ------------------------------------------------------------- settings
    def _browse_file(self, variable, title, expected: str) -> None:
        current = variable.get().strip()
        initial = str(Path(current).parent) if current and Path(current).parent.is_dir() else str(pl.APP_DIR)
        chosen = filedialog.askopenfilename(
            title=title,
            initialdir=initial,
            filetypes=[(expected, expected), ("Executables", "*.exe"), ("All files", "*.*")],
        )
        if chosen:
            variable.set(chosen)
            self._after_path_change()

    def _browse_folder(self, variable, title) -> None:
        current = variable.get().strip()
        initial = current if current and Path(current).is_dir() else str(pl.APP_DIR)
        chosen = filedialog.askdirectory(title=title, initialdir=initial, mustexist=False)
        if chosen:
            variable.set(chosen)
            self._after_path_change()

    def _collect_settings(self) -> pl.Settings:
        settings = self.settings.copy()
        settings.wolvenkit_cli = self.var_wk.get().strip()
        settings.blender = self.var_blender.get().strip()
        settings.game_root = self.var_game.get().strip()
        settings.npv_root = self.var_npv.get().strip()
        settings.output_root = self.var_output.get().strip()
        settings.render_proofs = bool(self.var_proofs.get())
        return settings

    def _after_path_change(self) -> None:
        self.settings = self._collect_settings()
        pl.save_settings(self.settings, logger=self.logger)
        self._schedule_refresh()

    def _after_option_change(self) -> None:
        """Persist option checkboxes that are part of settings.json."""
        self.settings = self._collect_settings()
        pl.save_settings(self.settings, logger=self.logger)

    def _schedule_refresh(self) -> None:
        """Coalesce several settings changes into one NPV scan / log block."""
        if self._refresh_pending:
            return
        self._refresh_pending = True
        try:
            self.root.after(80, self._run_pending_refresh)
        except Exception:  # noqa: BLE001
            self._refresh_pending = False
            self.refresh_npv_list()

    def _run_pending_refresh(self) -> None:
        self._refresh_pending = False
        if self._closing:
            return
        self.refresh_npv_list()

    # ------------------------------------------------------------ detection
    def _auto_detect_missing(self) -> None:
        missing: list[str] = []
        wk = self.var_wk.get().strip()
        if not wk or not Path(wk).is_file():
            missing.append("wolvenkit")
        blender = self.var_blender.get().strip()
        if not blender or not Path(blender).is_file():
            missing.append("blender")
        if pl.normalize_game_root(self.var_game.get()) is None:
            missing.append("game")
        if missing:
            self.logger.info("Auto-detecting missing paths: " + ", ".join(missing) + " …")
        for kind in missing:
            self._detect_async(kind, notify=False)

    def _detect_async(self, kind: str, notify: bool = True) -> None:
        game_value = self.var_game.get()
        self.status_label.configure(text=f"Detecting {kind}…")

        def task() -> None:
            found = None
            try:
                if kind == "wolvenkit":
                    found = pl.find_wolvenkit(self.logger)
                elif kind == "blender":
                    found = pl.find_blender(self.logger)
                elif kind == "game":
                    found = pl.detect_game_root(self.logger)
                elif kind == "npv":
                    game = pl.normalize_game_root(game_value)
                    if game is not None:
                        found = pl.derive_npv_root(game)
            except Exception:
                self.logger.error(traceback.format_exc())
            self._push(lambda: self._apply_detected(kind, found, notify))

        threading.Thread(target=task, name=f"vx-detect-{kind}", daemon=True).start()

    def _apply_detected(self, kind: str, found, notify: bool) -> None:
        labels = {
            "wolvenkit": "WolvenKit.CLI.exe",
            "blender": "blender.exe",
            "game": "Cyberpunk 2077",
            "npv": "ACM NPV folder",
        }
        if found:
            text = str(found)
            if kind == "wolvenkit":
                self.var_wk.set(text)
            elif kind == "blender":
                self.var_blender.set(text)
            elif kind == "game":
                self.var_game.set(text)
                if not self.var_npv.get().strip():
                    derived = pl.derive_npv_root(found)
                    if derived is not None:
                        self.var_npv.set(str(derived))
            elif kind == "npv":
                self.var_npv.set(text)
            self.logger.info(f"{labels[kind]} detected: {text}")
            self._after_path_change()
        else:
            self.logger.warn(f"{labels[kind]} not found automatically; use Browse.")
            if notify:
                messagebox.showwarning(
                    "Auto-detect failed",
                    f"{labels[kind]} was not found automatically.\n\n"
                    "Use 'Browse…' to point at it, or see README.md for the download/install steps.",
                )
        if not self._busy:
            self.status_label.configure(text="Ready")

    # --------------------------------------------------------------- NPVs
    def _find_npv(self, name: str) -> pl.NpvInfo | None:
        return next((info for info in self.npv_infos if info.name == name), None)

    def refresh_npv_list(self) -> None:
        previous = set(self.npv_list.checked())
        self.settings = self._collect_settings()
        configured = self.settings.npv_root.strip()
        root = pl.effective_npv_root(self.settings, self.logger)
        if root is not None and configured and not self._same_path(configured, root):
            # A game folder (or a folder inside it) was entered: use the real NPV path.
            self.var_npv.set(str(root))
            self.settings = self._collect_settings()
            pl.save_settings(self.settings, logger=self.logger)
        if root is None:
            self.npv_root_label.configure(text="NPV folder: not found (set the game folder)", fg="#b00020")
            self.npv_infos = []
            self.npv_list.set_items([])
            signature = ("missing", configured.lower())
            if signature != self._npv_signature:
                self._npv_signature = signature
                self.logger.warn("ACM NPV folder not found; check the game folder path.")
            return
        self.npv_root_label.configure(text=f"NPV folder: {root}", fg="#666666")
        self.npv_infos = pl.list_npvs(root, self.logger)
        ready = [info for info in self.npv_infos if info.exportable]
        drafts = [info for info in self.npv_infos if info.is_draft]
        items = [
            (info.name, self._npv_label(info), info.exportable)
            for info in self.npv_infos
        ]
        self.npv_list.set_items(items)
        still_there = {info.name for info in self.npv_infos}
        self.npv_list.set_checked(previous & still_there)
        if self.active_npv not in still_there:
            self.active_npv = None
            self._render_looks()
        signature = ("list", str(root).lower(), tuple((i.name, i.status) for i in self.npv_infos))
        if signature != self._npv_signature:
            self._npv_signature = signature
            for info in drafts:
                self.logger.warn(f"{info.name}: {pl.draft_message(info)}")
            self.logger.info(
                f"{len(ready)} packed, {len(drafts)} draft NPV folder(s) in {root}"
            )

    @staticmethod
    def _same_path(left: str, right) -> bool:
        return (
            os.path.normcase(str(left).rstrip("\\/"))
            == os.path.normcase(str(right).rstrip("\\/"))
        )

    @staticmethod
    def _npv_label(info: pl.NpvInfo) -> str:
        if info.is_draft:
            looks = ", ".join(info.draft_looks) if info.draft_looks else "no looks parsed"
            return (
                f"{info.name}    (DRAFT - not packed)  ·  looks: {looks}\n"
                f"    In-game: ACM NPV tab -> press Create NPV, then rescan."
            )
        if info.archives:
            count = len(info.archives)
            text = f"{info.name}    ({count} archive{'s' if count != 1 else ''})"
        else:
            text = f"{info.name}    (no .archive!)"
        extras = []
        if info.icon:
            extras.append("icon")
        if info.xl_files:
            extras.append(".xl")
        if info.share_zips:
            extras.append("share zip")
        if extras:
            text += "  ·  " + ", ".join(extras)
        return text

    def _npv_toggled(self, key, value) -> None:
        if key is None or not value:
            return
        self._activate_npv(key)

    def _activate_npv(self, name: str) -> None:
        self.active_npv = name
        self._render_looks()
        if name in self.look_state or name in self._look_scan_busy:
            return
        self._look_scan_busy.add(name)

        def task() -> None:
            scan = None
            try:
                scan = pl.cached_looks(name)
            except Exception:
                self.logger.error(traceback.format_exc())
            self._push(lambda: self._apply_cached_scan(name, scan))

        threading.Thread(target=task, name="vx-cache-scan", daemon=True).start()

    def _apply_cached_scan(self, name: str, scan) -> None:
        self._look_scan_busy.discard(name)
        if scan is not None and scan.ent_json is not None and name not in self.look_state:
            self.look_state[name] = {
                "looks": list(scan.looks),
                "checked": set(scan.looks),
                "ent": str(scan.ent_json),
                "raw": str(scan.raw_dir) if scan.raw_dir else None,
            }
            self.logger.info(
                f"[{name}] {len(scan.looks)} look(s) loaded from the uncook cache"
                + (": " + ", ".join(scan.looks) if scan.looks else " (no names found)")
            )
            if self.active_npv == name:
                self._render_looks()

    def _render_looks(self) -> None:
        if not self.active_npv:
            self.look_title.configure(text="No NPV selected")
            self.look_hint.configure(
                text="Tick an NPV on the left. Looks are discovered from the uncooked .ent.json; "
                     "use 'Scan looks (uncook)' when nothing is cached yet."
            )
            self.look_list.set_items([])
            return
        state = self.look_state.get(self.active_npv)
        if state is None:
            self.look_title.configure(text=f"Looks for {self.active_npv}")
            self.look_hint.configure(
                text="No uncook cache for this NPV. Click 'Scan looks (uncook)' "
                     "(one-time, can take minutes) or just export: "
                     "the exporter then imports ALL appearances found in the archive."
            )
            self.look_list.set_items([])
            return
        looks = state.get("looks") or []
        self.look_title.configure(text=f"Looks for {self.active_npv} ({len(looks)})")
        if looks:
            self.look_hint.configure(
                text="Untick looks to skip them. Leaving every look unticked exports all discovered looks."
            )
        else:
            self.look_hint.configure(
                text="No look names discovered in the entity - exporting will import ALL appearances "
                     "and export each appearance collection separately."
            )
        self.look_list.set_items([(name, name) for name in looks])
        self.look_list.set_checked([name for name in state.get("checked", set()) if name in looks])

    def _looks_changed(self, key, value) -> None:
        state = self.look_state.get(self.active_npv) if self.active_npv else None
        if state is None:
            return
        if key is None:
            state["checked"] = set(self.look_list.checked())
        elif value:
            state["checked"].add(key)
        else:
            state["checked"].discard(key)

    # --------------------------------------------------------------- scan
    def _scan_looks(self) -> None:
        if self._busy:
            return
        if not self.active_npv:
            messagebox.showinfo("Scan looks", "Tick an NPV first.")
            return
        info = self._find_npv(self.active_npv)
        if info is None:
            return
        settings = self._collect_settings()
        cli = settings.wolvenkit_cli.strip()
        if not cli or not Path(cli).is_file():
            messagebox.showerror(
                "WolvenKit.CLI.exe required",
                "Look discovery uncooks the NPV archive, which needs WolvenKit.CLI.exe.\n\n"
                "Download WolvenKit.Console-<version>.zip (10.0+ for game 2.5) from\n"
                "https://github.com/WolvenKit/WolvenKit/releases, extract it and select\n"
                "WolvenKit.CLI.exe in the setup row.\n\nSee README.md.",
            )
            return
        self._set_busy(True, f"Scanning {info.name}…")
        self.logger.step(f"Scanning looks for {info.name} (uncook) …")

        def task() -> None:
            try:
                scan = pl.scan_npv(info, settings, self.logger, force=self.var_force.get())
                self._push(lambda: self._scan_done(info.name, scan, None))
            except Exception as exc:
                detail = str(exc)
                self._push(lambda: self._scan_done(info.name, None, detail))

        self.worker = threading.Thread(target=task, name="vx-scan", daemon=True)
        self.worker.start()

    def _scan_done(self, name: str, scan, error) -> None:
        self._set_busy(False, "Ready")
        if error:
            self.logger.error(f"[{name}] Scan failed: {error}")
            messagebox.showerror("Scan failed", f"{name}: {error}")
            return
        if scan is None or scan.ent_json is None:
            self.logger.error(f"[{name}] No .ent.json found in the uncooked output.")
            messagebox.showerror(
                "Scan failed",
                f"{name}: no .ent.json was found after uncooking.\n\n"
                "The archive may be protected (ACM 'Protected' setting) or incomplete; see README.md.",
            )
            return
        self.look_state[name] = {
            "looks": list(scan.looks),
            "checked": set(scan.looks),
            "ent": str(scan.ent_json),
            "raw": str(scan.raw_dir) if scan.raw_dir else None,
        }
        self.logger.info(f"[{name}] Scan complete: {len(scan.looks)} look(s).")
        if self.active_npv == name:
            self._render_looks()

    # -------------------------------------------------------------- export
    def _collect_jobs(self):
        jobs = []
        for name in self.npv_list.checked():
            info = self._find_npv(name)
            if info is None or not info.exportable:
                continue
            state = self.look_state.get(name) or {}
            discovered = list(state.get("looks") or [])
            checked = [look for look in (state.get("checked") or []) if look in discovered]
            requested = checked if checked else discovered
            jobs.append((info, requested))
        return jobs

    def _start_export(self) -> None:
        if self._busy:
            return
        self.settings = self._collect_settings()
        pl.save_settings(self.settings, logger=self.logger)
        problems = pl.validate_settings(self.settings)
        if problems:
            self.logger.error("Export blocked: setup incomplete:")
            for problem in problems:
                self.logger.error("  • " + problem.replace("\n", " "))
            messagebox.showerror(
                "Setup required",
                "Fix these before exporting:\n\n"
                + "\n\n".join(problems)
                + "\n\nDownload/install steps: README.md; quick check: python v_exporter.py --selftest",
            )
            return
        jobs = self._collect_jobs()
        if not jobs:
            selected = [self._find_npv(name) for name in self.npv_list.checked()]
            selected = [info for info in selected if info is not None]
            drafts = [info for info in selected if info.is_draft]
            if drafts:
                self.logger.warn(
                    "Nothing exported: no selected NPV is packed yet:\n"
                    + "\n".join(f"{info.name}: {pl.draft_message(info)}" for info in drafts)
                )
                messagebox.showwarning(
                    "No packed NPV selected",
                    "Drafts cannot be exported yet:\n\n"
                    + "\n\n".join(f"{info.name}: {pl.draft_message(info)}" for info in drafts),
                )
            else:
                messagebox.showwarning(
                    "Nothing selected",
                    "Tick at least one packed NPV on the left "
                    "(draft NPVs cannot be exported until ACM packs them).",
                )
            return
        self._set_busy(True, "Exporting…")
        settings = self.settings.copy()
        force = bool(self.var_force.get())
        self.logger.step("=" * 68)
        self.logger.step(f"EXPORT START: {len(jobs)} NPV(s), output: {settings.output_root}")

        def task() -> None:
            failures = 0
            try:
                for index, (info, looks) in enumerate(jobs, start=1):
                    self.logger.step(f"--- NPV {index}/{len(jobs)}: {info.name} ---")
                    if self._closing:
                        break
                    try:
                        pl.export_npv(info, looks, settings, self.logger, force_uncook=force)
                    except pl.SetupError as exc:
                        self.logger.error(str(exc))
                        failures += 1
                    except Exception as exc:  # noqa: BLE001
                        self.logger.error(f"[{info.name}] {exc}")
                        self.logger.raw(traceback.format_exc())
                        failures += 1
            except Exception:  # noqa: BLE001
                self.logger.error(traceback.format_exc())
                failures += 1
            finally:
                self._push(lambda: self._export_finished(len(jobs), failures))

        self.worker = threading.Thread(target=task, name="vx-export", daemon=True)
        self.worker.start()

    def _export_finished(self, total: int, failures: int) -> None:
        self._set_busy(False, "Ready")
        if failures:
            self.logger.warn(f"EXPORT FINISHED with problems: {failures}/{total} NPV(s) failed.")
        else:
            self.logger.info(f"EXPORT FINISHED: {total} NPV(s) processed.")
        self.logger.info(f"Log: {pl.LOG_PATH}")
        output = Path(self._collect_settings().output_root)
        if output.is_dir():
            self.status_label.configure(text="Export finished")
            answer = messagebox.askyesno(
                "Export finished",
                f"{'Some NPVs failed. ' if failures else ''}Output: {output}\n\nOpen the output folder?",
            )
            if answer:
                self._open_output()
        else:
            messagebox.showerror(
                "Export finished with errors",
                f"No output folder was created at:\n{output}\n\nSee the log pane / v_exporter.log.",
            )

    def _run_selftest_ui(self) -> None:
        if self._busy:
            return
        self._set_busy(True, "Self-test…")
        settings = self._collect_settings()

        def task() -> None:
            try:
                pl.run_selftest(settings, self.logger)
            except Exception:  # noqa: BLE001
                self.logger.error(traceback.format_exc())
            finally:
                self._push(lambda: self._set_busy(False, "Ready"))

        threading.Thread(target=task, name="vx-selftest", daemon=True).start()

    # --------------------------------------------------------------- misc
    def _open_output(self) -> None:
        output = Path(self._collect_settings().output_root)
        if not output.is_dir():
            messagebox.showinfo(
                "Output folder",
                f"{output} does not exist yet.\n\nIt is created on the first successful export.",
            )
            return
        try:
            os.startfile(str(output))  # type: ignore[attr-defined]
        except AttributeError:
            messagebox.showinfo("Output folder", str(output))
        except OSError as exc:
            messagebox.showerror("Open output folder", f"Could not open {output}:\n{exc}")

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        try:
            self.btn_export.configure(state=state)
            self.btn_scan.configure(state=state)
            self.btn_selftest.configure(state=state)
        except Exception:
            pass
        if busy:
            try:
                self.progress.start(12)
            except Exception:
                pass
        else:
            try:
                self.progress.stop()
            except Exception:
                pass
        if status:
            try:
                self.status_label.configure(text=status)
            except Exception:
                pass

    # ----------------------------------------------------------- plumbing
    def _push(self, callback) -> None:
        self._ui_queue.put(callback)

    def _poll(self) -> None:
        try:
            while True:
                level, message = self._log_queue.get_nowait()
                self._append_log(level, message)
        except queue.Empty:
            pass
        try:
            while True:
                callback = self._ui_queue.get_nowait()
                try:
                    callback()
                except Exception:  # noqa: BLE001
                    self._append_log("ERROR", traceback.format_exc())
        except queue.Empty:
            pass
        if not self._closing:
            self.root.after(100, self._poll)

    def _append_log(self, level: str, message: str) -> None:
        tag = {"STEP": "step", "WARN": "warn", "ERROR": "error", "RAW": "raw"}.get(level, "info")
        self.log_text.configure(state="normal")
        for line in str(message).splitlines() or [""]:
            self.log_text.insert("end", line + "\n", tag)
        try:
            total = int(self.log_text.index("end-1c").split(".")[0])
            if total > 6000:
                self.log_text.delete("1.0", f"{total - 5000}.0")
        except (tk.TclError, ValueError):
            pass
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self._busy:
            keep_open = messagebox.askyesno(
                "Export in progress",
                "A pipeline step is still running.\n\n"
                "Closing now may leave a Blender/WolvenKit process running. Close anyway?",
            )
            if not keep_open:
                return
        self._closing = True
        try:
            self.settings = self._collect_settings()
            pl.save_settings(self.settings, logger=self.logger)
        except Exception:
            pass
        self.logger.close()
        try:
            self.root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        # Any command-line argument uses the headless CLI (no Tk import at startup).
        import cli

        return cli.main(argv)

    if _TK_ERROR is not None:
        print("tkinter is unavailable, so the GUI cannot start:")
        print(f"  {_TK_ERROR}")
        print("Install a standard Python build from python.org (it bundles tkinter),")
        print("or use the CLI: python v_exporter.py --help")
        return 1

    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not open a window: {exc}")
        return 1

    settings = pl.load_settings()
    App(root, settings)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
