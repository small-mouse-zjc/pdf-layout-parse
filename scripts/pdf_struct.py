#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_struct.py -- layout-aware PDF -> structured JSON (`result.json`).

What this script guarantees
---------------------------
1. Verbatim text.  Every character of the output comes from the PDF text layer
   through poppler; the model never retypes the paper.
2. Coordinate-decided reading order.  Columns are inferred from the body-text
   x-projection, bands from spanning lines, so two-column papers are read
   column by column instead of line by line across the gutter.
3. Marginalia and non-prose text separation: running heads, journal banners,
   folios, vertical arXiv stamps, figure-internal vector labels, table cells and
   display equations are classified before paragraphs are built.
4. Traceability.  Every emitted string is bound to (page, bbox) evidence.

Backends: poppler `pdftohtml -xml` (text + font size + glyph boxes + images),
`pypdf` (page census, encryption, garbled-text detection).  No LLM, no network.

Exit status is 0 whenever a verdict was produced, including reject/cannot_judge.
"""

from __future__ import annotations

import argparse
import collections
import glob
import html
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Iterable, Sequence

VERSION = "2.0.0"

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\u00a0\u2000-\u200a\u202f\u205f\u3000]+")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

MATH_FONT_RE = re.compile(
    r"(CMMI|CMSY|CMEX|CMR\d|CMBX|MSAM|MSBM|MSYM|STIX|Math|Symbol|Euclid|MTMI|MTSY|"
    r"TeX-?Math|Asana|LMMath|TXM|PXMath|NimbusSan.*Math)", re.I)
MATH_CHARS = set("=+−±×÷∑∫√≈≠≤≥<>∂∇∞→←↔∝∈∉⊂⊆∪∩⊗⊕⋅·′″^_{}[]|")


def unescape(s: str) -> str:
    return html.unescape(s)


def norm_space(s: str) -> str:
    s = CTRL_RE.sub("", s).replace("\u00ad", "")
    return WS_RE.sub(" ", s).strip()


def norm_key(s: str) -> str:
    s = norm_space(s).lower()
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"[^\w#\u4e00-\u9fff]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def r1(x: float) -> float:
    try:
        return round(float(x), 1)
    except Exception:
        return 0.0


def run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, errors="replace")


def have(binary: str) -> bool:
    from shutil import which
    return which(binary) is not None


def is_cjk(s: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", s))


# --------------------------------------------------------------------------
# 1. document probe
# --------------------------------------------------------------------------

@dataclass
class DocProbe:
    n_pages: int = 0
    encrypted: bool = False
    decrypt_ok: bool = False
    page_sizes: list = field(default_factory=list)
    chars_per_page: list = field(default_factory=list)
    images_per_page: list = field(default_factory=list)
    garbled_per_page: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    error: str = ""


def probe_pdf(path: str) -> DocProbe:
    p = DocProbe()
    try:
        import pypdf
    except Exception as exc:
        p.error = f"pypdf unavailable: {exc}"
        return p
    try:
        reader = pypdf.PdfReader(path, strict=False)
    except Exception as exc:
        p.error = f"cannot open pdf: {exc}"
        return p

    p.encrypted = bool(getattr(reader, "is_encrypted", False))
    if p.encrypted:
        try:
            p.decrypt_ok = reader.decrypt("") not in (0, None)
        except Exception:
            p.decrypt_ok = False
        if not p.decrypt_ok:
            try:
                p.n_pages = len(reader.pages)
            except Exception:
                p.n_pages = 0
            return p

    try:
        p.n_pages = len(reader.pages)
    except Exception as exc:
        p.error = f"page tree unreadable: {exc}"
        return p

    for page in reader.pages:
        try:
            box = page.mediabox
            p.page_sizes.append((float(box.width), float(box.height)))
        except Exception:
            p.page_sizes.append((0.0, 0.0))
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        p.chars_per_page.append(len(re.sub(r"\s+", "", txt)))
        p.garbled_per_page.append(len(re.findall(
            r"\(cid:\d+\)|\ufffd|/uni[0-9A-Fa-f]{6}", txt))
            + len(re.findall(r"[\ue000-\uf8ff]", txt)))
        nimg = 0
        try:
            res = page.get("/Resources")
            if res is not None:
                xo = res.get_object().get("/XObject")
                if xo is not None:
                    for _n, obj in xo.get_object().items():
                        try:
                            if obj.get_object().get("/Subtype") == "/Image":
                                nimg += 1
                        except Exception:
                            pass
        except Exception:
            pass
        p.images_per_page.append(nimg)
    try:
        md = reader.metadata or {}
        for k in ("/Title", "/Author", "/Subject", "/Producer", "/Creator"):
            if md.get(k):
                p.meta[k.lstrip("/").lower()] = str(md[k])
    except Exception:
        pass
    return p


# --------------------------------------------------------------------------
# 2. pdftohtml -xml -> items  (font table is DOCUMENT-global in poppler)
# --------------------------------------------------------------------------

@dataclass
class Item:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str = ""
    size: float = 10.0
    family: str = ""
    bold: bool = False
    italic: bool = False
    kind: str = "text"          # text | image
    idx: int = -1

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return 0.5 * (self.x0 + self.x1)

    @property
    def cy(self) -> float:
        return 0.5 * (self.y0 + self.y1)

    @property
    def is_math(self) -> bool:
        if MATH_FONT_RE.search(self.family or ""):
            return True
        t = self.text.strip()
        return bool(t) and len(t) <= 3 and all(c in MATH_CHARS for c in t)


@dataclass
class Page:
    number: int
    w: float
    h: float
    items: list = field(default_factory=list)
    body: float = 10.0


PAGE_TAG = re.compile(r"<page\b([^>]*)>", re.I)
ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
TEXT_TAG = re.compile(r"<text\b([^>]*)>(.*?)</text>", re.S | re.I)
IMG_TAG = re.compile(r"<image\b([^>]*)/?>", re.I)


def _attrs(s: str) -> dict:
    return {k: v for k, v in ATTR.findall(s)}


def _num(d: dict, k: str, default: float = 0.0) -> float:
    try:
        return float(d.get(k, default))
    except Exception:
        return default


def parse_pdftohtml_xml(xml_path: str) -> list[Page]:
    raw = open(xml_path, "r", encoding="utf-8", errors="replace").read()

    fonts: dict[str, tuple[float, str]] = {}
    for m in re.finditer(r"<fontspec\b([^>]*)/?>", raw, re.I):
        a = _attrs(m.group(1))
        if a.get("id") is not None:
            fonts[a["id"]] = (_num(a, "size", 10.0), a.get("family", "") or "")

    pages: list[Page] = []
    chunks = re.split(r"(?=<page\b)", raw)
    idx = 0
    for chunk in chunks:
        pm = PAGE_TAG.search(chunk)
        if not pm:
            continue
        pa = _attrs(pm.group(1))
        pg = Page(number=int(_num(pa, "number", len(pages) + 1)),
                  w=_num(pa, "width"), h=_num(pa, "height"))
        for tm in TEXT_TAG.finditer(chunk):
            ta = _attrs(tm.group(1))
            txt = unescape(TAG_RE.sub("", tm.group(2)))
            if not txt.strip():
                continue
            size, fam = fonts.get(ta.get("font", ""), (10.0, ""))
            low = fam.lower()
            pg.items.append(Item(
                page=pg.number,
                x0=_num(ta, "left"), y0=_num(ta, "top"),
                x1=_num(ta, "left") + _num(ta, "width"),
                y1=_num(ta, "top") + _num(ta, "height"),
                text=txt, size=size, family=fam,
                bold=bool(re.search(r"medi|bold|black|heavy|semib|demi", low)),
                italic=bool(re.search(r"ital|oblique|slanted", low)),
                idx=idx))
            idx += 1
        for im in IMG_TAG.finditer(chunk):
            ia = _attrs(im.group(1))
            w_, h_ = _num(ia, "width"), _num(ia, "height")
            if w_ <= 2 or h_ <= 2:
                continue
            pg.items.append(Item(page=pg.number, x0=_num(ia, "left"), y0=_num(ia, "top"),
                                 x1=_num(ia, "left") + w_, y1=_num(ia, "top") + h_,
                                 kind="image", idx=-1))
        if not pg.h:
            pg.h = 792.0
        if not pg.w:
            pg.w = 612.0
        pages.append(pg)
    pages.sort(key=lambda p: p.number)
    return pages


def body_font_size(pages: Sequence[Page]) -> float:
    hist: collections.Counter = collections.Counter()
    for pg in pages:
        for it in pg.items:
            if it.kind == "text" and it.text.strip():
                hist[round(it.size, 1)] += len(re.sub(r"\s", "", it.text))
    if not hist:
        return 10.0
    total = sum(hist.values())
    ranked = [(s, c) for s, c in hist.most_common(10) if c >= 0.02 * total] or hist.most_common(3)
    return float(max(ranked, key=lambda kv: kv[1])[0])


# --------------------------------------------------------------------------
# 3. lines
# --------------------------------------------------------------------------

@dataclass
class Line:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float
    bold: bool = False
    italic: bool = False
    frags: list = field(default_factory=list)      # [(x0, x1, text, size)]
    tag: str = ""                                   # body|title|heading|caption|...
    order: int = 0

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def cy(self) -> float:
        return 0.5 * (self.y0 + self.y1)

    @property
    def cx(self) -> float:
        return 0.5 * (self.x0 + self.x1)

    def bbox(self, scale: float = 1.0) -> list:
        return [r1(self.x0 * scale), r1(self.y0 * scale),
                r1(self.x1 * scale), r1(self.y1 * scale)]


def _vov(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def build_lines(items: Sequence[Item], gap_factor: float = 1.25,
                gutters: Sequence[tuple[float, float]] = ()) -> list[Line]:
    """Merge glyph runs into typographic lines; keep per-fragment geometry.

    gap_factor=1.25 keeps only obvious same-word fragments (first pass, used to
    find gutters).  A second pass with a larger factor re-attaches inline math
    and wide word spaces, but never across a gutter.
    """
    txt = [it for it in items if it.kind == "text" and it.text.strip()]
    txt = [it for it in txt if it.w > 0.4 and it.h > 0.8]      # drops rotated stamps
    txt.sort(key=lambda i: (round(i.y0, 1), i.x0))
    lines: list[Line] = []
    for it in txt:
        target = None
        for ln in reversed(lines[-14:]):
            v = _vov(it.y0, it.y1, ln.y0, ln.y1)
            if v <= 0:
                continue
            hmin = max(1.0, min(it.h, ln.h))
            if v / hmin < 0.45:
                small = it.h < 0.80 * ln.h
                if not (small and abs(it.cy - ln.cy) < 0.80 * ln.h):
                    continue
            gap = it.x0 - ln.x1
            limit = max(3.0, gap_factor * it.size)
            if gap > limit:
                prev_math = bool(ln.frags) and (
                    MATH_FONT_RE.search(ln.frags[-1][2]) or
                    (len(ln.frags[-1][2].strip()) <= 3 and
                     all(ch in MATH_CHARS for ch in ln.frags[-1][2].strip() or "x")))
                if not (it.is_math or prev_math) or gap > 14.0 * it.size:
                    continue
            if gap < -2.5:
                continue
            if gap > 1.4 * it.size:                     # never bridge a gutter
                if any(ln.x1 <= g1 + 2 and it.x0 >= g0 - 2 for g0, g1 in gutters):
                    continue
                if gap > 2.2 * it.size and abs(it.size - ln.size) > 0.05 * max(it.size, ln.size):
                    continue                            # artwork label beside text
            target = ln
            break
        if target is None:
            lines.append(Line(page=it.page, x0=it.x0, y0=it.y0, x1=it.x1, y1=it.y1,
                              text=it.text, size=it.size, bold=it.bold,
                              italic=it.italic,
                              frags=[(it.x0, it.x1, it.text, it.size)]))
        else:
            gap = it.x0 - target.x1
            sep = "" if gap <= 0.22 * max(6.0, it.size) else " "
            target.text = target.text + sep + it.text
            target.x1 = max(target.x1, it.x1)
            target.y0 = min(target.y0, it.y0)
            target.y1 = max(target.y1, it.y1)
            target.frags.append((it.x0, it.x1, it.text, it.size))
            if it.size > target.size + 0.4:
                target.size = it.size
            target.bold = target.bold or it.bold
            target.italic = target.italic or it.italic
    out = []
    for ln in lines:
        ln.text = norm_space(ln.text)
        if ln.text:
            ln.frags.sort(key=lambda f: f[0])
            out.append(ln)
    out.sort(key=lambda l: (l.y0, l.x0))
    return out


# --------------------------------------------------------------------------
# 4. display equations (detected on items, before paragraph assembly)
# --------------------------------------------------------------------------

EQ_NUM_RE = re.compile(r"^\(\s*(\d{1,3}[a-z]?)\s*\)$")
EQ_TAIL_RE = re.compile(r"\(\s*(\d{1,3}[a-z]?)\s*\)\s*$")


@dataclass
class Equation:
    page: int
    num: str
    raw: str
    bbox: list
    items: list = field(default_factory=list)
    confidence: str = "high"


def detect_equations(pages: Sequence[Page], body: float) -> list[Equation]:
    out: list[Equation] = []
    for pg in pages:
        txt = [it for it in pg.items if it.kind == "text" and it.text.strip() and it.w > 0.4]
        if not txt:
            continue
        nbands = max(1, int(pg.h / max(4.0, 0.45 * body)))
        bands: dict[int, list[Item]] = collections.defaultdict(list)
        for it in txt:
            bands[int(it.cy / max(4.0, 0.45 * body))].append(it)
        used = set()
        for bi in sorted(bands):
            grp = [it for it in bands[bi] if it.idx not in used]
            if len(grp) < 4:
                continue
            # a band may hold the equations of BOTH columns: cluster by x first
            grp.sort(key=lambda i: i.x0)
            clusters: list[list[Item]] = [[grp[0]]]
            for it in grp[1:]:
                if it.x0 - clusters[-1][-1].x1 > 2.0 * body:
                    clusters.append([it])
                else:
                    clusters[-1].append(it)
            for cl in clusters:
                if len(cl) < 4:
                    continue
                math_n = sum(1 for it in cl if it.is_math)
                if math_n < 3 or math_n / len(cl) < 0.45:
                    continue
                cl_x1 = max(it.x1 for it in cl)
                cl_x0 = min(it.x0 for it in cl)
                # the number sits to the right of the formula, same or next band
                num_it, num_band = None, bi
                for cand_band in (bi, bi + 1, bi - 1):
                    for it in bands.get(cand_band, []):
                        if (it.idx in used or it in cl
                                or not EQ_NUM_RE.match(it.text.strip())):
                            continue
                        if it.x0 > cl_x1 - 2 and it.x0 > cl_x0:
                            num_it, num_band = it, cand_band
                            break
                    if num_it:
                        break
                if num_it is None:
                    tail = "".join(it.text for it in cl)
                    m = EQ_TAIL_RE.search(tail)
                    if not (m and len(tail) < 200):
                        continue
                    num = m.group(1)
                else:
                    num = EQ_NUM_RE.match(num_it.text.strip()).group(1)
                cl_cy = statistics.median([it.cy for it in cl])
                near = [it for b in (bi - 1, bi, bi + 1) for it in bands.get(b, [])
                        if it.idx not in used and it is not num_it
                        and cl_x0 - 4 <= it.cx <= cl_x1 + 4
                        and abs(it.cy - cl_cy) <= 1.35 * body
                        and not EQ_NUM_RE.match(it.text.strip())]
                seen_ids = {id(it) for it in cl}
                body_items = list(cl) + [it for it in near if id(it) not in seen_ids]
                body_items.sort(key=lambda i: (i.x0, i.y0))
                raw, prev_x = "", None
                for it in body_items:
                    t = it.text.strip()
                    if not t:
                        continue
                    if prev_x is not None:
                        gap = it.x0 - prev_x
                        raw += ("" if gap <= 0.30 * it.size else " ")
                    raw += t
                    prev_x = it.x1
                raw = norm_space(raw)
                raw = re.sub(r"\s*\(\s*%s\s*\)\s*$" % re.escape(num), "", raw).strip()
                if len(raw) < 3:
                    continue
                xs = [it.x0 for it in body_items] + [it.x1 for it in body_items]
                ys = [it.y0 for it in body_items] + [it.y1 for it in body_items]
                for it in cl + ([num_it] if num_it else []):
                    used.add(it.idx)
                out.append(Equation(page=pg.number, num=num, raw=raw,
                                    bbox=[r1(min(xs)), r1(min(ys)), r1(max(xs)), r1(max(ys))],
                                    items=body_items + ([num_it] if num_it else [])))
    # fallback: PDFs that emit the formula as one text run (Word, matplotlib)
    for pg in pages:
        txt = [it for it in pg.items if it.kind == "text" and it.text.strip() and it.w > 0.4]
        bands2: dict[int, list[Item]] = collections.defaultdict(list)
        for it in txt:
            bands2[int(it.cy / max(4.0, 0.45 * body))].append(it)
        for bi, grp in bands2.items():
            if len(grp) > 6:
                continue
            nums = [it for it in grp if EQ_NUM_RE.match(it.text.strip())]
            if len(nums) != 1:
                continue
            num_it = nums[0]
            others = [it for it in grp if it is not num_it and it.idx not in
                      {i.idx for e in out for i in e.items}]
            if not others:
                continue
            others.sort(key=lambda i: i.x0)
            raw = " ".join(i.text.strip() for i in others if i.text.strip())
            if not re.search(r"[=<>\u2248\u2264\u2265]", raw) or len(raw) > 160:
                continue
            if any(e.num == EQ_NUM_RE.match(num_it.text.strip()).group(1) for e in out):
                continue
            xs = [i.x0 for i in others] + [num_it.x0]
            ys = [i.y0 for i in others] + [num_it.y1]
            out.append(Equation(page=pg.number,
                                num=EQ_NUM_RE.match(num_it.text.strip()).group(1),
                                raw=norm_space(raw),
                                bbox=[r1(min(xs)), r1(min(ys)), r1(max(i.x1 for i in others)),
                                      r1(max(ys))],
                                items=others + [num_it], confidence="medium"))

    # one record per equation number (first with content wins)
    seen, uniq = {}, []
    for e in out:
        if e.num in seen:
            if not seen[e.num].raw and e.raw:
                seen[e.num] = e
                uniq = [x for x in uniq if x.num != e.num] + [e]
            continue
        seen[e.num] = e
        uniq.append(e)
    uniq.sort(key=lambda e: (e.page, e.bbox[1]))
    return uniq


# --------------------------------------------------------------------------
# 5. marginalia
# --------------------------------------------------------------------------

FOLIO_RE = re.compile(r"^[\s\-–—\[\](){}|.]*\d{1,4}[\s\-–—\[\](){}|.]*$")
PAGE_OF_RE = re.compile(r"^(page\s*)?\d{1,4}\s*(/|of)\s*\d{1,4}$", re.I)
BANNER_RE = re.compile(
    r"(journal|transactions|proceedings|annals|letters|review of|bulletin|"
    r"nature|science|cell press|arxiv|preprint|issn|doi|https?://|www\.|"
    r"received|accepted|revised|published|copyright|©|all rights reserved|"
    r"vol\.?\s*\d|no\.\s*\d|第\s*\d+\s*卷|第\s*\d+\s*期)", re.I)


def detect_marginalia(pages: Sequence[Page], lines_by_page: dict[int, list[Line]],
                      body: float) -> dict:
    n = max(1, len(pages))
    rep: collections.Counter = collections.Counter()
    for pg in pages:
        for ln in lines_by_page.get(pg.number, []):
            if ln.y1 <= 0.09 * pg.h or ln.y0 >= 0.90 * pg.h:
                k = norm_key(ln.text)
                if k:
                    rep[k] += 1
    repeated = {k for k, c in rep.items() if c >= max(2, math.ceil(0.35 * n))}

    drop: dict[int, set] = {}
    detail = []
    for pg in pages:
        flagged = set()
        for i, ln in enumerate(lines_by_page.get(pg.number, [])):
            t = norm_space(ln.text)
            top = ln.y1 <= 0.09 * pg.h
            bot = ln.y0 >= 0.885 * pg.h
            side = (ln.x1 <= 0.055 * pg.w) or (ln.x0 >= 0.945 * pg.w)
            reasons = []
            if (top or bot) and FOLIO_RE.match(t) and len(t) <= 12:
                reasons.append("page_number")
            if (top or bot) and PAGE_OF_RE.match(t):
                reasons.append("page_number")
            if (top or bot) and len(t) <= 140 and norm_key(t) in repeated:
                reasons.append("running_head")
            if (top or bot) and len(t) <= 150 and BANNER_RE.search(t):
                reasons.append("journal_banner")
            if side and ln.w <= 0.03 * pg.w and not t:
                reasons.append("side_stamp")
            if reasons and not (pg.number == 1 and ln.size >= 1.35 * body):
                flagged.add(i)
                detail.append({"page": pg.number, "text": t[:100], "reason": reasons,
                               "bbox": ln.bbox()})
        drop[pg.number] = flagged
    return {"drop": drop, "detail": detail}


# --------------------------------------------------------------------------
# 6. captions, figure regions, table regions
# --------------------------------------------------------------------------

FIG_RE = re.compile(r"^(?:figure|fig\.?)\s*([0-9]{1,3}|[IVXLC]{1,6})\s*[.:|]\s*", re.I)
TAB_RE = re.compile(r"^(?:table|tab\.?)\s*([0-9]{1,3}|[IVXLC]{1,6})\s*[.:|]\s*", re.I)
HEAD_NUM_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+(\S.{0,88})$")
HEAD_APPX_RE = re.compile(r"^[A-Z]\.\s+[A-Z][A-Za-z].{0,80}$")
HEAD_WORD_RE = re.compile(
    r"^(?:abstract|summary|introduction|background|related work|previous work|"
    r"materials and methods|methods?|methodology|approach|model|proposed|"
    r"experiments?(?: and results)?|results?(?: and discussion)?|discussion|"
    r"conclusions?(?: and future work| and outlook)?|concluding remarks|"
    r"acknowledg(?:e)?ments?|references|bibliography|"
    r"appendix(?:\s+[A-Z0-9]{1,3})?|supplementary material|"
    r"author contributions|funding|data availability|conflicts? of interest|"
    r"摘\s*要|引\s*言|前\s*言|结\s*论|讨\s*论|参考文献|致\s*谢|附\s*录)$", re.I)


def is_caption_start(t: str) -> tuple[str, str] | tuple[None, None]:
    s = norm_space(t)
    m = FIG_RE.match(s)
    if m and len(s) > len(m.group(0)) + 3:
        return "figure", m.group(1)
    m = TAB_RE.match(s)
    if m and len(s) > len(m.group(0)) + 3:
        return "table", m.group(1)
    return None, None


def looks_like_row(ln: Line, body: float) -> bool:
    """True when a line reads as a table row (>=2 cells, short/numeric content)."""
    cells = split_cells(ln, body)
    if len(cells) < 2:
        return False
    short = sum(1 for cell in cells if len(cell[2]) <= 16)
    numeric = sum(1 for cell in cells if re.search(r"\d", cell[2]))
    return (short >= len(cells) - 1) and (numeric >= 1 or len(cells) >= 3)


def collect_captions(lines: Sequence[Line], body: float) -> list[dict]:
    """Group caption text blocks (caption line + its continuation lines)."""
    caps = []
    i = 0
    L = list(lines)
    while i < len(L):
        kind, num = is_caption_start(L[i].text)
        if not kind:
            i += 1
            continue
        head = L[i]
        preds = [q for q in L if q.page == head.page and q.y1 <= head.y0 + 1
                 and q.y1 >= head.y0 - 1.8 * max(6.0, head.h)
                 and q.x1 > head.x0 - 2 and q.x0 < head.x1
                 and q.size >= 0.85 * head.size]
        if preds:
            pv = max(preds, key=lambda q: q.y1)
            if (not pv.text.rstrip().endswith((".", ":", "!", "?", "\u3002"))
                    and pv.x1 > head.x0 - 2):
                i += 1                     # "... as shown in Fig. 4. First, ..." is prose
                continue
        grp = [head]
        j = i + 1
        while j < len(L):
            q = L[j]
            if q.page != head.page:
                break
            if is_caption_start(q.text)[0]:
                break
            if is_heading_like(q, body):
                break
            gap = q.y0 - grp[-1].y1
            pitch = max(6.0, q.h)
            if gap > 0.85 * pitch:
                break
            if abs(q.size - head.size) > max(1.0, 0.22 * head.size):
                break
            if looks_like_row(q, body):
                break
            # a caption never jumps across the gutter / into another column
            ov = min(q.x1, head.x1) - max(q.x0, head.x0)
            if ov < 0.45 * max(1.0, min(q.w, head.w)):
                break
            grp.append(q)
            j += 1
            if sum(len(g.text) for g in grp) > 900:
                break
        # a caption line split by a wide space leaves an orphan fragment on the
        # same baseline: re-attach it when it is horizontally adjacent
        known = {id(g) for g in grp}
        extra = []
        for q in L:
            if id(q) in known or q.page != head.page or is_caption_start(q.text)[0]:
                continue
            if abs(q.size - head.size) > max(0.9, 0.10 * head.size):
                continue
            same_base = any(abs(q.cy - g.cy) <= 0.60 * max(q.h, g.h) for g in grp)
            if not same_base:
                continue
            near = any(0 < (q.x0 - g.x1) <= 3.0 * max(q.size, 6.0) or
                       0 < (g.x0 - q.x1) <= 3.0 * max(q.size, 6.0) for g in grp)
            if not near:
                continue
            if is_heading_like(q, body) or looks_like_row(q, body):
                continue
            extra.append(q)
        if extra:
            grp = sorted(grp + extra, key=lambda g: (round(g.y0, 1), g.x0))
        text = " ".join(g.text.strip() for g in grp)
        caps.append({"kind": kind, "num": num, "text": text, "lines": grp,
                     "page": head.page, "bbox": [r1(head.x0), r1(head.y0),
                                                 r1(max(g.x1 for g in grp)),
                                                 r1(max(g.y1 for g in grp))]})
        i = j
    return caps


def is_heading_like(ln: Line, body: float) -> bool:
    t = norm_space(ln.text)
    if not t or len(t) > 120:
        return False
    if HEAD_NUM_RE.match(t) and len(t) <= 95:
        return True
    if HEAD_WORD_RE.match(t):
        return True
    return ln.size >= 1.12 * body and len(t.split()) <= 14 and not t.endswith(".")


def figure_label_mask(caps: Sequence[dict], lines_by_page: dict[int, list[Line]],
                      pages: dict[int, Page], body: float) -> dict[int, set]:
    """Lines that belong to a figure's own vector artwork (axis labels, legends)."""
    mask: dict[int, set] = collections.defaultdict(set)
    for c in caps:
        if c["kind"] != "figure":
            continue
        pg = pages[c["page"]]
        L = lines_by_page[c["page"]]
        cap_idx = {id(g) for g in c["lines"]}
        x0c, x1c = c["bbox"][0], c["bbox"][2]
        top_y = c["lines"][0].y0
        # scan upward through the caption's x-band while lines are non-body
        band = [ln for ln in L if id(ln) not in cap_idx
                and ln.x1 >= x0c - 12 and ln.x0 <= x1c + 12 and ln.y1 <= top_y + 2]
        band.sort(key=lambda l: -l.y1)
        for ln in band:
            words = len(ln.text.split())
            if ln.y1 < top_y - 0.92 * pg.h:
                break                      # reached the page top
            if ln.size >= 1.02 * body and words >= 2:
                break                      # title / heading above the figure
            if abs(ln.size - body) <= max(0.35, 0.05 * body) and (
                    words >= 5 or (words >= 3 and ln.text.rstrip().endswith("."))):
                break                      # running body text
            mask[c["page"]].add(id(ln))
    # anything painted inside a raster image box is also not body text
    for pno, pg in pages.items():
        for im in [it for it in pg.items if it.kind == "image"]:
            for ln in lines_by_page.get(pno, []):
                if (ln.x0 >= im.x0 - 2 and ln.x1 <= im.x1 + 2
                        and ln.y0 >= im.y0 - 2 and ln.y1 <= im.y1 + 2):
                    mask[pno].add(id(ln))
    return mask


