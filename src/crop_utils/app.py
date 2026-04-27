"""
crop_utils.app
==============
Tkinter GUI for cropping images by grid or custom rectangular selection.

New in v0.2
-----------
* Grid **Offset X/Y** (pixels before first cell) and **Margin X/Y** (gap
  between cells) — live overlay preview as you type.
* Custom mode: configurable fixed selection **Width / Height**; click
  places a rectangle of that size; drag still works freehand.
* **Ghost preview** – dashed rect shows where the next selection lands.
* **Grid hover** – white dashed outline highlights the cell under cursor.
* Click an existing custom selection to **select** it (yellow border);
  selected area shown with thicker border in preview panel.
* **Arrow-key movement** of selected area (canvas must have focus –
  click it once):
    - No modifier  → 1 px
    - Shift        → 10 px
    - Ctrl         → 5 px
    - Ctrl + Shift → 50 px
  Holding a key repeats after 400 ms at 50 ms intervals.
* **Duplicate** selected area (Ctrl+D or button).
* Background thread resizes the image so the UI never freezes;
  ``root.after(0, …)`` delivers the result to the main thread.
* Canvas overlay (grid lines / selection rects) is redrawn without
  re-rendering the image — overlays update instantly on every change.

Requires: Python ≥ 3.10, Pillow ≥ 10.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AUTOSAVE_MS          = 30_000   # autosave interval
_KEY_INITIAL_DELAY_MS = 400      # delay before key-hold repeat begins
_KEY_REPEAT_MS        = 50       # repeat interval while key held
_OVERLAY_DEBOUNCE_MS  = 150      # debounce for live spinbox → overlay
_DUPLICATE_OFFSET_PX  = 10       # pixel offset when duplicating an area
_SHIFT_MASK           = 0x0001   # Tkinter event.state bit for Shift
_CTRL_MASK            = 0x0004   # Tkinter event.state bit for Control
_ARROW_KEYS           = frozenset({"Up", "Down", "Left", "Right"})
_ARROW_DELTAS: dict[str, tuple[int, int]] = {
    "Left": (-1, 0), "Right": (1, 0), "Up": (0, -1), "Down": (0, 1),
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class GridSize:
    width: int    = 64
    height: int   = 64
    offset_x: int = 0   # pixels before the first column
    offset_y: int = 0   # pixels before the first row
    margin_x: int = 0   # horizontal gap between cells
    margin_y: int = 0   # vertical gap between cells


@dataclass
class CropArea:
    x: int
    y: int
    width: int
    height: int


@dataclass
class ProjectData:
    image_path: str            = ""
    crop_type: str             = "grid"   # "grid" | "custom"
    grid_size: GridSize        = field(default_factory=GridSize)
    naming_template: str       = "frame_%num%"
    crop_areas: list[CropArea] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "image_path": self.image_path,
            "crop_type":  self.crop_type,
            "grid_size": {
                "width":    self.grid_size.width,
                "height":   self.grid_size.height,
                "offset_x": self.grid_size.offset_x,
                "offset_y": self.grid_size.offset_y,
                "margin_x": self.grid_size.margin_x,
                "margin_y": self.grid_size.margin_y,
            },
            "naming_template": self.naming_template,
            "crop_areas": [
                {"x": a.x, "y": a.y, "width": a.width, "height": a.height}
                for a in self.crop_areas
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectData":
        proj = cls()
        proj.image_path      = data.get("image_path", "")
        proj.crop_type       = data.get("crop_type", "grid")
        gs = data.get("grid_size", {})
        proj.grid_size = GridSize(
            width=int(gs.get("width",    64)),
            height=int(gs.get("height",  64)),
            offset_x=int(gs.get("offset_x", 0)),
            offset_y=int(gs.get("offset_y", 0)),
            margin_x=int(gs.get("margin_x", 0)),
            margin_y=int(gs.get("margin_y", 0)),
        )
        proj.naming_template = data.get("naming_template", "frame_%num%")
        proj.crop_areas = [
            CropArea(int(a["x"]), int(a["y"]), int(a["width"]), int(a["height"]))
            for a in data.get("crop_areas", [])
        ]
        return proj


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class CropApp:
    """Main application window."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Crop Utils")
        self.root.geometry("1340x860")
        self.root.minsize(980, 650)

        # ── project / image ──────────────────────────────────────────────
        self.project: ProjectData         = ProjectData()
        self.project_path: Optional[Path] = None
        self.image: Optional[Image.Image] = None

        # ── canvas rendering ─────────────────────────────────────────────
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._scale: float                        = 1.0
        self._offset: tuple[float, float]         = (10.0, 10.0)

        # ── threaded render ───────────────────────────────────────────────
        self._render_cancel: Optional[threading.Event] = None

        # ── selection state ───────────────────────────────────────────────
        self._grid_cells: set[tuple[int, int]] = set()
        self._selected_idx: Optional[int]      = None   # active custom area
        self._drawing: bool                    = False
        self._draw_start: Optional[tuple[float, float]] = None

        # ── hover / ghost ─────────────────────────────────────────────────
        self._hover_img: Optional[tuple[float, float]] = None

        # ── arrow-key hold repeat ─────────────────────────────────────────
        self._keys_held: dict[str, int]     = {}   # key → step
        self._key_repeat_ids: dict[str, str] = {}  # key → after-id

        # ── debounce id for live overlay redraw ───────────────────────────
        self._overlay_debounce_id: Optional[str] = None

        # ── preview thumbnail refs (prevent GC) ───────────────────────────
        self._thumb_refs: list[Optional[ImageTk.PhotoImage]] = []

        # ── build ─────────────────────────────────────────────────────────
        self._build_menu()
        self._build_ui()
        self._schedule_autosave()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ===================================================================
    # Menu
    # ===================================================================

    def _build_menu(self) -> None:
        bar = tk.Menu(self.root)
        self.root.config(menu=bar)

        fm = tk.Menu(bar, tearoff=False)
        bar.add_cascade(label="File", menu=fm)
        fm.add_command(label="Open Image…",   command=self._cmd_open_image,
                       accelerator="Ctrl+O")
        fm.add_separator()
        fm.add_command(label="Save Project",  command=self._cmd_save,
                       accelerator="Ctrl+S")
        fm.add_command(label="Save Project As…", command=self._cmd_save_as)
        fm.add_command(label="Open Project…", command=self._cmd_open_project,
                       accelerator="Ctrl+Shift+O")
        fm.add_separator()
        fm.add_command(label="Export Crops…", command=self._cmd_export)
        fm.add_separator()
        fm.add_command(label="Exit", command=self._on_close)

        self.root.bind_all("<Control-o>", lambda _e: self._cmd_open_image())
        self.root.bind_all("<Control-s>", lambda _e: self._cmd_save())
        self.root.bind_all("<Control-O>", lambda _e: self._cmd_open_project())
        self.root.bind_all("<Control-d>", lambda _e: self._cmd_duplicate())

    # ===================================================================
    # UI Construction
    # ===================================================================

    def _build_ui(self) -> None:
        self._pw = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self._pw.pack(fill=tk.BOTH, expand=True)

        self._build_left_panel()
        self._build_canvas_panel()
        self._build_preview_panel()

        self._statusbar_var = tk.StringVar(value="Ready – open an image to begin.")
        ttk.Label(
            self.root, textvariable=self._statusbar_var,
            relief=tk.SUNKEN, anchor=tk.W, padding=(5, 2),
        ).pack(side=tk.BOTTOM, fill=tk.X)

    # ---- Left controls panel ------------------------------------------

    def _build_left_panel(self) -> None:
        lframe = ttk.Frame(self._pw, width=235)
        lframe.pack_propagate(False)
        self._pw.add(lframe, weight=0)

        # Scrollable inner area so controls are never clipped
        vsb = ttk.Scrollbar(lframe, orient=tk.VERTICAL)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        lcanvas = tk.Canvas(lframe, yscrollcommand=vsb.set, highlightthickness=0)
        lcanvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.config(command=lcanvas.yview)
        inner = ttk.Frame(lcanvas)
        win_id = lcanvas.create_window((0, 0), window=inner, anchor=tk.NW)
        inner.bind("<Configure>", lambda _e: lcanvas.configure(
            scrollregion=lcanvas.bbox("all")))
        lcanvas.bind("<Configure>", lambda e: lcanvas.itemconfig(win_id, width=e.width))
        for w in (lcanvas, inner):
            w.bind("<MouseWheel>",
                   lambda e: lcanvas.yview_scroll(-1 * (e.delta // 120), "units"))
            w.bind("<Button-4>", lambda _e: lcanvas.yview_scroll(-1, "units"))
            w.bind("<Button-5>", lambda _e: lcanvas.yview_scroll( 1, "units"))

        pad = dict(fill=tk.X, padx=5, pady=4)

        # -- Image --------------------------------------------------------
        sec = ttk.LabelFrame(inner, text="Image", padding=5)
        sec.pack(**pad)
        ttk.Button(sec, text="Open Image…", command=self._cmd_open_image).pack(fill=tk.X)
        self._img_name_var = tk.StringVar(value="—")
        ttk.Label(sec, textvariable=self._img_name_var, wraplength=210,
                  foreground="gray", font=("TkDefaultFont", 8)).pack(
                  anchor=tk.W, pady=(2, 0))

        # -- Crop Settings (Notebook = crop-type selector) ----------------
        sec = ttk.LabelFrame(inner, text="Crop Settings", padding=5)
        sec.pack(**pad)
        self._settings_nb = ttk.Notebook(sec)
        self._settings_nb.pack(fill=tk.BOTH)
        self._settings_nb.bind("<<NotebookTabChanged>>", self._on_tab_change)

        grid_tab   = ttk.Frame(self._settings_nb, padding=4)
        custom_tab = ttk.Frame(self._settings_nb, padding=4)
        self._settings_nb.add(grid_tab,   text=" Grid ")
        self._settings_nb.add(custom_tab, text=" Custom ")

        # Grid tab -------------------------------------------------------
        self._grid_w_var  = tk.IntVar(value=64)
        self._grid_h_var  = tk.IntVar(value=64)
        self._grid_ox_var = tk.IntVar(value=0)
        self._grid_oy_var = tk.IntVar(value=0)
        self._grid_mx_var = tk.IntVar(value=0)
        self._grid_my_var = tk.IntVar(value=0)

        for label, var, lo in [
            ("Cell Width (px):",  self._grid_w_var,  1),
            ("Cell Height (px):", self._grid_h_var,  1),
            ("Offset X (px):",    self._grid_ox_var, 0),
            ("Offset Y (px):",    self._grid_oy_var, 0),
            ("Margin X (px):",    self._grid_mx_var, 0),
            ("Margin Y (px):",    self._grid_my_var, 0),
        ]:
            ttk.Label(grid_tab, text=label).pack(anchor=tk.W)
            ttk.Spinbox(grid_tab, from_=lo, to=9999,
                        textvariable=var, width=9).pack(anchor=tk.W)

        ttk.Button(grid_tab, text="Apply / Refresh Grid",
                   command=self._cmd_apply_grid).pack(fill=tk.X, pady=(6, 1))
        ttk.Button(grid_tab, text="Select All Cells",
                   command=self._cmd_select_all).pack(fill=tk.X, pady=1)
        ttk.Button(grid_tab, text="Clear Selection",
                   command=self._cmd_clear).pack(fill=tk.X, pady=1)

        # Debounced live overlay when grid vars change
        for var in [self._grid_w_var, self._grid_h_var,
                    self._grid_ox_var, self._grid_oy_var,
                    self._grid_mx_var, self._grid_my_var]:
            var.trace_add("write", lambda *_: self._schedule_overlay_redraw())

        # Custom tab -----------------------------------------------------
        self._sel_w_var = tk.IntVar(value=64)
        self._sel_h_var = tk.IntVar(value=64)

        ttk.Label(custom_tab, text="Selection Width (px):").pack(anchor=tk.W)
        ttk.Spinbox(custom_tab, from_=1, to=9999,
                    textvariable=self._sel_w_var, width=9).pack(anchor=tk.W)
        ttk.Label(custom_tab, text="Selection Height (px):").pack(anchor=tk.W)
        ttk.Spinbox(custom_tab, from_=1, to=9999,
                    textvariable=self._sel_h_var, width=9).pack(anchor=tk.W)

        ttk.Label(
            custom_tab,
            text=(
                "Click → place fixed size\n"
                "Drag → freehand rectangle\n"
                "Click area → select it\n"
                "Arrow keys move selected\n"
                "  Shift ×10  |  Ctrl ×5\n"
                "  Ctrl+Shift ×50\n"
                "Hold key → auto-repeat"
            ),
            foreground="gray", font=("TkDefaultFont", 7),
            wraplength=205, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(5, 3))

        ttk.Button(custom_tab, text="Duplicate Selected  (Ctrl+D)",
                   command=self._cmd_duplicate).pack(fill=tk.X, pady=(4, 1))
        ttk.Button(custom_tab, text="Clear Selection",
                   command=self._cmd_clear).pack(fill=tk.X, pady=1)

        # Live ghost when custom size changes
        self._sel_w_var.trace_add("write", lambda *_: self._draw_ghost())
        self._sel_h_var.trace_add("write", lambda *_: self._draw_ghost())

        # -- Naming template ----------------------------------------------
        sec = ttk.LabelFrame(inner, text="Naming Template", padding=5)
        sec.pack(**pad)
        ttk.Label(sec, text="Template:").pack(anchor=tk.W)
        self._naming_var = tk.StringVar(value="frame_%num%")
        ttk.Entry(sec, textvariable=self._naming_var).pack(fill=tk.X)
        ttk.Label(
            sec,
            text="%num% = order   %col% = column   %row% = row",
            foreground="gray", font=("TkDefaultFont", 7), wraplength=210,
        ).pack(anchor=tk.W, pady=(2, 0))
        ttk.Button(sec, text="Refresh Preview Names",
                   command=self._refresh_preview).pack(fill=tk.X, pady=(4, 0))

        # -- Export -------------------------------------------------------
        sec = ttk.LabelFrame(inner, text="Export", padding=5)
        sec.pack(**pad)
        self._export_fmt_var = tk.StringVar(value="PNG")
        ttk.Label(sec, text="Format:").pack(anchor=tk.W)
        ttk.Combobox(sec, textvariable=self._export_fmt_var, state="readonly",
                     values=["PNG", "JPEG", "BMP", "TIFF"], width=8).pack(anchor=tk.W)
        ttk.Button(sec, text="Export Crops…",
                   command=self._cmd_export).pack(fill=tk.X, pady=(4, 0))

        # -- Zoom ---------------------------------------------------------
        sec = ttk.LabelFrame(inner, text="Canvas Zoom", padding=5)
        sec.pack(**pad)
        zrow = ttk.Frame(sec)
        zrow.pack()
        ttk.Button(zrow, text="−", width=3, command=self._zoom_out).pack(side=tk.LEFT)
        self._zoom_label_var = tk.StringVar(value="100 %")
        ttk.Label(zrow, textvariable=self._zoom_label_var,
                  width=7, anchor=tk.CENTER).pack(side=tk.LEFT)
        ttk.Button(zrow, text="+", width=3, command=self._zoom_in).pack(side=tk.LEFT)
        ttk.Button(sec, text="Fit to Window",
                   command=self._zoom_fit).pack(fill=tk.X, pady=(3, 0))

    # ---- Center canvas panel ------------------------------------------

    def _build_canvas_panel(self) -> None:
        frame = ttk.Frame(self._pw)
        self._pw.add(frame, weight=3)

        ttk.Label(frame, text="Image Canvas",
                  font=("TkDefaultFont", 9, "bold")).pack(anchor=tk.W, padx=5, pady=2)

        cont = ttk.Frame(frame)
        cont.pack(fill=tk.BOTH, expand=True)

        hbar = ttk.Scrollbar(cont, orient=tk.HORIZONTAL)
        hbar.pack(side=tk.BOTTOM, fill=tk.X)
        vbar = ttk.Scrollbar(cont, orient=tk.VERTICAL)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._canvas = tk.Canvas(
            cont, bg="#2a2a2a", cursor="crosshair",
            xscrollcommand=hbar.set, yscrollcommand=vbar.set,
            takefocus=True,    # needed for KeyPress events
        )
        self._canvas.pack(fill=tk.BOTH, expand=True)
        hbar.config(command=self._canvas.xview)
        vbar.config(command=self._canvas.yview)

        self._canvas.bind("<Button-1>",        self._canvas_click)
        self._canvas.bind("<B1-Motion>",        self._canvas_drag)
        self._canvas.bind("<ButtonRelease-1>",  self._canvas_release)
        self._canvas.bind("<Button-3>",         self._canvas_right_click)
        self._canvas.bind("<Motion>",           self._canvas_motion)
        self._canvas.bind("<Leave>",            self._canvas_leave)
        self._canvas.bind("<Configure>",        self._canvas_configure)
        self._canvas.bind("<MouseWheel>",
                          lambda e: (self._zoom_in() if e.delta > 0 else self._zoom_out()))
        self._canvas.bind("<Button-4>", lambda _e: self._zoom_in())
        self._canvas.bind("<Button-5>", lambda _e: self._zoom_out())
        # Arrow key movement (canvas receives these once it has focus)
        self._canvas.bind("<KeyPress>",   self._on_key_press)
        self._canvas.bind("<KeyRelease>", self._on_key_release)

    # ---- Right preview panel ------------------------------------------

    def _build_preview_panel(self) -> None:
        frame = ttk.Frame(self._pw, width=255)
        frame.pack_propagate(False)
        self._pw.add(frame, weight=1)

        hdr = ttk.Frame(frame)
        hdr.pack(fill=tk.X, padx=5, pady=2)
        ttk.Label(hdr, text="Preview",
                  font=("TkDefaultFont", 9, "bold")).pack(side=tk.LEFT)
        self._preview_count_var = tk.StringVar(value="(0)")
        ttk.Label(hdr, textvariable=self._preview_count_var,
                  foreground="gray").pack(side=tk.LEFT, padx=4)

        cont = ttk.Frame(frame)
        cont.pack(fill=tk.BOTH, expand=True, padx=3, pady=2)
        vbar = ttk.Scrollbar(cont, orient=tk.VERTICAL)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._prev_canvas = tk.Canvas(cont, bg="#1e1e1e", yscrollcommand=vbar.set)
        self._prev_canvas.pack(fill=tk.BOTH, expand=True)
        vbar.config(command=self._prev_canvas.yview)

        self._prev_inner = ttk.Frame(self._prev_canvas)
        self._prev_win_id = self._prev_canvas.create_window(
            (0, 0), window=self._prev_inner, anchor=tk.NW)

        self._prev_inner.bind("<Configure>", lambda _e: self._prev_canvas.configure(
            scrollregion=self._prev_canvas.bbox("all")))
        self._prev_canvas.bind("<Configure>", lambda e: self._prev_canvas.itemconfig(
            self._prev_win_id, width=e.width))

        for widget in (self._prev_canvas, self._prev_inner):
            widget.bind("<MouseWheel>",
                        lambda e: self._prev_canvas.yview_scroll(
                            -1 * (e.delta // 120), "units"))
            widget.bind("<Button-4>",
                        lambda _e: self._prev_canvas.yview_scroll(-1, "units"))
            widget.bind("<Button-5>",
                        lambda _e: self._prev_canvas.yview_scroll( 1, "units"))

    # ===================================================================
    # Commands (menu / button callbacks)
    # ===================================================================

    def _cmd_open_image(self) -> None:
        path = filedialog.askopenfilename(
            title="Open Image",
            filetypes=[("Images",
                        "*.png *.jpg *.jpeg *.bmp *.gif *.tiff *.tif *.webp"),
                       ("All files", "*.*")],
        )
        if not path:
            return
        try:
            img = Image.open(path)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            self.image = img
            self.project.image_path = path
            self._img_name_var.set(os.path.basename(path))
            self.project.crop_areas.clear()
            self._grid_cells.clear()
            self._selected_idx = None
            self._zoom_fit()
            self._refresh_preview()
            self._set_status(
                f"Opened: {os.path.basename(path)}  {img.width}×{img.height} px")
        except Exception as exc:
            messagebox.showerror("Open Image", f"Cannot open image:\n{exc}")

    def _cmd_save(self) -> None:
        if self.project_path is None:
            self._cmd_save_as()
        else:
            self._write_project(self.project_path)

    def _cmd_save_as(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save Project As", defaultextension=".json",
            filetypes=[("JSON project", "*.json"), ("All files", "*.*")])
        if path:
            self.project_path = Path(path)
            self._write_project(self.project_path)

    def _cmd_open_project(self) -> None:
        path = filedialog.askopenfilename(
            title="Open Project",
            filetypes=[("JSON project", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.project      = ProjectData.from_dict(data)
            self.project_path = Path(path)

            # Sync widgets
            tab_idx = 0 if self.project.crop_type == "grid" else 1
            self._settings_nb.select(tab_idx)
            self._grid_w_var.set(self.project.grid_size.width)
            self._grid_h_var.set(self.project.grid_size.height)
            self._grid_ox_var.set(self.project.grid_size.offset_x)
            self._grid_oy_var.set(self.project.grid_size.offset_y)
            self._grid_mx_var.set(self.project.grid_size.margin_x)
            self._grid_my_var.set(self.project.grid_size.margin_y)
            self._naming_var.set(self.project.naming_template)

            if self.project.image_path:
                if os.path.exists(self.project.image_path):
                    img = Image.open(self.project.image_path)
                    if img.mode not in ("RGB", "RGBA"):
                        img = img.convert("RGBA")
                    self.image = img
                    self._img_name_var.set(os.path.basename(self.project.image_path))
                else:
                    messagebox.showwarning(
                        "Image Not Found",
                        f"Could not locate:\n{self.project.image_path}\n\n"
                        "Open the image manually when ready.")

            self._rebuild_grid_cells()
            self._selected_idx = None
            self._zoom_fit()
            self._refresh_preview()
            self.root.title(f"Crop Utils – {Path(path).name}")
            self._set_status(f"Project loaded: {path}")
        except Exception as exc:
            messagebox.showerror("Open Project", f"Cannot open project:\n{exc}")

    def _cmd_apply_grid(self) -> None:
        self._collect_grid_vars()
        self.project.crop_areas.clear()
        self._grid_cells.clear()
        self._selected_idx = None
        self._redraw()
        self._refresh_preview()

    def _cmd_select_all(self) -> None:
        if self.image is None:
            return
        self._collect_grid_vars()
        gs       = self.project.grid_size
        stride_x = gs.width  + max(0, gs.margin_x)
        stride_y = gs.height + max(0, gs.margin_y)
        areas: list[CropArea] = []
        row = 0
        while True:
            y = gs.offset_y + row * stride_y
            if y + gs.height > self.image.height:
                break
            col = 0
            while True:
                x = gs.offset_x + col * stride_x
                if x + gs.width > self.image.width:
                    break
                areas.append(CropArea(x, y, gs.width, gs.height))
                self._grid_cells.add((col, row))
                col += 1
            row += 1
        self.project.crop_areas = areas
        self._redraw()
        self._refresh_preview()

    def _cmd_clear(self) -> None:
        self.project.crop_areas.clear()
        self._grid_cells.clear()
        self._selected_idx = None
        self._redraw_overlays()
        self._refresh_preview()

    def _cmd_duplicate(self) -> None:
        if self._current_mode() != "custom":
            return
        if self._selected_idx is None:
            return
        areas = self.project.crop_areas
        if not (0 <= self._selected_idx < len(areas)):
            return
        a   = areas[self._selected_idx]
        nx  = a.x + _DUPLICATE_OFFSET_PX
        ny  = a.y + _DUPLICATE_OFFSET_PX
        if self.image is not None:
            nx = min(nx, self.image.width  - a.width)
            ny = min(ny, self.image.height - a.height)
        new_area   = CropArea(max(0, nx), max(0, ny), a.width, a.height)
        insert_at  = self._selected_idx + 1
        areas.insert(insert_at, new_area)
        self._selected_idx = insert_at
        self._redraw_overlays()
        self._refresh_preview()

    def _cmd_export(self) -> None:
        if self.image is None:
            messagebox.showwarning("Export", "Open an image first.")
            return
        if not self.project.crop_areas:
            messagebox.showwarning("Export", "No areas selected for export.")
            return
        out_dir = filedialog.askdirectory(title="Select Export Directory")
        if not out_dir:
            return
        fmt      = self._export_fmt_var.get()
        ext_map  = {"PNG": ".png", "JPEG": ".jpg", "BMP": ".bmp", "TIFF": ".tif"}
        ext      = ext_map.get(fmt, ".png")
        template = self._naming_var.get()
        ok = err = 0
        for i, area in enumerate(self.project.crop_areas):
            name  = self._fmt_name(template, i + 1, area)
            fpath = os.path.join(out_dir, name + ext)
            try:
                crop = self.image.crop(
                    (area.x, area.y, area.x + area.width, area.y + area.height))
                if fmt == "JPEG" and crop.mode == "RGBA":
                    crop = crop.convert("RGB")
                crop.save(fpath, fmt)
                ok += 1
            except Exception:
                err += 1
        msg = f"Exported {ok} image(s) to:\n{out_dir}"
        if err:
            msg += f"\n\n⚠ {err} error(s) occurred."
        messagebox.showinfo("Export Complete", msg)
        self._set_status(f"Exported {ok} crops → {out_dir}")

    # ===================================================================
    # Tab / mode change
    # ===================================================================

    def _on_tab_change(self, _event: Optional[tk.Event] = None) -> None:
        tab      = self._settings_nb.index(self._settings_nb.select())
        new_type = "grid" if tab == 0 else "custom"
        if new_type == self.project.crop_type:
            return
        self.project.crop_type = new_type
        self._cmd_clear()

    def _current_mode(self) -> str:
        return self.project.crop_type

    # ===================================================================
    # Canvas event handlers
    # ===================================================================

    def _canvas_configure(self, _event: tk.Event) -> None:
        if self.image is not None:
            self._zoom_fit()

    def _canvas_motion(self, event: tk.Event) -> None:
        if self.image is None:
            return
        cx, cy    = self._canvas.canvasx(event.x), self._canvas.canvasy(event.y)
        ix, iy    = self._c2i(cx, cy)
        self._hover_img = (ix, iy)
        if self._current_mode() == "grid":
            self._draw_hover_cell()
        else:
            self._draw_ghost()

    def _canvas_leave(self, _event: tk.Event) -> None:
        self._hover_img = None
        self._canvas.delete("hover")
        self._canvas.delete("ghost")

    def _canvas_click(self, event: tk.Event) -> None:
        self._canvas.focus_set()
        if self.image is None:
            return
        cx, cy = self._canvas.canvasx(event.x), self._canvas.canvasy(event.y)
        ix, iy = self._c2i(cx, cy)

        if self._current_mode() == "grid":
            self._grid_toggle_cell(ix, iy)
        else:
            hit = self._hit_test(ix, iy)
            if hit is not None:
                self._selected_idx = hit
                self._redraw_overlays()
                self._refresh_preview()
            else:
                self._drawing    = True
                self._draw_start = (ix, iy)

    def _canvas_drag(self, event: tk.Event) -> None:
        if not self._drawing or self._draw_start is None:
            return
        cx, cy = self._canvas.canvasx(event.x), self._canvas.canvasy(event.y)
        ix, iy = self._c2i(cx, cy)
        self._canvas.delete("temp_rect")
        x0c, y0c = self._i2c(*self._draw_start)
        x1c, y1c = self._i2c(ix, iy)
        self._canvas.create_rectangle(
            x0c, y0c, x1c, y1c,
            outline="yellow", width=2, dash=(5, 3), tags="temp_rect")

    def _canvas_release(self, event: tk.Event) -> None:
        if not self._drawing or self._draw_start is None:
            return
        self._drawing = False
        cx, cy = self._canvas.canvasx(event.x), self._canvas.canvasy(event.y)
        ix, iy = self._c2i(cx, cy)
        self._canvas.delete("temp_rect")

        x0 = int(min(self._draw_start[0], ix))
        y0 = int(min(self._draw_start[1], iy))
        x1 = int(max(self._draw_start[0], ix))
        y1 = int(max(self._draw_start[1], iy))
        self._draw_start = None

        w, h = x1 - x0, y1 - y0
        # Tiny drag → place fixed-size rect at click position
        if w < 4 and h < 4:
            try:
                w = max(1, self._sel_w_var.get())
                h = max(1, self._sel_h_var.get())
            except tk.TclError:
                w = h = 64

        if self.image is not None:
            x0 = max(0, min(x0, self.image.width  - 1))
            y0 = max(0, min(y0, self.image.height - 1))
            w  = min(w, self.image.width  - x0)
            h  = min(h, self.image.height - y0)

        if w < 1 or h < 1:
            return

        self.project.crop_areas.append(CropArea(x0, y0, w, h))
        self._selected_idx = len(self.project.crop_areas) - 1
        self._redraw_overlays()
        self._refresh_preview()

    def _canvas_right_click(self, event: tk.Event) -> None:
        if self._current_mode() != "custom" or self.image is None:
            return
        cx, cy = self._canvas.canvasx(event.x), self._canvas.canvasy(event.y)
        ix, iy = self._c2i(cx, cy)
        hit = self._hit_test(ix, iy)
        if hit is not None:
            self.project.crop_areas.pop(hit)
            if self._selected_idx is not None:
                if self._selected_idx == hit:
                    self._selected_idx = None
                elif self._selected_idx > hit:
                    self._selected_idx -= 1
            self._redraw_overlays()
            self._refresh_preview()

    # ---- Grid cell toggle --------------------------------------------

    def _grid_toggle_cell(self, ix: float, iy: float) -> None:
        if self.image is None:
            return
        gs = self._live_grid_size()
        if gs.width <= 0 or gs.height <= 0:
            return
        stride_x = gs.width  + gs.margin_x
        stride_y = gs.height + gs.margin_y
        rel_x    = ix - gs.offset_x
        rel_y    = iy - gs.offset_y
        if rel_x < 0 or rel_y < 0:
            return
        col  = int(rel_x // stride_x)
        row  = int(rel_y // stride_y)
        cx   = gs.offset_x + col * stride_x
        cy   = gs.offset_y + row * stride_y
        # Reject click inside margin gap
        if ix > cx + gs.width or iy > cy + gs.height:
            return
        # Reject if cell goes outside image
        if cx + gs.width > self.image.width or cy + gs.height > self.image.height:
            return
        key = (col, row)
        if key in self._grid_cells:
            self._grid_cells.discard(key)
            self.project.crop_areas = [
                a for a in self.project.crop_areas
                if not (a.x == cx and a.y == cy)
            ]
        else:
            self._grid_cells.add(key)
            self.project.crop_areas.append(CropArea(cx, cy, gs.width, gs.height))
        self._redraw_overlays()
        self._refresh_preview()

    def _hit_test(self, ix: float, iy: float) -> Optional[int]:
        """Return topmost custom-area index at image coordinate, or None."""
        for i in range(len(self.project.crop_areas) - 1, -1, -1):
            a = self.project.crop_areas[i]
            if a.x <= ix <= a.x + a.width and a.y <= iy <= a.y + a.height:
                return i
        return None

    # ===================================================================
    # Arrow-key movement
    # ===================================================================

    def _on_key_press(self, event: tk.Event) -> None:
        key = event.keysym
        if key not in _ARROW_KEYS:
            return
        if self._current_mode() != "custom" or self._selected_idx is None:
            return
        # step multipliers: Shift×10, Ctrl×5 (together → ×50)
        step = 1
        if event.state & _SHIFT_MASK:
            step *= 10
        if event.state & _CTRL_MASK:
            step *= 5
        if key not in self._keys_held:
            self._keys_held[key] = step
            self._do_arrow_move(key, step)
            rid = self.root.after(
                _KEY_INITIAL_DELAY_MS, lambda k=key: self._key_repeat(k))
            self._key_repeat_ids[key] = rid

    def _on_key_release(self, event: tk.Event) -> None:
        key = event.keysym
        self._keys_held.pop(key, None)
        rid = self._key_repeat_ids.pop(key, None)
        if rid is not None:
            self.root.after_cancel(rid)

    def _key_repeat(self, key: str) -> None:
        if key not in self._keys_held:
            return
        self._do_arrow_move(key, self._keys_held[key])
        rid = self.root.after(_KEY_REPEAT_MS, lambda: self._key_repeat(key))
        self._key_repeat_ids[key] = rid

    def _do_arrow_move(self, key: str, step: int) -> None:
        if self._selected_idx is None:
            return
        areas = self.project.crop_areas
        if not (0 <= self._selected_idx < len(areas)):
            return
        a = areas[self._selected_idx]
        ddx, ddy = _ARROW_DELTAS[key]
        nx, ny = a.x + ddx * step, a.y + ddy * step
        if self.image is not None:
            nx = max(0, min(nx, self.image.width  - a.width))
            ny = max(0, min(ny, self.image.height - a.height))
        areas[self._selected_idx] = CropArea(nx, ny, a.width, a.height)
        self._redraw_overlays()
        self._refresh_preview()

    # ===================================================================
    # Zoom
    # ===================================================================

    def _zoom_in(self) -> None:
        self._scale = min(self._scale * 1.25, 8.0)
        self._update_zoom_label()
        self._redraw()

    def _zoom_out(self) -> None:
        self._scale = max(self._scale / 1.25, 0.05)
        self._update_zoom_label()
        self._redraw()

    def _zoom_fit(self) -> None:
        if self.image is None:
            return
        cw = self._canvas.winfo_width()  or 640
        ch = self._canvas.winfo_height() or 480
        sx = cw / self.image.width
        sy = ch / self.image.height
        self._scale = min(sx, sy) * 0.95
        ox = max(10.0, (cw - self.image.width  * self._scale) / 2)
        oy = max(10.0, (ch - self.image.height * self._scale) / 2)
        self._offset = (ox, oy)
        self._update_zoom_label()
        self._redraw()

    def _update_zoom_label(self) -> None:
        self._zoom_label_var.set(f"{int(self._scale * 100)} %")

    # ===================================================================
    # Coordinate helpers
    # ===================================================================

    def _c2i(self, cx: float, cy: float) -> tuple[float, float]:
        """Canvas → image coordinates."""
        ox, oy = self._offset
        return (cx - ox) / self._scale, (cy - oy) / self._scale

    def _i2c(self, ix: float, iy: float) -> tuple[float, float]:
        """Image → canvas coordinates."""
        ox, oy = self._offset
        return ix * self._scale + ox, iy * self._scale + oy

    # ===================================================================
    # Canvas drawing – full redraw (threaded image resize)
    # ===================================================================

    def _redraw(self) -> None:
        """Resize image in a background thread; deliver result via root.after."""
        if self._render_cancel is not None:
            self._render_cancel.set()

        if self.image is None:
            self._canvas.delete("all")
            self._canvas.create_text(
                (self._canvas.winfo_width()  or 400) // 2,
                (self._canvas.winfo_height() or 300) // 2,
                text="Open an image to begin",
                fill="#888888", font=("TkDefaultFont", 14),
            )
            return

        cancel = threading.Event()
        self._render_cancel = cancel
        image  = self.image    # capture for thread
        scale  = self._scale
        offset = self._offset

        def worker() -> None:
            dw = max(1, int(image.width  * scale))
            dh = max(1, int(image.height * scale))
            if cancel.is_set():
                return
            try:
                resized = image.resize((dw, dh), Image.LANCZOS)
            except Exception:
                return
            if cancel.is_set():
                return
            self.root.after(0, lambda: self._finish_redraw(resized, offset, cancel))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_redraw(self, resized: Image.Image,
                       offset: tuple[float, float],
                       cancel: threading.Event) -> None:
        if cancel.is_set():
            return
        photo       = ImageTk.PhotoImage(resized)
        self._photo = photo   # prevent GC
        ox, oy      = offset
        self._canvas.delete("img")
        self._canvas.create_image(ox, oy, image=photo, anchor=tk.NW, tags="img")
        self._canvas.tag_lower("img")
        dw, dh = resized.width, resized.height
        self._canvas.configure(scrollregion=(0, 0, dw + ox * 2, dh + oy * 2))
        self._redraw_overlays()

    # ===================================================================
    # Canvas drawing – overlays only (fast, no image resize)
    # ===================================================================

    def _redraw_overlays(self) -> None:
        """Redraw selection overlays without touching the image layer."""
        self._canvas.delete("overlay")
        self._canvas.delete("hover")
        self._canvas.delete("ghost")
        self._canvas.delete("temp_rect")
        if self._current_mode() == "grid":
            self._draw_grid_overlay()
        else:
            self._draw_custom_overlay()
        self._draw_ghost()

    def _schedule_overlay_redraw(self) -> None:
        """Debounced overlay refresh triggered by live spinbox changes."""
        if self._overlay_debounce_id is not None:
            self.root.after_cancel(self._overlay_debounce_id)
        self._overlay_debounce_id = self.root.after(
            _OVERLAY_DEBOUNCE_MS, self._redraw_overlays)

    # ---- Grid overlay ------------------------------------------------

    def _live_grid_size(self) -> GridSize:
        """Read current spinbox values as a GridSize (for live preview)."""
        try:
            return GridSize(
                width=max(1,    self._grid_w_var.get()),
                height=max(1,   self._grid_h_var.get()),
                offset_x=max(0, self._grid_ox_var.get()),
                offset_y=max(0, self._grid_oy_var.get()),
                margin_x=max(0, self._grid_mx_var.get()),
                margin_y=max(0, self._grid_my_var.get()),
            )
        except tk.TclError:
            return GridSize()

    def _draw_grid_overlay(self) -> None:
        if self.image is None:
            return
        gs       = self._live_grid_size()
        stride_x = gs.width  + gs.margin_x
        stride_y = gs.height + gs.margin_y
        iw, ih   = self.image.width, self.image.height

        # Build order lookup from saved crop_areas
        cell_order: dict[tuple[int, int], int] = {}
        for idx, area in enumerate(self.project.crop_areas):
            col = (area.x - gs.offset_x) // stride_x if stride_x else 0
            row = (area.y - gs.offset_y) // stride_y if stride_y else 0
            cell_order[(col, row)] = idx + 1

        row = 0
        while True:
            y = gs.offset_y + row * stride_y
            if y >= ih:
                break
            col = 0
            while True:
                x = gs.offset_x + col * stride_x
                if x >= iw:
                    break
                x1 = min(x + gs.width,  iw)
                y1 = min(y + gs.height, ih)
                x0c, y0c = self._i2c(x,  y)
                x1c, y1c = self._i2c(x1, y1)
                key = (col, row)
                if key in cell_order:
                    self._canvas.create_rectangle(
                        x0c, y0c, x1c, y1c,
                        fill="#4488ff", outline="#2266dd",
                        stipple="gray25", tags="overlay")
                    mx, my = (x0c + x1c) / 2, (y0c + y1c) / 2
                    self._canvas.create_text(
                        mx, my, text=str(cell_order[key]),
                        fill="white", font=("TkDefaultFont", 8, "bold"),
                        tags="overlay")
                else:
                    self._canvas.create_rectangle(
                        x0c, y0c, x1c, y1c,
                        fill="", outline="#505050", width=1, tags="overlay")
                col += 1
            row += 1

    # ---- Custom overlay ----------------------------------------------

    def _draw_custom_overlay(self) -> None:
        for i, area in enumerate(self.project.crop_areas):
            selected  = (i == self._selected_idx)
            x0c, y0c  = self._i2c(area.x, area.y)
            x1c, y1c  = self._i2c(area.x + area.width, area.y + area.height)
            if selected:
                self._canvas.create_rectangle(
                    x0c, y0c, x1c, y1c,
                    fill="", outline="#ffdd00", width=3, tags="overlay")
            else:
                self._canvas.create_rectangle(
                    x0c, y0c, x1c, y1c,
                    fill="#ff4400", outline="#ff6622",
                    width=2, stipple="gray25", tags="overlay")
            mx, my = (x0c + x1c) / 2, (y0c + y1c) / 2
            self._canvas.create_text(
                mx, my, text=str(i + 1), fill="white",
                font=("TkDefaultFont", 9, "bold"), tags="overlay")

        if (self._selected_idx is not None
                and 0 <= self._selected_idx < len(self.project.crop_areas)):
            a = self.project.crop_areas[self._selected_idx]
            self._set_status(
                f"Selected #{self._selected_idx + 1}: "
                f"{a.width}×{a.height}  pos ({a.x},{a.y})  "
                "← Arrow keys to move  |  Ctrl+D duplicate")

    # ---- Ghost / hover helpers ---------------------------------------

    def _draw_ghost(self) -> None:
        """Dashed preview of where the next custom selection will be placed."""
        self._canvas.delete("ghost")
        if (self.image is None
                or self._current_mode() != "custom"
                or self._hover_img is None
                or self._drawing):
            return
        ix, iy = self._hover_img
        try:
            w = max(1, self._sel_w_var.get())
            h = max(1, self._sel_h_var.get())
        except tk.TclError:
            return
        x0c, y0c = self._i2c(ix, iy)
        x1c, y1c = self._i2c(ix + w, iy + h)
        self._canvas.create_rectangle(
            x0c, y0c, x1c, y1c,
            outline="yellow", width=1, dash=(4, 4), tags="ghost")

    def _draw_hover_cell(self) -> None:
        """Highlight the grid cell currently under the cursor."""
        self._canvas.delete("hover")
        if self.image is None or self._hover_img is None:
            return
        gs       = self._live_grid_size()
        ix, iy   = self._hover_img
        stride_x = gs.width + gs.margin_x
        stride_y = gs.height + gs.margin_y
        rel_x    = ix - gs.offset_x
        rel_y    = iy - gs.offset_y
        if rel_x < 0 or rel_y < 0:
            return
        col = int(rel_x // stride_x)
        row = int(rel_y // stride_y)
        cx  = gs.offset_x + col * stride_x
        cy  = gs.offset_y + row * stride_y
        if ix > cx + gs.width or iy > cy + gs.height:
            return
        if cx + gs.width > self.image.width or cy + gs.height > self.image.height:
            return
        x0c, y0c = self._i2c(cx, cy)
        x1c, y1c = self._i2c(cx + gs.width, cy + gs.height)
        self._canvas.create_rectangle(
            x0c, y0c, x1c, y1c,
            outline="white", width=1, dash=(3, 3), tags="hover")

    # ===================================================================
    # Preview panel
    # ===================================================================

    def _refresh_preview(self) -> None:
        for w in self._prev_inner.winfo_children():
            w.destroy()
        self._thumb_refs.clear()
        areas = self.project.crop_areas
        self._preview_count_var.set(f"({len(areas)})")
        if not areas or self.image is None:
            return
        template = self._naming_var.get()
        for i, area in enumerate(areas):
            self._add_preview_row(i, area, template)
        self._prev_inner.update_idletasks()
        self._prev_canvas.configure(scrollregion=self._prev_canvas.bbox("all"))

    def _add_preview_row(self, idx: int, area: CropArea, template: str) -> None:
        THUMB    = 72
        selected = (idx == self._selected_idx
                    and self._current_mode() == "custom")
        row = ttk.Frame(
            self._prev_inner,
            relief=tk.SOLID  if selected else tk.RIDGE,
            borderwidth=2    if selected else 1,
        )
        row.pack(fill=tk.X, padx=2, pady=1)

        ph: Optional[ImageTk.PhotoImage] = None
        if self.image is not None:
            try:
                crop = self.image.crop(
                    (area.x, area.y, area.x + area.width, area.y + area.height))
                crop.thumbnail((THUMB, THUMB), Image.LANCZOS)
                ph = ImageTk.PhotoImage(crop)
            except Exception:
                ph = None
        self._thumb_refs.append(ph)

        if ph:
            ttk.Label(row, image=ph).pack(side=tk.LEFT, padx=2, pady=2)
        else:
            ttk.Label(row, text="⚠", width=5).pack(side=tk.LEFT, padx=2)

        info = ttk.Frame(row)
        info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)
        name = self._fmt_name(template, idx + 1, area)
        ttk.Label(info, text=name, font=("TkDefaultFont", 8, "bold"),
                  wraplength=105, anchor=tk.W).pack(fill=tk.X)
        ttk.Label(info,
                  text=f"{area.width}×{area.height}  ({area.x},{area.y})",
                  font=("TkDefaultFont", 7), foreground="#888888",
                  anchor=tk.W).pack(fill=tk.X)

        btns = ttk.Frame(row)
        btns.pack(side=tk.RIGHT, padx=2)
        ttk.Button(btns, text="↑", width=2,
                   command=lambda i=idx: self._move_area(i, -1)).pack(pady=1)
        ttk.Button(btns, text="↓", width=2,
                   command=lambda i=idx: self._move_area(i, +1)).pack(pady=1)
        ttk.Button(btns, text="✕", width=2,
                   command=lambda i=idx: self._delete_area(i)).pack(pady=1)

    def _move_area(self, idx: int, delta: int) -> None:
        areas   = self.project.crop_areas
        new_idx = idx + delta
        if 0 <= new_idx < len(areas):
            areas[idx], areas[new_idx] = areas[new_idx], areas[idx]
            if self._selected_idx == idx:
                self._selected_idx = new_idx
            elif self._selected_idx == new_idx:
                self._selected_idx = idx
            self._refresh_preview()
            self._redraw_overlays()

    def _delete_area(self, idx: int) -> None:
        areas = self.project.crop_areas
        if 0 <= idx < len(areas):
            removed = areas.pop(idx)
            if self._current_mode() == "grid":
                gs = self._live_grid_size()
                sx = max(1, gs.width  + gs.margin_x)
                sy = max(1, gs.height + gs.margin_y)
                self._grid_cells.discard(
                    ((removed.x - gs.offset_x) // sx,
                     (removed.y - gs.offset_y) // sy))
            if self._selected_idx is not None:
                if self._selected_idx == idx:
                    self._selected_idx = None
                elif self._selected_idx > idx:
                    self._selected_idx -= 1
            self._refresh_preview()
            self._redraw_overlays()

    # ===================================================================
    # Naming
    # ===================================================================

    def _fmt_name(self, template: str, num: int, area: CropArea) -> str:
        try:
            gs  = self._live_grid_size()
            sx  = max(1, gs.width  + gs.margin_x)
            sy  = max(1, gs.height + gs.margin_y)
            col = max(0, (area.x - gs.offset_x) // sx)
            row = max(0, (area.y - gs.offset_y) // sy)
        except Exception:
            col = row = 0
        return (template
                .replace("%num%", str(num))
                .replace("%col%", str(col))
                .replace("%row%", str(row)))

    # ===================================================================
    # Grid helper state
    # ===================================================================

    def _collect_grid_vars(self) -> None:
        """Sync spinbox values → project.grid_size."""
        gs = self.project.grid_size
        try:
            gs.width    = max(1, self._grid_w_var.get())
            gs.height   = max(1, self._grid_h_var.get())
            gs.offset_x = max(0, self._grid_ox_var.get())
            gs.offset_y = max(0, self._grid_oy_var.get())
            gs.margin_x = max(0, self._grid_mx_var.get())
            gs.margin_y = max(0, self._grid_my_var.get())
        except tk.TclError:
            pass

    def _rebuild_grid_cells(self) -> None:
        """Derive _grid_cells from project.crop_areas after project load."""
        gs = self.project.grid_size
        sx = max(1, gs.width  + gs.margin_x)
        sy = max(1, gs.height + gs.margin_y)
        self._grid_cells = {
            ((a.x - gs.offset_x) // sx, (a.y - gs.offset_y) // sy)
            for a in self.project.crop_areas
        }

    # ===================================================================
    # Project save / load
    # ===================================================================

    def _collect_project(self) -> None:
        self.project.crop_type       = self._current_mode()
        self.project.naming_template = self._naming_var.get()
        self._collect_grid_vars()

    def _write_project(self, path: Path) -> None:
        try:
            self._collect_project()
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.project.to_dict(), fh, indent=2, ensure_ascii=False)
            self.root.title(f"Crop Utils – {path.name}")
            self._set_status(f"Saved: {path}")
        except Exception as exc:
            messagebox.showerror("Save Project", f"Cannot save project:\n{exc}")

    # ===================================================================
    # Autosave
    # ===================================================================

    def _schedule_autosave(self) -> None:
        self.root.after(_AUTOSAVE_MS, self._autosave_tick)

    def _autosave_tick(self) -> None:
        self._do_autosave()
        self.root.after(_AUTOSAVE_MS, self._autosave_tick)

    def _do_autosave(self) -> None:
        if not self.project.image_path and not self.project.crop_areas:
            return
        try:
            self._collect_project()
            if self.project_path:
                backup = self.project_path.parent / (
                    self.project_path.stem + ".backup.json")
            elif self.project.image_path:
                img    = Path(self.project.image_path)
                backup = img.parent / (img.stem + ".backup.json")
            else:
                backup = Path.home() / ".crop_utils_autosave.json"
            with open(backup, "w", encoding="utf-8") as fh:
                json.dump(self.project.to_dict(), fh, indent=2, ensure_ascii=False)
            self._set_status(
                f"Autosaved → {backup.name}  "
                f"({len(self.project.crop_areas)} areas)")
        except Exception:
            pass  # autosave must never crash the app

    # ===================================================================
    # Misc
    # ===================================================================

    def _on_close(self) -> None:
        self.root.destroy()

    def _set_status(self, text: str) -> None:
        self._statusbar_var.set(text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.0)
    except Exception:
        pass
    CropApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
