# 输出契约（result.json 字段级规范）

本文件是 `SKILL.md` 第 5 节的展开版。任何字段的语义、取值、以及"没有证据时怎么写"，以本文件为准。

## 1. 顶层结构

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `case_id` | string | 是 | 原样复制输入 `case.json` 的 id；没有 id 时才回退文件名，并在 `status_reason` 注明 |
| `parsed_result` | object | 是 | 见第 2 节 |
| `evidence` | array | 是 | 见第 3 节，非空（`reject` / `cannot_judge` 除外） |
| `uncertain_items` | array | 是 | 可为空数组，不许缺字段 |
| `run_status` | enum | 是 | `success` / `partial_complete` / `cannot_judge` / `reject` |
| `status_reason` | string | 是 | 非空，说明版面处理生效点与状态依据 |

## 2. parsed_result

### 2.1 title / authors / abstract

- `title`：首页最大字号连续文本块；跨行标题按行合并，用单空格连接。找不到则空字符串 + `uncertain_items`。
- `authors`：姓名数组，逐个元素是一位作者（`["Kaiming He", "Xiangyu Zhang"]`）。同一基线上的多个姓名按片段间隙切分，不合并成一条。
- `authors.affiliation`：不设字段；单位/邮箱行不进入 `authors`，可选地在 evidence 里用 `authors.affiliation` 记录（便于审计），不写入 `authors`。
- `abstract`：`Abstract`/`摘要` 标签之后、下一个标题之前的正文，逐字保留。

### 2.2 sections

```json
{"heading": "3.2. Identity Mapping by Shortcuts", "content": "第一段…\n\n第二段…", "level": 2}
```

- `level`：编号层数（`3` → 1，`3.1` → 2，`3.1.2` → 3）；无编号但为独行标题（`Abstract`、`References`、`Acknowledgments`）→ 1；行内小标题 → 2 或 3。
- `content`：段落之间用 `\n\n`；段落内部不保留 PDF 换行，只保留词间空格。**不去连字符**（`learn- ing` 原样保留），因为改写会破坏"逐字一致"。
- 允许 `content` 为空（例如一级标题下直接是子标题）：这是原文档的真实结构，不要填造文字。
- 参考文献、图题、表题、脚注**不混入** sections 正文。

### 2.3 tables

```json
{"table_id": "Table 1", "caption": "Table 1. Architectures for ImageNet. …", "markdown": "| … |"}
```

- `table_id` 统一为 `Table <原文编号>`。
- `caption` 为表题原文（含 `Table 1.` 前缀与续行）。
- `markdown` 由单元格坐标聚类重建：
  - 表头可判定（首行全为非数值短标签）→ 首行为表头；
  - 不可判定 → 首行为空表头 `|  |  |` 占位，所有数据行完整保留，不臆造列名。
- 无法重建 ≥2 列时：保留 `caption`，`markdown` 置空字符串，并在 `uncertain_items` 记录原因。

### 2.4 figures

```json
{"figure_id": "Figure 1", "caption": "Figure 1. Training error (left) and test error (right) on CIFAR-10 …"}
```

本课题不要求复现图像，只提取图注；`figure_id` 统一为 `Figure <编号>`，`caption` 逐字保留（含 `Fig. 1.` 这类原始写法）。

### 2.5 equations

```json
{"eq_id": "Eq. (1)", "raw_text": "y =F(x,{Wi}) +x."}
```

- 只收录**带编号**的展示公式；编号取自右端 `(1)`、`(2a)`。
- `raw_text` 为按 x 排序的字形拼接（数学排版本身有空隙，不重排、不改写上下标写法）。
- 编号存在但公式体不可恢复时，`raw_text` 置空字符串并记入 `uncertain_items`。

### 2.6 references

字符串数组，一条文献一个元素，按原文顺序；`[1] …`、`1. …`、`Author, A. …` 三种起始样式都按同一标记切分。

## 3. evidence

```json
{"page": 1, "block_bbox": [75.0, 804.0, 190.0, 820.0], "target_key": "sections[0]"}
```

- 坐标系：PDF point，原点左上，`y` 向下；`[x0, y0, x1, y1]`，满足 `x1 ≥ x0`、`y1 ≥ y0`。
- `page` 从 1 开始；`1 ≤ page ≤ 文档页数`。
- `target_key` 必须能在 `parsed_result` 中解析到实际字段（见下表）；一个字段允许多条 evidence（逐块绑定），但不允许"有内容无证据"。
- `reject` / `cannot_judge` 允许 `evidence` 为空数组（无可提取内容）。

| target_key | 指向 |
| --- | --- |
| `title` | `parsed_result.title` |
| `authors` | `parsed_result.authors`（每行/每块一条） |
| `authors.affiliation` | 可选，仅用于审计单位行 |
| `abstract` | `parsed_result.abstract` |
| `sections[i]` | 第 i 个 section 的标题块与正文块 |
| `tables[i]` | 第 i 个表的表题与表格行 |
| `figures[i]` | 第 i 个图的图注（含图像区域 bbox，如有） |
| `equations[i]` | 第 i 个公式的公式块与编号块 |
| `references[i]` | 第 i 条参考文献 |

## 4. run_status 判定

| 条件 | 状态 |
| --- | --- |
| 加密，无法用空口令打开 | `reject` |
| 可抽取字符 ≥90% 页面为空、或全篇 < 40 字符/页 | `cannot_judge`（需 OCR） |
| 有内容但存在缺字段、页码乱、字体乱码、表格无法重建、个别页版面失败 | `partial_complete` |
| 关键结构齐全、自检通过 | `success` |

`status_reason` 建议模板：

```
坐标驱动解析 N 页（正文字号 Xpt）。在 M 页检出栏缝 [页码列表]；阅读顺序为"先分带、再分栏"，
跨栏元素（标题、通栏表）原位保留，未出现跨栏黏连行。剔除 K 条页眉/页脚/页码/侧边水印，
掩蔽 J 行图内矢量标签；重建 T 个表格、E 个公式、R 条参考文献，共 V 条证据绑定页码与坐标。
run_status=S。共 U 项限制见 uncertain_items。
```

## 5. 最小合法样例（单页、结构最少）

```json
{
  "case_id": "demo-001",
  "parsed_result": {
    "title": "A Minimal Demo",
    "authors": ["A. Author"],
    "abstract": "We show a minimal document.",
    "sections": [{"heading": "1. Introduction", "content": "Text of the introduction.", "level": 1}],
    "tables": [],
    "figures": [],
    "equations": [],
    "references": ["[1] A. Author. A reference. J. Demo, 2020."]
  },
  "evidence": [
    {"page": 1, "block_bbox": [72.0, 90.0, 300.0, 108.0], "target_key": "title"},
    {"page": 1, "block_bbox": [72.0, 120.0, 300.0, 136.0], "target_key": "authors"},
    {"page": 1, "block_bbox": [72.0, 160.0, 300.0, 200.0], "target_key": "abstract"},
    {"page": 1, "block_bbox": [72.0, 240.0, 300.0, 258.0], "target_key": "sections[0]"},
    {"page": 1, "block_bbox": [72.0, 620.0, 300.0, 640.0], "target_key": "references[0]"}
  ],
  "uncertain_items": [],
  "run_status": "success",
  "status_reason": "单页单栏示例；坐标驱动解析 1 页，1 个标题、1 个章节、1 条参考文献，5 条证据。"
}
```
