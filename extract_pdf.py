"""
extract_pdf.py — Extract text and images from a PDF using PyMuPDF.

Usage:
    python extract_pdf.py <input.pdf> [--output-dir OUTPUT_DIR] [--min-dim PIXELS]

Outputs (relative to --output-dir, defaults to a sibling folder next to the PDF):
    extract.txt          : All extracted text, with clear page separators.
    extract_images/      : All embedded images from the PDF.
        page{N}_img{M}_{xref}.{ext}     : One file per image, with the source
                                           page baked into the filename so the
                                           origin is unambiguous.
        manifest.json     : Machine-readable list of every extracted image
                           (path, page, xref, width, height, ext, etc.).
    extract_log.txt      : Human-readable log of what was extracted and any
                           issues that were skipped.

Dependencies:
    pip install pymupdf

Tested with PyMuPDF >= 1.24 (works with the modern `pymupdf` import name).
Falls back to the legacy `fitz` import if `pymupdf` isn't available.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Library import — PyMuPDF exposes itself under either `pymupdf` (new) or
# `fitz` (legacy).  We try the modern name first, then fall back so the
# script keeps working on older installs.
# ---------------------------------------------------------------------------
try:
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover
    try:
        import fitz  # type: ignore
    except ImportError as exc:  # pragma: no cover
        print(
            "ERROR: PyMuPDF is not installed.\n"
            "Install it with:  pip install pymupdf",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TEXT_FILENAME = "extract.txt"
IMAGES_SUBDIR = "extract_images"
TABLES_SUBDIR = "tables"
LOG_FILENAME = "extract_log.txt"
MANIFEST_FILENAME = "manifest.json"

TEXT_PAGE_SEPARATOR = "\n".join(
    ["=" * 72, "PAGE {page}  (index {index}, file page {label})", "=" * 72, ""]
)


# ---------------------------------------------------------------------------
# Core extraction helpers
# ---------------------------------------------------------------------------
def _open_pdf(path: Path):
    """Open the PDF, raising a clean error if it can't be read."""
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")
    if path.is_dir():
        raise IsADirectoryError(f"Expected a PDF file, got a directory: {path}")
    try:
        return fitz.open(path.as_posix())
    except Exception as exc:  # pragma: no cover - very thin wrapper
        raise RuntimeError(f"PyMuPDF could not open '{path}': {exc}") from exc


def _iter_images_on_page(doc, page) -> Iterable[tuple]:
    """Yield (xref, base_info) tuples for every image on `page`.

    Uses ``Page.get_images(full=True)`` so we keep the xref for de-duplication
    and downstream extraction via ``Document.extract_image(xref)``.
    """
    try:
        return page.get_images(full=True)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  ! get_images failed on page {page.number}: {exc}", file=sys.stderr)
        return []


# Regex that captures a "Figure N", "Table N", "Fig. N", etc. caption
# opener.  We anchor on the colon form (e.g. "Figure 1:") so that the
# in-body text "in Figure 1 we show" doesn't get mistaken for a caption.
_CAPTION_NEEDLES = ("Figure", "Fig.", "Table", "Tab.")
_CAPTION_RE = None  # built lazily (re module import below)


_NON_BREAKING_TRANS = str.maketrans("\xa0\u2002\u00a0", "   ")


def _build_caption_regex():
    global _CAPTION_RE
    if _CAPTION_RE is not None:
        return _CAPTION_RE
    import re

    parts = "|".join(re.escape(n) for n in _CAPTION_NEEDLES)
    _CAPTION_RE = re.compile(
        rf"({parts})\s*([\d\.]+)\s*[:\.\-\s]\s*([^\n]{{0,200}})",
        re.IGNORECASE,
    )
    return _CAPTION_RE


def _find_nearby_caption(page, img_rect) -> dict | None:
    """Look for a "Figure N: …" caption adjacent to an image on a page.

    PDF layout quirks mean a "Model Architecture" heading on page N may
    actually have its image anchored to page N+1 (LaTeX float placement).
    So we can't trust the page-level association: we look at the *spatial*
    relationship between the image and the caption text on the page.

    Strategy:
      1. Get all text words on the page, sorted top-to-bottom.
      2. For each caption-shaped hit (e.g. "Figure 1:"), check whether
         the line sits within `img_rect`'s vertical band, OR just below /
         just above it (within 60 pt).  The closest match wins.
      3. Return the matched caption line + the relative position, or None.
    """
    if img_rect is None:
        return None
    try:
        words = page.get_text("words")  # [x0,y0,x1,y1,word,block,line,word_no]
    except Exception:
        return None
    if not words:
        return None

    regex = _build_caption_regex()
    raw_text = page.get_text("text") or ""
    page_text_full = raw_text.translate(_NON_BREAKING_TRANS)
    if not page_text_full:
        return None

    # Build per-line captions by scanning the full text with regex.
    candidates = []
    for m in regex.finditer(page_text_full):
        num_str = m.group(2).split(".")[0]
        num_val = int(num_str) if num_str.isdigit() else 0
        candidates.append(
            {
                "kind": m.group(1),
                "number": num_val,
                "snippet": (m.group(0) or "").strip().replace("\n", " "),
            }
        )
    if not candidates:
        return None

    # Find which words sit on the same line as each candidate's first word.
    # Approximate: search for the first word of the candidate in the words
    # list and grab the bbox of that line.
    # We do a soft match: take the first 4+ chars of the caption snippet.
    best = None
    best_dist = float("inf")
    img_y0 = img_rect.y0
    img_y1 = img_rect.y1

    for cand in candidates:
        snippet = cand["snippet"]
        # Pick a distinctive fragment: "Figure 1" or "Fig. 1" etc.
        prefix = f"{cand['kind']} {cand['number']}"
        prefix_norm = prefix.replace(" ", "").lower()

        line_bbox = None
        for w in words:
            word = w[4]
            if not word:
                continue
            # match either with or without space
            if word.lower().startswith(prefix_norm[:5]) or word.replace(
                " ", ""
            ).lower().startswith(prefix_norm.replace(" ", "")[:5]):
                # Build the line bbox: take this word + next ~6 same-line words
                same_line = []
                for w2 in words:
                    if abs(w2[1] - w[1]) < 2 and abs(w2[0] - w[0]) < 200:
                        same_line.append(w2)
                same_line.sort(key=lambda r: r[0])
                xs0 = min(r[0] for r in same_line)
                ys0 = min(r[1] for r in same_line)
                xs1 = max(r[2] for r in same_line)
                ys1 = max(r[3] for r in same_line)
                line_bbox = fitz.Rect(xs0, ys0, xs1, ys1)
                break
        if line_bbox is None:
            continue

        # Distance from caption line to image rect (vertical, with overlap = 0).
        if line_bbox.y1 < img_y0:
            dist = img_y0 - line_bbox.y1
            rel = "above"
        elif line_bbox.y0 > img_y1:
            dist = line_bbox.y0 - img_y1
            rel = "below"
        else:
            dist = 0.0
            rel = "adjacent"

        if dist > 60.0:
            # Caption is more than 60pt away from this image; ignore.
            continue

        if dist < best_dist:
            best_dist = dist
            best = {
                "kind": cand["kind"],
                "number": cand["number"],
                "snippet": cand["snippet"],
                "caption_rect": [line_bbox.x0, line_bbox.y0, line_bbox.x1, line_bbox.y1],
                "image_rect": [img_rect.x0, img_rect.y0, img_rect.x1, img_rect.y1],
                "relative_position": rel,
                "distance_pt": round(dist, 1),
            }

    return best


