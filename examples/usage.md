# 最小调用示例

## 1. 一条命令解析（评测环境）

```bash
python3 /skills/weekly-skill/scripts/pdf_struct.py \
        --case /input/case.json \
        --out-dir /app/output --debug
```

标准输出是一行 JSON 摘要，便于 Agent 判断是否继续：

```json
{"ok": true, "out": "/app/output/result.json", "status": "success",
 "counts": {"title": 53, "authors": 2, "abstract": 528, "sections": 4,
            "tables": 1, "figures": 0, "equations": 1, "references": 3},
 "evidence": 38, "uncertain": 0}
```

## 2. 自带样例（可复现）

```bash
# 1) 生成可控的双栏测试件（无需外部数据）
python3 /skills/weekly-skill/examples/gen_synthetic.py     # -> synthetic_two_column.pdf

# 2) 解析
python3 /skills/weekly-skill/scripts/pdf_struct.py \
        --pdf synthetic_two_column.pdf --out-dir /tmp/demo --debug

# 3) 自检
python3 /skills/weekly-skill/scripts/selfcheck.py \
        --result /tmp/demo/result.json --pdf synthetic_two_column.pdf

# 4) 基线对照（可选）
python3 /skills/weekly-skill/scripts/layout_bench.py \
        --pdf synthetic_two_column.pdf --out-dir /tmp/demo

# 5) 证据叠图（可选）
python3 /skills/weekly-skill/scripts/bbox_overlay.py \
        --pdf synthetic_two_column.pdf --result /tmp/demo/result.json \
        --pages 1 --out-dir /tmp/demo/audit
```

## 3. 示意输出（示例，非正式案例结果）

以下摘自上述合成件的 `result.json`，用于说明字段形态；**它是示例，不是任何真实案例的答案**。

```json
{
  "case_id": "synthetic_two_column",
  "parsed_result": {
    "title": "Parsing Two-Column Papers Without Crossing the Gutter",
    "authors": ["A. Author", "B. Author", "C. Author"],
    "abstract": "Two-column scientific layouts are common in engineering and life-science publishing, and naive text extraction walks straight across the gutter. …",
    "sections": [
      {"heading": "1. Column Ordering",
       "content": "Reading order is a property of the layout, not of the content stream. A parser that sorts glyphs by their y coordinate alone will interleave the left and right columns line by line, producing sentences that alternate between two unrelated paragraphs. …",
       "level": 1},
      {"heading": "2. Table Reconstruction",
       "content": "Table reconstruction needs cell geometry rather than whitespace heuristics. …",
       "level": 1}
    ],
    "tables": [
      {"table_id": "Table 1",
       "caption": "Table 1. Measured layout statistics on three layouts.",
       "markdown": "| layout | columns | gutter (pt) | score |\n| --- | --- | --- | --- |\n| single | 1 | 0 | 0.91 |\n| two | 2 | 34 | 0.97 |\n| three | 3 | 18 | 0.94 |"}
    ],
    "figures": [{"figure_id": "Figure 1", "caption": "Figure 1. Reading order of a band that contains a spanning table. Columns are read left to right inside each band."}],
    "equations": [{"eq_id": "Eq. (1)", "raw_text": "y = f(x) + x"}],
    "references": [
      "[1] A. Author. Column-aware extraction. J. Layout, 12(3):1-20, 2021.",
      "[2] B. Author. Table grids from geometry. In Proc. DocEng, 2022.",
      "[3] C. Author. Equation recovery in PDF. PDF Journal, 8:44-59, 2023."
    ]
  },
  "evidence": [
    {"page": 1, "block_bbox": [144.0, 138.0, 936.0, 175.5], "target_key": "title"},
    {"page": 1, "block_bbox": [138.0, 210.0, 561.0, 243.0], "target_key": "sections[0]"},
    {"page": 2, "block_bbox": [123.0, 141.0, 429.0, 165.0], "target_key": "tables[0]"}
  ],
  "uncertain_items": [],
  "run_status": "success",
  "status_reason": "Coordinate-driven parse of 2 pages (body font 19.0pt). Column gutters found on 1 page(s) [1]; reading order is band-then-column …"
}
```

（`evidence` 只截取了 3 条，实际会为每个章节、表格、图、公式、文献逐条列出；bbox 单位是 PDF point。）

## 4. 预期耗时

| 文档规模 | 耗时（单核） |
| --- | --- |
| 2 页合成件 | ~0.4 s |
| 12 页双栏论文 | ~1.7 s |
| 15 页单栏论文 | ~1.7 s |

耗时来自 `pdftohtml` 解析 + 纯 Python 后处理，不含网络与模型调用。

## 5. 在 Agent 会话里的一分钟流程

1. `cat /input/case.json` → 拿到 `case_id` 与 PDF 路径（脚本也会自行搜索 `/input/*.pdf`）。
2. 跑步骤 1 命令 → 看 `status` 与 `counts`。
3. 跑 `selfcheck.py` → 必须 `PASS`。
4. `write` 一份 `/app/output/parse_report.md`：页数、栏数、剔除的页眉页脚、各结构计数、`run_status`、不确定项；如 `task.md` 有额外问题，在此逐条作答并给页码。
5. 结束。不要手工改 `result.json` 的任何文字。
