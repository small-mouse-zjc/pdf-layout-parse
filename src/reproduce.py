#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""src/reproduce.py -- one-command reproduction of the skill's measured behaviour.

It does three things on a controllable sample, and writes the numbers it obtains
to results/metrics.json:

  1. regenerate the two-column sample PDF (examples/gen_synthetic.py, matplotlib)
  2. parse it with the skill (scripts/pdf_struct.py)
  3. validate the output (scripts/selfcheck.py) and measure the layout gain
     against the content-stream baseline (scripts/layout_bench.py)

Nothing here is hard-coded: every number in results/metrics.json is read back
from the run. Use --pdf to run on your own document instead of the sample.

Usage:
  python3 src/reproduce.py [--pdf paper.pdf] [--out results]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def run(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    cp = subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True)
    return cp.returncode, cp.stdout, cp.stderr


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default="")
    ap.add_argument("--out", default="results")
    args = ap.parse_args(argv)

    os.makedirs(os.path.join(ROOT, args.out), exist_ok=True)
    py = sys.executable
    t0 = time.time()

    pdf = args.pdf
    if not pdf:
        gen = os.path.join(ROOT, "examples", "gen_synthetic.py")
        rc, out, err = run([py, gen], cwd=os.path.join(ROOT, "examples"))
        if rc != 0:
            print("generator failed (matplotlib missing?):", err.strip()[:300])
            print("pass --pdf <your document> instead")
            return 2
        pdf = os.path.join(ROOT, "examples", "synthetic_two_column.pdf")
        print(f"generated sample: {pdf}")

    work = os.path.join(ROOT, args.out, "work")
    os.makedirs(work, exist_ok=True)
    t_parse = time.time()
    rc, out, err = run([py, os.path.join(ROOT, "scripts", "pdf_struct.py"),
                        "--pdf", pdf, "--out-dir", work])
    parse_s = time.time() - t_parse
    print(f"parse rc={rc} {parse_s:.2f}s :: {out.strip()[:200]}")
    if rc != 0:
        print(err.strip()[:400])
        return rc

    t_check = time.time()
    rc2, out2, _ = run([py, os.path.join(ROOT, "scripts", "selfcheck.py"),
                        "--result", os.path.join(work, "result.json"), "--pdf", pdf])
    check_s = time.time() - t_check
    passed = "SELF-CHECK: PASS" in out2
    print(f"selfcheck {'PASS' if passed else 'FAIL'} ({check_s:.2f}s)")

    rc3, out3, _ = run([py, os.path.join(ROOT, "scripts", "layout_bench.py"),
                        "--pdf", pdf, "--out-dir", work])
    bench = None
    bench_path = os.path.join(work, "layout_bench.json")
    if rc3 == 0 and os.path.exists(bench_path):
        bench = json.load(open(bench_path, encoding="utf-8"))

    res = json.load(open(os.path.join(work, "result.json"), encoding="utf-8"))
    pr = res.get("parsed_result", {})
    metrics = {
        "input_pdf": os.path.basename(pdf),
        "run_status": res.get("run_status"),
        "selfcheck_passed": bool(passed),
        "counts": {
            "authors": len(pr.get("authors", [])),
            "abstract_chars": len(pr.get("abstract", "")),
            "sections": len(pr.get("sections", [])),
            "tables": len(pr.get("tables", [])),
            "figures": len(pr.get("figures", [])),
            "equations": len(pr.get("equations", [])),
            "references": len(pr.get("references", [])),
            "evidence_rows": len(res.get("evidence", [])),
        },
        "timing_s": {"parse": round(parse_s, 3), "selfcheck": round(check_s, 3),
                     "total": round(time.time() - t0, 3)},
    }
    if bench:
        metrics["layout_gain"] = {
            "pages": bench["pages"],
            "pages_with_gutter": bench["pages_with_gutter"],
            "cross_column_lines": {"stream": bench["stream"]["cross_column_lines"],
                                   "skill": bench["skill"]["cross_column_lines"]},
            "order_inversions": {"stream": bench["stream"]["order_inversions"],
                                 "skill": bench["skill"]["order_inversions"]},
            "marginalia_left_in_body": {"stream": bench["stream"]["marginalia_leak"],
                                        "skill": bench["skill"]["marginalia_leak"]},
        }
    out_path = os.path.join(ROOT, args.out, "metrics.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, ensure_ascii=False, indent=2)
    print("wrote", os.path.relpath(out_path, ROOT))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