def _image_bbox_for_xref(page, xref):
    """Return the (first) image rect for a given xref on this page, or None.

    Uses ``Page.get_image_rects(xref)`` when available (it considers Form
    XObjects too) and falls back to ``Page.get_image_bbox(xref)``.
    """
    try:
        rects = page.get_image_rects(xref)
        if rects:
            return rects[0]
    except Exception:
        pass
    try:
        bbox = page.get_image_bbox(xref)
        # get_image_bbox can return INFINITE_RECT for dead refs.
        if bbox is None or not bbox.is_finite:
            return None
        return bbox
    except Exception:
        return None


def _collect_overlay_text(page, img_rect, max_distance: float = 24.0) -> dict:
    """For an image on a page, classify nearby text as either "on_top" or
    "beside" the image, so a downstream agent can reason about overlays.

    Definition:
      on_top   — the text bbox intersects the image bbox (text drawn
                 literally on top of the image, or just inside its frame).
      beside   — the text bbox sits *adjacent* to the image bbox on the
                 left, right, top, or bottom — within `max_distance` pt.

    Returns:
      {
        "on_top": [ {text, bbox, ...}, ... ],
        "beside": {
          "left":   [ ... ],
          "right":  [ ... ],
          "above":  [ ... ],
          "below":  [ ... ],
        },
      }

    Empty lists mean no text in that category.  An LLM can then answer
    questions like "what caption sits beside this image?" or "what label
    is on top of this chart bar?" without doing any geometry of its own.
    """
    result = {"on_top": [], "beside": {"left": [], "right": [], "above": [], "below": []}}
    if img_rect is None:
        return result
    try:
        d = page.get_text("dict")
    except Exception:
        return result
    blocks = d.get("blocks", []) if isinstance(d, dict) else []

    for block in blocks:
        if block.get("type") != 0:  # skip image blocks
            continue
        bbox = block.get("bbox")
        if not bbox:
            continue
        text_rect = fitz.Rect(*bbox)
        text = "".join(
            span["text"] for line in block.get("lines", []) for span in line.get("spans", [])
        ).strip()
        if not text:
            continue

        # On-top: text bbox intersects image bbox (with a 2pt tolerance).
        shrunk = fitz.Rect(
            img_rect.x0 - 2, img_rect.y0 - 2, img_rect.x1 + 2, img_rect.y1 + 2
        )
        if text_rect.intersects(shrunk):
            result["on_top"].append(
                {
                    "text": text,
                    "bbox": [round(c, 1) for c in text_rect],
                }
            )
            continue

        # Beside: text sits within max_distance pt of the image on one side.
        # Find the smallest gap to any side and use that as the side.  We
        # name the side from the *text's* point of view (i.e. "right" means
        # the text sits to the right of the image), which is what a human
        # looking at the layout would call it.  Gaps are non-negative only
        # when the text is *actually* on that side of the image.
        gaps = {
            "right":  text_rect.x0 - img_rect.x1,
            "left":   img_rect.x0 - text_rect.x1,
            "below":  text_rect.y0 - img_rect.y1,
            "above":  img_rect.y0 - text_rect.y1,
        }
        # Only consider sides where the text is genuinely outside the image.
        gaps = {k: v for k, v in gaps.items() if v >= 0}
        if not gaps:
            continue  # shouldn't happen since we already filtered "on_top"
        side, dist = min(gaps.items(), key=lambda kv: kv[1])
        if dist > max_distance:
            continue
        # Only count it as beside on the chosen side if the text actually
        # overlaps the image's perpendicular span (otherwise it's a totally
        # separate text block).
        if side in ("left", "right"):
            vertical_overlap = not (text_rect.y1 < img_rect.y0 or text_rect.y0 > img_rect.y1)
            if not vertical_overlap:
                continue
        else:
            horizontal_overlap = not (text_rect.x1 < img_rect.x0 or text_rect.x0 > img_rect.x1)
            if not horizontal_overlap:
                continue
        result["beside"][side].append(
            {
                "text": text,
                "bbox": [round(c, 1) for c in text_rect],
                "distance_pt": round(dist, 1),
            }
        )

    # Sort each list top-to-bottom / left-to-right for readability.
    for key in ("on_top",):
        result[key].sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
    for side, items in result["beside"].items():
        if side in ("above", "below"):
            items.sort(key=lambda b: b["bbox"][0])
        else:
            items.sort(key=lambda b: b["bbox"][1])
    return result


