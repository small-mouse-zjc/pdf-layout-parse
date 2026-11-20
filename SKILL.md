---
name: pdf-layout-parse
version: 2.0.0
description: 把学术 PDF 解析成带页码与坐标溯源的结构化 JSON（result.json）。用坐标重建双栏/多栏阅读顺序，剔除页眉页脚与图内矢量文字，单独归类图题、表题、表格网格、公式编号与参考文献；识别加密、纯扫描、部分页识别失败等异常并给出对应状态。适用于论文阅读、参数抽取、以及任何需要"可复核的结构化全文"的场合。
argument-hint: "--pdf <file> | --case <case.json>  [--out-dir /app/output] [--debug]"
---

# 文献文档解析 Skill

## 1. 这个 Skill 解决什么问题

朴素抽取（`pdftotext` 默认输出、或让模型"读一遍 PDF"）在学术排版上有四类稳定缺陷：

1. **串栏**：按 y 坐标或内容流顺序输出，左栏第 1 行接右栏第 1 行，句子被拼成两篇不相关的段落。
2. **页眉页脚混入正文**：期刊横幅、running head、页码、侧边 arXiv 水印被当成正文首段。
3. **图内矢量文字污染**：图中的坐标刻度、图例（`56-layer`、`0 1 2 3 4`）与正文同流输出。
4. **结构丢失**：图题、表题、表格行列、公式编号、参考文献不再是可定位对象。

本 Skill 用**确定性脚本**解决上述问题，并强制每条结论回绑页码与区块坐标，使结果可逐条复核。文本一律来自 PDF 文本层，模型不重打、不改写、不补全。

## 2. 何时使用

- 输入是学术论文 PDF（期刊双栏、会议双栏、单栏预印本、中文期刊），需要结构化全文而非纯文本。
- 下游要做翻译、问答、参数/结果抽取、跨文献比对，必须先保证阅读顺序正确。
- 需要"每条结论指回原页"的可核查交付。

不适用：纯扫描件（无文本层）——本 Skill 不做 OCR，只如实报 `cannot_judge`；加密件——报 `reject`。

## 3. 输入与输出

| 项 | 说明 |
| --- | --- |
| 输入 | `/input/case.json`（含 `case_id`，可能含 PDF 路径）、`/input/task.md`（可选任务说明）、实际 PDF 文件 |
| 必产出 | `/app/output/result.json`：严格按第 5 节 schema |
| 附带产出 | `/app/output/parse_debug.json`（分栏、页眉过滤、段落切分的中间结果）、`/app/output/selfcheck.json`、`/app/output/layout_bench.md`、`/app/output/parse_report.md` |
| 工作目录 | `/app/work`（可写临时文件） |

`case_id` 必须原样写入输出，不得改写、不得用文件名顶替（仅当 `case.json` 完全没有 id 时才回退到文件名，并在 `status_reason` 说明）。

## 4. 执行流程（按顺序做，不要跳步）

### 步骤 0：确认输入真实存在

```bash
ls -l /input/ 2>/dev/null; cat /input/case.json 2>/dev/null; cat /input/task.md 2>/dev/null
```

若 `/input/case.json` 缺失，直接扫 `/input/*.pdf`；两者都没有，输出
`run_status="cannot_judge"`，`uncertain_items` 写"未找到输入 PDF"，不要编造内容。

### 步骤 1：解析主入口（唯一的文本来源）

```bash
python3 <SKILL_DIR>/scripts/pdf_struct.py --case /input/case.json --out-dir /app/output --debug
```

- `<SKILL_DIR>` 为安装目录（评测环境为 `/skills/weekly-skill`）。
- 若 `case.json` 的字段名与预期不同，脚本会自行搜索任意 `*.pdf` 键与 `/input/**/*.pdf`；也可显式指定：`--pdf /input/paper.pdf`。
- 脚本无第三方依赖，只用 `pypdf` + poppler 的 `pdftohtml`；**不访问网络、不调用任何大模型**。

### 步骤 2：自检（硬门槛）

```bash
python3 <SKILL_DIR>/scripts/selfcheck.py --result /app/output/result.json --pdf /input/<paper>.pdf
```

必须看到 `SELF-CHECK: PASS`。若为 `FAIL`：

| 报错 | 处置 |
| --- | --- |
| `fields without evidence` | 说明写入逻辑漏绑，重跑步骤 1；仍失败则在 `uncertain_items` 记该项，**不得**手写坐标 |
| `schema` 类报错 | 重跑步骤 1，禁止手工拼 JSON |
| `run_status` 与内容不一致 | 重跑；仍不一致则按内容判定状态并在 `status_reason` 说明 |

### 步骤 3：可复核性与增益记录（建议）

```bash
python3 <SKILL_DIR>/scripts/layout_bench.py --pdf /input/<paper>.pdf --out-dir /app/output
python3 <SKILL_DIR>/scripts/bbox_overlay.py --pdf /input/<paper>.pdf \
        --result /app/output/result.json --pages 1 2 --out-dir /app/output/audit
```

`layout_bench.md` 给出"内容流顺序 vs 本 Skill 顺序"的客观对照（跨栏黏连行、栏内倒序、页眉残留）。
`audit/pageN_evidence.png` 把 evidence 的 bbox 画在渲染页上，人工 30 秒即可抽查。

