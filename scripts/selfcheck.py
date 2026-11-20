#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selfcheck.py -- validate result.json against the output contract.

Checks
------
1. Schema: required keys, types, enum of run_status.
2. Evidence 1:1: every parsed field resolves to at least one evidence row, every
   evidence target_key resolves to an existing field, pages and boxes are sane.
3. Status logic: reject / cannot_judge / partial_complete are consistent with the
   payload (e.g. a reject must not carry parsed content).
4. Verbatim sanity: no empty required strings, no duplicated paragraph blocks.

Usage
-----
  python3 selfcheck.py --result /app/output/result.json [--pdf paper.pdf]

Exit code 0 = all checks passed, 1 = at least one ERROR (warnings do not fail).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

STATUSES = {"success", "partial_complete", "cannot_judge", "reject"}
REQUIRED = ["case_id", "parsed_result", "evidence", "uncertain_items",
            "run_status", "status_reason"]
PR_FIELDS = {"title": str, "authors": list, "abstract": str, "sections": list,
             "tables": list, "figures": list, "equations": list, "references": list}


class Report:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []

    def err(self, msg: str):
        self.errors.append(msg)

    def warn(self, msg: str):
        self.warnings.append(msg)

    def ok(self, msg: str):
        self.info.append(msg)

    def dump(self) -> int:
        for m in self.info:
            print(f"  ok   {m}")
        for m in self.warnings:
            print(f"  WARN {m}")
        for m in self.errors:
            print(f"  FAIL {m}")
        print(f"\n{len(self.info)} ok, {len(self.warnings)} warnings, {len(self.errors)} errors")
        return 0 if not self.errors else 1


def check_schema(res: dict, rep: Report):
    for k in REQUIRED:
        if k not in res:
            rep.err(f"missing top-level key: {k}")
    if not isinstance(res.get("case_id"), str) or not str(res.get("case_id")).strip():
        rep.err("case_id must be a non-empty string")
    if res.get("run_status") not in STATUSES:
        rep.err(f"run_status '{res.get('run_status')}' not in {sorted(STATUSES)}")
    pr = res.get("parsed_result")
    if not isinstance(pr, dict):
        rep.err("parsed_result must be an object")
        return
    for k, typ in PR_FIELDS.items():
        if k not in pr:
            rep.err(f"parsed_result missing '{k}'")
        elif not isinstance(pr[k], typ):
            rep.err(f"parsed_result.{k} must be {typ.__name__}")
    for i, s in enumerate(pr.get("sections") or []):
        if set(s) != {"heading", "content", "level"}:
            rep.err(f"sections[{i}] keys must be heading/content/level, got {sorted(s)}")
        elif not isinstance(s["level"], int) or not (1 <= s["level"] <= 6):
            rep.err(f"sections[{i}].level must be int 1..6")
    for i, t in enumerate(pr.get("tables") or []):
        if set(t) != {"table_id", "caption", "markdown"}:
            rep.err(f"tables[{i}] keys must be table_id/caption/markdown")
    for i, f in enumerate(pr.get("figures") or []):
        if set(f) != {"figure_id", "caption"}:
            rep.err(f"figures[{i}] keys must be figure_id/caption")
    for i, e in enumerate(pr.get("equations") or []):
        if set(e) != {"eq_id", "raw_text"}:
            rep.err(f"equations[{i}] keys must be eq_id/raw_text")
    if isinstance(pr.get("references"), list):
        for i, r in enumerate(pr["references"]):
            if not isinstance(r, str):
                rep.err(f"references[{i}] must be a string")
    for i, u in enumerate(res.get("uncertain_items") or []):
        if set(u) != {"item", "reason"}:
            rep.err(f"uncertain_items[{i}] keys must be item/reason")
    if not isinstance(res.get("status_reason"), str) or not res["status_reason"].strip():
        rep.err("status_reason must be a non-empty string")
    rep.ok("schema: required keys, types and enums")


def valid_targets(pr: dict) -> set[str]:
    t = {"title", "authors", "authors.affiliation", "abstract"}
    for i in range(len(pr.get("sections") or [])):
        t.add(f"sections[{i}]")
    for i in range(len(pr.get("tables") or [])):
        t.add(f"tables[{i}]")
    for i in range(len(pr.get("figures") or [])):
        t.add(f"figures[{i}]")
    for i in range(len(pr.get("equations") or [])):
        t.add(f"equations[{i}]")
    for i in range(len(pr.get("references") or [])):
        t.add(f"references[{i}]")
    return t