def _recover_image(doc, img_info) -> dict | None:
    """Reconstruct the image bytes, handling SMask + colorspace quirks.

    Returns a dict like ``Document.extract_image(xref)`` or ``None`` if the
    xref cannot be extracted (e.g. it's an inline image).
    """
    xref = img_info[0]
    try:
        base = doc.extract_image(xref)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  ! extract_image({xref}) failed: {exc}", file=sys.stderr)
        return None
    if not base:
        return None

    smask = base.get("smask", 0)
    if smask:
        # The image has a soft mask (transparency).  Combine them via Pixmap
        # so the saved PNG keeps its alpha channel.
        try:
            pix = fitz.Pixmap(doc, xref)
            mask = fitz.Pixmap(doc, smask)
            combined = fitz.Pixmap(pix, mask)  # adds the alpha channel
            # Reuse the metadata from base; only swap the bytes.
            base = dict(base)
            base["image"] = combined.tobytes("png")
            base["ext"] = "png"
            # We deliberately drop `smask` since it's now baked in.
            base["smask"] = 0
            pix = mask = combined = None  # free
        except Exception as exc:  # pragma: no cover - defensive
            print(
                f"  ! could not apply smask for xref={xref}: {exc}; "
                "saving without alpha.",
                file=sys.stderr,
            )
    return base


def extract_text(doc, log) -> str:
    """Walk every page and return the combined plain-text body."""
    parts: list[str] = []
    for index, page in enumerate(doc):
        # PyMuPDF accepts the page's logical label (e.g. "vii", "1") via
        # ``get_label``; fall back to 1-based numbering if it isn't set.
        try:
            label = page.get_label() or str(index + 1)
        except Exception:  # pragma: no cover
            label = str(index + 1)

        try:
            text = page.get_text("text") or ""
        except Exception as exc:  # pragma: no cover - defensive
            log.append(f"[text] page {index + 1}: extraction failed: {exc}")
            text = ""

        parts.append(TEXT_PAGE_SEPARATOR.format(page=label, index=index, label=label))
        parts.append(text)
        if not text.endswith("\n"):
            parts.append("\n")
        log.append(f"[text] page {label}: {len(text)} chars")

    return "".join(parts).rstrip() + "\n"


def extract_images(doc, images_dir: Path, log, min_dim: int) -> list[dict]:
    """Extract every embedded image to ``images_dir``.

    Returns a manifest list — each entry carries the page number and on-disk
    path so the caller can rebuild the page-to-image mapping.
    """
    images_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    seen_xrefs: set[int] = set()
    page_image_counts: dict[int, int] = {}

    for index, page in enumerate(doc):
        try:
            label = page.get_label() or str(index + 1)
        except Exception:  # pragma: no cover
            label = str(index + 1)

        infos = _iter_images_on_page(doc, page)
        if not infos:
            log.append(f"[img ] page {label}: no images")
            continue

        for raw_info in infos:
            xref = raw_info[0]
            # Width / height come back as item[2] / item[3] from get_images().
            width = raw_info[2]
            height = raw_info[3]

            # Filter out tiny "stencil mask" / pseudo images.
            if min(width, height) < min_dim:
                log.append(
                    f"[img ] page {label} xref={xref}: skipped "
                    f"(size {width}x{height} < min {min_dim})"
                )
                continue

            # Avoid saving the same physical image multiple times — it can be
            # referenced from many pages.  We still record each occurrence
            # below so the manifest lists every page the image appears on.
            already_saved = xref in seen_xrefs

            if not already_saved:
                image = _recover_image(doc, raw_info)
                if image is None:
                    log.append(
                        f"[img ] page {label} xref={xref}: skipped "
                        "(could not extract bytes)"
                    )
                    continue

                ext = image.get("ext", "png")
                page_image_counts[index] = page_image_counts.get(index, 0) + 1
                sequence = page_image_counts[index]

                filename = f"page{index + 1:03d}_img{sequence:02d}_xref{xref}.{ext}"
                out_path = images_dir / filename
                try:
                    out_path.write_bytes(image["image"])
                except OSError as exc:
                    log.append(
                        f"[img ] page {label} xref={xref}: "
                        f"failed to write '{out_path}': {exc}"
                    )
                    continue

                seen_xrefs.add(xref)
                log.append(
                    f"[img ] page {label} xref={xref}: saved "
                    f"{filename} ({width}x{height}, {len(image['image'])} bytes)"
                )
                img_rect = _image_bbox_for_xref(page, xref)
                caption = _find_nearby_caption(page, img_rect)
                overlay = _collect_overlay_text(page, img_rect)
                manifest.append(
                    {
                        "path": str(out_path.relative_to(images_dir.parent)),
                        "xref": xref,
                        "page": index + 1,
                        "page_label": label,
                        "width": width,
                        "height": height,
                        "ext": ext,
                        "size_bytes": len(image["image"]),
                        "colorspace": image.get("colorspace"),
                        "has_mask": bool(image.get("smask")),
                        "image_rect": (
                            [round(c, 1) for c in img_rect]
                            if img_rect is not None
                            else None
                        ),
                        "first_occurrence_only": True,
                        "nearby_caption": caption,
                        "text_overlay": overlay,
                    }
                )
            else:
                log.append(
                    f"[img ] page {label} xref={xref}: already saved, "
                    "recording additional reference"
                )
                img_rect = _image_bbox_for_xref(page, xref)
                caption = _find_nearby_caption(page, img_rect)
                overlay = _collect_overlay_text(page, img_rect)
                manifest.append(
                    {
                        "path": None,  # points at the xref entry above
                        "xref": xref,
                        "page": index + 1,
                        "page_label": label,
                        "width": width,
                        "height": height,
                        "image_rect": (
                            [round(c, 1) for c in img_rect]
                            if img_rect is not None
                            else None
                        ),
                        "first_occurrence_only": False,
                        "nearby_caption": caption,
                        "text_overlay": overlay,
                    }
                )

    return manifest