### 步骤 4：写人读摘要

用 `write` 生成 `/app/output/parse_report.md`，内容包括：页数、检出栏数、剔除的页眉页脚条数、各结构计数、`run_status` 与不确定项、以及 task.md 若有额外提问则在文中回答并标注页码。**摘要里不得出现 result.json 中不存在的文字**。

### 步骤 5：若 task.md 提出额外问题

把回答写进 `parse_report.md`（或 `answers.md`），逐条给出结论 + 页码 + bbox；`result.json` 保持第 5 节 schema 不变，不塞入自定义字段。

## 5. 输出 schema（字段名不可改）

```json
{
  "case_id": "string，原样保留",
  "parsed_result": {
    "title": "string",
    "authors": ["string"],
    "abstract": "string",
    "sections": [{"heading": "string", "content": "string", "level": 1}],
    "tables": [{"table_id": "Table 1", "caption": "string", "markdown": "| a | b |\\n| --- | --- |\\n| 1 | 2 |"}],
    "figures": [{"figure_id": "Figure 1", "caption": "string"}],
    "equations": [{"eq_id": "Eq. (1)", "raw_text": "y = F(x,{Wi}) +x."}],
    "references": ["string"]
  },
  "evidence": [{"page": 1, "block_bbox": [75.0, 804.0, 190.0, 820.0], "target_key": "sections[0]"}],
  "uncertain_items": [{"item": "abstract", "reason": "no 'Abstract' label detected on page 1"}],
  "run_status": "success",
  "status_reason": "string，写明本次版面处理生效点与状态依据"
}
```

约定（务必遵守，评测按此对齐）：

- `block_bbox` 为 `[x0, y0, x1, y1]`，单位 **PDF point**（72/inch），原点在页面左上，`y` 向下增长；可直接乘 `dpi/72` 叠到渲染图上。
- `target_key` 与 `parsed_result` 一一对应：`title`、`authors`、`authors.affiliation`（可选）、`abstract`、`sections[i]`、`tables[i]`、`figures[i]`、`equations[i]`、`references[i]`。一个字段可有多条 evidence（多行/多块），但每条 evidence 必须能解析到真实字段。
- `sections[].level`：`1` 为一级标题（`1 Introduction`、`References`），`2` 为 `3.1`，依此类推；行内小标题（`Plain Networks.`）给 `2` 或 `3` 并在 `content` 保留原句。
- `sections[].content` 用 `\n\n` 分隔段落，段落内保留原文换行合并后的空格；**不做去连字符改写**（保留 `learn- ing` 原样，除非明确要求）。
- 脚注不单列字段：若正文有脚注，作为 `heading="Footnotes"`、`level=3` 的 section 附在末尾，内容逐字保留。
- 表格 `markdown` 无表头时用空表头行 `|  |  |` 占位，**不得**臆造表头文字。
- 参考文献为**字符串数组**，按原文编号/作者年顺序，逐条一条。

## 6. 状态判定（必须如实，不许美化）

| 情况 | `run_status` | `uncertain_items` |
| --- | --- | --- |
| 加密且无口令 | `reject` | `whole document`：加密不可解，未解析 |
| 无文本层（纯扫描/图片页 ≥90%） | `cannot_judge` | 说明缺文字层、需 OCR，给出页数 |
| 有文字层但字形无 Unicode 映射（私有区码位/`(cid:xx)`，可读字符 <20%） | `cannot_judge` | 说明需原字体 ToUnicode 或 OCR，给出可读/总字符数 |
| 部分页版面识别失败、个别字段缺失、少量乱码行 | `partial_complete` | 逐页/逐项列出 |
| 全部关键结构抽取成功 | `success` | 空数组 |

判定口径：只要有任一"读不出/不敢确定"的项，就不能写 `success`；只要抽到了正文或结构，就不能写 `cannot_judge`。`status_reason` 必须写明：页数、检出栏的页、剔除的页眉页脚条数、图内文字掩蔽行数、各类结构计数，以及本次处理相对朴素的改进点（跨栏黏连、栏内倒序、页眉残留三项的对照数字可引用 `layout_bench.md`）。

## 7. 禁止行为

- ❌ 用模型重打正文、补全被截断的句子、猜测缺失的作者/数值/结论。
- ❌ 手工编造 `block_bbox` 或页码。
- ❌ 只交纯文本、或改字段名、或把 result.json 写成散文。
- ❌ 把页眉、页码、期刊横幅写进 `abstract` / `sections`。
- ❌ 针对固定测试案例硬编码答案（本 Skill 不含任何案例文本，全部输出由脚本从 PDF 现算）。

## 8. 已知边界（写进 status_reason 与 uncertain_items，不要隐瞒）

- 纯矢量图内的密集标签：以"图注上方、非正文字号的短行"识别并掩蔽，若图注缺失则可能残留少量标签行。
- 无框线且列间距很窄的稠密数值表：网格可能合并相邻列；此时保留 `caption`，`markdown` 为空并记入 `uncertain_items`。
- 扫描件、加密件不做兜底识别（无 OCR、无破解）。
- 双栏页若某一栏无正文（整栏放图），该页不产生栏缝，按整页自上而下排序。
