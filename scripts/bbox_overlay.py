#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bbox_overlay.py -- draw result.json evidence boxes on rendered pages.

This is the manual audit path: it renders page N as an image and draws the
bounding boxes recorded in `evidence`, so a reviewer can confirm that a field
really comes from that region of that page.

  python3 bbox_overlay.py --pdf paper.pdf --result result.json \
      --pages 1 3 --out-dir audit

Boxes are converted from PDF points to pixels with the requested DPI. Colour
encodes the target family: title (blue), abstract (green), sections (grey),
tables (orange), figures (purple), equations (red), references (teal).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

COLOURS = {
    "title": (30, 90, 200),
    "authors": (0, 140, 120),
    "abstract": (0, 150, 60),
    "sections": (110, 110, 110),
    "tables": (230, 130, 0),
    "figures": (150, 60, 200),
    "equations": (210, 40, 40),
    "references": (0, 120, 150),
}


def colour_for(key: str):
    for prefix, col in COLOURS.items():
        if key.startswith(prefix):
            return col
    return (80, 80, 80)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--result", required=True)
    ap.add_argument("--pages", nargs="*", type=int, default=[])
    ap.add_argument("--out-dir", default="audit")
    ap.add_argument("--dpi", type=int, default=110)
    args = ap.parse_args(argv)

    try:
        from PIL import Image, ImageDraw
    except Exception as exc:
        print(f"Pillow is required: {exc}")
        return 2

    res = json.load(open(args.result, encoding="utf-8"))
    ev = res.get("evidence") or []
    if not ev:
        print("no evidence rows to draw")
        return 1
    pages = args.pages or sorted({int(e["page"]) for e in ev})[:3]
    os.makedirs(args.out_dir, exist_ok=True)
    scale = args.dpi / 72.0

    for pno in pages:
        base = os.path.join(args.out_dir, f"page{pno}")
        cp = subprocess.run(["pdftoppm", "-f", str(pno), "-l", str(pno), "-r", str(args.dpi),
                             "-png", "-singlefile", args.pdf, base],
                            capture_output=True, text=True)
        png = base + ".png"
        if not os.path.exists(png):
            print(f"render failed for page {pno}: {cp.stderr[:200]}")
            continue
        im = Image.open(png).convert("RGB")
        dr = ImageDraw.Draw(im)
        n = 0
        for e in ev:
            if int(e["page"]) != pno:
                continue
            x0, y0, x1, y1 = e["block_bbox"]
            box = [x0 * scale, y0 * scale, x1 * scale, y1 * scale]
            col = colour_for(e["target_key"])
            dr.rectangle(box, outline=col, width=2)
            n += 1
        out = os.path.join(args.out_dir, f"page{pno}_evidence.png")
        im.save(out)
        print(f"page {pno}: {n} boxes -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