def table_region(cap: dict, lines_by_page: dict[int, list[Line]], pages: dict[int, Page],
                 body: float, direction: str = "below") -> list[Line]:
    """Lines that make up the table body under (or above) its caption."""
    pg = pages[cap["page"]]
    L = lines_by_page[cap["page"]]
    cap_lines = cap["lines"]
    cap_bottom = max(l.y1 for l in cap_lines)
    cap_top = min(l.y0 for l in cap_lines)
    x0c, x1c = cap["bbox"][0], cap["bbox"][2]
    wide = (x1c - x0c) > 0.62 * pg.w
    out: list[Line] = []
    if direction == "above":
        cand = [l for l in L if l.y1 <= cap_top + 2]
        if not wide:
            cand = [l for l in cand if l.x1 > x0c - 30 and l.x0 < x1c + 30]
        cand.sort(key=lambda l: -l.y1)
        prev_y = cap_top
    else:
        if wide:
            cand = [l for l in L if l.y0 >= cap_bottom - 2]
        else:
            cand = [l for l in L if l.y0 >= cap_bottom - 2 and l.x1 > x0c - 30 and l.x0 < x1c + 30]
        cand.sort(key=lambda l: l.y0)
        prev_y = cap_bottom
    for ln in cand:
        if id(ln) in {id(g) for g in cap_lines}:
            continue
        if is_caption_start(ln.text)[0]:
            break
        if ln.size >= 1.10 * body and is_heading_like(ln, body):
            break
        is_prose = (ln.size >= 0.96 * body and len(ln.text) > 70
                    and not looks_like_row(ln, body))
        if is_prose:
            break
        gap = (ln.y0 - prev_y) if direction != "above" else (prev_y - ln.y1)
        if gap > 3.2 * max(8.0, ln.h):
            break
        out.append(ln)
        prev_y = max(prev_y, ln.y1) if direction != "above" else min(prev_y, ln.y0)
        if len(out) > 400:
            break
    if direction == "above":
        out = list(reversed(out))
    return out


