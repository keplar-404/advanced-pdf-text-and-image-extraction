"""Verify that the extracted images match their expected source colors
and that the manifest exists / has the expected schema.

Usage:
    python test_extract.py [extracted_images_dir]
        default: extracted_sample/extract_images
"""
import json
import sys
from pathlib import Path

from PIL import Image

EXPECTED = {
    "page001_img01_xref8.png": (220, 50, 50),
    "page002_img01_xref16.png": (40, 170, 80),
    "page002_img02_xref18.png": (40, 80, 220),
}

images_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("extracted_sample/extract_images")
output_dir = images_dir.parent

all_ok = True

# --- 1. Verify image bytes ---
for fn, exp_color in EXPECTED.items():
    path = images_dir / fn
    if not path.exists():
        print(f"{fn}: MISSING ({path})")
        all_ok = False
        continue
    img = Image.open(path)
    cx, cy = img.width // 2, img.height // 2
    pixel = img.getpixel((cx, cy))
    close = all(abs(pixel[i] - exp_color[i]) < 10 for i in range(3))
    all_ok &= close
    print(f"{fn}: {img.size} center={pixel} expected~{exp_color} OK={close}")

# --- 2. Verify manifest schema ---
manifest_path = images_dir / "manifest.json"
if not manifest_path.exists():
    print(f"manifest.json: MISSING ({manifest_path})")
    all_ok = False
else:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert isinstance(manifest, list), "manifest must be a list"
        assert manifest, "manifest must not be empty"
        first = manifest[0]
        for key in ("path", "xref", "page", "page_label", "width", "height",
                    "first_occurrence_only", "nearby_caption"):
            assert key in first, f"manifest entry missing key {key!r}"
        print(f"manifest.json: schema OK ({len(manifest)} entries)")
    except Exception as exc:
        print(f"manifest.json: SCHEMA FAILED ({exc})")
        all_ok = False

# --- 3. Verify text file exists and is non-empty ---
text_path = output_dir / "extract.txt"
if not text_path.exists():
    print(f"extract.txt: MISSING ({text_path})")
    all_ok = False
else:
    text = text_path.read_text(encoding="utf-8")
    if "PAGE 1" in text and "PAGE 2" in text and "PAGE 3" in text:
        print(f"extract.txt: OK ({len(text)} chars, page markers found)")
    else:
        print(f"extract.txt: MISSING page markers")
        all_ok = False

print("ALL OK" if all_ok else "FAILED")