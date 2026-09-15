# Advanced PDF Text & Image Extraction

A Python tool that uses [PyMuPDF](https://pymupdf.readthedocs.io/) to extract
**text**, **raster images**, **vector figures** (flowcharts, pie charts,
chemical diagrams), and **tables** from any PDF — with smart caption detection,
column-aware layout analysis, and a JSON manifest linking every image back to
its source page and caption.

## ⚡ Quick Setup

### 1. Clone the repository

```bash
git clone https://github.com/keplar-404/advanced-pdf-text-and-image-extraction.git
cd advanced-pdf-text-and-image-extraction
```

### 2. Create and activate a Python virtual environment

```bash
python3 -m venv .venv
```

**On Linux / macOS:**
```bash
source .venv/bin/activate
```

**On Windows:**
```bash
.venv\Scripts\activate
```

> You should see `(.venv)` appear at the start of your terminal prompt.

### 3. Install all required packages

```bash
pip install pymupdf pillow pymupdf-layout
```

| Package | Purpose |
|---|---|
| `pymupdf` | Core PDF parsing and rendering engine |
| `pillow` | Image processing (PNG/JPEG saving) |
| `pymupdf-layout` | Advanced ML-based table detection (uses ONNX model) |

### 4. Run the extractor on your PDF

```bash
python extract_pdf.py path/to/your_file.pdf
```

That's it! Output is automatically saved next to your PDF in a folder named `your_file_extracted/`.

---

## 📁 Output Structure

```
your_file_extracted/
├── extract.txt                    # All page text with PAGE N headers
├── extract_log.txt                # Detailed processing log
├── layout.json                    # Column layout analysis per page
├── drawings.json                  # Vector drawing classification per page
├── tables/
│   ├── page013_table01.md         # Detected data tables as Markdown
│   └── page021_table01.md
└── extract_images/
    ├── manifest.json              # JSON index: every image + caption metadata
    ├── page007_vec01.png          # Vector figures (flowcharts, diagrams, charts)
    └── page024_img01_xref137.png  # Embedded raster photos
```

## ⚙️ Options

```bash
# Use a custom output directory
python extract_pdf.py input.pdf --output-dir /path/to/output/

# Skip images smaller than N pixels (filters out tiny icons)
python extract_pdf.py input.pdf --min-dim 16
```

## 📋 Files in this Repository

| File | Purpose |
|---|---|
| `extract_pdf.py` | **Main script** — run this on any PDF |
| `make_fixture.py` | Builds `sample.pdf` test fixture |
| `make_chart.py` | Builds a test chart image |
| `make_overlay_fixture.py` | Builds `overlay.pdf` for overlay classification tests |
| `make_realworld_fixture.py` | Builds `realworld.pdf` for table/layout tests |
| `show_overlay.py` | Prints text-overlay classification for each image |
| `test_extract.py` | Sanity-checks extracted images against known colors |

# (optionally, for re-building the test fixture)
pip install reportlab
```

> PyMuPDF 1.24+ exposes itself as `pymupdf`.  The script also falls back to
> the legacy `fitz` name if you have an older install.

## Usage

```bash
python extract_pdf.py path/to/some.pdf
# or with explicit options
python extract_pdf.py some.pdf --output-dir out/ --min-dim 16
```

| Flag            | Default                                  | Description                                            |
| --------------- | ---------------------------------------- | ------------------------------------------------------ |
| `pdf`           | *(required, positional)*                 | Path to the input PDF.                                 |
| `--output-dir`  | `<pdf-stem>_extracted/` next to the PDF  | Where to write `extract.txt`, `extract_images/`, etc.  |
| `--min-dim`     | `8`                                      | Skip images whose width **or** height is below this.   |

## What you get

```
extracted_sample/
├── extract.txt              # All page text, with clear "PAGE n" separators.
├── extract_log.txt          # Human-readable log + summary.
├── layout.json              # Per-page column count + per-column bboxes.
├── drawings.json            # Per-page vector drawing classification.
└── extract_images/
    ├── manifest.json        # Machine-readable list of every image + page ref.
    ├── page001_img01_xref8.png
    ├── page002_img01_xref16.png
    └── page002_img02_xref18.png
└── tables/                  # (only if PyMuPDF find_tables() finds real tables)
    ├── page003_table01.md   # Each table is a Markdown file with row separators.
    └── page005_table01.md
```

### Image filenames

Every image file is named so the source page is obvious at a glance:

```
page{PAGE}_img{SEQ}_xref{XREF}.{ext}
   │       │       │          │
   │       │       │          └─ original image extension (png, jpeg, …)
   │       │       └─ PDF xref of the image (useful for dedup)
   │       └─ sequence index on that page (1-based)
   └─ source page number (1-based)
```

### Manifest format

`extract_images/manifest.json` is an array of entries like:

```json
{
  "path": "extract_images/page002_img01_xref16.png",
  "xref": 16,
  "page": 2,
  "page_label": "2",
  "width": 256,
  "height": 256,
  "ext": "png",
  "size_bytes": 1225,
  "colorspace": 3,
  "has_mask": false,
  "first_occurrence_only": true,
  "nearby_caption": null
}
```

`first_occurrence_only: false` entries point at an image that was already
saved under another page (the same xref referenced from a different page);
their `path` is `null` and the original is the entry with
`first_occurrence_only: true` and the same `xref`.

### `nearby_caption` — finding the right image by content

The `nearby_caption` field is the secret weapon for downstream agents.
Many PDFs (especially academic / LaTeX papers) have **figure floats** that
sit on a different page than the section that introduces them.  For
example, in "Attention Is All You Need", the section "3 Model
Architecture" begins on page 2 but the actual Transformer diagram
("Figure 1: The Transformer — model architecture") is anchored to page 3.

To handle this, for every image the extractor scans the page text for
"Figure N:", "Fig. N:", "Table N:", etc. and records the nearest such
caption within 60 pt (above, below, or adjacent to the image).  The
field looks like:

```json
"nearby_caption": {
  "kind": "Figure",
  "number": 1,
  "snippet": "Figure 1: The Transformer - model architecture.",
  "caption_rect": [210.0, 404.7, 401.9, 414.7],
  "image_rect":   [196.5,  72.0, 415.4, 394.4],
  "relative_position": "below",
  "distance_pt": 10.3
}
```

So an agent that needs "the model architecture diagram" can now do
`grep -i "model architecture" manifest.json` and find it directly,
instead of guessing by page number.  When the image has no nearby
caption (e.g. a decorative banner), `nearby_caption` is `null`.

### `text_overlay` — figuring out what text is on top of / beside each image

PDFs often place text in spatial relation to an image: a callout label
on top of a chart bar, a caption to the right, a note below, etc.  The
`text_overlay` field classifies every text block on the same page into
one of two categories based on its bbox vs the image's bbox:

| Category | Meaning |
| --- | --- |
| `on_top`    | The text bbox intersects the image bbox.  This is a real "label drawn on the chart" / "annotation over the photo". |
| `beside.<side>` | The text sits adjacent to the image on the named side (`left`, `right`, `above`, `below`) within 24 pt, and overlaps the image on the perpendicular axis.  This covers "caption to the right of a figure", "footnote below", etc. |

Example (from the `overlay.pdf` fixture, page 2):

```json
"text_overlay": {
  "on_top": [],
  "beside": {
    "right": [
      {
        "text": "Figure A: This caption is a real text object ...",
        "bbox": [370.0, 90.0, 539.5, 221.9],
        "distance_pt": 18.0
      }
    ],
    "left":   [],
    "above":  [],
    "below":  []
  }
}
```

This means: the image has no text on top of it, but there's a text block
18 pt to the right that overlaps the image vertically — i.e. it's a
caption sitting beside the figure.

When the image has no associated text (e.g. a decorative banner, or —
crucially — an image whose labels were baked into the pixels), all
categories are empty:

```json
"text_overlay": { "on_top": [], "beside": {"left": [], "right": [], "above": [], "below": []} }
```

**The "burned-in pixels" caveat.** If the PDF author rendered labels
*as part of the image* (e.g. a chart exported from Excel where the axis
labels are pixels, not separate text), neither `get_text()` nor the
manifest can see them — only OCR could recover them.  An empty
`text_overlay` together with the existence of the image is your
signal that OCR is needed.

## How it works (in 30 seconds)

1. **Text:** for every page, call `page.get_text("text")` and concatenate,
   with a `PAGE n` separator so each page's text is easy to find.
2. **Raster images:** for every page, call `page.get_images(full=True)` to get each
   `xref` + dimensions.  Then call `doc.extract_image(xref)` to pull the raw
   bytes (preserving the original format: png/jpeg/etc.) and write them out.
3. **Soft masks (`/SMask`):** if the image has a transparency mask, the
   script reconstructs the full RGBA pixmap with PyMuPDF's `Pixmap(pix, mask)`
   and saves it as PNG so the alpha channel is preserved.
4. **De-duplication:** each `xref` is only saved once, but every page that
   references it is recorded in the manifest.
5. **Stencil masks / tiny pixels:** images smaller than `--min-dim` on either
   side are skipped and logged — these are usually PDF transparency layers.
6. **Vector-only figures:** many academic / technical PDFs draw figures as
   vector graphics (lines, polygons, text labels) instead of embedding a
   raster image — `get_images()` returns zero hits for these.  The extractor
   has a second pass that detects pages with significant vector content
   (many drawings or large path bboxes) and renders the bounding box of the
   drawing cluster to a PNG via `page.get_pixmap(clip=…)` at 200 DPI.
   These entries are recorded in the manifest with `kind: "vector_render"`,
   the original `source_bbox_pt`, and the `drawing_count`.
7. **Nearby-caption lookup:** for every image, the script locates the image
   rect on its page (`Page.get_image_rects(xref)`) and looks for the closest
   "Figure N:" / "Fig. N:" / "Table N:" caption within 60 pt.  This is
   recorded as `nearby_caption` in the manifest so downstream agents can
   find images by content (e.g. "the model architecture diagram") instead
   of guessing by page number — see the next section for why this matters.
8. **Text overlay classification:** for every image, the script also walks
   the same page's text blocks and classifies each one as either `on_top`
   (bbox intersects the image) or `beside.<side>` (sits adjacent on the
   left/right/above/below within 24 pt, with perpendicular-axis overlap).
   This lets a downstream agent answer "what label is on top of this
   chart?" or "what caption sits beside this image?" without doing
   geometry of its own.
9. **Table detection:** `page.find_tables()` is PyMuPDF's built-in table
   detector.  For every detected table, the extractor writes a Markdown
   file under `tables/` and records `bbox`, `row_count`, `col_count`,
   and (if find_tables can guess it) the `header` column names in the
   manifest.  A sanity check rejects "tables" that are actually dense
   grids of vector drawings (e.g. attention-head visualizations in a
   research paper) so they don't pollute the output.
10. **Multi-column detection:** for every page, the extractor groups text
    blocks by their left-edge x-position.  Pages with 2+ distinct columns
    each having 2+ blocks get reported in `layout.json`, with per-column
    bboxes.  Wide top blocks (titles / banners that span the page) are
    excluded so they don't force every column to look the same width.
11. **Drawing classification:** for every page, the script looks at the
    number of `lines`, `rects`, `curves`, and `quads` returned by
    `get_drawings()` and labels the page as one of: `sketch` (curves +
    freeform strokes, e.g. hand-drawn sketch), `grid` (many horizontal
    lines, e.g. ruled notebook paper or a table grid), `flowchart`
    (rectangles + connecting lines), `decorative` (few shapes), or
    `unknown`.  The result lands in `drawings.json`.

## Real-world layout coverage

The extractor is built to handle the messy things real PDFs do.  The
`realworld.pdf` fixture exercises the most common cases; each row is
something the extractor now recovers automatically.

| Case on the page                        | What the extractor produces                              |
| --------------------------------------- | -------------------------------------------------------- |
| Hand-drawn-style sketch (Béziers, arrow) | Vector-rendered PNG (`page001_vec01.png`); labeled `sketch` in `drawings.json` |
| Ruled notebook grid                     | Vector-rendered PNG of the whole grid region             |
| Real data table (3×4 with header)        | `tables/page003_table01.md` as Markdown; `row_count`/`col_count` in manifest |
| Multi-column article (2 cols + sidebar) | 2 columns reported in `layout.json` with per-column bboxes |
| Newspaper layout (banner + body + table + footer) | All text + table + columns detected                      |
| Flowchart / org chart (boxes + arrows)   | Vector-rendered PNG; labeled `flowchart` in `drawings.json` |

## Testing it

```bash
source .venv/bin/activate
python make_fixture.py              # builds sample.pdf (3 pages, text + 3 images)
python extract_pdf.py sample.pdf
python test_extract.py              # confirms extracted image bytes are correct

python make_chart.py                # builds chart.png (a chart with burned-in labels)
python make_overlay_fixture.py      # builds overlay.pdf (4 pages, exercises all 3 overlay cases)
python extract_pdf.py overlay.pdf
python show_overlay.py              # shows the text_overlay classification per image

python make_realworld_fixture.py    # builds realworld.pdf (6 pages: sketch, grid, table, multi-col, newspaper, flowchart)
python extract_pdf.py realworld.pdf
# Inspect: ls realworld_extracted/extract_images/
#          ls realworld_extracted/tables/
#          cat realworld_extracted/layout.json
#          cat realworld_extracted/drawings.json
```

Expected `test_extract.py` output:

```
page001_img01_xref8.png: (256, 256) center=(220, 50, 50) expected~(220, 50, 50) OK=True
page002_img01_xref16.png: (256, 256) center=(40, 170, 80) expected~(40, 170, 80) OK=True
page002_img02_xref18.png: (256, 256) center=(40, 80, 220) expected~(40, 80, 220) OK=True
ALL OK
```

## Errors

| Situation                   | Behaviour                                                |
| --------------------------- | -------------------------------------------------------- |
| PyMuPDF not installed       | Prints a clear install hint and exits with code 2.       |
| PDF path doesn't exist      | `ERROR: PDF not found: <path>` and exits with code 1.    |
| Path is a directory         | `ERROR: Expected a PDF file, got a directory: <path>`    |
| PyMuPDF fails to open file  | The underlying error is wrapped and reported.            |
| An individual image fails   | That image is skipped and the failure is recorded in the log. |