#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""layout_bench.py -- measure what the layout pass changes, on a given PDF.

Two conditions are compared on exactly the same glyph set:

  stream : the order in which the PDF content stream emits the glyphs. This is
           what a naive extractor (`pdftotext`, most "read the PDF" prompts)
           produces: it walks left column line 1, right column line 1, left
           column line 2, ... and it keeps running heads and folios.
  skill  : the order produced by pdf_struct (bands -> columns -> top-down).

Metrics, all computed from coordinates so they are objective:

  cross_column_lines   share of text lines whose glyphs lie on both sides of a
                       detected gutter (columns glued into one line)
  column_switch_rate   share of consecutive line pairs that jump between
                       columns  (interleaved reading)
  order_inversions     share of consecutive pairs that move *upwards* inside
                       one column
  marginalia_leak      running heads / journal banners / folios that survive
                       into the body stream
  chars, lines         size of the compared material

Usage
  python3 layout_bench.py --pdf paper.pdf [--out-dir DIR] [--json]

Numbers are printed as a Markdown table; nothing is estimated or simulated.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pdf_struct as P  # noqa: E402


def _lines_in_stream_order(page, used_ids):
    """Group items into lines without reordering the stream (baseline model)."""
    lines, cur = [], []
    for it in page.items:
        if it.kind != "text" or not it.text.strip() or it.idx in used_ids:
            continue
        if cur and abs(it.cy - statistics.median([c.cy for c in cur])) <= 0.5 * max(4.0, it.size):
            cur.append(it)
        else:
            if cur:
                lines.append(cur)
            cur = [it]
    if cur:
        lines.append(cur)
    out = []
    for grp in lines:
        grp.sort(key=lambda i: i.x0)
        x0 = min(i.x0 for i in grp)
        x1 = max(i.x1 for i in grp)
        y0 = min(i.y0 for i in grp)
        y1 = max(i.y1 for i in grp)
        out.append(P.Line(page=page.number, x0=x0, y0=y0, x1=x1, y1=y1,
                          text=" ".join(i.text.strip() for i in grp), size=grp[0].size))
    return out


def _metrics(lines, gutters, body, banner_keys, page_h, spanning_ok=False):
    if not lines:
        return {"lines": 0, "cross_column_lines": 0.0, "column_switch_rate": 0.0,
                "order_inversions": 0.0, "marginalia_leak": 0, "chars": 0}
    cross = 0
    for ln in lines:
        for g0, g1 in gutters:
            if ln.x1 > g1 and ln.x0 < g0:
                # a deliberately spanning element (title, full-width table) is
                # legitimate; a glued line is one whose fragments sit on both
                # sides without the builder having marked it as spanning
                if spanning_ok and len(getattr(ln, "frags", [])) > 1:
                    left = sum(1 for f in ln.frags if f[1] <= g0)
                    right = sum(1 for f in ln.frags if f[0] >= g1)
                    if left >= 2 and right >= 2:
                        cross += 1
                elif not spanning_ok:
                    cross += 1
                break

    def col(ln):
        return P.column_of(ln, gutters) if gutters else 0

    switches = sum(1 for a, b in zip(lines, lines[1:]) if col(a) != col(b) and col(a) >= 0 and col(b) >= 0)
    inversions = sum(1 for a, b in zip(lines, lines[1:])
                     if col(a) == col(b) and col(a) >= 0 and b.y0 < a.y0 - 1)
    leak = 0
    for ln in lines:
        t = P.norm_space(ln.text)
        if not t:
            continue
        top = ln.y1 <= 0.09 * page_h
        bot = ln.y0 >= 0.885 * page_h
        if (top or bot) and (P.FOLIO_RE.match(t) or P.BANNER_RE.search(t)
                             or P.norm_key(t) in banner_keys):
            leak += 1
    n = len(lines)
    return {"lines": n,
            "cross_column_lines": round(cross / n, 3),
            "column_switch_rate": round(switches / max(1, n - 1), 3),
            "switches": switches,
            "order_inversions": round(inversions / max(1, n - 1), 3),
            "marginalia_leak": leak,
            "chars": sum(len(ln.text) for ln in lines)}