# ---------------------------------------------------------------------------
# Vector-figure extraction (Figures 3, 4, 5 in research.pdf, etc.)
# ---------------------------------------------------------------------------
#
# A *lot* of PDFs draw figures as vector graphics (lines, polygons, text)
# rather than embedding a raster image.  PyMuPDF's ``page.get_images()``
# only finds raster images, so those figures are invisible to the normal
# pass.  ``page.get_drawings()`` returns every vector primitive on the
# page, so we can:
#
#   1. Decide whether the page looks "figure-y" (lots of drawings, large
#      total coverage, no raster image).
#   2. Cluster the drawing rects into spatial regions — each region is
#      almost certainly a figure (or part of one).
#   3. Render each region as a PNG via ``page.get_pixmap(clip=region)``
#      so a downstream agent can view it.
#   4. Append the rendered PNG to the manifest with ``kind:
#      "vector_render"`` so the agent can tell them apart from true
#      raster extractions.
#
# Heuristics tuned against research.pdf and similar LaTeX papers:
#   - a region is a figure if it has >= MIN_DRAWINGS_PER_REGION drawings
#     and >= MIN_REGION_AREA in² of coverage
#   - we skip regions that overlap a raster image we already saved (no
#     point duplicating it)
#   - we also skip tiny regions (sub-pixel text rendering artefacts)
VECTOR_FIGURE_MIN_DRAWINGS = 3  # paths
VECTOR_FIGURE_MIN_ITEMS = 3  # primitive items inside those paths
VECTOR_FIGURE_MIN_AREA = 1500  # square points
VECTOR_FIGURE_MIN_REGION_DIM = 30  # pixels


def _is_body_text_block(text: str, block: tuple) -> bool:
    """True if block is long body paragraph text rather than a diagram label."""
    h = block[3] - block[1]
    return len(text) > 180 or h > 80


def _is_header_or_footer(block: tuple, page_h: float) -> bool:
    """True if block is in the top 5% or bottom 5% of the page."""
    return block[1] < page_h * 0.05 or block[3] > page_h * 0.95


def _collect_figure_regions_from_captions(page, padding: float = 10.0) -> list[dict]:
    """Collect vector figure regions anchored on 'Figure X.Y' captions.

    Finds caption blocks, collects associated text labels and vector drawing
    paths sitting above/near the caption in the same column zone, and returns
    consolidated figure bounding boxes.
    """
    import re

    cap_pattern = re.compile(r"^(Figure|Fig\.)\s*([\d\.]+)", re.IGNORECASE)
    page_h = page.rect.height
    page_w = page.rect.width

    try:
        all_blocks = sorted(page.get_text("blocks"), key=lambda b: b[1])
    except Exception:
        return []

    try:
        drawings = page.get_drawings() or []
    except Exception:
        drawings = []

    captions = []
    for b in all_blocks:
        text = (b[4] or "").translate(_NON_BREAKING_TRANS).strip().replace("\n", " ")
        m = cap_pattern.match(text)
        if m and not _is_body_text_block(text, b) and not _is_header_or_footer(b, page_h):
            captions.append({
                "block": b,
                "text": text,
                "bbox": fitz.Rect(b[:4]),
            })

    if not captions:
        return []

    results = []
    for cap in captions:
        cap_rect = cap["bbox"]
        figure_rects = [cap_rect]
        drawing_count = 0

        # Collect text labels above the caption in the same column zone
        for b in all_blocks:
            text = (b[4] or "").translate(_NON_BREAKING_TRANS).strip().replace("\n", " ")
            bbox = fitz.Rect(b[:4])
            if bbox == cap_rect:
                continue
            if _is_header_or_footer(b, page_h):
                continue
            if _is_body_text_block(text, b):
                continue
            if b[1] >= cap_rect.y1 - 5:
                continue
            if b[0] - cap_rect.x1 > 80 or cap_rect.x0 - b[2] > 80:
                continue
            figure_rects.append(bbox)

        # Collect drawing rects above the caption in the same column zone
        for d in drawings:
            r = d.get("rect")
            if not r:
                continue
            if r.x0 - cap_rect.x1 > 80 or cap_rect.x0 - r.x1 > 80:
                continue
            if r.y0 >= cap_rect.y1 - 5:
                continue
            figure_rects.append(r)
            drawing_count += 1

        combined = figure_rects[0]
        for r in figure_rects[1:]:
            combined |= r

        clip = fitz.Rect(
            max(0.0, combined.x0 - padding),
            max(0.0, combined.y0 - padding),
            min(page_w, combined.x1 + padding),
            min(page_h, combined.y1 + padding),
        )

        results.append({
            "rect": clip,
            "count": drawing_count,
            "caption": cap["text"],
            "caption_rect": [round(c, 1) for c in cap_rect],
        })

    return results


