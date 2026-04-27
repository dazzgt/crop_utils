# crop_utils

A Python / Tkinter GUI for slicing sprite-sheets and other images either
by a **pixel grid** or **freehand rectangles**, with live preview and
flexible file-naming templates.

---

## Features

| Feature | Details |
|---|---|
| **Grid crop** | Set cell width & height in pixels; click cells on the canvas to add/remove them |
| **Custom selection** | Draw rectangles with the mouse; right-click a rectangle to delete it |
| **Live preview** | Right panel shows numbered thumbnails in export order |
| **Reordering** | ↑ / ↓ buttons on each preview item move it in the list |
| **Naming template** | `%num%` → 1-based index, `%col%` → grid column, `%row%` → grid row |
| **Save / Load project** | JSON file stores image path, crop type, grid size and all selected areas |
| **Autosave** | Every 30 s a `.backup.json` is written beside the project (or image) file |
| **Export** | Batch-export all selected regions as PNG / JPEG / BMP / TIFF |

---

## Requirements

* Python ≥ 3.10
* [uv](https://github.com/astral-sh/uv) (dependency & venv manager)
* Pillow ≥ 10 (installed automatically by uv)
* Tkinter (bundled with most Python distributions)

---

## Quick start

```bash
# 1. Clone / enter the repo
git clone https://github.com/dazzgt/crop_utils
cd crop_utils

# 2. Create virtual environment and install dependencies
uv sync

# 3. Run the app
uv run crop-utils
```

Or without installing as a package:

```bash
uv run python -m crop_utils.app
```

---

## Usage

1. **Open Image** – `File → Open Image…` or `Ctrl+O`.
2. Choose **Crop Type**:
   - *Grid* – adjust *Cell Width* and *Cell Height*, click **Apply / Refresh
     Grid**, then click cells on the canvas to select them (blue highlight).
     Click again to deselect.
   - *Custom Selection* – drag a rectangle on the canvas; right-click any
     rectangle to remove it.
3. Edit the **Naming Template** (e.g. `ptero_%num%`).
4. Use **↑ / ↓** in the preview panel to reorder crops.
5. **Save Project** (`Ctrl+S`) → saves a `.json` file you can reload later.
6. **Export Crops…** → choose a directory; each region is saved as
   `<name>.<ext>`.

---

## Project JSON format

```json
{
  "image_path": "/path/to/sprite.png",
  "crop_type": "grid",
  "grid_size": { "width": 64, "height": 64 },
  "naming_template": "ptero_%num%",
  "crop_areas": [
    { "x": 0,  "y": 0, "width": 64, "height": 64 },
    { "x": 64, "y": 0, "width": 64, "height": 64 }
  ]
}
```