def bench(pdf: str) -> dict:
    probe = P.probe_pdf(pdf)
    body = 10.0
    xml = os.path.join("/tmp", f"_bench_{os.getpid()}.xml")
    cp = P.run(["pdftohtml", "-xml", "-hidden", "-i", "-q", pdf, xml])
    if not os.path.exists(xml):
        raise SystemExit(f"pdftohtml failed: {cp.stderr[:200]}")
    pages = P.parse_pdftohtml_xml(xml)
    body = P.body_font_size(pages)
    for pg in pages:
        pg.body = body
    eqs = P.detect_equations(pages, body)
    used = {it.idx for e in eqs for it in e.items}

    agg_stream = {"lines": 0, "cross_column_lines": 0.0, "column_switch_rate": 0.0,
                  "order_inversions": 0.0, "marginalia_leak": 0, "chars": 0, "switches": 0}
    agg_skill = {k: 0.0 for k in agg_stream}
    gutter_pages = 0
    per_page = []
    for pg in pages:
        lines1 = P.build_lines([it for it in pg.items if it.idx not in used])
        bl = [l for l in lines1 if abs(l.size - body) <= 0.06 * body
              and 0.055 * pg.h < l.y0 < 0.945 * pg.h]
        if len(bl) < 8:
            hist = {}
            for l in lines1:
                hist[round(l.size, 1)] = hist.get(round(l.size, 1), 0) + len(l.text)
            if hist:
                local = max(hist.items(), key=lambda kv: kv[1])[0]
                bl = [l for l in lines1 if abs(l.size - local) <= 0.06 * local
                      and 0.055 * pg.h < l.y0 < 0.945 * pg.h]
        guts = P.detect_gutters(bl, pg.w, pg.h)
        if guts:
            gutter_pages += 1
        lines2 = P.build_lines([it for it in pg.items if it.idx not in used],
                               gap_factor=2.2, gutters=guts)
        stream_lines = _lines_in_stream_order(pg, used)
        banner_keys = set()
        marg = P.detect_marginalia([pg], {pg.number: lines2}, body)
        drop = marg["drop"].get(pg.number, set())
        clean = [ln for i, ln in enumerate(lines2) if i not in drop]
        m_stream = _metrics(stream_lines, guts, body, banner_keys, pg.h)
        m_skill = _metrics(P.order_page(clean, guts), guts, body, banner_keys, pg.h,
                           spanning_ok=True)
        for k in agg_stream:
            agg_stream[k] += m_stream[k]
            agg_skill[k] += m_skill[k]
        per_page.append({"page": pg.number, "gutters": [[round(a), round(b)] for a, b in guts],
                         "stream": m_stream, "skill": m_skill})
    for agg in (agg_stream, agg_skill):
        n = max(1, agg["lines"])
        for k in ("cross_column_lines", "column_switch_rate", "order_inversions"):
            agg[k] = round(agg[k] / len(pages), 3)
    return {"pdf": os.path.abspath(pdf), "pages": len(pages), "body_font": body,
            "pages_with_gutter": gutter_pages, "stream": agg_stream,
            "skill": agg_skill, "per_page": per_page}


def to_markdown(res: dict) -> str:
    s, k = res["stream"], res["skill"]
    def pct(x):
        return f"{100 * x:.1f}%"
    rows = [
        ("lines with glyphs on both sides of the gutter (columns glued)",
         pct(s["cross_column_lines"]), pct(k["cross_column_lines"])),
        ("column switches between consecutive lines (total; ~2 per band boundary is normal)",
         str(int(s["switches"])), str(int(k["switches"]))),
        ("order inversions inside a column (text read upwards)",
         pct(s["order_inversions"]), pct(k["order_inversions"])),
        ("running heads / banners / folios left in the body",
         str(int(s["marginalia_leak"])), str(int(k["marginalia_leak"]))),
        ("lines offered to the reader", str(int(s["lines"])), str(int(k["lines"]))),
        ("characters offered to the reader", str(int(s["chars"])), str(int(k["chars"]))),
    ]
    out = [f"### {os.path.basename(res['pdf'])}",
           "",
           f"pages: {res['pages']} | body font: {res['body_font']}pt | "
           f"pages with a detected gutter: {res['pages_with_gutter']}",
           "",
           "| metric | stream order (baseline) | skill order |",
           "| --- | --- | --- |"]
    out += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    res = bench(args.pdf)
    md = to_markdown(res)
    print(md)
    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        with open(os.path.join(args.out_dir, "layout_bench.json"), "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
        with open(os.path.join(args.out_dir, "layout_bench.md"), "w", encoding="utf-8") as fh:
            fh.write(md + "\n")
    if args.json:
        print(json.dumps({k: res[k] for k in ("pages", "pages_with_gutter", "stream", "skill")},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