def _cluster_drawings_into_regions(page, padding: float = 6.0) -> list[dict]:
    """Cluster all vector drawings on a page into spatial regions (fallback)."""
    drawings = page.get_drawings() or []
    rects = [d["rect"] for d in drawings if "rect" in d]
    if not rects:
        return []

    regions: list[dict] = []
    for r in rects:
        padded = fitz.Rect(
            r.x0 - padding, r.y0 - padding, r.x1 + padding, r.y1 + padding
        )
        merged = False
        for reg in regions:
            if reg["rect"].intersects(padded):
                reg["rect"] |= r
                reg["count"] += 1
                merged = True
                break
        if not merged:
            regions.append({"rect": fitz.Rect(r), "count": 1})

    regions.sort(key=lambda r: (r["rect"].y0, r["rect"].x0))
    return regions


def _has_raster_image_already(page, region_rect) -> bool:
    """True if a raster image on the page already covers most of this region."""
    try:
        region_area = region_rect.width * region_rect.height
        if region_area <= 0:
            return False
        for info in page.get_images(full=True):
            xref = info[0]
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                continue
            for rect in rects or []:
                if rect.intersects(region_rect):
                    intersection = rect & region_rect
                    inter_area = intersection.width * intersection.height
                    # Skip rendering if raster covers >70% of region
                    if inter_area / region_area > 0.7:
                        return True
    except Exception:
        pass
    return False


def extract_vector_figures(
    doc,
    images_dir: Path,
    log,
    dpi: int = 200,
) -> list[dict]:
    """Render vector-only figures to PNGs and append them to the manifest."""
    images_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    seen_region_hashes: set[tuple] = set()

    for index, page in enumerate(doc):
        try:
            label = page.get_label() or str(index + 1)
        except Exception:
            label = str(index + 1)

        # 1. Try caption-anchored figure extraction
        caption_regions = _collect_figure_regions_from_captions(page)
        
        if caption_regions:
            regions = caption_regions
        else:
            # 2. Fallback: drawing path clustering if page has drawings
            try:
                drawings = page.get_drawings() or []
            except Exception:
                drawings = []
            if len(drawings) < VECTOR_FIGURE_MIN_DRAWINGS:
                continue
            regions = _cluster_drawings_into_regions(page)

        for seq, region in enumerate(regions, start=1):
            rect = region["rect"]
            count = region.get("count", 0)
            caption_text = region.get("caption")
            width_pt = rect.width
            height_pt = rect.height

            if (width_pt * height_pt) < VECTOR_FIGURE_MIN_AREA:
                continue
            if width_pt < VECTOR_FIGURE_MIN_REGION_DIM or height_pt < VECTOR_FIGURE_MIN_REGION_DIM:
                continue
            if _has_raster_image_already(page, rect):
                continue

            key = (round(rect.x0, 1), round(rect.y0, 1),
                   round(rect.x1, 1), round(rect.y1, 1))
            if key in seen_region_hashes:
                continue
            seen_region_hashes.add(key)

            try:
                pix = page.get_pixmap(matrix=matrix, clip=rect, alpha=False)
            except Exception as exc:
                log.append(
                    f"[vec ] page {label} region {seq}: render failed: {exc}"
                )
                continue

            filename = f"page{index + 1:03d}_vec{seq:02d}.png"
            out_path = images_dir / filename
            try:
                pix.save(out_path.as_posix())
            except OSError as exc:
                log.append(
                    f"[vec ] page {label} region {seq}: "
                    f"failed to write '{out_path}': {exc}"
                )
                continue

            log.append(
                f"[vec ] page {label} region {seq}: rendered "
                f"{filename} from {width_pt:.0f}x{height_pt:.0f}pt region "
                f"({count} drawings, {pix.width}x{pix.height}px @ {dpi}dpi)"
            )

            nearby_cap = None
            if caption_text:
                nearby_cap = {
                    "snippet": caption_text,
                    "caption_rect": region.get("caption_rect"),
                    "image_rect": [round(c, 1) for c in rect],
                    "relative_position": "below",
                    "distance_pt": 0.0,
                }
            else:
                nearby_cap = _find_nearby_caption(page, rect)

            entry = {
                "kind": "vector_render",
                "path": str(out_path.relative_to(images_dir.parent)),
                "xref": None,
                "page": index + 1,
                "page_label": label,
                "width": pix.width,
                "height": pix.height,
                "ext": "png",
                "size_bytes": out_path.stat().st_size,
                "source_bbox_pt": [round(rect.x0, 1), round(rect.y0, 1),
                                   round(rect.x1, 1), round(rect.y1, 1)],
                "drawing_count": count,
                "rendered_at_dpi": dpi,
                "first_occurrence_only": True,
                "nearby_caption": nearby_cap,
                "text_overlay": _collect_overlay_text(page, rect),
            }
            manifest.append(entry)

    return manifest


# ---------------------------------------------------------------------------
# Table detection (vector-gridlined tables, real data tables)
# ---------------------------------------------------------------------------
#
# ``page.find_tables()`` is PyMuPDF's built-in table detector.  It works
# on both raster tables (pictures of spreadsheets) and vector-gridlined
# tables — exactly the case the manual text extraction cannot recover.
# We:
#
#   1. Call find_tables() with relaxed settings so even slightly-noisy
#      grids are detected.
#   2. Extract each table's cells to a Markdown table for LLM-friendly
#      consumption.
#   3. Write each Markdown file to tables/page{N}_table{M}.md.
#   4. Record the table in the manifest with bbox + structure metadata.
def _table_to_markdown(table) -> str:
    """Convert a PyMuPDF Table object to a Markdown string."""
    try:
        rows = table.extract()  # list[list[str | None]]
    except Exception:
        return ""
    md_lines = []
    for r, row in enumerate(rows):
        cells = [(c if c else "").replace("|", "\\|").replace("\n", " ")
                 for c in row]
        md_lines.append("| " + " | ".join(cells) + " |")
        if r == 0:
            md_lines.append("|" + "|".join(["---"] * len(cells)) + "|")
    return "\n".join(md_lines)


