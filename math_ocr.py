"""
math_ocr.py — Math-OCR post-processor for extract_pdf.py output.

Reads the PDF and the existing extract.txt produced by extract_pdf.py,
detects equation regions on every page using font-name and geometry
heuristics, renders each region at 300 DPI via PyMuPDF, converts it to
LaTeX with Pix2Text, and writes extract_math.txt with the garbled math
replaced by proper LaTeX.

Usage:
    python math_ocr.py <input.pdf> [options]

Options:
    --extract-txt PATH    Path to existing extract.txt
                          (default: <pdf-stem>_extracted/extract.txt)
    --output-dir PATH     Where to write outputs
                          (default: same directory as extract.txt)
    --dpi INT             Render DPI for equation crops (default: 300)
    --save-eq-images      Save cropped equation PNGs + .tex files to
                          <output_dir>/equation_images/
    --no-display-eq       Skip display equation detection
    --no-inline-eq        Skip inline/span-level equation detection

Outputs written:
    extract_math.txt      Full text with math replaced by LaTeX
    math_manifest.json    Per-equation record: page, bbox, original, latex
    math_log.txt          Human-readable per-page processing log

Dependencies:
    pip install pix2text pillow pymupdf
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# PyMuPDF import
# ---------------------------------------------------------------------------
try:
    import pymupdf as fitz  # type: ignore
except ImportError:
    try:
        import fitz  # type: ignore
    except ImportError:
        print(
            "ERROR: PyMuPDF is not installed.\n"
            "Install it with:  pip install pymupdf",
            file=sys.stderr,
        )
        raise SystemExit(2)

# ---------------------------------------------------------------------------
# Pix2Text import
# ---------------------------------------------------------------------------
try:
    from pix2text import Pix2Text  # type: ignore
    from PIL import Image  # type: ignore
except ImportError:
    print(
        "ERROR: pix2text or Pillow is not installed.\n"
        "Install with:  pip install pix2text pillow",
        file=sys.stderr,
    )
    raise SystemExit(2)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Font-name substrings that indicate LaTeX math fonts.
# Matched case-insensitively against each span's font name.
MATH_FONT_MARKERS = (
    "cmmi",   # Computer Modern Math Italic  (variables like x, y, p)
    "cmex",   # Computer Modern Math Extension (∑, ∫, large brackets)
    "cmsy",   # Computer Modern Symbol (∗, ·, ≤, ≥, ±, ∞ …)
    "msam",   # AMS Symbol font A
    "msbm",   # AMS Symbol font B (Blackboard bold ℝ, ℤ …)
    "matha",  # MathTime Pro / similar
    "mathb",
    "symbol", # Generic Symbol font
    "stmary", # St Mary Road symbols
)

# Unicode chars that are almost never body text and strongly indicate math.
MATH_CHARS = set(
    "∑∫∂∏∇∆∞±≤≥≠≈≡∈∉⊂⊃⊆⊇∪∩∧∨¬→←↔⇒⇐⇔"
    "αβγδεζηθικλμνξπρστυφχψω"
    "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΠΡΣΤΥΦΧΨΩ"
    "∗·×÷⊗⊕"
)

# Regex that matches a standalone LaTeX equation-number like (1) or (2.3).
_EQ_NUMBER_RE = re.compile(r"^\s*\(\d+(?:\.\d+)?\)\s*$")

# Page-section header written by extract_pdf.py.
_PAGE_SEP_RE = re.compile(
    r"^={72}\nPAGE (\S+)\s+\(index (\d+),.+\)\n={72}",
    re.MULTILINE,
)

RENDER_DPI = 300           # pixels per inch for equation crops
MERGE_GAP_PT = 14.0        # merge equation rects within this many points
EDGE_PAD_PT = 4.0          # padding added around detected rect before render

OUTPUT_TEXT_FILENAME = "extract_math.txt"
OUTPUT_MANIFEST_FILENAME = "math_manifest.json"
OUTPUT_LOG_FILENAME = "math_log.txt"
EQ_IMAGES_SUBDIR = "equation_images"


# ---------------------------------------------------------------------------
# Pix2Text singleton (loaded lazily once)
# ---------------------------------------------------------------------------
_p2t: Pix2Text | None = None


def _get_p2t() -> Pix2Text:
    global _p2t
    if _p2t is None:
        print(
            "  Loading Pix2Text model …"
            " (first run downloads ~155 MB to %APPDATA%\\pix2text\\)"
        )
        _p2t = Pix2Text.from_config()
        print("  Pix2Text model ready.")
    return _p2t


# ---------------------------------------------------------------------------
# Stage 2 — Equation region detector
# ---------------------------------------------------------------------------

def _span_is_math(span: dict) -> bool:
    """Return True if a text span looks like it belongs to a math equation."""
    font: str = span.get("font", "") or ""
    font_lower = font.lower()
    if any(m in font_lower for m in MATH_FONT_MARKERS):
        return True
    text: str = span.get("text", "") or ""
    if any(ch in MATH_CHARS for ch in text):
        return True
    return False


def _block_is_equation_number(text: str) -> bool:
    """Return True if the block text is just an equation label like (1)."""
    return bool(_EQ_NUMBER_RE.match(text.strip()))


def _block_is_centered(block_bbox: tuple, page_width: float,
                        col_ranges: list[tuple[float, float]] | None) -> bool:
    """
    Return True if the block is horizontally centered relative to the page
    or to whichever column it sits in.
    """
    bx0, _, bx1, _ = block_bbox[:4]
    block_mid = (bx0 + bx1) / 2.0

    if col_ranges:
        # Find the column this block falls inside.
        for col_x0, col_x1 in col_ranges:
            if bx0 >= col_x0 - 10 and bx1 <= col_x1 + 10:
                col_mid = (col_x0 + col_x1) / 2.0
                return abs(block_mid - col_mid) < 30.0
    # Fall back to page-level centering test.
    page_mid = page_width / 2.0
    return abs(block_mid - page_mid) < 35.0


def detect_equation_blocks(
    page,
    detect_display: bool = True,
    detect_inline: bool = True,
    merge_gap_pt: float = MERGE_GAP_PT,
    col_ranges: list[tuple[float, float]] | None = None,
) -> list[dict]:
    """
    Scan a PDF page and return a list of detected equation records.

    Each record is a dict:
        {
            "rect":       fitz.Rect,   # equation bounding box (padded)
            "is_display": bool,        # True = display equation; False = inline
            "raw_text":   str,         # text PyMuPDF extracted from that region
        }
    """
    try:
        page_dict = page.get_text("dict")
    except Exception:
        return []

    blocks = page_dict.get("blocks", []) if isinstance(page_dict, dict) else []
    page_w = page.rect.width
    page_h = page.rect.height

    # --- Per-span math detection -------------------------------------------
    math_rects: list[fitz.Rect] = []

    for block in blocks:
        if block.get("type") != 0:   # skip image blocks
            continue
        block_bbox = block.get("bbox")
        if not block_bbox:
            continue

        lines = block.get("lines", [])
        block_text = "".join(
            span.get("text", "")
            for line in lines
            for span in line.get("spans", [])
        ).strip()

        # Reject full prose paragraphs (> 350 chars or > 5 lines) from being treated as standalone equations
        if len(block_text) > 350 or len(lines) > 5:
            continue

        block_rect = fitz.Rect(block_bbox)

        # Signal 1: any span inside this block uses a math font.
        has_math_span = any(
            _span_is_math(span)
            for line in lines
            for span in line.get("spans", [])
        )

        # Signal 2: block is horizontally centered (display equation).
        is_centered = _block_is_centered(block_bbox, page_w, col_ranges)

        # Signal 3: block text is just an equation number (1), (2), …
        is_eq_number = _block_is_equation_number(block_text)

        # Signal 4: block contains known garbled math artifacts.
        # A single isolated capital letter on its own line in the middle of
        # the page is characteristic of a misread summation symbol (∑ → X).
        is_isolated_glyph = (
            is_centered
            and len(block_text.strip()) <= 3
            and block_text.strip().isalpha()
            and not block_text.strip().lower() in {"a", "i"}  # common inline words
        )

        # Decide whether to flag this block as a display equation candidate.
        is_math_block = False
        if detect_display and (is_eq_number or is_isolated_glyph):
            is_math_block = True
        elif detect_display and has_math_span and (is_centered or len(lines) <= 3):
            # Only flag short blocks with math font spans
            is_math_block = True

        if is_math_block:
            math_rects.append(fitz.Rect(block_bbox))

    if not math_rects:
        return []

    # --- Merge nearby rects -------------------------------------------------
    # Pad each rect by merge_gap_pt, then union overlapping padded rects.
    merged: list[fitz.Rect] = []
    for r in sorted(math_rects, key=lambda rect: rect.y0):
        padded = fitz.Rect(
            r.x0 - merge_gap_pt,
            r.y0 - merge_gap_pt,
            r.x1 + merge_gap_pt,
            r.y1 + merge_gap_pt,
        )
        found = False
        for i, existing in enumerate(merged):
            if existing.intersects(padded):
                merged[i] = existing | r   # union without the padding
                found = True
                break
        if not found:
            merged.append(fitz.Rect(r))

    # --- Classify each merged rect and collect its raw text ----------------
    results: list[dict] = []
    for rect in merged:
        # Add rendering padding (smaller than merge gap).
        padded_rect = fitz.Rect(
            max(0.0, rect.x0 - EDGE_PAD_PT),
            max(0.0, rect.y0 - EDGE_PAD_PT),
            min(page_w, rect.x1 + EDGE_PAD_PT),
            min(page_h, rect.y1 + EDGE_PAD_PT),
        )

        # Determine display vs inline.
        rect_mid_x = (padded_rect.x0 + padded_rect.x1) / 2.0
        is_display = _block_is_centered(
            (padded_rect.x0, padded_rect.y0, padded_rect.x1, padded_rect.y1),
            page_w, col_ranges,
        )

        # Collect the text PyMuPDF already extracted from this region
        # (the garbled version — we keep it for the manifest).
        try:
            raw_text = page.get_text("text", clip=padded_rect) or ""
        except Exception:
            raw_text = ""

        results.append(
            {
                "rect": padded_rect,
                "is_display": is_display,
                "raw_text": raw_text.strip(),
            }
        )

    return results


# ---------------------------------------------------------------------------
# Stage 3 — Region renderer
# ---------------------------------------------------------------------------

def render_equation_region(page, rect: fitz.Rect, dpi: int = RENDER_DPI) -> "Image.Image":
    """Render a PDF page region to a greyscale PIL Image at the given DPI."""
    matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pix = page.get_pixmap(
        matrix=matrix,
        clip=rect,
        colorspace=fitz.csGRAY,
        alpha=False,
    )
    img_bytes = pix.tobytes("png")
    return Image.open(io.BytesIO(img_bytes)).convert("RGB")


# ---------------------------------------------------------------------------
# Stage 4 — Pix2Text OCR wrapper
# ---------------------------------------------------------------------------

def ocr_equation(pil_image: "Image.Image", is_display: bool) -> str:
    """
    Pass a PIL Image to Pix2Text and return a LaTeX string.

    Display equations are wrapped in $$...$$.
    Inline equations are wrapped in $...$.
    Returns empty string on failure.
    """
    p2t = _get_p2t()
    try:
        latex: str = p2t.recognize_formula(pil_image)
    except Exception as exc:
        print(f"    ! Pix2Text error: {exc}", file=sys.stderr)
        return ""
    latex = (latex or "").strip()
    if not latex:
        return ""
    if is_display:
        return f"$$\n{latex}\n$$"
    return f"${latex}$"


# ---------------------------------------------------------------------------
# Stage 5 — Map equation bboxes → lines in extract.txt
# ---------------------------------------------------------------------------

def _get_page_column_ranges(layout_path: Path | None, page_index: int) -> list[tuple[float, float]]:
    """Load column x-ranges for a page from layout.json if it exists."""
    if layout_path is None or not layout_path.exists():
        return []
    try:
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        for entry in layout:
            if entry.get("page") == page_index + 1:
                cols = entry.get("columns", [])
                return [(c["x0"], c["x1"]) for c in cols]
    except Exception:
        pass
    return []


def _text_blocks_for_page(page) -> list[tuple[fitz.Rect, str]]:
    """Return list of (rect, text) for every text block on the page."""
    try:
        blocks_raw = page.get_text("blocks")
    except Exception:
        return []
    result = []
    for b in blocks_raw:
        if len(b) < 5:
            continue
        if b[6] != 0:   # skip image blocks (type=1)
            continue
        rect = fitz.Rect(b[:4])
        text = (b[4] or "").strip()
        if text:
            result.append((rect, text))
    return result


def build_page_line_map(extract_txt: str) -> list[dict]:
    """
    Parse extract.txt into a list of page records, each containing:
        {
            "page_index": int,   # 0-based
            "page_label": str,
            "start_line": int,   # 0-based line index in the file
            "end_line":   int,   # exclusive
            "lines":      list[str],
        }
    """
    all_lines = extract_txt.splitlines(keepends=True)
    records: list[dict] = []

    sep_indices: list[tuple[int, str, int]] = []  # (line_index, label, page_index)
    i = 0
    while i < len(all_lines):
        # Look for the 3-line separator block:
        #   === ... ===\n
        #   PAGE N  (index M, ...)\n
        #   === ... ===\n
        if (
            all_lines[i].startswith("=" * 72)
            and i + 2 < len(all_lines)
            and all_lines[i + 2].startswith("=" * 72)
        ):
            header = all_lines[i + 1]
            m = re.match(r"PAGE (\S+)\s+\(index (\d+),", header)
            if m:
                label = m.group(1)
                index = int(m.group(2))
                sep_indices.append((i, label, index))
                i += 3
                continue
        i += 1

    for k, (sep_line, label, page_index) in enumerate(sep_indices):
        start = sep_line + 3   # first content line after separator
        end = sep_indices[k + 1][0] if k + 1 < len(sep_indices) else len(all_lines)
        records.append(
            {
                "page_index": page_index,
                "page_label": label,
                "start_line": sep_line,      # include the separator itself
                "end_line": end,
                "content_start": start,
                "lines": all_lines[sep_line:end],
            }
        )

    return records, all_lines


def _lines_overlapping_rect(
    page,
    eq_rect: fitz.Rect,
    content_lines: list[str],
    content_start_abs: int,   # absolute 0-based index of first content line
) -> tuple[int, int] | None:
    """
    Find which consecutive lines in content_lines correspond to eq_rect
    by matching their text against the raw text extracted from that rect.

    Returns (rel_start, rel_end) — indices relative to content_lines —
    or None if no match is found.
    """
    try:
        region_text = page.get_text("text", clip=eq_rect) or ""
    except Exception:
        return None

    region_text = region_text.strip()
    if not region_text:
        return None

    # Build a version of the region text stripped of whitespace for fuzzy matching.
    import difflib

    region_norm = re.sub(r"\s+", " ", region_text.strip()).lower()
    if not region_norm:
        return None

    # Slide a window over content_lines looking for the best match.
    max_window = min(15, len(content_lines))
    best_score = 0.0
    best_span: tuple[int, int] | None = None

    for start in range(len(content_lines)):
        for window in range(1, max_window + 1):
            end = start + window
            if end > len(content_lines):
                break
            window_text = "".join(content_lines[start:end])
            window_norm = re.sub(r"\s+", " ", window_text.strip()).lower()
            if not window_norm:
                continue

            ratio = difflib.SequenceMatcher(None, region_norm, window_norm).ratio()
            if ratio > best_score and ratio >= 0.45:
                best_score = ratio
                best_span = (start, end)

    return best_span



# ---------------------------------------------------------------------------
# Stage 6 — Save equation images (optional)
# ---------------------------------------------------------------------------

def save_equation_image(
    images_dir: Path,
    page_number: int,
    eq_index: int,
    pil_image: "Image.Image",
    latex: str,
) -> None:
    images_dir.mkdir(parents=True, exist_ok=True)
    stem = f"page{page_number:03d}_eq{eq_index:02d}"
    png_path = images_dir / f"{stem}.png"
    tex_path = images_dir / f"{stem}.tex"
    try:
        pil_image.save(png_path.as_posix())
        tex_path.write_text(latex, encoding="utf-8")
    except OSError as exc:
        print(f"  ! Could not save equation image: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main processing function
# ---------------------------------------------------------------------------

def process(
    pdf_path: Path,
    extract_txt_path: Path,
    output_dir: Path,
    dpi: int = RENDER_DPI,
    save_images: bool = False,
    detect_display: bool = True,
    detect_inline: bool = True,
) -> int:
    """
    Full pipeline.  Returns 0 on success, 1 on error.
    """
    # Open PDF
    try:
        doc = fitz.open(pdf_path.as_posix())
    except Exception as exc:
        print(f"ERROR: Could not open PDF '{pdf_path}': {exc}", file=sys.stderr)
        return 1

    # Load extract.txt
    try:
        raw_txt = extract_txt_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: Could not read '{extract_txt_path}': {exc}", file=sys.stderr)
        doc.close()
        return 1

    # Layout info (optional — used for column-aware centering test)
    layout_path = extract_txt_path.parent / "layout.json"

    # Parse extract.txt into per-page records.
    page_records, all_lines = build_page_line_map(raw_txt)

    output_dir.mkdir(parents=True, exist_ok=True)
    eq_images_dir = output_dir / EQ_IMAGES_SUBDIR if save_images else None

    manifest: list[dict] = []
    log_lines: list[str] = []

    # We will build the patched output as a mutable list of lines,
    # then apply replacements in reverse order to preserve indices.
    patched_lines: list[str] = list(all_lines)
    replacements: list[tuple[int, int, str]] = []   # (abs_start, abs_end, new_text)

    total_equations = 0
    started = time.time()

    for rec in page_records:
        page_index = rec["page_index"]
        page_label = rec["page_label"]

        if page_index >= doc.page_count:
            log_lines.append(f"[page {page_label}] index {page_index} out of range — skipped")
            continue

        page = doc[page_index]
        col_ranges = _get_page_column_ranges(layout_path, page_index)
        content_lines = rec["lines"]   # includes separator header lines
        content_start_abs = rec["start_line"]

        # Detect equation blocks on this page.
        eq_blocks = detect_equation_blocks(
            page,
            detect_display=detect_display,
            detect_inline=detect_inline,
            col_ranges=col_ranges,
        )

        if not eq_blocks:
            log_lines.append(f"[page {page_label}] no equations detected")
            continue

        log_lines.append(f"[page {page_label}] {len(eq_blocks)} equation(s) detected")

        for eq_idx, eq in enumerate(eq_blocks, start=1):
            rect: fitz.Rect = eq["rect"]
            is_display: bool = eq["is_display"]
            raw_text: str = eq["raw_text"]

            # Render the equation region.
            try:
                pil_img = render_equation_region(page, rect, dpi=dpi)
            except Exception as exc:
                log_lines.append(
                    f"  eq{eq_idx}: render failed: {exc} — skipped"
                )
                continue

            # OCR the image.
            latex = ocr_equation(pil_img, is_display=is_display)
            if not latex:
                log_lines.append(
                    f"  eq{eq_idx}: OCR returned empty — kept original text"
                )
                status = "ocr_empty"
            else:
                status = "ok"
                total_equations += 1
                log_lines.append(
                    f"  eq{eq_idx}: {'display' if is_display else 'inline'} — "
                    f"OCR'd → {latex[:80].replace(chr(10),' ')} …"
                )

            # Save image (optional).
            if save_images and eq_images_dir is not None:
                save_equation_image(
                    eq_images_dir, page_index + 1, eq_idx,
                    pil_img, latex or raw_text,
                )

            # Map the equation rect → lines in extract.txt.
            # content_lines includes the separator header (first 3 lines).
            body_lines = content_lines[3:]   # skip === / PAGE N / === lines
            body_start_abs = content_start_abs + 3

            span = _lines_overlapping_rect(page, rect, body_lines, body_start_abs)
            if span is None:
                log_lines.append(
                    f"  eq{eq_idx}: could not map to text lines — skipped replacement"
                )
                status = "no_match"
            elif latex:
                rel_start, rel_end = span
                abs_start = body_start_abs + rel_start
                abs_end = body_start_abs + rel_end
                replacements.append((abs_start, abs_end, latex + "\n"))
                log_lines.append(
                    f"  eq{eq_idx}: will replace lines {abs_start}–{abs_end}"
                )

            manifest.append(
                {
                    "page": page_index + 1,
                    "page_label": page_label,
                    "eq_index": eq_idx,
                    "is_display": is_display,
                    "bbox_pt": [
                        round(rect.x0, 1), round(rect.y0, 1),
                        round(rect.x1, 1), round(rect.y1, 1),
                    ],
                    "original_text": raw_text,
                    "latex": latex,
                    "status": status,
                }
            )

    doc.close()

    # Filter out overlapping replacements
    filtered_replacements: list[tuple[int, int, str]] = []
    replacements.sort(key=lambda t: (t[0], t[1]))
    last_end = -1
    for start, end, text in replacements:
        if start >= last_end:
            filtered_replacements.append((start, end, text))
            last_end = end

    # Apply replacements in reverse order so later replacements don't
    # shift the indices of earlier ones.
    filtered_replacements.sort(key=lambda t: t[0], reverse=True)
    for abs_start, abs_end, new_text in filtered_replacements:
        patched_lines[abs_start:abs_end] = [new_text]

    # Write outputs.
    out_txt = output_dir / OUTPUT_TEXT_FILENAME
    out_manifest = output_dir / OUTPUT_MANIFEST_FILENAME
    out_log = output_dir / OUTPUT_LOG_FILENAME

    out_txt.write_text("".join(patched_lines), encoding="utf-8")
    out_manifest.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Rebuild LLM document (extract_llm.md) with updated LaTeX math
    try:
        import build_llm_document
        build_llm_document.build_llm_markdown(
            pdf_path,
            output_dir,
            output_dir / "extract_llm.md",
            output_dir / "llm_summary.json",
        )
    except Exception as exc:
        log_lines.append(f"  ! build_llm_document update failed: {exc}")

    elapsed = time.time() - started
    log_lines.append(f"\nDone in {elapsed:.1f}s — {total_equations} equations OCR'd")
    out_log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    print(f"\n{'=' * 72}")
    print("SUMMARY")
    print(f"{'=' * 72}")
    print(f"Pages processed     : {len(page_records)}")
    print(f"Equations OCR'd     : {total_equations}")
    print(f"Output text         : {out_txt}")
    print(f"LLM Document        : {output_dir / 'extract_llm.md'}")
    print(f"Manifest            : {out_manifest}")
    print(f"Log                 : {out_log}")
    if save_images and eq_images_dir:
        imgs = list(eq_images_dir.glob("*.png"))
        print(f"Equation images     : {len(imgs)} files in {eq_images_dir}")
    print(f"Elapsed             : {elapsed:.2f}s")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_extract_txt(pdf_path: Path) -> Path:
    return pdf_path.parent / f"{pdf_path.stem}_extracted" / "extract.txt"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Math-OCR post-processor: replace garbled equations in "
                    "extract.txt with proper LaTeX using Pix2Text.",
    )
    parser.add_argument("pdf", type=Path, help="Path to the input PDF file.")
    parser.add_argument(
        "--extract-txt",
        type=Path,
        default=None,
        help="Path to existing extract.txt. "
             "Defaults to <pdf-stem>_extracted/extract.txt.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to write outputs. Defaults to the same directory "
             "as extract.txt.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=RENDER_DPI,
        help=f"Render DPI for equation image crops (default: {RENDER_DPI}).",
    )
    parser.add_argument(
        "--save-eq-images",
        action="store_true",
        help="Save cropped equation PNGs and .tex files to "
             "<output-dir>/equation_images/.",
    )
    parser.add_argument(
        "--no-display-eq",
        action="store_true",
        help="Skip display equation detection.",
    )
    parser.add_argument(
        "--no-inline-eq",
        action="store_true",
        help="Skip inline equation detection.",
    )

    args = parser.parse_args(argv)

    extract_txt = args.extract_txt or _default_extract_txt(args.pdf)
    output_dir = args.output_dir or extract_txt.parent

    if not args.pdf.exists():
        print(f"ERROR: PDF not found: {args.pdf}", file=sys.stderr)
        return 1
    if not extract_txt.exists():
        print(
            f"ERROR: extract.txt not found at '{extract_txt}'.\n"
            "Run extract_pdf.py first to generate it.",
            file=sys.stderr,
        )
        return 1

    print(f"PDF             : {args.pdf}")
    print(f"Extract text    : {extract_txt}")
    print(f"Output dir      : {output_dir}")
    print(f"Render DPI      : {args.dpi}")
    print(f"Save eq images  : {args.save_eq_images}")
    print()

    return process(
        pdf_path=args.pdf,
        extract_txt_path=extract_txt,
        output_dir=output_dir,
        dpi=args.dpi,
        save_images=args.save_eq_images,
        detect_display=not args.no_display_eq,
        detect_inline=not args.no_inline_eq,
    )


if __name__ == "__main__":
    raise SystemExit(main())

