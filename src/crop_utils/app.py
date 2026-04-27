"""
crop_utils.app
==============
Tkinter GUI for cropping images either by a fixed pixel grid or by
freehand rectangular selections.

Features
--------
* Grid mode  – overlay a configurable pixel grid; click cells to add/remove
  them from the export list.
* Custom mode – draw rectangles with the mouse; right-click to remove.
* Preview panel – scrollable list of crop thumbnails with ↑ / ↓ reorder
  and ✕ delete buttons.
* Naming template – e.g. ``ptero_%num%``; ``%num%`` is replaced by the
  1-based position in the preview list; ``%col%`` / ``%row%`` give the
  grid column / row of the area.
* Save / Load project as JSON (image path, crop type, grid size, areas).
* Autosave – every 30 s a ``.backup.json`` file is written next to the
  project file (or image file, or ``~/.crop_utils_autosave.json``).
* Export – saves each cropped region as PNG / JPEG / BMP / TIFF.

Requires: Python ≥ 3.10, Pillow ≥ 10.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

_AUTOSAVE_MS = 30_000  # 30 s


@dataclass
class GridSize:
    width: int = 64
    height: int = 64


@dataclass
class CropArea:
    x: int
    y: int
    width: int
    height: int


@dataclass
class ProjectData:
    image_path: str = ""
    crop_type: str = "grid"  # "grid" | "custom"
    grid_size: GridSize = field(default_factory=GridSize)
    naming_template: str = "frame_%num%"
    crop_areas: list[CropArea] = field(default_factory=list)

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "image_path": self.image_path,
            "crop_type": self.crop_type,
            "grid_size": {
                "width": self.grid_size.width,
                "height": self.grid_size.height,
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
        proj.image_path = data.get("image_path", "")
        proj.crop_type = data.get("crop_type", "grid")
        gs = data.get("grid_size", {})
        proj.grid_size = GridSize(
            width=int(gs.get("width", 64)),
            height=int(gs.get("height", 64)),
        )
        proj.naming_template = data.get("naming_template", "frame_%num%")
        proj.crop_areas = [
            CropArea(
                x=int(a["x"]),
                y=int(a["y"]),
                width=int(a["width"]),
                height=int(a["height"]),
            )
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
        self.root.geometry("1280x820")
        self.root.minsize(900, 620)

        # ---- state ----
        self.project = ProjectData()
        self.project_path: Optional[Path] = None
        self.image: Optional[Image.Image] = None

        # canvas rendering
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._scale: float = 1.0
        self._offset: tuple[float, float] = (10.0, 10.0)

        # preview thumbnail refs (must stay alive to avoid GC)
        self._thumb_refs: list[ImageTk.PhotoImage] = []

        # grid selection tracking
        self._grid_cells: set[tuple[int, int]] = set()

        # custom-selection drawing
        self._drawing: bool = False
        self._draw_start: Optional[tuple[float, float]] = None
        self._temp_rid: Optional[int] = None

        # ---- build UI ----
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
        fm.add_command(
            label="Open Image…",
            command=self._cmd_open_image,
            accelerator="Ctrl+O",
        )
        fm.add_separator()
        fm.add_command(
            label="Save Project",
            command=self._cmd_save,
            accelerator="Ctrl+S",
        )
        fm.add_command(label="Save Project As…", command=self._cmd_save_as)
        fm.add_command(
            label="Open Project…",
            command=self._cmd_open_project,
            accelerator="Ctrl+Shift+O",
        )
        fm.add_separator()
        fm.add_command(label="Export Crops…", command=self._cmd_export)
        fm.add_separator()
        fm.add_command(label="Exit", command=self._on_close)

        self.root.bind_all("<Control-o>", lambda _e: self._cmd_open_image())
        self.root.bind_all("<Control-s>", lambda _e: self._cmd_save())
        self.root.bind_all("<Control-O>", lambda _e: self._cmd_open_project())

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
            self.root,
            textvariable=self._statusbar_var,
            relief=tk.SUNKEN,
            anchor=tk.W,
            padding=(5, 2),
        ).pack(side=tk.BOTTOM, fill=tk.X)

    # ---- Left controls panel ------------------------------------------

    def _build_left_panel(self) -> None:
        frame = ttk.Frame(self._pw, width=215)
        frame.pack_propagate(False)
        self._pw.add(frame, weight=0)

        # -- Image --
        sec = ttk.LabelFrame(frame, text="Image", padding=5)
        sec.pack(fill=tk.X, padx=5, pady=4)
        ttk.Button(sec, text="Open Image…", command=self._cmd_open_image).pack(
            fill=tk.X
        )
        self._img_name_var = tk.StringVar(value="—")
        ttk.Label(
            sec,
            textvariable=self._img_name_var,
            wraplength=195,
            foreground="gray",
            font=("TkDefaultFont", 8),
        ).pack(anchor=tk.W, pady=(2, 0))

        # -- Crop type --
        sec = ttk.LabelFrame(frame, text="Crop Type", padding=5)
        sec.pack(fill=tk.X, padx=5, pady=4)
        self._crop_type_var = tk.StringVar(value="grid")
        ttk.Radiobutton(
            sec,
            text="Grid",
            variable=self._crop_type_var,
            value="grid",
            command=self._on_type_change,
        ).pack(anchor=tk.W)
        ttk.Radiobutton(
            sec,
            text="Custom Selection",
            variable=self._crop_type_var,
            value="custom",
            command=self._on_type_change,
        ).pack(anchor=tk.W)

        # -- Grid settings --
        sec = ttk.LabelFrame(frame, text="Grid Settings", padding=5)
        sec.pack(fill=tk.X, padx=5, pady=4)

        ttk.Label(sec, text="Cell Width (px):").pack(anchor=tk.W)
        self._grid_w_var = tk.IntVar(value=64)
        ttk.Spinbox(
            sec, from_=1, to=9999, textvariable=self._grid_w_var, width=8
        ).pack(anchor=tk.W)

        ttk.Label(sec, text="Cell Height (px):").pack(anchor=tk.W)
        self._grid_h_var = tk.IntVar(value=64)
        ttk.Spinbox(
            sec, from_=1, to=9999, textvariable=self._grid_h_var, width=8
        ).pack(anchor=tk.W)

        ttk.Button(
            sec, text="Apply / Refresh Grid", command=self._cmd_apply_grid
        ).pack(fill=tk.X, pady=(5, 1))
        ttk.Button(sec, text="Select All Cells", command=self._cmd_select_all).pack(
            fill=tk.X, pady=1
        )
        ttk.Button(
            sec, text="Clear Selection", command=self._cmd_clear
        ).pack(fill=tk.X, pady=1)

        # -- Naming template --
        sec = ttk.LabelFrame(frame, text="Naming Template", padding=5)
        sec.pack(fill=tk.X, padx=5, pady=4)
        ttk.Label(sec, text="Template:").pack(anchor=tk.W)
        self._naming_var = tk.StringVar(value="frame_%num%")
        ttk.Entry(sec, textvariable=self._naming_var).pack(fill=tk.X)
        ttk.Label(
            sec,
            text="%num% = order   %col% = column   %row% = row",
            foreground="gray",
            font=("TkDefaultFont", 7),
            wraplength=195,
        ).pack(anchor=tk.W, pady=(2, 0))
        ttk.Button(
            sec, text="Refresh Preview Names", command=self._refresh_preview
        ).pack(fill=tk.X, pady=(4, 0))

        # -- Export --
        sec = ttk.LabelFrame(frame, text="Export", padding=5)
        sec.pack(fill=tk.X, padx=5, pady=4)
        self._export_fmt_var = tk.StringVar(value="PNG")
        ttk.Label(sec, text="Format:").pack(anchor=tk.W)
        ttk.Combobox(
            sec,
            textvariable=self._export_fmt_var,
            state="readonly",
            values=["PNG", "JPEG", "BMP", "TIFF"],
            width=8,
        ).pack(anchor=tk.W)
        ttk.Button(sec, text="Export Crops…", command=self._cmd_export).pack(
            fill=tk.X, pady=(4, 0)
        )

        # -- Zoom --
        sec = ttk.LabelFrame(frame, text="Canvas Zoom", padding=5)
        sec.pack(fill=tk.X, padx=5, pady=4)
        zrow = ttk.Frame(sec)
        zrow.pack()
        ttk.Button(zrow, text="−", width=3, command=self._zoom_out).pack(side=tk.LEFT)
        self._zoom_label_var = tk.StringVar(value="100 %")
        ttk.Label(
            zrow, textvariable=self._zoom_label_var, width=7, anchor=tk.CENTER
        ).pack(side=tk.LEFT)
        ttk.Button(zrow, text="+", width=3, command=self._zoom_in).pack(side=tk.LEFT)
        ttk.Button(sec, text="Fit to Window", command=self._zoom_fit).pack(
            fill=tk.X, pady=(3, 0)
        )

    # ---- Center canvas panel ------------------------------------------

    def _build_canvas_panel(self) -> None:
        frame = ttk.Frame(self._pw)
        self._pw.add(frame, weight=3)

        ttk.Label(
            frame, text="Image Canvas", font=("TkDefaultFont", 9, "bold")
        ).pack(anchor=tk.W, padx=5, pady=2)

        cont = ttk.Frame(frame)
        cont.pack(fill=tk.BOTH, expand=True)

        hbar = ttk.Scrollbar(cont, orient=tk.HORIZONTAL)
        hbar.pack(side=tk.BOTTOM, fill=tk.X)
        vbar = ttk.Scrollbar(cont, orient=tk.VERTICAL)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._canvas = tk.Canvas(
            cont,
            bg="#2a2a2a",
            cursor="crosshair",
            xscrollcommand=hbar.set,
            yscrollcommand=vbar.set,
        )
        self._canvas.pack(fill=tk.BOTH, expand=True)
        hbar.config(command=self._canvas.xview)
        vbar.config(command=self._canvas.yview)

        self._canvas.bind("<Button-1>", self._canvas_click)
        self._canvas.bind("<B1-Motion>", self._canvas_drag)
        self._canvas.bind("<ButtonRelease-1>", self._canvas_release)
        self._canvas.bind("<Button-3>", self._canvas_right_click)
        # Zoom on scroll (Windows / macOS)
        self._canvas.bind(
            "<MouseWheel>",
            lambda e: (self._zoom_in() if e.delta > 0 else self._zoom_out()),
        )
        # Zoom on scroll (Linux)
        self._canvas.bind("<Button-4>", lambda _e: self._zoom_in())
        self._canvas.bind("<Button-5>", lambda _e: self._zoom_out())
        self._canvas.bind("<Configure>", self._canvas_configure)

    # ---- Right preview panel ------------------------------------------

    def _build_preview_panel(self) -> None:
        frame = ttk.Frame(self._pw, width=250)
        frame.pack_propagate(False)
        self._pw.add(frame, weight=1)

        hdr = ttk.Frame(frame)
        hdr.pack(fill=tk.X, padx=5, pady=2)
        ttk.Label(hdr, text="Preview", font=("TkDefaultFont", 9, "bold")).pack(
            side=tk.LEFT
        )
        self._preview_count_var = tk.StringVar(value="(0)")
        ttk.Label(
            hdr, textvariable=self._preview_count_var, foreground="gray"
        ).pack(side=tk.LEFT, padx=4)

        cont = ttk.Frame(frame)
        cont.pack(fill=tk.BOTH, expand=True, padx=3, pady=2)
        vbar = ttk.Scrollbar(cont, orient=tk.VERTICAL)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._prev_canvas = tk.Canvas(
            cont, bg="#1e1e1e", yscrollcommand=vbar.set
        )
        self._prev_canvas.pack(fill=tk.BOTH, expand=True)
        vbar.config(command=self._prev_canvas.yview)

        self._prev_inner = ttk.Frame(self._prev_canvas)
        self._prev_win_id = self._prev_canvas.create_window(
            (0, 0), window=self._prev_inner, anchor=tk.NW
        )

        self._prev_inner.bind(
            "<Configure>",
            lambda _e: self._prev_canvas.configure(
                scrollregion=self._prev_canvas.bbox("all")
            ),
        )
        self._prev_canvas.bind(
            "<Configure>",
            lambda e: self._prev_canvas.itemconfig(
                self._prev_win_id, width=e.width
            ),
        )

        for widget in (self._prev_canvas, self._prev_inner):
            widget.bind(
                "<MouseWheel>",
                lambda e: self._prev_canvas.yview_scroll(
                    -1 * (e.delta // 120), "units"
                ),
            )
            widget.bind(
                "<Button-4>",
                lambda _e: self._prev_canvas.yview_scroll(-1, "units"),
            )
            widget.bind(
                "<Button-5>",
                lambda _e: self._prev_canvas.yview_scroll(1, "units"),
            )

    # ===================================================================
    # Commands (menu / button callbacks)
    # ===================================================================

    def _cmd_open_image(self) -> None:
        path = filedialog.askopenfilename(
            title="Open Image",
            filetypes=[
                (
                    "Images",
                    "*.png *.jpg *.jpeg *.bmp *.gif *.tiff *.tif *.webp",
                ),
                ("All files", "*.*"),
            ],
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
            self._zoom_fit()
            self._refresh_preview()
            self._set_status(
                f"Opened: {os.path.basename(path)}  "
                f"{img.width}×{img.height} px"
            )
        except Exception as exc:
            messagebox.showerror("Open Image", f"Cannot open image:\n{exc}")

    def _cmd_save(self) -> None:
        if self.project_path is None:
            self._cmd_save_as()
        else:
            self._write_project(self.project_path)

    def _cmd_save_as(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save Project As",
            defaultextension=".json",
            filetypes=[("JSON project", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.project_path = Path(path)
            self._write_project(self.project_path)

    def _cmd_open_project(self) -> None:
        path = filedialog.askopenfilename(
            title="Open Project",
            filetypes=[("JSON project", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.project = ProjectData.from_dict(data)
            self.project_path = Path(path)

            # Sync widgets with loaded data
            self._crop_type_var.set(self.project.crop_type)
            self._grid_w_var.set(self.project.grid_size.width)
            self._grid_h_var.set(self.project.grid_size.height)
            self._naming_var.set(self.project.naming_template)

            # Load image referenced by project
            if self.project.image_path:
                if os.path.exists(self.project.image_path):
                    img = Image.open(self.project.image_path)
                    if img.mode not in ("RGB", "RGBA"):
                        img = img.convert("RGBA")
                    self.image = img
                    self._img_name_var.set(
                        os.path.basename(self.project.image_path)
                    )
                else:
                    messagebox.showwarning(
                        "Image Not Found",
                        f"Could not locate:\n{self.project.image_path}\n\n"
                        "Open the image manually when ready.",
                    )

            # Rebuild grid helper state from loaded areas
            self._rebuild_grid_cells()
            self._zoom_fit()
            self._refresh_preview()
            self.root.title(f"Crop Utils – {Path(path).name}")
            self._set_status(f"Project loaded: {path}")
        except Exception as exc:
            messagebox.showerror("Open Project", f"Cannot open project:\n{exc}")

    def _cmd_apply_grid(self) -> None:
        self.project.grid_size.width = self._grid_w_var.get()
        self.project.grid_size.height = self._grid_h_var.get()
        self.project.crop_areas.clear()
        self._grid_cells.clear()
        self._redraw()
        self._refresh_preview()

    def _cmd_select_all(self) -> None:
        if self.image is None:
            return
        cw = self._grid_w_var.get()
        ch = self._grid_h_var.get()
        cols = self.image.width // cw
        rows = self.image.height // ch
        self.project.crop_areas = [
            CropArea(x=c * cw, y=r * ch, width=cw, height=ch)
            for r in range(rows)
            for c in range(cols)
        ]
        self._rebuild_grid_cells()
        self._redraw()
        self._refresh_preview()

    def _cmd_clear(self) -> None:
        self.project.crop_areas.clear()
        self._grid_cells.clear()
        self._redraw()
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

        fmt = self._export_fmt_var.get()
        ext_map = {"PNG": ".png", "JPEG": ".jpg", "BMP": ".bmp", "TIFF": ".tif"}
        ext = ext_map.get(fmt, ".png")
        template = self._naming_var.get()

        ok = err = 0
        for i, area in enumerate(self.project.crop_areas):
            name = self._fmt_name(template, i + 1, area)
            fpath = os.path.join(out_dir, name + ext)
            try:
                crop = self.image.crop(
                    (area.x, area.y, area.x + area.width, area.y + area.height)
                )
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
    # Canvas events
    # ===================================================================

    def _canvas_configure(self, _event: tk.Event) -> None:
        if self.image is not None:
            self._zoom_fit()

    def _canvas_click(self, event: tk.Event) -> None:
        if self.image is None:
            return
        cx = self._canvas.canvasx(event.x)
        cy = self._canvas.canvasy(event.y)
        ix, iy = self._c2i(cx, cy)

        if self._crop_type_var.get() == "grid":
            cw = self._grid_w_var.get()
            ch = self._grid_h_var.get()
            if 0 <= ix < self.image.width and 0 <= iy < self.image.height:
                col = int(ix // cw)
                row = int(iy // ch)
                key = (col, row)
                if key in self._grid_cells:
                    self._grid_cells.discard(key)
                    self.project.crop_areas = [
                        a
                        for a in self.project.crop_areas
                        if not (a.x == col * cw and a.y == row * ch)
                    ]
                else:
                    self._grid_cells.add(key)
                    self.project.crop_areas.append(
                        CropArea(
                            x=col * cw, y=row * ch, width=cw, height=ch
                        )
                    )
                self._redraw()
                self._refresh_preview()
        else:
            # Begin drawing a custom rectangle
            self._drawing = True
            self._draw_start = (ix, iy)

    def _canvas_drag(self, event: tk.Event) -> None:
        if not self._drawing or self._draw_start is None:
            return
        cx = self._canvas.canvasx(event.x)
        cy = self._canvas.canvasy(event.y)
        ix, iy = self._c2i(cx, cy)
        if self._temp_rid is not None:
            self._canvas.delete(self._temp_rid)
        x0c, y0c = self._i2c(*self._draw_start)
        x1c, y1c = self._i2c(ix, iy)
        self._temp_rid = self._canvas.create_rectangle(
            x0c, y0c, x1c, y1c, outline="yellow", width=2, dash=(5, 3)
        )

    def _canvas_release(self, event: tk.Event) -> None:
        if not self._drawing or self._draw_start is None:
            return
        self._drawing = False
        cx = self._canvas.canvasx(event.x)
        cy = self._canvas.canvasy(event.y)
        ix, iy = self._c2i(cx, cy)

        if self._temp_rid is not None:
            self._canvas.delete(self._temp_rid)
            self._temp_rid = None

        x0 = int(min(self._draw_start[0], ix))
        y0 = int(min(self._draw_start[1], iy))
        x1 = int(max(self._draw_start[0], ix))
        y1 = int(max(self._draw_start[1], iy))
        self._draw_start = None

        w, h = x1 - x0, y1 - y0
        if w < 4 or h < 4:
            return  # too small – ignore

        if self.image is not None:
            x0 = max(0, min(x0, self.image.width))
            y0 = max(0, min(y0, self.image.height))
            w = min(w, self.image.width - x0)
            h = min(h, self.image.height - y0)

        self.project.crop_areas.append(CropArea(x=x0, y=y0, width=w, height=h))
        self._redraw()
        self._refresh_preview()

    def _canvas_right_click(self, event: tk.Event) -> None:
        """Remove a custom selection that contains the right-clicked point."""
        if self._crop_type_var.get() != "custom" or self.image is None:
            return
        cx = self._canvas.canvasx(event.x)
        cy = self._canvas.canvasy(event.y)
        ix, iy = self._c2i(cx, cy)
        for i, a in enumerate(self.project.crop_areas):
            if a.x <= ix <= a.x + a.width and a.y <= iy <= a.y + a.height:
                self.project.crop_areas.pop(i)
                self._redraw()
                self._refresh_preview()
                return

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
        cw = self._canvas.winfo_width() or 640
        ch = self._canvas.winfo_height() or 480
        sx = cw / self.image.width
        sy = ch / self.image.height
        self._scale = min(sx, sy) * 0.95
        ox = max(10.0, (cw - self.image.width * self._scale) / 2)
        oy = max(10.0, (ch - self.image.height * self._scale) / 2)
        self._offset = (ox, oy)
        self._update_zoom_label()
        self._redraw()

    def _update_zoom_label(self) -> None:
        self._zoom_label_var.set(f"{int(self._scale * 100)} %")

    # ===================================================================
    # Canvas drawing helpers
    # ===================================================================

    def _c2i(self, cx: float, cy: float) -> tuple[float, float]:
        """Canvas → image coordinates."""
        ox, oy = self._offset
        return (cx - ox) / self._scale, (cy - oy) / self._scale

    def _i2c(self, ix: float, iy: float) -> tuple[float, float]:
        """Image → canvas coordinates."""
        ox, oy = self._offset
        return ix * self._scale + ox, iy * self._scale + oy

    def _redraw(self) -> None:
        self._canvas.delete("all")
        if self.image is None:
            self._canvas.create_text(
                (self._canvas.winfo_width() or 400) // 2,
                (self._canvas.winfo_height() or 300) // 2,
                text="Open an image to begin",
                fill="#888888",
                font=("TkDefaultFont", 14),
            )
            return

        dw = max(1, int(self.image.width * self._scale))
        dh = max(1, int(self.image.height * self._scale))
        resized = self.image.resize((dw, dh), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(resized)
        ox, oy = self._offset
        self._canvas.create_image(ox, oy, image=self._photo, anchor=tk.NW)
        self._canvas.configure(
            scrollregion=(0, 0, dw + ox * 2, dh + oy * 2)
        )

        if self._crop_type_var.get() == "grid":
            self._draw_grid_overlay()
        else:
            self._draw_custom_overlay()

    def _draw_grid_overlay(self) -> None:
        if self.image is None:
            return
        cw = self._grid_w_var.get()
        ch = self._grid_h_var.get()
        iw, ih = self.image.width, self.image.height

        # Build index lookup for selected cells
        cell_order: dict[tuple[int, int], int] = {}
        for idx, area in enumerate(self.project.crop_areas):
            col = area.x // cw
            row = area.y // ch
            cell_order[(col, row)] = idx + 1

        # Grid lines
        x = 0
        while x <= iw:
            x0c, y0c = self._i2c(x, 0)
            _, y1c = self._i2c(x, ih)
            self._canvas.create_line(x0c, y0c, x0c, y1c, fill="#505050", width=1)
            x += cw
        y = 0
        while y <= ih:
            x0c, y0c = self._i2c(0, y)
            x1c, _ = self._i2c(iw, y)
            self._canvas.create_line(x0c, y0c, x1c, y0c, fill="#505050", width=1)
            y += ch

        # Highlighted / selected cells
        for (col, row), order in cell_order.items():
            x0c, y0c = self._i2c(col * cw, row * ch)
            x1c, y1c = self._i2c((col + 1) * cw, (row + 1) * ch)
            self._canvas.create_rectangle(
                x0c,
                y0c,
                x1c,
                y1c,
                fill="#4488ff",
                outline="#2266dd",
                stipple="gray25",
            )
            mx, my = (x0c + x1c) / 2, (y0c + y1c) / 2
            self._canvas.create_text(
                mx, my, text=str(order), fill="white",
                font=("TkDefaultFont", 8, "bold"),
            )

    def _draw_custom_overlay(self) -> None:
        for i, area in enumerate(self.project.crop_areas):
            x0c, y0c = self._i2c(area.x, area.y)
            x1c, y1c = self._i2c(area.x + area.width, area.y + area.height)
            self._canvas.create_rectangle(
                x0c,
                y0c,
                x1c,
                y1c,
                fill="#ff4400",
                outline="#ff6622",
                width=2,
                stipple="gray25",
            )
            mx, my = (x0c + x1c) / 2, (y0c + y1c) / 2
            self._canvas.create_text(
                mx, my, text=str(i + 1), fill="white",
                font=("TkDefaultFont", 9, "bold"),
            )

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
        self._prev_canvas.configure(
            scrollregion=self._prev_canvas.bbox("all")
        )

    def _add_preview_row(
        self, idx: int, area: CropArea, template: str
    ) -> None:
        THUMB = 72
        row = ttk.Frame(self._prev_inner, relief=tk.RIDGE, borderwidth=1)
        row.pack(fill=tk.X, padx=2, pady=1)

        # Thumbnail image
        ph: Optional[ImageTk.PhotoImage] = None
        if self.image is not None:
            try:
                crop = self.image.crop(
                    (area.x, area.y, area.x + area.width, area.y + area.height)
                )
                crop.thumbnail((THUMB, THUMB), Image.LANCZOS)
                ph = ImageTk.PhotoImage(crop)
            except Exception:
                ph = None
        self._thumb_refs.append(ph)  # keep reference alive

        if ph:
            ttk.Label(row, image=ph).pack(side=tk.LEFT, padx=2, pady=2)
        else:
            ttk.Label(row, text="⚠", width=5).pack(side=tk.LEFT, padx=2)

        # Info block
        info = ttk.Frame(row)
        info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)
        name = self._fmt_name(template, idx + 1, area)
        ttk.Label(
            info, text=name, font=("TkDefaultFont", 8, "bold"),
            wraplength=105, anchor=tk.W,
        ).pack(fill=tk.X)
        ttk.Label(
            info,
            text=f"{area.width}×{area.height}  ({area.x},{area.y})",
            font=("TkDefaultFont", 7),
            foreground="#888888",
            anchor=tk.W,
        ).pack(fill=tk.X)

        # Reorder / delete buttons
        btns = ttk.Frame(row)
        btns.pack(side=tk.RIGHT, padx=2)
        ttk.Button(
            btns, text="↑", width=2,
            command=lambda i=idx: self._move_area(i, -1),
        ).pack(pady=1)
        ttk.Button(
            btns, text="↓", width=2,
            command=lambda i=idx: self._move_area(i, +1),
        ).pack(pady=1)
        ttk.Button(
            btns, text="✕", width=2,
            command=lambda i=idx: self._delete_area(i),
        ).pack(pady=1)

    def _move_area(self, idx: int, delta: int) -> None:
        areas = self.project.crop_areas
        new_idx = idx + delta
        if 0 <= new_idx < len(areas):
            areas[idx], areas[new_idx] = areas[new_idx], areas[idx]
            self._refresh_preview()
            self._redraw()

    def _delete_area(self, idx: int) -> None:
        areas = self.project.crop_areas
        if 0 <= idx < len(areas):
            removed = areas.pop(idx)
            if self._crop_type_var.get() == "grid":
                cw = self._grid_w_var.get()
                ch = self._grid_h_var.get()
                self._grid_cells.discard((removed.x // cw, removed.y // ch))
            self._refresh_preview()
            self._redraw()

    # ===================================================================
    # Naming
    # ===================================================================

    def _fmt_name(self, template: str, num: int, area: CropArea) -> str:
        cw = max(1, self._grid_w_var.get())
        ch = max(1, self._grid_h_var.get())
        return (
            template
            .replace("%num%", str(num))
            .replace("%col%", str(area.x // cw))
            .replace("%row%", str(area.y // ch))
        )

    # ===================================================================
    # Grid helper state
    # ===================================================================

    def _rebuild_grid_cells(self) -> None:
        """Derive _grid_cells from project.crop_areas after project load."""
        cw = max(1, self.project.grid_size.width)
        ch = max(1, self.project.grid_size.height)
        self._grid_cells = {
            (a.x // cw, a.y // ch) for a in self.project.crop_areas
        }

    # ===================================================================
    # Project save / load
    # ===================================================================

    def _collect_project(self) -> None:
        """Sync mutable widget state → project model."""
        self.project.crop_type = self._crop_type_var.get()
        self.project.grid_size.width = self._grid_w_var.get()
        self.project.grid_size.height = self._grid_h_var.get()
        self.project.naming_template = self._naming_var.get()

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
            return  # nothing worth saving yet
        try:
            self._collect_project()
            if self.project_path:
                backup = self.project_path.parent / (
                    self.project_path.stem + ".backup.json"
                )
            elif self.project.image_path:
                img = Path(self.project.image_path)
                backup = img.parent / (img.stem + ".backup.json")
            else:
                backup = Path.home() / ".crop_utils_autosave.json"

            with open(backup, "w", encoding="utf-8") as fh:
                json.dump(self.project.to_dict(), fh, indent=2, ensure_ascii=False)
            self._set_status(
                f"Autosaved → {backup.name}  "
                f"({len(self.project.crop_areas)} areas)"
            )
        except Exception:
            pass  # autosave must never crash the app

    # ===================================================================
    # Misc
    # ===================================================================

    def _on_type_change(self) -> None:
        self.project.crop_type = self._crop_type_var.get()
        self._cmd_clear()

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
        # Consistent DPI on most platforms
        root.tk.call("tk", "scaling", 1.0)
    except Exception:
        pass
    CropApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
