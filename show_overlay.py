"""Print a concise summary of text_overlay per image in the manifest."""
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("overlay_extracted/extract_images/manifest.json")
manifest = json.loads(manifest_path.read_text())

for entry in manifest:
    page = entry["page"]
    xref = entry["xref"]
    overlay = entry["text_overlay"]
    on_top = [b["text"] for b in overlay["on_top"]]
    beside = overlay["beside"]
    parts = []
    if on_top:
        parts.append(f"on_top={on_top}")
    for side, items in beside.items():
        if items:
            parts.append(f"beside.{side}=[{', '.join(b['text'][:40] for b in items)}]")
    print(f"page {page} xref={xref}: {' | '.join(parts) if parts else '(no associated text)'}")