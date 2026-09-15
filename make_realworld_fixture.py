"""Build a 6-page PDF that exercises the messy real-world cases that a
naive text+image extractor misses.  Each page targets a specific case.

Page 1 — Hand-drawn-style sketch (circles, arrows, free-form curves)
Page 2 — Three-line ruled grid (notebook page)
Page 3 — Real data table (header + 4 rows × 3 cols, vector gridlines)
Page 4 — Multi-column article (2 cols + sidebar + figure)
Page 5 — Newspaper layout (banner + lead + body + table + footer)
Page 6 — Flowchart (boxes + arrows, org-chart style)
"""
from __future__ import annotations

from pathlib import Path

import pymupdf

HERE = Path(__file__).parent
FIXTURE = HERE / "realworld.pdf"


def _shape_sketch(page, x0, y0, scale=1.0):
    """Draw a "hand-drawn" looking sketch: a circle, a wavy line, an arrow."""
    shape = page.new_shape()
    s = scale
    # Approximate circle with 4 cubic Béziers.
    cx, cy, r = x0 + 40 * s, y0 + 40 * s, 30 * s
    k = 0.5523 * r
    shape.draw_bezier((cx - r, cy), (cx - r, cy - k), (cx - k, cy - r), (cx, cy - r))
    shape.draw_bezier((cx, cy - r), (cx + k, cy - r), (cx + r, cy - k), (cx + r, cy))
    shape.draw_bezier((cx + r, cy), (cx + r, cy + k), (cx + k, cy + r), (cx, cy + r))
    shape.draw_bezier((cx, cy + r), (cx - k, cy + r), (cx - r, cy + k), (cx - r, cy))
    shape.finish(color=(0.2, 0.4, 0.8), width=2 * s)
    # Wavy line (3 humps).
    pts = [
        (x0, y0 + 90 * s),
        (x0 + 20 * s, y0 + 80 * s),
        (x0 + 40 * s, y0 + 100 * s),
        (x0 + 60 * s, y0 + 80 * s),
        (x0 + 80 * s, y0 + 100 * s),
    ]
    for i in range(len(pts) - 1):
        shape.draw_line(pts[i], pts[i + 1])
    shape.finish(color=(0.8, 0.2, 0.2), width=1.5 * s)
    # Arrow: shaft + 2 small lines at the head.
    shape.draw_line((x0 + 90 * s, y0 + 40 * s), (x0 + 140 * s, y0 + 40 * s))
    shape.draw_line((x0 + 140 * s, y0 + 40 * s), (x0 + 130 * s, y0 + 35 * s))
    shape.draw_line((x0 + 140 * s, y0 + 40 * s), (x0 + 130 * s, y0 + 45 * s))
    shape.finish(color=(0.2, 0.2, 0.2), width=1.5 * s)
    # Label.
    shape.insert_text((x0, y0 + 110 * s), "sketch region", fontsize=10)
    shape.commit()


def _shape_grid(page, x0, y0, width=400, height=500, lines=12):
    """Draw a ruled notebook page: 3 holes + horizontal lines + margin."""
    shape = page.new_shape()
    # Horizontal ruled lines.
    line_h = height / lines
    for i in range(lines + 1):
        y = y0 + i * line_h
        shape.draw_line((x0, y), (x0 + width, y))
    shape.finish(color=(0.65, 0.75, 0.95), width=0.5)
    # Red margin line.
    shape.draw_line((x0 + 50, y0), (x0 + 50, y0 + height))
    shape.finish(color=(0.9, 0.4, 0.4), width=1)
    # 3 hole punches (left margin).
    for hole_y in (y0 + 50, y0 + height / 2, y0 + height - 50):
        shape.draw_circle((x0 - 15, hole_y), 6)
    shape.finish(color=(1, 1, 1), fill=(1, 1, 1), width=0.5)
    # Fake handwriting inside (so text extraction has something to find).
    shape.commit()