def table_rows(lines_seq: Sequence[Line], body: float) -> list[list[tuple[float, float, str]]]:
    """Group table lines into rows (a row may be split by the gutter), then cells."""
    if not lines_seq:
        return []
    hs = [l.h for l in lines_seq if l.h > 0] or [10.0]
    tol = max(3.0, 0.55 * statistics.median(hs))
    ordered = sorted(lines_seq, key=lambda l: (l.y0, l.x0))
    groups: list[list[Line]] = []
    cur: list[Line] = []
    cy = None
    for ln in ordered:
        if cy is None or abs(ln.y0 - cy) <= tol:
            cur.append(ln)
            cy = ln.y0 if cy is None else cy
        else:
            groups.append(cur)
            cur, cy = [ln], ln.y0
    if cur:
        groups.append(cur)
    rows = []
    for g in groups:
        g.sort(key=lambda l: l.x0)
        cells: list[tuple[float, float, str]] = []
        for ln in g:
            cells.extend(split_cells(ln, body))
        if cells:
            rows.append(cells)
    return rows


def split_cells(ln: Line, body: float) -> list[tuple[float, float, str]]:
    """Split a table line into cells using the original fragment gaps."""
    if not ln.frags:
        return [(ln.x0, ln.x1, ln.text)]
    cells, cur_x0, cur = [], ln.frags[0][0], [ln.frags[0][2].strip()]
    prev_x1 = ln.frags[0][1]
    for x0, x1, t, sz in ln.frags[1:]:
        gap = x0 - prev_x1
        if gap > max(0.50 * max(sz, 5.0), 3.0):
            cells.append((cur_x0, prev_x1, norm_space(" ".join(cur))))
            cur_x0, cur = x0, [t.strip()]
        else:
            cur.append(t.strip())
        prev_x1 = x1
    cells.append((cur_x0, prev_x1, norm_space(" ".join(cur))))
    return [(a, b, c) for a, b, c in cells if c]