def _looks_like_real_table(page, table, row_count: int, col_count: int) -> bool:
    """Sanity check: reject "tables" that are actually drawing grids.

    ``find_tables()`` gets fooled by dense grids of vector lines (e.g.
    the attention-head visualizations in research.pdf, which are 947
    small filled rectangles arranged in a grid).  We require three
    signals of "real table-ness":

      1. At least 30% of all cells contain non-whitespace text.
      2. At least 30% of rows contain at least one non-empty cell.
      3. The average token length per non-empty cell is >= 3 chars.
         (Word grids in attention figures are 1-2 chars per cell —
         a single letter like "It" or "is".  Real data tables contain
         longer cells like "Alice", "92", "WSJ only, discriminative".)
    """
    try:
        rows = table.extract()
    except Exception:
        return False
    if not rows:
        return False
    total_cells = max(sum(len(r) for r in rows), 1)
    non_empty_cells = [
        c for row in rows for c in row if c and c.strip()
    ]
    if not non_empty_cells:
        return False
    non_empty = len(non_empty_cells)
    rows_with_content = sum(
        1 for row in rows if any(c and c.strip() for c in row)
    )
    rows_total = len(rows)
    avg_chars = sum(len(c.strip()) for c in non_empty_cells) / non_empty
    return (
        (non_empty / total_cells) >= 0.30
        and (rows_with_content / rows_total) >= 0.30
        and avg_chars >= 3.0
    )


def extract_tables(doc, tables_dir: Path, log) -> list[dict]:
    """Detect tables on every page and write them as Markdown."""
    tables_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []

    for index, page in enumerate(doc):
        try:
            label = page.get_label() or str(index + 1)
        except Exception:
            label = str(index + 1)
        try:
            finder = page.find_tables()
        except Exception as exc:
            log.append(f"[tbl ] page {label}: find_tables failed: {exc}")
            continue

        try:
            tables = list(finder.tables)
        except Exception:
            tables = []

        if not tables:
            log.append(f"[tbl ] page {label}: no tables")
            continue

        for seq, table in enumerate(tables, start=1):
            try:
                md = _table_to_markdown(table)
            except Exception as exc:
                log.append(f"[tbl ] page {label} #{seq}: extract failed: {exc}")
                continue
            if not md.strip():
                continue

            try:
                rows_for_check = table.extract()
                rc = len(rows_for_check)
                cc = max((len(r) for r in rows_for_check), default=0)
            except Exception:
                rc = cc = 0

            if not _looks_like_real_table(page, table, rc, cc):
                log.append(
                    f"[tbl ] page {label} #{seq}: rejected "
                    f"(looks like a drawing grid, not a data table)"
                )
                continue

            filename = f"page{index + 1:03d}_table{seq:02d}.md"
            out_path = tables_dir / filename
            try:
                out_path.write_text(md + "\n", encoding="utf-8")
            except OSError as exc:
                log.append(
                    f"[tbl ] page {label} #{seq}: failed to write '{out_path}': {exc}"
                )
                continue

            try:
                rows = table.extract()
                row_count = len(rows)
                col_count = max((len(r) for r in rows), default=0)
            except Exception:
                row_count = col_count = 0

            try:
                bbox = list(table.bbox)
                bbox = [round(c, 1) for c in bbox]
            except Exception:
                bbox = None

            log.append(
                f"[tbl ] page {label} #{seq}: saved {filename} "
                f"({row_count} rows × {col_count} cols)"
            )

            try:
                header_names = list(table.header.names)
            except Exception:
                header_names = None

            manifest.append(
                {
                    "kind": "table",
                    "path": str(out_path.relative_to(tables_dir.parent)),
                    "page": index + 1,
                    "page_label": label,
                    "bbox": bbox,
                    "row_count": row_count,
                    "col_count": col_count,
                    "header": header_names,
                    "ext": "md",
                }
            )

    return manifest