def _shape_table(page, x0, y0, cols=3, rows=4, cell_w=120, cell_h=30):
    """Draw a real vector table with gridlines + cell text."""
    shape = page.new_shape()
    width = cols * cell_w
    height = rows * cell_h
    # Gridlines.
    for c in range(cols + 1):
        shape.draw_line((x0 + c * cell_w, y0), (x0 + c * cell_w, y0 + height))
    for r in range(rows + 1):
        shape.draw_line((x0, y0 + r * cell_h), (x0 + width, y0 + r * cell_h))
    shape.finish(color=(0, 0, 0), width=0.8)
    # Header row fill.
    shape.draw_rect((x0, y0, x0 + width, y0 + cell_h))
    shape.finish(color=(0, 0, 0), fill=(0.85, 0.85, 0.95), width=0.5)
    # Header text.
    headers = ["Name", "Score", "Grade"]
    for c, h in enumerate(headers):
        shape.insert_text(
            (x0 + c * cell_w + 6, y0 + cell_h - 9), h, fontsize=11
        )
    # Body text.
    rows_data = [
        ["Alice", "92", "A"],
        ["Bob", "78", "B"],
        ["Carol", "85", "B+"],
        ["Dave", "67", "C"],
    ]
    for r, row in enumerate(rows_data, start=1):
        for c, val in enumerate(row):
            shape.insert_text(
                (x0 + c * cell_w + 6, y0 + (r + 1) * cell_h - 9),
                val,
                fontsize=10,
            )
    shape.commit()


def _shape_flowchart(page, x0, y0):
    """Draw a small org-chart: 3 boxes connected by arrows."""
    shape = page.new_shape()
    # Top box.
    shape.draw_rect((x0 + 60, y0, x0 + 180, y0 + 40))
    shape.finish(color=(0, 0, 0), fill=(0.7, 0.85, 1.0), width=1)
    # Two child boxes.
    shape.draw_rect((x0, y0 + 100, x0 + 120, y0 + 140))
    shape.finish(color=(0, 0, 0), fill=(0.95, 0.95, 0.95), width=1)
    shape.draw_rect((x0 + 140, y0 + 100, x0 + 260, y0 + 140))
    shape.finish(color=(0, 0, 0), fill=(0.95, 0.95, 0.95), width=1)
    # Vertical line from top to junction.
    shape.draw_line((x0 + 120, y0 + 40), (x0 + 120, y0 + 70))
    shape.finish(color=(0, 0, 0), width=1)
    # Horizontal junction line.
    shape.draw_line((x0 + 60, y0 + 70), (x0 + 200, y0 + 70))
    shape.finish(color=(0, 0, 0), width=1)
    # Verticals down to children.
    shape.draw_line((x0 + 60, y0 + 70), (x0 + 60, y0 + 100))
    shape.draw_line((x0 + 200, y0 + 70), (x0 + 200, y0 + 100))
    shape.finish(color=(0, 0, 0), width=1)
    # Labels.
    shape.insert_text((x0 + 90, y0 + 14), "Manager", fontsize=11)
    shape.insert_text((x0 + 25, y0 + 114), "Worker A", fontsize=10)
    shape.insert_text((x0 + 165, y0 + 114), "Worker B", fontsize=10)
    shape.commit()