def build_grid(rows: Sequence[Sequence[tuple[float, float, str]]], body: float) -> dict:
    """Rebuild a table grid; columns are interval clusters of cell boxes."""
    if not rows:
        return {}
    ivals = sorted((a, b) for r in rows for a, b, _t in r)
    spans: list[list[float]] = []
    for a, b in ivals:
        placed = False
        for sp in spans:
            ov = min(b, sp[1]) - max(a, sp[0])
            if ov > 0 and ov >= 0.55 * min(b - a, sp[1] - sp[0]):
                sp[0] = min(sp[0], a)
                sp[1] = max(sp[1], b)
                placed = True
                break
        if not placed:
            spans.append([a, b])
    spans.sort()
    ncol = len(spans)
    if ncol < 2:
        return {}
    grid = [["" for _ in range(ncol)] for _ in rows]
    for ri, r in enumerate(rows):
        for a, b, txt in r:
            ci, best = 0, -1.0
            for k, (s0, s1) in enumerate(spans):
                ov = min(b, s1) - max(a, s0)
                if ov > best:
                    best, ci = ov, k
            grid[ri][ci] = (grid[ri][ci] + " " + txt).strip() if grid[ri][ci] else txt
    filled = [[c for c in row if c] for row in grid]
    if sum(1 for f in filled if len(f) >= 2) < 2:
        return {}

    def looks_numeric(c: str) -> bool:
        return bool(re.fullmatch(r"[-\u2212+]?[\d.,%()\s]*\d[\d.,%()\s]*", c or ""))

    first = grid[0]
    has_header = (all(c for c in first) and not any(looks_numeric(c) for c in first)
                  and all(len(c) < 45 for c in first) and len(grid) > 2)
    header = first if has_header else [""] * ncol
    data = grid[1:] if has_header else grid

    def esc(c: str) -> str:
        return (c or "").replace("|", "\\|")

    md = ["| " + " | ".join(esc(c) for c in header) + " |",
          "|" + "|".join([" --- "] * ncol) + "|"]
    for r in data:
        md.append("| " + " | ".join(esc(c) for c in r) + " |")
    return {"markdown": "\n".join(md), "ncol": ncol, "nrow": len(data),
            "has_header": has_header}


# --------------------------------------------------------------------------
# 7. reading order
# --------------------------------------------------------------------------

def _coverage_profile(lines: Sequence[Line], xlo: float, xhi: float,
                      ylo: float, yhi: float, nx: int = 400, ny: int = 160):
    """Fraction of the body height covered at each x bin.

    A gutter is a strip that is crossed by at most a couple of spanning lines
    (a title, a wide abstract), so its vertical coverage is low even though its
    horizontal occupancy is not exactly zero.
    """
    try:
        import numpy as np
    except Exception:
        return None
    if not lines or xhi <= xlo or yhi <= ylo:
        return None
    G = np.zeros((ny, nx), dtype=bool)
    for ln in lines:
        a = int(max(0, min(nx - 1, (ln.x0 - xlo) / (xhi - xlo) * (nx - 1))))
        b = int(max(0, min(nx - 1, (ln.x1 - xlo) / (xhi - xlo) * (nx - 1))))
        c = int(max(0, min(ny - 1, (ln.y0 - ylo) / (yhi - ylo) * (ny - 1))))
        d = int(max(0, min(ny - 1, (ln.y1 - ylo) / (yhi - ylo) * (ny - 1))))
        G[c:d + 1, a:b + 1] = True
    return G.mean(axis=0), xlo, xhi


def coverage_gaps(lines: Sequence[Line], xlo: float, xhi: float,
                  ylo: float = 0.0, yhi: float = 0.0,
                  max_frac: float = 0.45) -> list[tuple[float, float]]:
    if yhi <= ylo:
        ylo = min(l.y0 for l in lines)
        yhi = max(l.y1 for l in lines)
    prof = _coverage_profile(lines, xlo, xhi, ylo, yhi)
    if prof is None:                      # numpy missing: fall back to pure counts
        nbin = max(1, int(round(xhi - xlo)))
        cov = [0] * nbin
        for ln in lines:
            a = int(max(0, math.floor(ln.x0 - xlo)))
            b = int(min(nbin - 1, math.ceil(ln.x1 - xlo)))
            for i in range(a, b + 1):
                cov[i] += 1
        thr = max(1, int(0.15 * max(1, statistics.median([c for c in cov if c] or [1]))))
        prof_arr = cov
    else:
        prof_arr, xlo, xhi = prof
        thr = max_frac
    n = len(prof_arr)
    step = (xhi - xlo) / max(1, n - 1)
    runs, start = [], None
    for i, v in enumerate(prof_arr):
        low = (v <= thr)
        if low:
            start = i if start is None else start
        elif start is not None:
            runs.append((xlo + start * step, xlo + i * step))
            start = None
    if start is not None:
        runs.append((xlo + start * step, xhi))
    return runs