def check_evidence(res: dict, rep: Report, n_pages: int | None):
    pr = res.get("parsed_result") or {}
    ev = res.get("evidence")
    if not isinstance(ev, list):
        rep.err("evidence must be a list")
        return
    valid = valid_targets(pr)
    seen: dict[str, int] = {}
    for i, e in enumerate(ev):
        if set(e) != {"page", "block_bbox", "target_key"}:
            rep.err(f"evidence[{i}] keys must be page/block_bbox/target_key")
            continue
        if not isinstance(e["page"], int) or e["page"] < 1:
            rep.err(f"evidence[{i}].page must be a positive int")
        if n_pages and isinstance(e["page"], int) and e["page"] > n_pages:
            rep.err(f"evidence[{i}].page {e['page']} exceeds page count {n_pages}")
        bb = e["block_bbox"]
        if not (isinstance(bb, list) and len(bb) == 4 and all(isinstance(v, (int, float)) for v in bb)):
            rep.err(f"evidence[{i}].block_bbox must be [x0,y0,x1,y1] numbers")
        elif bb[2] < bb[0] or bb[3] < bb[1]:
            rep.err(f"evidence[{i}].block_bbox is inverted: {bb}")
        if e["target_key"] not in valid:
            rep.err(f"evidence[{i}].target_key '{e['target_key']}' does not exist in parsed_result")
        seen[e["target_key"]] = seen.get(e["target_key"], 0) + 1

    required = ["title", "abstract"]
    for i, _s in enumerate(pr.get("sections") or []):
        required.append(f"sections[{i}]")
    for i, _t in enumerate(pr.get("tables") or []):
        required.append(f"tables[{i}]")
    for i, _f in enumerate(pr.get("figures") or []):
        required.append(f"figures[{i}]")
    for i, _e in enumerate(pr.get("equations") or []):
        required.append(f"equations[{i}]")
    for i, _r in enumerate(pr.get("references") or []):
        required.append(f"references[{i}]")

    # empty fields need no evidence; skip them
    def is_empty(key: str) -> bool:
        m = re.match(r"(\w+)\[(\d+)\]", key)
        if not m:
            return not pr.get(key)
        name, idx = m.group(1), int(m.group(2))
        lst = pr.get(name) or []
        if idx >= len(lst):
            return True
        item = lst[idx]
        if isinstance(item, dict):
            return not any(str(v).strip() for v in item.values())
        return not str(item).strip()

    missing = [k for k in required if k not in seen and not is_empty(k)]
    if missing:
        rep.err(f"fields without evidence: {missing[:10]}")
    else:
        rep.ok(f"evidence: {len(ev)} rows, every non-empty field covered "
               f"({len(seen)} distinct targets)")
    if pr.get("authors") and "authors" not in seen:
        rep.err("authors non-empty but no evidence bound")


def check_status(res: dict, rep: Report):
    st = res.get("run_status")
    pr = res.get("parsed_result") or {}
    has_content = bool((pr.get("title") or "").strip() or pr.get("sections")
                       or pr.get("references"))
    unc = res.get("uncertain_items") or []
    if st == "reject":
        if has_content:
            rep.err("run_status=reject must not carry parsed content")
        if not any("encrypt" in (u.get("reason", "") + u.get("item", "")).lower() for u in unc):
            rep.warn("reject without an encryption-related uncertain_item")
    if st == "cannot_judge" and has_content:
        rep.warn("cannot_judge but content is present; consider partial_complete")
    if st == "success" and unc:
        rep.warn("success with non-empty uncertain_items; partial_complete may be more honest")
    if st in ("success", "partial_complete") and not has_content:
        rep.err(f"run_status={st} but no content was extracted")
    if st == "partial_complete" and not unc:
        rep.warn("partial_complete without uncertain_items")
    rep.ok(f"status logic: {st}")


def check_verbatim(res: dict, rep: Report):
    pr = res.get("parsed_result") or {}
    txt = " ".join([pr.get("abstract", "")] + [s.get("content", "") for s in pr.get("sections", [])])
    if re.search(r"(?i)(as an ai language model|i cannot (assist|help)|作为\s*AI|抱歉，我)", txt):
        rep.err("text contains model boilerplate: the output must be verbatim PDF text")
    blocks = [s.get("content", "") for s in pr.get("sections", [])]
    dup = [b for b in blocks if b and blocks.count(b) > 1]
    if dup:
        rep.warn(f"{len(set(dup))} duplicated section bodies (cross-page repetition?)")
    refs = pr.get("references") or []
    if refs and len(set(refs)) < 0.8 * len(refs):
        rep.warn("many duplicate reference strings")
    rep.ok("verbatim sanity")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", default="/app/output/result.json")
    ap.add_argument("--pdf", default="")
    args = ap.parse_args(argv)
    rep = Report()
    if not os.path.exists(args.result):
        print(f"FAIL result file not found: {args.result}")
        return 1
    try:
        res = json.load(open(args.result, encoding="utf-8"))
    except Exception as exc:
        print(f"FAIL result.json is not valid JSON: {exc}")
        return 1
    n_pages = None
    if args.pdf and os.path.exists(args.pdf):
        try:
            import pypdf
            n_pages = len(pypdf.PdfReader(args.pdf, strict=False).pages)
        except Exception:
            n_pages = None
    check_schema(res, rep)
    check_evidence(res, rep, n_pages)
    check_status(res, rep)
    check_verbatim(res, rep)
    rc = rep.dump()
    print("SELF-CHECK:", "PASS" if rc == 0 else "FAIL")
    out = {"passed": rc == 0, "errors": rep.errors, "warnings": rep.warnings,
           "n_checks_ok": len(rep.info)}
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(args.result)),
                               "selfcheck.json"), "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
