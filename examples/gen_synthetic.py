#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate a controllable two-column test PDF (title/abstract spanning, body
columns, a full-width table, a numbered equation, a figure caption, running
banner and folio).  Run:  python3 gen_synthetic.py  -> synthetic_two_column.pdf
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

W, H = 8.5, 11.0          # inches
LM, RM = 0.85, 7.65       # body extent
GUT = 4.25                # gutter centre
def line(ax, x, y, s, size=9, weight="normal", style="normal"):
    ax.text(x, y, s, fontsize=size, family="serif", weight=weight, style=style,
            ha="left", va="baseline")

def wrap(txt, n=64):
    out, cur = [], ""
    for w in txt.split():
        if len(cur) + len(w) + 1 > n:
            out.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur: out.append(cur)
    return out

P1 = "Two-column scientific layouts are common in engineering and life-science publishing, and naive text extraction walks straight across the gutter. This synthetic manuscript exercises a parser on a controllable layout: spanning title, spanning abstract, two body columns, a full-width table, a numbered display equation, a figure caption and a reference list. The intended reading order is column-major inside each band and top-to-bottom across bands. Margins carry a running banner and a folio that must never reach the body text."
P2 = "Reading order is a property of the layout, not of the content stream. A parser that sorts glyphs by their y coordinate alone will interleave the left and right columns line by line, producing sentences that alternate between two unrelated paragraphs. Sorting by x first is equally wrong, because it destroys the vertical flow inside a column. The correct model cuts the page into bands at spanning elements and then reads each column of a band from top to bottom."
P3 = "Table reconstruction needs cell geometry rather than whitespace heuristics. When a tabular block is drawn without ruling lines, the only reliable signal is the alignment of cell boxes across rows, combined with the vertical gaps between cells inside a row. Column anchors derived from those boxes let a grid be rebuilt as Markdown, which is what downstream parameter extraction expects."
P4 = "Equations are detected before prose assembly, because their glyph runs are interleaved with superscripts and subscripts that would otherwise fragment a paragraph. LaTeX math fonts are a strong signal, and the equation number at the right margin confirms the block. The raw text is joined in reading order without rewriting the mathematics."

with PdfPages("synthetic_two_column.pdf") as pdf:
    for page_no in (1, 2):
        fig = plt.figure(figsize=(W, H))
        ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, W); ax.set_ylim(0, H); ax.axis("off")
        # running banner + folio
        ax.text(W/2, H-0.45, "Journal of Synthetic Layout Research, Vol. 3, No. 2, 2024",
                fontsize=7.5, family="serif", ha="center")
        ax.text(W/2, 0.42, str(page_no), fontsize=9, family="serif", ha="center")
        y = H - 1.15
        if page_no == 1:
            line(ax, 2.0, y, "Parsing Two-Column Papers Without Crossing the Gutter", 15, "bold")
            y -= 0.35
            line(ax, 2.55, y, "A. Author, B. Author and C. Author", 10)
            y -= 0.25
            line(ax, 2.9, y, "Institute of Layout Studies", 8, style="italic")
            y -= 0.45
            line(ax, LM, y, "Abstract", 10, "bold"); y -= 0.22
            for s in wrap(P1, 96):
                line(ax, LM, y, s, 8.5); y -= 0.16
            y -= 0.12
        # two columns of body
        ytop = y
        for col, (x0, txt, head) in enumerate([(LM, P2, "1. Column Ordering"),
                                               (GUT+0.28, P3, "2. Table Reconstruction")]):
            yy = ytop
            line(ax, x0, yy, head, 10, "bold"); yy -= 0.22
            for s in wrap(txt, 44):
                line(ax, x0, yy, s, 8.5); yy -= 0.16
            if page_no == 2 and col == 0:
                yy -= 0.16
                line(ax, x0+0.5, yy, "y = f(x) + x", 9); ax.text(x0+3.0, yy, "(1)", fontsize=9, family="serif")
                yy -= 0.28
                for s in wrap(P4, 44):
                    line(ax, x0, yy, s, 8.5); yy -= 0.16
        if page_no == 1:
            # full-width table + caption
            ty = 2.35
            line(ax, LM, ty+0.10, "Table 1. Measured layout statistics on three layouts.", 8.5)
            rows = [("layout", "columns", "gutter (pt)", "score"),
                    ("single", "1", "0", "0.91"),
                    ("two", "2", "34", "0.97"),
                    ("three", "3", "18", "0.94")]
            for i, r in enumerate(rows):
                yy = ty - 0.05 - i*0.18
                for x, c in zip((LM, LM+1.5, LM+3.0, LM+4.6), r):
                    line(ax, x, yy, c, 8)
            line(ax, LM, 1.55, "Figure 1. Reading order of a band that contains a spanning table.", 8.5)
            line(ax, LM, 1.35, "Columns are read left to right inside each band.", 8.5)
        if page_no == 2:
            ry = 1.75
            line(ax, LM, ry, "References", 10, "bold"); ry -= 0.22
            refs = ["[1] A. Author. Column-aware extraction. J. Layout, 12(3):1-20, 2021.",
                    "[2] B. Author. Table grids from geometry. In Proc. DocEng, 2022.",
                    "[3] C. Author. Equation recovery in PDF. PDF Journal, 8:44-59, 2023."]
            for r in refs:
                line(ax, LM, ry, r, 8); ry -= 0.17
        pdf.savefig(fig); plt.close(fig)
print("written")