def main() -> None:
    doc = pymupdf.open()
    try:
        # ---- Page 1: hand-drawn sketch ----------------------------------
        page = doc.new_page()
        page.insert_text((72, 60), "Case A — hand-drawn-style sketch", fontsize=14)
        _shape_sketch(page, 72, 100, scale=1.5)
        page.insert_text(
            (72, 320),
            "Note: the circle, wavy line, and arrow are all vector drawings, "
            "not raster images.",
            fontsize=10,
        )

        # ---- Page 2: ruled grid -----------------------------------------
        page = doc.new_page()
        page.insert_text((72, 60), "Case B — ruled notebook grid", fontsize=14)
        _shape_grid(page, 130, 100, width=350, height=500, lines=12)
        # Some text in the grid.
        page.insert_textbox(
            pymupdf.Rect(190, 110, 480, 600),
            (
                "Line one of fake handwriting.\n"
                "Line two of fake handwriting.\n"
                "Line three.\n"
                "Line four."
            ),
            fontsize=12,
        )

        # ---- Page 3: data table -----------------------------------------
        page = doc.new_page()
        page.insert_text((72, 60), "Case C — data table (vector gridlines)", fontsize=14)
        page.insert_text(
            (72, 80),
            "This table has real vector gridlines — find_tables() should pick it up.",
            fontsize=10,
        )
        _shape_table(page, 72, 110, cols=3, rows=4, cell_w=120, cell_h=30)

        # ---- Page 4: multi-column article -------------------------------
        page = doc.new_page()
        page.insert_text((72, 60), "Case D — multi-column article", fontsize=18)
        page.insert_text(
            (72, 90),
            "Two columns of body text, a small figure on the left, and a "
            "sidebar on the right.",
            fontsize=10,
        )
        # Left column body — multiple paragraphs so each is its own block.
        page.insert_textbox(
            pymupdf.Rect(72, 130, 250, 250),
            "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do "
            "eiusmod tempor incididunt ut labore et dolore magna aliqua.",
            fontsize=10,
        )
        page.insert_textbox(
            pymupdf.Rect(72, 270, 250, 380),
            "Ut enim ad minim veniam, quis nostrud exercitation ullamco "
            "laboris nisi ut aliquip ex ea commodo consequat.",
            fontsize=10,
        )
        page.insert_textbox(
            pymupdf.Rect(72, 400, 250, 510),
            "Duis aute irure dolor in reprehenderit in voluptate velit esse "
            "cillum dolore eu fugiat nulla pariatur.",
            fontsize=10,
        )
        # Right column body — same structure.
        page.insert_textbox(
            pymupdf.Rect(270, 130, 450, 250),
            "Excepteur sint occaecat cupidatat non proident, sunt in culpa "
            "qui officia deserunt mollit anim id est laborum.",
            fontsize=10,
        )
        page.insert_textbox(
            pymupdf.Rect(270, 270, 450, 380),
            "Sed ut perspiciatis unde omnis iste natus error sit voluptatem "
            "accusantium doloremque laudantium.",
            fontsize=10,
        )
        page.insert_textbox(
            pymupdf.Rect(270, 400, 450, 510),
            "Totam rem aperiam, eaque ipsa quae ab illo inventore veritatis.",
            fontsize=10,
        )
        # Sidebar (one block) — won't count as a column because it only has 1.
        shape = page.new_shape()
        shape.draw_rect((470, 130, 540, 600))
        shape.finish(color=(0.6, 0.6, 0.6), fill=(0.95, 0.95, 0.9), width=0.5)
        shape.commit()
        page.insert_textbox(
            pymupdf.Rect(475, 135, 535, 595),
            "Sidebar:\nExtra notes here.",
            fontsize=10,
        )

        # ---- Page 5: newspaper layout -----------------------------------
        page = doc.new_page()
        # Banner.
        shape = page.new_shape()
        shape.draw_rect((72, 60, 540, 100))
        shape.finish(color=(0, 0, 0), fill=(1.0, 0.95, 0.8), width=1)
        shape.commit()
        page.insert_text((80, 86), "DAILY NEWS — front page", fontsize=20)
        # Lead paragraph.
        page.insert_textbox(
            pymupdf.Rect(72, 115, 540, 175),
            "Breaking: a fully real-world PDF was generated to test the "
            "extractor's coverage of complex layouts.",
            fontsize=11,
        )
        # Body paragraph.
        page.insert_textbox(
            pymupdf.Rect(72, 185, 320, 380),
            (
                "The body of the article continues here in a single column. "
                "To the right is a stock-table summary, and below is a "
                "small data table that find_tables() should recover."
            ),
            fontsize=10,
        )
        # Sidebar table-like box.
        _shape_table(page, 340, 185, cols=2, rows=5, cell_w=95, cell_h=22)
        # Footer line.
        shape = page.new_shape()
        shape.draw_line((72, 760), (540, 760))
        shape.finish(color=(0.5, 0.5, 0.5), width=0.5)
        shape.commit()
        page.insert_text(
            (72, 775), "© 2026 — page 5 of realworld.pdf", fontsize=8
        )

        # ---- Page 6: flowchart ------------------------------------------
        page = doc.new_page()
        page.insert_text((72, 60), "Case E — flowchart / org chart", fontsize=14)
        _shape_flowchart(page, 72, 100)

        doc.save(FIXTURE.as_posix())
    finally:
        doc.close()

    print(f"Wrote fixture: {FIXTURE}")


if __name__ == "__main__":
    main()