def _edge_clusters(values: Sequence[float], tol: float, min_members: int) -> list[float]:
    vals = sorted(values)
    if not vals:
        return []
    groups: list[list[float]] = [[vals[0]]]
    for v in vals[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [statistics.median(g) for g in groups if len(g) >= min_members]


def detect_gutters(body_lines: Sequence[Line], page_w: float,
                   page_h: float = 0.0) -> list[tuple[float, float]]:
    """Find inter-column gutters from the geometry of body-size lines.

    Body columns share a left margin and a right justification edge, so both
    edge sets cluster.  A gutter is the strip between one margin cluster and the
    right edge cluster that belongs to the column left of it.  Spanning lines
    (titles, full-width tables) simply contribute additional edge clusters and
    cannot fill a real gutter, because the strip is bounded by the two clusters.
    """
    n = len(body_lines)
    if n < 8:
        return []
    tol = max(5.0, 0.010 * page_w)
    min_members = max(2, int(0.08 * n))
    margins = _edge_clusters([l.x0 for l in body_lines], tol, min_members)
    rights = _edge_clusters([l.x1 for l in body_lines], tol, min_members)
    if len(margins) < 2:
        return []
    out: list[tuple[float, float]] = []
    for a, b in ((x, y) for i, x in enumerate(margins) for y in margins[i + 1:]):
        if b - a < 0.18 * page_w:
            continue
        left = [l for l in body_lines if abs(l.x0 - a) <= 2.5 * tol]
        right = [l for l in body_lines if abs(l.x0 - b) <= 2.5 * tol]
        if len(left) < 0.10 * n or len(right) < 0.10 * n:
            continue
        closed = [l for l in left if l.x1 <= b - tol]
        if len(closed) < 0.50 * len(left):
            continue                       # single wide column, not two columns
        if page_h > 0 and (len(closed) < 6 or len(right) < 6):
            continue
        x1s = sorted(l.x1 for l in closed)
        g0 = x1s[int(0.90 * (len(x1s) - 1))]
        g1 = b
        if not (0.004 * page_w <= g1 - g0 <= 0.11 * page_w):
            cands = [c for c in rights if a + 0.02 * page_w <= c <= b - 2.0 * tol]
            if not cands:
                continue
            g0 = max(cands)
            if not (0.004 * page_w <= g1 - g0 <= 0.11 * page_w):
                continue
        out.append((g0, g1))
    out.sort()
    keep: list[tuple[float, float]] = []
    for g in out:
        if all(g[1] <= k[0] + 1 or g[0] >= k[1] - 1 for k in keep):
            keep.append(g)
    return keep


def column_of(ln: Line, gutters: Sequence[tuple[float, float]]) -> int:
    c = 0
    for g0, g1 in gutters:
        if ln.x1 <= g0 + 1.5:
            break
        if ln.x0 >= g1 - 1.5:
            c += 1
        else:
            return -1
    return c


def order_page(lines: Sequence[Line], gutters: Sequence[tuple[float, float]]) -> list[Line]:
    """Band-and-column ordering: spanning lines open a band, columns read L->R."""
    if not lines:
        return []
    if not gutters:
        return sorted(lines, key=lambda l: (round(l.y0, 1), l.x0))
    cols = len(gutters) + 1
    items = sorted(lines, key=lambda l: (l.y0, l.x0))
    bands: list[list[Line]] = []
    cur: list[Line] = []
    for ln in items:
        if column_of(ln, gutters) < 0:
            if cur:
                bands.append(cur)
                cur = []
            bands.append([ln])
        else:
            cur.append(ln)
    if cur:
        bands.append(cur)
    ordered: list[Line] = []
    for band in bands:
        if len(band) == 1 and column_of(band[0], gutters) < 0:
            ordered.append(band[0])
            continue
        buckets: dict[int, list[Line]] = collections.defaultdict(list)
        for ln in band:
            buckets[max(0, column_of(ln, gutters))].append(ln)
        for c in range(cols):
            ordered.extend(sorted(buckets.get(c, []), key=lambda l: (round(l.y0, 1), l.x0)))
    return ordered


# --------------------------------------------------------------------------
# 8. paragraphs
# --------------------------------------------------------------------------

@dataclass
class Block:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float
    bold: bool
    lines: list = field(default_factory=list)
    heading: bool = False
    level: int = 0

    def bbox(self, scale: float = 1.0) -> list:
        return [r1(self.x0 * scale), r1(self.y0 * scale),
                r1(self.x1 * scale), r1(self.y1 * scale)]


def heading_level(t: str, size: float, bold: bool, body: float) -> int:
    s = norm_space(t)
    if not s or len(s) > 130:
        return 0
    if is_caption_start(s)[0]:
        return 0
    if size < 0.92 * body:
        return 0                       # table/caption-sized runs are not headings
    m = HEAD_NUM_RE.match(s)
    if m and len(s) <= 100:
        num = m.group(1)
        rest = m.group(2)
        if len(rest.split()) <= 14 and not rest.endswith(".") and re.match(r"[A-Z\u4e00-\u9fff]", rest):
            return min(4, num.count(".") + 1)
    if HEAD_WORD_RE.match(s):
        return 1
    if re.search(r"\.\s+[A-Za-z\u4e00-\u9fff]", s):
        return 0                      # run-in heading glued to body prose
    if re.search(r":\s+(?:\S+\s+){3,}\S", s):
        return 0                      # "Decoder: the decoder is ..." run-in label
    if size >= 1.12 * body and len(s.split()) <= 14 and not s.endswith(".") and not s.endswith(","):
        return 1
    if bold and size >= 0.97 * body and len(s.split()) <= 10 and \
            re.search(r"[A-Za-z\u4e00-\u9fff]", s) and not s.endswith((".", ":", ";")):
        return 2
    return 0


def build_blocks(lines: Sequence[Line], body: float,
                 gutters: Sequence[tuple[float, float]],
                 page_w: float) -> list[Block]:
    if not lines:
        return []
    # per-column left/right reference edges from body-size lines
    cols: dict[int, list[Line]] = collections.defaultdict(list)
    for ln in lines:
        c = column_of(ln, gutters) if gutters else 0
        if c < 0:
            continue                       # spanning lines define no column edge
        cols[c].append(ln)
    edges = {}
    for c, ls in cols.items():
        bl = [l for l in ls if abs(l.size - body) <= 0.06 * body] or ls
        x0s = sorted(l.x0 for l in bl)
        x1s = sorted(l.x1 for l in bl)
        edges[c] = (x0s[int(0.05 * (len(x0s) - 1))], x1s[int(0.92 * (len(x1s) - 1))])

    blocks: list[Block] = []
    prev: Line | None = None
    for ln in lines:
        c = column_of(ln, gutters) if gutters else 0
        c = max(0, c)
        lref, rref = edges.get(c, (ln.x0, ln.x1))
        new = True
        if prev is not None and blocks:
            lh = max(6.0, statistics.median([prev.h, ln.h]))
            gap = ln.y0 - prev.y1
            same_col = (column_of(prev, gutters) if gutters else 0) == (c if gutters else 0)
            font_jump = abs(ln.size - prev.size) > max(0.8, 0.06 * max(ln.size, prev.size))
            colw = max(1.0, rref - lref)
            prev_short = prev.x1 < lref + 0.55 * colw
            indented = ln.x0 > lref + 0.75 * ln.size
            ptxt = prev.text.rstrip()
            continues = (bool(ptxt) and ptxt[-1] not in ".!?:;\u3002\uff1f\uff01"
                          and (ln.text[:1].islower() or is_cjk(ln.text[:1])))
            if (same_col and not font_jump and gap < 0.62 * lh
                    and not blocks[-1].heading
                    and (continues or not (prev_short or indented))):
                new = False
        if new:
            blocks.append(Block(page=ln.page, x0=ln.x0, y0=ln.y0, x1=ln.x1, y1=ln.y1,
                                text=ln.text, size=ln.size, bold=ln.bold, lines=[ln]))
        else:
            b = blocks[-1]
            joiner = "" if b.text.endswith("-") else " "
            b.text = b.text + joiner + ln.text
            b.x0 = min(b.x0, ln.x0)
            b.x1 = max(b.x1, ln.x1)
            b.y1 = max(b.y1, ln.y1)
            b.lines.append(ln)
        prev = ln
    for b in blocks:
        b.level = heading_level(b.text, b.size, b.bold, body)
        b.heading = b.level > 0
    return blocks


# --------------------------------------------------------------------------
# 9. front matter
# --------------------------------------------------------------------------

AFF_RE = re.compile(
    r"(@|universit|institut|department|dept\.|laborator|school of|college|academy|"
    r"research|inc\.|ltd|corp|center|centre|faculty|hospital|"
    r"大学|学院|研究所|实验室|系\b)", re.I)


def front_matter(blocks: Sequence[Block], body: float) -> dict:
    p1 = [b for b in blocks if b.page == 1]
    if not p1:
        return {}
    stop = len(p1)
    max_front_size = max(b.size for b in p1[:12]) if p1 else body
    for i, b in enumerate(p1):
        t = norm_space(b.text)
        if re.match(r"^(abstract|summary|摘\s*要)\b", t, re.I) and b.size >= 0.95 * body:
            stop = i
            break
        if re.match(r"^(key\s*words?|关键词|索引词)\b", t, re.I):
            stop = i
            break
        if HEAD_NUM_RE.match(t) and b.size >= 0.98 * body:
            stop = i
            break
        if b.heading and b.size >= 0.80 * max_front_size:
            stop = i
            break
    front = p1[:stop] or p1[:1]
    max_size = max(b.size for b in front)
    title_blocks = [b for b in front if b.size >= max_size - 0.55]
    title_blocks.sort(key=lambda b: b.y0)
    title_blocks = title_blocks[:4]
    title = " ".join(b.text.strip() for b in title_blocks)
    used = {id(b) for b in title_blocks}
    rest = [b for b in front if id(b) not in used]

    authors, affils = [], []
    for b in rest:
        t = b.text.strip()
        if not t or len(t) > 500:
            continue
        if AFF_RE.search(t) and not re.search(r",\s*[A-Z][a-z]+\s+[A-Z]", t):
            affils.append(t)
            continue
        if b.size < 0.90 * body:
            continue
        parts = re.split(r"\s{2,}|[;†‡§¶]|\s+and\s+|\u00b7|\u2022|\|", t)
        if len(b.lines) == 1 and len(b.lines[0].frags) > 1:
            fr = b.lines[0].frags
            segs, cur = [], [fr[0][2]]
            for (px0, px1, _t, psz), nxt in zip(fr, fr[1:]):
                if nxt[0] - px1 > 1.4 * max(psz, body):
                    segs.append(" ".join(cur))
                    cur = [nxt[2]]
                else:
                    cur.append(nxt[2])
            segs.append(" ".join(cur))
            if len(segs) > len(parts):
                parts = [norm_space(x) for x in segs if x.strip()]
        for c in parts:
            c = c.strip(" ,*†‡§¶0123456789")
            c = re.sub(r"\s+", " ", c)
            if (2 <= len(c) <= 90 and re.search(r"[A-Za-z\u4e00-\u9fff]", c)
                    and not re.search(r"[{}@/\\<>]", c)
                    and not c[0].islower()
                    and not re.fullmatch(r"[^A-Z\u4e00-\u9fff]+", c)):
                authors.append(c)
    return {"title": title, "title_blocks": title_blocks,
            "authors": authors, "affiliations": affils, "rest": rest}


def extract_abstract(blocks: Sequence[Block], body: float) -> dict:
    p1 = [b for b in blocks if b.page == 1]
    idx = None
    inline = ""
    for i, b in enumerate(p1):
        m = re.match(r"^(abstract|summary|摘\s*要)\b[.:：]?\s*", b.text.strip(), re.I)
        if m and b.size >= 0.9 * body:
            idx = i
            inline = b.text.strip()[m.end():].strip()
            break
    if idx is None:
        return {}
    parts, bs = ([inline] if len(inline) > 40 else []), [p1[idx]]
    for b in p1[idx + 1:]:
        t = b.text.strip()
        if b.heading or re.match(r"^(key\s*words?|索引词|关键词)\b", t, re.I):
            break
        if b.size < 0.80 * body:
            break
        if abs(b.size - p1[idx].size) > 0.35 * body and not parts:
            continue
        parts.append(t)
        bs.append(b)
        if len(parts) > 16:
            break
    return {"text": " ".join(parts).strip(), "blocks": bs}


REF_HEAD_RE = re.compile(r"^(references|bibliography|literature cited|参考文献)$", re.I)
REF_ENTRY_RE = re.compile(
    r"^(?:\[\s*\d{1,3}\s*\]|\(\s*\d{1,3}\s*\)|\d{1,3}[.)]\s|"
    r"[A-Z][A-Za-z\u00c0-\u024f'’\-]+,\s*(?:[A-Z]\.|[A-Z][a-z]+,)|"
    r"[A-Z][A-Za-z\u00c0-\u024f'’\-]+\s+[A-Z][a-z]+,\s)")

FOOT_RE = re.compile(r"^\s*[\*\u2217\u2020\u2021\d]{1,3}\s*$")


# --------------------------------------------------------------------------
# 10. main assembly
# --------------------------------------------------------------------------

def load_case(path: str | None) -> tuple[str, str]:
    case_id, pdf = "", ""
    if path and os.path.exists(path):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:
            data = {}
        if isinstance(data, dict):
            for k in ("case_id", "id", "task_id", "caseId", "case"):
                v = data.get(k)
                if isinstance(v, (str, int)) and str(v).strip():
                    case_id = str(v).strip()
                    break
            for k in ("pdf_path", "pdf", "pdf_file", "file", "input_file", "input_path", "path"):
                v = data.get(k)
                if isinstance(v, str) and v.lower().endswith(".pdf"):
                    cand = v if os.path.isabs(v) else os.path.join(os.path.dirname(os.path.abspath(path)), v)
                    if os.path.exists(cand):
                        pdf = cand
                        break
            if not pdf:
                def walk(o):
                    nonlocal pdf
                    if pdf:
                        return
                    if isinstance(o, dict):
                        for v in o.values():
                            walk(v)
                    elif isinstance(o, list):
                        for v in o:
                            walk(v)
                    elif isinstance(o, str) and o.lower().endswith(".pdf") and os.path.exists(o):
                        pdf = o
                walk(data)
    if not pdf:
        for pat in ("/input/**/*.pdf", "/input/*.pdf", "/app/input/**/*.pdf",
                    "./*.pdf", "/data/**/*.pdf"):
            hits = sorted(glob.glob(pat, recursive=True))
            if hits:
                pdf = hits[0]
                break
    if not case_id:
        case_id = os.path.splitext(os.path.basename(pdf))[0] if pdf else "unknown-case"
    return case_id, pdf


def build_result(pdf: str, case_id: str, debug: bool = False):
    notes = {"version": VERSION, "pdf": os.path.abspath(pdf) if pdf else "",
             "case_id": case_id, "warnings": []}
    empty = {"case_id": case_id,
             "parsed_result": {"title": "", "authors": [], "abstract": "", "sections": [],
                               "tables": [], "figures": [], "equations": [], "references": []},
             "evidence": [], "uncertain_items": [], "run_status": "success",
             "status_reason": ""}

    probe = probe_pdf(pdf) if pdf and os.path.exists(pdf) else DocProbe(error="missing file")
    notes["probe"] = {"n_pages": probe.n_pages, "encrypted": probe.encrypted,
                      "decrypt_ok": probe.decrypt_ok,
                      "chars_per_page": probe.chars_per_page,
                      "images_per_page": probe.images_per_page,
                      "garbled_per_page": probe.garbled_per_page,
                      "error": probe.error}

    def verdict(status: str, reason: str, item: str = "", det: str = ""):
        empty["run_status"] = status
        empty["status_reason"] = reason
        if item:
            empty["uncertain_items"].append({"item": item, "reason": det or reason})
        return empty, notes

    if not pdf or not os.path.exists(pdf):
        return verdict("cannot_judge",
                       "No input PDF could be resolved from case.json or /input; nothing was parsed.",
                       "whole document", "missing input file")
    if probe.encrypted and not probe.decrypt_ok:
        return verdict("reject",
                       "The PDF is encrypted and no password is available; its text layer is "
                       "inaccessible, so the document is rejected rather than guessed.",
                       "whole document", "encrypted PDF without accessible content")
    if probe.error and probe.n_pages == 0:
        return verdict("cannot_judge", f"The PDF could not be opened: {probe.error}",
                       "whole document", probe.error)

    n = max(1, probe.n_pages)
    total_chars = sum(probe.chars_per_page)
    empty_pages = [i + 1 for i, c in enumerate(probe.chars_per_page) if c < 20]
    if total_chars < 40 * n or len(empty_pages) >= max(1, int(0.9 * n)):
        return verdict("cannot_judge",
                       "The PDF carries no usable text layer (scanned/image-only pages). "
                       "Recovering text would require OCR; this skill reports cannot_judge "
                       "instead of inventing content.",
                       "whole document",
                       f"extractable characters={total_chars} over {n} pages; "
                       f"pages without text: {empty_pages[:20]}")
    garble_total = sum(probe.garbled_per_page)
    if total_chars and garble_total > 0.35 * total_chars:
        return verdict("cannot_judge",
                       "The PDF has a text layer but its glyphs carry no Unicode mapping "
                       "(private-use codes / (cid:NNN) / U+FFFD). Extracting text would mean "
                       "guessing characters from glyph ids, so the document is reported as "
                       "cannot_judge; repair needs the original font's ToUnicode table or OCR.",
                       "whole document",
                       f"{garble_total} of {total_chars} non-space glyphs are undecodable "
                       f"({100.0 * garble_total / max(1, total_chars):.0f}%)")
    if not have("pdftohtml"):
        return verdict("cannot_judge",
                       "poppler pdftohtml is unavailable, so glyph coordinates could not be "
                       "read and reading order cannot be established.",
                       "whole document", "missing binary: pdftohtml")

    # never write into the input directory: it is frequently read-only (/input)
    xml_dir = tempfile.mkdtemp(prefix="pdf_struct_")
    xml_path = os.path.join(xml_dir, "doc.xml")
    cp = run(["pdftohtml", "-xml", "-hidden", "-i", "-q", pdf, xml_path])
    if not os.path.exists(xml_path):
        return verdict("cannot_judge", f"pdftohtml failed: {cp.stderr[:300]}",
                       "whole document", "layout extraction failed")

    pages = parse_pdftohtml_xml(xml_path)
    if not pages:
        return verdict("cannot_judge", "pdftohtml returned no page objects",
                       "whole document", "empty layout")

    xml_text = "".join(it.text for pg in pages for it in pg.items if it.kind == "text")
    xml_chars = len(re.sub(r"\s", "", xml_text))
    pua_chars = len(re.findall(r"[\ue000-\uf8ff\ufffd]", xml_text))
    if total_chars > 200 and xml_chars < 0.20 * total_chars:
        return verdict("cannot_judge",
                       "The page text layer is not decodable by the layout backend: its glyphs "
                       "carry no usable Unicode mapping (private-use / CID codes), so only "
                       f"{xml_chars} readable characters were recovered against {total_chars} "
                       "in the raw text layer. Recovering the text needs the original font's "
                       "ToUnicode table or OCR; nothing is guessed here.",
                       "whole document",
                       f"undecodable glyphs: {100.0 - 100.0 * xml_chars / max(1, total_chars):.0f}% "
                       "of the text layer")
    if xml_chars and pua_chars > 0.30 * xml_chars:
        return verdict("cannot_judge",
                       "Most recovered glyphs are private-use codes rather than readable text; "
                       "the document is reported as cannot_judge instead of emitting glyph ids.",
                       "whole document", f"{pua_chars} of {xml_chars} recovered glyphs are private-use")

    body = body_font_size(pages)
    for pg in pages:
        pg.body = body
    page_by_no = {p.number: p for p in pages}
    notes["body_font_size"] = body

    scale = 1.0
    sc = []
    for pg in pages:
        i = pg.number - 1
        if 0 <= i < len(probe.page_sizes):
            wpt, _hpt = probe.page_sizes[i]
            if pg.w > 0 and wpt > 0:
                sc.append(wpt / pg.w)
    if sc:
        scale = statistics.median(sc)
    notes["scale_units_to_pt"] = scale

    # ---- equations first (their glyph soup must not reach the prose stream)
    eqs = detect_equations(pages, body)
    eq_item_ids = {it.idx for e in eqs for it in e.items}
    notes["equations"] = [{"num": e.num, "page": e.page, "raw": e.raw[:120]} for e in eqs]

    kept_by_page = {pg.number: [it for it in pg.items if it.idx not in eq_item_ids]
                    for pg in pages}
    # pass 1: conservative lines -> gutters
    lines1 = {p: build_lines(v) for p, v in kept_by_page.items()}
    gutters1: dict[int, list] = {}
    for pg in pages:
        pg_lines = [l for l in lines1[pg.number] if 0.055 * pg.h < l.y0 < 0.945 * pg.h]
        bl = [l for l in pg_lines if abs(l.size - body) <= 0.06 * body]
        if len(bl) < 8:                       # reference lists use a smaller size
            hist: collections.Counter = collections.Counter()
            for l in pg_lines:
                hist[round(l.size, 1)] += len(l.text)
            if hist:
                local = max(hist.items(), key=lambda kv: kv[1])[0]
                bl = [l for l in pg_lines if abs(l.size - local) <= 0.06 * local]
        gutters1[pg.number] = detect_gutters(bl, pg.w, pg.h)
    # pass 2: relaxed line assembly inside each column (inline math, wide spaces)
    lines_by_page = {p: build_lines(v, gap_factor=6.0, gutters=gutters1[p])
                     for p, v in kept_by_page.items()}
    notes["lines_per_page"] = {p: len(v) for p, v in lines_by_page.items()}
    notes["gutters_pass1"] = {p: [[r1(a), r1(b)] for a, b in g] for p, g in gutters1.items()}

    marg = detect_marginalia(pages, lines_by_page, body)
    notes["marginalia_removed"] = marg["detail"]

    def pool(pno: int, skip: set) -> list[Line]:
        flag = marg["drop"].get(pno, set())
        out = []
        for i, ln in enumerate(lines_by_page[pno]):
            if i in flag or id(ln) in skip:
                continue
            # detached superscript / footnote markers ("1", "*", "a")
            if ln.size < 0.72 * body and len(ln.text) <= 3 and re.fullmatch(r"[\d\W]+", ln.text):
                continue
            out.append(ln)
        return out

    caps = collect_captions([ln for p in pages for ln in pool(p.number, set())], body)
    notes["captions"] = [{"kind": c["kind"], "num": c["num"], "text": c["text"][:120],
                          "page": c["page"]} for c in caps]

    fig_mask = figure_label_mask(caps, lines_by_page, page_by_no, body)
    notes["figure_label_lines_masked"] = sum(len(v) for v in fig_mask.values())

    cap_line_ids = {id(g) for c in caps for g in c["lines"]}
    skip_all = cap_line_ids | set().union(*fig_mask.values()) if fig_mask else cap_line_ids

    # ---- table bodies
    tables_out, table_line_ids = [], set()
    for c in [c for c in caps if c["kind"] == "table"]:
        best = {"markdown": "", "grid": {}, "lines": []}
        for direction in ("below", "above"):
            tl = [ln for ln in table_region(c, lines_by_page, page_by_no, body, direction)
                  if id(ln) not in cap_line_ids]
            g = build_grid(table_rows(tl, body), body)
            if g.get("ncol", 0) >= 2 and g.get("nrow", 0) >= 1:
                best = {"markdown": g["markdown"], "grid": g, "lines": tl}
                break
            if not best["lines"]:
                best = {"markdown": "", "grid": g, "lines": tl}
        # table body continued on the next page
        if not best["grid"] and (c["page"] + 1) in page_by_no:
            nxt = page_by_no[c["page"] + 1]
            lead = [ln for ln in lines_by_page[nxt.number]
                    if ln.y0 < 0.5 * nxt.h and id(ln) not in cap_line_ids
                    and ln.size < 0.98 * body]
            g2 = build_grid(table_rows(lead, body), body)
            if g2.get("ncol", 0) >= 2:
                best = {"markdown": g2["markdown"], "grid": g2, "lines": lead}
        table_line_ids.update(id(ln) for ln in best["lines"])
        tables_out.append({"table_id": f"Table {c['num']}", "caption": c["text"],
                           "markdown": best["markdown"], "cap": c,
                           "lines": best["lines"], "grid": best["grid"]})
    notes["tables"] = [{"id": t["table_id"], "rows": t["grid"].get("nrow", 0),
                        "cols": t["grid"].get("ncol", 0)} for t in tables_out]

    # ---- reading order per page (body lines only)
    order: dict[int, list[Line]] = {}
    gutters_by_page: dict[int, list] = {}
    for pg in pages:
        cand = pool(pg.number, skip_all | table_line_ids)
        # gutters come from pass 1 (conservative lines): pass-2 lines may already
        # have bridged a gutter, so they must not be used to re-derive it
        guts = gutters1.get(pg.number) or []
        if not guts:
            body_lines = [l for l in cand if abs(l.size - body) <= 0.06 * body]
            guts = detect_gutters(body_lines, pg.w, pg.h)
        gutters_by_page[pg.number] = guts
        cand_non_caption = [l for l in cand if id(l) not in table_line_ids]
        order[pg.number] = order_page(cand_non_caption, guts)
    notes["gutters"] = {p: [[r1(a), r1(b)] for a, b in g] for p, g in gutters_by_page.items()}

    blocks: list[Block] = []
    for pg in pages:
        blocks.extend(build_blocks(order[pg.number], body, gutters_by_page[pg.number], pg.w))
    abs_idx = None
    for i, b in enumerate(blocks):
        if b.page == 1 and re.match(r"^(abstract|summary|摘\s*要)\b", norm_space(b.text), re.I):
            abs_idx = i
            break
    if abs_idx:
        for b in blocks[:abs_idx]:
            b.heading = False
            b.level = 0
    notes["blocks"] = len(blocks)

    # ---- front matter / abstract / references / footnotes
    fm = front_matter(blocks, body)
    absd = extract_abstract(blocks, body)

    ref_start = None
    for i, b in enumerate(blocks):
        if REF_HEAD_RE.match(norm_space(b.text)):
            ref_start = i
            break
    ref_blocks, refs = [], []
    if ref_start is not None:
        cur = None
        for b in blocks[ref_start + 1:]:
            if (b.heading and b.size >= 1.02 * body and not REF_ENTRY_RE.match(b.text)
                    and (HEAD_NUM_RE.match(norm_space(b.text))
                         or HEAD_WORD_RE.match(norm_space(b.text))
                         or HEAD_APPX_RE.match(norm_space(b.text)))):
                break
            if HEAD_APPX_RE.match(norm_space(b.text)) and b.size >= 1.02 * body:
                break
            if REF_ENTRY_RE.match(norm_space(b.text)):
                if cur:
                    refs.append(cur)
                cur = [b]
            elif cur is None:
                cur = [b]
            else:
                cur.append(b)
        if cur:
            refs.append(cur)
        ref_blocks = [b for g in refs for b in g]

    # footnotes: small print near the bottom of a page, not already claimed
    claimed = {id(b) for b in blocks if b in (fm.get("title_blocks", []) + fm.get("rest", [])
                                              + absd.get("blocks", []) + ref_blocks)}
    foot_lines = []
    for pg in pages:
        for ln in order[pg.number]:
            if id(ln) in claimed or id(ln) in table_line_ids or id(ln) in cap_line_ids:
                continue
            if ln.y0 > 0.80 * pg.h and ln.size < 0.82 * body and len(ln.text) > 25:
                foot_lines.append(ln)
    foot_text = " ".join(l.text.strip() for l in foot_lines).strip()

    # ---- sections
    claimed_ids = claimed | {id(l) for l in foot_lines}
    # residual table/graph debris: off-size short runs that no caption claimed
    body_blocks = [b for b in blocks if abs(b.size - body) <= 0.06 * body]
    if len(body_blocks) >= 15:
        for b in blocks:
            if id(b) in claimed_ids or id(b) in table_line_ids or b.heading:
                continue
            if (abs(b.size - body) > 0.06 * body and len(b.text.split()) <= 12
                    and len(b.text) <= 120):
                claimed_ids.add(id(b))
    sections: list[dict] = []
    cur = None
    for b in blocks:
        if id(b) in claimed_ids:
            continue
        if id(b) in table_line_ids:
            continue
        if b.heading:
            if REF_HEAD_RE.match(norm_space(b.text)) and b.level <= 1:
                break                                  # references handled separately
            cur = {"heading": b.text.strip(), "paras": [], "level": b.level, "head": b}
            sections.append(cur)
            continue
        if cur is None:
            cur = {"heading": "", "paras": [], "level": 1, "head": None}
            sections.append(cur)
        cur["paras"].append(b)
    sections = [s for s in sections if s["paras"] or s["heading"]]
    sec_out = []
    for s in sections:
        sec_out.append({"heading": s["heading"], "level": s["level"],
                        "content": "\n\n".join(p.text.strip() for p in s["paras"]),
                        "paras": s["paras"], "head": s.get("head")})

    # ---- assemble parsed_result
    pr = empty["parsed_result"]
    pr["title"] = fm.get("title", "")
    pr["authors"] = fm.get("authors", [])
    pr["abstract"] = absd.get("text", "")
    pr["sections"] = [{"heading": s["heading"], "content": s["content"],
                       "level": s["level"]} for s in sec_out]
    if foot_text:
        pr["sections"].append({"heading": "Footnotes", "content": foot_text, "level": 3})
    pr["tables"] = [{"table_id": t["table_id"], "caption": t["caption"],
                     "markdown": t["markdown"]} for t in tables_out]
    pr["figures"] = [{"figure_id": f"Figure {c['num']}", "caption": c["text"]}
                     for c in caps if c["kind"] == "figure"]
    pr["equations"] = [{"eq_id": f"Eq. ({e.num})", "raw_text": e.raw} for e in eqs]
    split_refs: list[tuple[str, list]] = []

    def _marker_style(text: str) -> str:
        if len(re.findall(r"\[\d{1,3}\]", text)) >= 3:
            return "bracket"
        if len(re.findall(r"(?:^|\s)\d{1,3}\.\s+[A-Z]", text)) >= 3:
            return "numdot"
        return ""

    def _split_by_marker(text: str, style: str) -> list[str]:
        pat = r"\s*(?=\[\d{1,3}\])" if style == "bracket" else r"\s*(?=(?:^|\s)\d{1,3}\.\s+[A-Z])"
        return [x.strip() for x in re.split(pat, text) if x.strip()]

    region = " ".join(b.text.strip() for g in refs for b in g).strip()
    style = _marker_style(region)
    block_lookup = [b for g in refs for b in g]
    if style == "bracket" and len(re.findall(r"\[\d{1,3}\]", region)) > 1.6 * max(1, len(refs)):
        # block segmentation merged or split entries: rebuild from the marker stream
        pieces = _split_by_marker(region, style)
        for piece in pieces:
            head = piece[:24]
            owner = next((b for b in block_lookup if head and head[:16] in b.text), None)
            split_refs.append((norm_space(piece), [owner] if owner else block_lookup[:1]))
    else:
        for g in refs:
            txt = " ".join(b.text.strip() for b in g).strip()
            pieces = _split_by_marker(txt, style) if style else re.split(
                r"\s+(?=(?:\[\d{1,3}\]|\(\d{1,3}\)))", txt)
            if len(pieces) > 1:
                for piece in pieces:
                    if len(piece.strip()) > 6:
                        split_refs.append((norm_space(piece), g))
            elif txt:
                split_refs.append((txt, g))
    pr["references"] = [t for t, _g in split_refs]

    # ---- drop fields that are only undecodable glyph codes
    def pua_ratio(txt: str) -> float:
        if not txt:
            return 0.0
        bad = len(re.findall(r"[\ue000-\uf8ff\ufffd]|\(cid:\d+\)|/uni[0-9A-Fa-f]{6}", txt))
        return bad / max(1, len(txt))

    if pua_ratio(pr.get("title", "")) > 0.30:
        pr["title"] = ""
        empty["uncertain_items"].append(
            {"item": "title", "reason": "title glyphs are undecodable codes, not text"})
    clean_secs = []
    for sec in pr["sections"]:
        if pua_ratio(sec["content"]) > 0.30 and sec["content"]:
            empty["uncertain_items"].append(
                {"item": f"section '{sec['heading'][:40]}'",
                 "reason": "body glyphs are undecodable codes; text withheld rather than guessed"})
            continue
        clean_secs.append(sec)
    pr["sections"] = [{"heading": x["heading"], "content": x["content"], "level": x["level"]}
                      for x in clean_secs]

    # ---- evidence
    ev = empty["evidence"]

    def add_ev(spans: Iterable, key: str):
        for sp in spans:
            ev.append({"page": int(sp.page), "block_bbox": sp.bbox(scale), "target_key": key})

    add_ev(fm.get("title_blocks", []), "title")
    add_ev(fm.get("rest", []), "authors")
    if fm.get("affiliations"):
        add_ev([b for b in fm["rest"] if AFF_RE.search(b.text)][:3], "authors.affiliation")
    add_ev(absd.get("blocks", []), "abstract")
    for i, s in enumerate(sec_out):
        add_ev(([s["head"]] if s.get("head") else []) + s["paras"], f"sections[{i}]")
    if foot_text:
        add_ev(foot_lines, f"sections[{len(sec_out)}]")
    for i, t in enumerate(tables_out):
        add_ev([b for b in [t["cap"]["lines"][0]]] + t["lines"], f"tables[{i}]")
    fig_i = 0
    for c in caps:
        if c["kind"] != "figure":
            continue
        add_ev(c["lines"], f"figures[{fig_i}]")
        for im in [it for it in page_by_no[c["page"]].items if it.kind == "image"]:
            if im.y1 <= c["lines"][0].y0 + 10 and im.y0 > c["lines"][0].y0 - 0.95 * page_by_no[c["page"]].h:
                ev.append({"page": int(im.page),
                           "block_bbox": [r1(im.x0 * scale), r1(im.y0 * scale),
                                          r1(im.x1 * scale), r1(im.y1 * scale)],
                           "target_key": f"figures[{fig_i}]"})
                break
        fig_i += 1
    for i, e in enumerate(eqs):
        ev.append({"page": int(e.page),
                   "block_bbox": [r1(e.bbox[0] * scale), r1(e.bbox[1] * scale),
                                  r1(e.bbox[2] * scale), r1(e.bbox[3] * scale)],
                   "target_key": f"equations[{i}]"})
    for i, (_t, g) in enumerate(split_refs):
        add_ev(g, f"references[{i}]")

    # ---- uncertainty and status
    unc = empty["uncertain_items"]
    if not pr["title"]:
        unc.append({"item": "title", "reason": "no dominant large-font run in the page-1 front "
                    "matter; left empty instead of guessed"})
    if not pr["authors"]:
        unc.append({"item": "authors", "reason": "author names could not be separated from "
                    "affiliations; left empty instead of guessed"})
    if not pr["abstract"]:
        unc.append({"item": "abstract", "reason": "no 'Abstract' section label detected on page 1"})
    for t in tables_out:
        if not t["markdown"]:
            unc.append({"item": t["table_id"], "reason": "caption found but no ≥2-column cell "
                        "grid could be reconstructed; caption kept, cells omitted"})
    for e in eqs:
        if len(e.raw) < 3:
            unc.append({"item": f"Eq. ({e.num})", "reason": "equation number found without a "
                        "recoverable formula body"})
    garbled_total = sum(probe.garbled_per_page)
    garbled_pages = [i + 1 for i, c in enumerate(probe.garbled_per_page) if c > 5]
    if garbled_total > 0.02 * max(1, total_chars):
        unc.append({"item": "text layer", "reason": f"{garbled_total} glyphs decoded as "
                    "(cid:..)/U+FFFD on pages {garbled_pages[:12]}; those runs are preserved "
                    "as-is and may be unreadable without font mapping"})
    thin_pages = []
    for pg in pages:
        body_lines = [l for l in order[pg.number] if abs(l.size - body) <= 0.06 * body]
        if len(body_lines) < 4 and probe.chars_per_page[pg.number - 1] > 200:
            thin_pages.append(pg.number)
    if thin_pages:
        unc.append({"item": f"pages {thin_pages}", "reason": "very little body-size text was "
                    "recovered although the page has a text layer; the page may be a "
                    "full-page figure or a layout this pass could not segment"})

    if pr["sections"] or pr["title"] or pr["references"]:
        status = "partial_complete" if unc else "success"
    else:
        status = "cannot_judge"
        unc.append({"item": "whole document", "reason": "no section text could be assembled "
                    "from the text layer"})
    empty["run_status"] = status

    multi = [p for p, g in gutters_by_page.items() if g]
    empty["status_reason"] = (
        f"Coordinate-driven parse of {len(pages)} pages (body font {body}pt). "
        f"Column gutters found on {len(multi)} page(s) {multi[:12]}; reading order is "
        f"band-then-column, so spans (title, full-width tables) stay in place and no line "
        f"crosses the gutter. {len(marg['detail'])} marginal blocks removed "
        f"(running heads, journal banners, folios, side stamps); "
        f"{sum(len(v) for v in fig_mask.values())} figure-internal vector label lines masked; "
        f"{len(tables_out)} tables rebuilt as grids, {len(eqs)} display equations recovered "
        f"before prose assembly, {len(refs)} references segmented. "
        f"{len(ev)} evidence rows bind every field to page+bbox. run_status={status}."
    )
    if unc:
        empty["status_reason"] += f" {len(unc)} limitation(s) recorded in uncertain_items."

    notes["counts"] = {"sections": len(pr["sections"]), "tables": len(pr["tables"]),
                       "figures": len(pr["figures"]), "equations": len(pr["equations"]),
                       "references": len(pr["references"]), "evidence": len(ev),
                       "uncertain": len(unc)}
    if debug:
        notes["debug"] = {
            "gutters": notes["gutters"],
            "marginalia": marg["detail"],
            "blocks": [{"page": b.page, "bbox": b.bbox(), "size": b.size,
                        "heading": b.heading, "level": b.level, "text": b.text[:200]}
                       for b in blocks],
            "counts": notes["counts"],
        }
    return empty, notes


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Layout-aware PDF -> result.json")
    ap.add_argument("--pdf")
    ap.add_argument("--case")
    ap.add_argument("--out-dir", default="/app/output")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    case_id, pdf = load_case(args.case)
    if args.pdf:
        pdf = args.pdf
    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, "result.json")

    try:
        result, notes = build_result(pdf, case_id, debug=args.debug)
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        result = {
            "case_id": case_id,
            "parsed_result": {"title": "", "authors": [], "abstract": "", "sections": [],
                              "tables": [], "figures": [], "equations": [], "references": []},
            "evidence": [],
            "uncertain_items": [{"item": "whole document",
                                 "reason": f"parser raised {type(exc).__name__}: {exc}"}],
            "run_status": "cannot_judge",
            "status_reason": "Internal parser error; nothing was invented. "
                             + tb.strip().splitlines()[-1],
        }
        notes = {"error": str(exc), "traceback": tb}

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    if args.debug:
        with open(os.path.join(args.out_dir, "parse_debug.json"), "w", encoding="utf-8") as fh:
            json.dump(notes, fh, ensure_ascii=False, indent=2, default=str)

    print(json.dumps({"ok": True, "out": out_path, "status": result["run_status"],
                      "counts": {k: len(v) for k, v in result["parsed_result"].items()},
                      "evidence": len(result["evidence"]),
                      "uncertain": len(result["uncertain_items"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