# ---------------------------------------------------------------------------
# Page-layout analysis (multi-column detection)
# ---------------------------------------------------------------------------
#
# A surprising amount of information lives in *how* a page is laid out,
# not what's drawn on it:
#
#   - "this page has 2 columns" tells an LLM that text from the two
#     columns should be interleaved row-by-row, not concatenated.
#   - "the page has a left column + a right sidebar" tells the LLM to
#     treat the sidebar as separate from body text.
#
# We compute columns by clustering text-block x0 positions: a column
# is a vertical strip whose text blocks share a left edge within a
# tolerance.  This is simple but works for ~90% of real cases.
def _detect_columns(page, x_tolerance: float = 24.0) -> list[dict]:
    """Detect vertical columns on a page.

    Returns a list of column descriptors:
        [{"x0": …, "x1": …, "y0": …, "y1": …, "block_count": …}, ...]
    sorted left-to-right.  An empty list means "single column".

    Algorithm:
      1. Drop the top-most text block(s) — they're typically a header /
         title that spans the full width and would otherwise force every
         column to start at the same y.  We use "top-most" to mean any
         block whose x-range covers > 60% of the page width and whose y0
         is in the top 30% of the page.
      2. Cluster the remaining blocks by x0 within tolerance.
      3. Each cluster with >= 2 blocks becomes a column.
      4. Keep only columns with width >= 60 pt that don't overlap the
         previous column by more than 30%.
    """
    MIN_BLOCKS_PER_COLUMN = 2
    MIN_COLUMN_WIDTH = 60  # pt
    MAX_HORIZONTAL_OVERLAP_FRACTION = 0.30

    try:
        d = page.get_text("dict")
    except Exception:
        return []
    blocks = d.get("blocks", []) if isinstance(d, dict) else []
    text_blocks = [
        b for b in blocks
        if b.get("type") == 0 and b.get("bbox")
    ]
    if len(text_blocks) < 2:
        return []

    page_w = page.rect.width
    page_h = page.rect.height

    # Drop wide top blocks (titles / banners) so they don't dominate the
    # y-coverage of every column.  A "wide top block" is one whose width
    # is meaningfully larger than the median text-block width (which
    # would mean it spans multiple columns).
    widths = sorted(b["bbox"][2] - b["bbox"][0] for b in text_blocks)
    median_w = widths[len(widths) // 2]
    body_blocks = []
    for b in text_blocks:
        bbox = b["bbox"]
        by = bbox[1]
        bw = bbox[2] - bbox[0]
        # A block is "wide top" if it's both wider than the median AND
        # sits in the top 30% of the page.
        if bw > median_w * 1.4 and by / page_h < 0.3:
            continue
        body_blocks.append(b)

    if len(body_blocks) < 2:
        return []

    # Cluster by x0 within tolerance.
    x0s = sorted(b["bbox"][0] for b in body_blocks)
    groups: list[list[float]] = [[x0s[0]]]
    for x in x0s[1:]:
        last = groups[-1][-1]
        if abs(x - last) <= x_tolerance:
            groups[-1].append(x)
        else:
            groups.append([x])

    if len(groups) < 2:
        return []
    cluster_x0s = [sum(g) / len(g) for g in groups]

    columns: list[dict] = []
    for cx in cluster_x0s:
        col_blocks = [b for b in body_blocks if abs(b["bbox"][0] - cx) <= x_tolerance]
        if len(col_blocks) < MIN_BLOCKS_PER_COLUMN:
            continue
        x0 = min(b["bbox"][0] for b in col_blocks)
        y0 = min(b["bbox"][1] for b in col_blocks)
        x1 = max(b["bbox"][2] for b in col_blocks)
        y1 = max(b["bbox"][3] for b in col_blocks)
        if (x1 - x0) < MIN_COLUMN_WIDTH:
            continue
        columns.append(
            {
                "x0": round(x0, 1),
                "y0": round(y0, 1),
                "x1": round(x1, 1),
                "y1": round(y1, 1),
                "block_count": len(col_blocks),
            }
        )

    if len(columns) < 2:
        return []
    columns.sort(key=lambda c: c["x0"])

    # Drop columns that overlap the previous one too much (likely part of
    # the same wide column).
    filtered = [columns[0]]
    for cur in columns[1:]:
        prev = filtered[-1]
        overlap = max(0.0, prev["x1"] - cur["x0"])
        width = min(prev["x1"] - prev["x0"], cur["x1"] - cur["x0"])
        if width > 0 and overlap / width > MAX_HORIZONTAL_OVERLAP_FRACTION:
            continue
        filtered.append(cur)

    return filtered if len(filtered) >= 2 else []


def extract_layout(doc, log) -> list[dict]:
    """Detect per-page layout: column count + per-column bboxes."""
    layout: list[dict] = []
    for index, page in enumerate(doc):
        try:
            label = page.get_label() or str(index + 1)
        except Exception:
            label = str(index + 1)
        try:
            columns = _detect_columns(page)
        except Exception as exc:
            log.append(f"[lay ] page {label}: column detection failed: {exc}")
            continue
        layout.append(
            {
                "page": index + 1,
                "page_label": label,
                "column_count": len(columns),
                "columns": columns,
            }
        )
        if columns:
            log.append(
                f"[lay ] page {label}: {len(columns)} columns detected"
            )
        else:
            log.append(f"[lay ] page {label}: single column")
    return layout


# ---------------------------------------------------------------------------
# Drawing-shape classification (hand-drawn, grid, flowchart, decorative)
# ---------------------------------------------------------------------------
#
# ``page.get_drawings()`` returns one entry per "path" — i.e. one closed
# set of strokes/fills that share properties.  We summarize these into
# shape types and label the page accordingly:
#
#   many_curves + few_lines    -> "sketch" / "hand-drawn"
#   many_horizontal_lines      -> "ruled grid" / "table" / "form"
#   many_lines + many_rects    -> "flowchart" / "diagram"
#   few + scattered            -> "decorative border" / "logo"
SHAPE_LABELS = {
    "sketch":     "sketch (curves + freeform strokes, likely hand-drawn)",
    "grid":       "ruled grid / lined paper",
    "flowchart":  "flowchart / diagram (boxes + connecting lines)",
    "decorative": "decorative shapes (borders, logos)",
    "unknown":    "other vector content",
}


def _classify_drawings(page) -> dict:
    """Return a coarse classification of the page's vector drawings.

    Each call to ``get_drawings()`` returns one entry per ``finish()``
    group, but that group may contain many primitive items (one path
    with 14 items == 14 strokes).  So we count *items*, not paths, when
    classifying — that's what reflects actual drawing density.
    """
    try:
        drawings = page.get_drawings() or []
    except Exception:
        return {"label": "unknown", "counts": {}, "summary": ""}
    if not drawings:
        return {"label": "none", "counts": {}, "summary": ""}

    counts: dict[str, int] = {"paths": len(drawings)}
    for d in drawings:
        items = d.get("items", [])
        for item in items:
            kind = item[0] if item else "?"
            if kind == "l":
                counts["lines"] = counts.get("lines", 0) + 1
            elif kind == "re":
                counts["rects"] = counts.get("rects", 0) + 1
            elif kind == "c":
                counts["curves"] = counts.get("curves", 0) + 1
            elif kind == "qu":
                counts["quads"] = counts.get("quads", 0) + 1
            else:
                counts["other"] = counts.get("other", 0) + 1

    total_items = sum(v for k, v in counts.items() if k != "paths")
    curves = counts.get("curves", 0)
    lines = counts.get("lines", 0)
    rects = counts.get("rects", 0)

    if curves >= 3 and curves >= lines // 2 and rects <= 2:
        label = "sketch"
    elif rects >= 2 and lines >= 5 and lines <= rects * 6:
        label = "flowchart"
    elif lines >= 8 and lines >= rects * 2:
        label = "grid"
    elif total_items <= 4:
        label = "decorative"
    else:
        label = "unknown"

    parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    return {
        "label": label,
        "label_description": SHAPE_LABELS.get(label, label),
        "counts": counts,
        "total_items": total_items,
        "summary": parts,
    }


def extract_drawing_summary(doc, log) -> list[dict]:
    """Per-page: classify the vector drawing content."""
    out: list[dict] = []
    for index, page in enumerate(doc):
        try:
            label = page.get_label() or str(index + 1)
        except Exception:
            label = str(index + 1)
        cls = _classify_drawings(page)
        cls["page"] = index + 1
        cls["page_label"] = label
        log.append(
            f"[drw ] page {label}: {cls.get('label','none')} "
            f"({cls.get('total_items', 0)} items: {cls.get('summary','')})"
        )
        out.append(cls)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _default_output_dir(pdf_path: Path) -> Path:
    """Place outputs next to the PDF, namespaced by the PDF's stem."""
    return pdf_path.parent / f"{pdf_path.stem}_extracted"


def _write_outputs(
    output_dir: Path,
    text: str,
    manifest: list[dict],
    log_lines: list[str],
    layout: list[dict],
    drawings: list[dict],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    text_path = output_dir / TEXT_FILENAME
    text_path.write_text(text, encoding="utf-8")

    manifest_path = output_dir / IMAGES_SUBDIR / MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    layout_path = output_dir / "layout.json"
    layout_path.write_text(
        json.dumps(layout, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    drawings_path = output_dir / "drawings.json"
    drawings_path.write_text(
        json.dumps(drawings, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log_path = output_dir / LOG_FILENAME
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract text and images from a PDF using PyMuPDF.",
    )
    parser.add_argument(
        "pdf",
        type=Path,
        help="Path to the input PDF file.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to write outputs to. Defaults to '<pdf-stem>_extracted' "
        "next to the PDF.",
    )
    parser.add_argument(
        "--min-dim",
        type=int,
        default=8,
        help="Skip images whose width or height is below this many pixels "
        "(filters out stencil masks / decorative pixels).",
    )
    args = parser.parse_args(argv)

    output_dir = args.output_dir or _default_output_dir(args.pdf)
    images_dir = output_dir / IMAGES_SUBDIR
    log: list[str] = []
    started = time.time()

    print(f"Opening '{args.pdf}' …")
    try:
        doc = _open_pdf(args.pdf)
    except (FileNotFoundError, IsADirectoryError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        page_count = doc.page_count
        log.append(
            f"PyMuPDF version: {getattr(fitz, '__version__', 'unknown')}"
        )
        log.append(f"Input PDF: {args.pdf}")
        log.append(f"Page count: {page_count}")
        log.append(f"Output dir: {output_dir}")
        log.append(f"Min image dimension: {args.min_dim}px")
        log.append("")

        print(f"  {page_count} pages")
        print("Extracting text …")
        text = extract_text(doc, log)

        print("Extracting raster images …")
        manifest = extract_images(doc, images_dir, log, args.min_dim)

        print("Rendering vector-only figures …")
        manifest.extend(extract_vector_figures(doc, images_dir, log))

        print("Detecting tables …")
        tables_dir = output_dir / TABLES_SUBDIR
        table_manifest = extract_tables(doc, tables_dir, log)
        manifest.extend(table_manifest)

        print("Analysing page layout (columns) …")
        layout = extract_layout(doc, log)

        print("Classifying vector drawings …")
        drawings = extract_drawing_summary(doc, log)

        _write_outputs(output_dir, text, manifest, log, layout, drawings)

        # Build LLM-friendly document (extract_llm.md)
        try:
            import build_llm_document
            print("Building LLM-friendly document (extract_llm.md) …")
            build_llm_document.build_llm_markdown(
                args.pdf,
                output_dir,
                output_dir / "extract_llm.md",
                output_dir / "llm_summary.json",
            )
        except Exception as exc:
            log.append(f"  ! build_llm_document failed: {exc}")
    finally:
        doc.close()

    elapsed = time.time() - started
    image_files = list(images_dir.glob("*"))
    image_files = [p for p in image_files if p.name != MANIFEST_FILENAME]
    table_files = list(tables_dir.glob("*.md"))

    summary = [
        "",
        "=" * 72,
        "SUMMARY",
        "=" * 72,
        f"Pages processed     : {page_count}",
        f"Images saved        : {len(image_files)} files in {images_dir}",
        f"Tables saved        : {len(table_files)} files in {tables_dir}",
        f"Text file           : {output_dir / TEXT_FILENAME}",
        f"LLM Document        : {output_dir / 'extract_llm.md'}",
        f"Manifest            : {images_dir / MANIFEST_FILENAME}",
        f"Layout              : {output_dir / 'layout.json'}",
        f"Drawing summary     : {output_dir / 'drawings.json'}",
        f"Log                 : {output_dir / LOG_FILENAME}",
        f"Elapsed             : {elapsed:.2f}s",
    ]
    log.extend(summary)
    (output_dir / LOG_FILENAME).write_text("\n".join(log) + "\n", encoding="utf-8")
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())