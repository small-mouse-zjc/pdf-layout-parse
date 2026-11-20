# pdf-layout-parse

面向学术 PDF 的**版面感知结构化解析** Skill：把论文解析成带页码与坐标溯源的 `result.json`，供阅读、问答、参数抽取等下游任务使用。

核心主张只有一句：**阅读顺序由坐标决定，文本由 PDF 文本层决定，模型不参与重写。**

## 为什么不是"把 PDF 丢给模型读一遍"

| 朴素做法 | 后果 |
| --- | --- |
| 按 y 坐标排序 | 左右栏逐行交替，句子被拼成两篇不相关段落 |
| 按内容流顺序输出 | 跨栏标题、通栏表与栏内文字黏成一行 |
| 全文照搬 | 期刊横幅、running head、页码、arXiv 侧边水印进入正文首段 |
| 图中文字同流输出 | `56-layer`、`0 1 2 3 4`、`3x3 conv, 64` 混入段落 |
| 让模型"读图补字" | 出现原文没有的句子与数值，且无法溯源 |

本仓库用确定性脚本处理这些版面问题，并把每一条输出绑回 `(页码, bbox)`。

## 目录结构

```
submission/
├── SKILL.md                      # Skill 主体：何时用、怎么跑、输出契约、禁止行为
├── README.md                     # 本文件
├── scripts/
│   ├── pdf_struct.py             # 解析主程序（无 LLM、无网络）
│   ├── selfcheck.py              # schema + 证据双向校验 + 状态一致性
│   ├── layout_bench.py           # 内容流顺序 vs 本 Skill 顺序的客观对照
│   └── bbox_overlay.py           # 把 evidence 的 bbox 画到渲染页上，供人工抽查
├── reference/
│   ├── output_contract.md        # 字段级契约与 status 判定
│   ├── algorithm.md              # 算法与参数（为何这样切栏、切段、切表）
│   ├── troubleshooting.md        # 现象 → 原因 → 处置
│   └── measured_results.md       # 实测数字与已知不达标项
├── src/reproduce.py             # 一条命令复现：生成样张 → 解析 → 自检 → 基线对照 → results/metrics.json
├── results/                     # metrics.json（实测数值）与 run.log（真实运行记录）
├── characterization.json        # 与源文档的实测偏差、适用边界、失效模式
├── arm_manifest.json            # ARM v1.1 清单（入口、执行块、预期产物、轨迹）
├── trace/trace.jsonl            # 本次会话的步骤记录
├── GITHUB_URL.txt               # 作品公开链接
└── examples/
    ├── usage.md                  # 最小调用示例 + 示意输出
    ├── gen_synthetic.py          # 生成可控双栏测试件（可复现）
    └── selftest/                 # 自测运行记录（非案例答案，见其 README）
```

## 依赖

| 组件 | 版本 | 用途 | 缺失时 |
| --- | --- | --- | --- |
| Python | ≥3.9（实测 3.12） | 运行时 | 必需 |
| poppler-utils（`pdftohtml`） | 实测 24.02 | 取字形坐标与字号 | 报 `cannot_judge`（无坐标即无法排序） |
| `pypdf` | ≥3.0（实测 6.10） | 页数、加密、乱码探测 | 降级：加密检测失效 |
| `numpy` | 可选 | 覆盖率剖面加速 | 自动降级为计数判据 |
| Pillow + `pdftoppm` | 可选 | `bbox_overlay.py` 叠图 | 仅影响人工抽查 |

安装：`pip install pypdf numpy pillow`；poppler 由系统包管理器提供（`apt-get install poppler-utils`）。

## 复现

```bash
python3 src/reproduce.py            # 生成样张、解析、自检、对照基线，写出 results/metrics.json
python3 src/reproduce.py --pdf my.pdf   # 或直接在你的文档上跑
```

`results/run.log` 是产出 `results/metrics.json` 的那次运行的真实记录；两者数值一致。

## 快速开始

```bash
python3 scripts/pdf_struct.py --pdf paper.pdf --out-dir out --debug
python3 scripts/selfcheck.py  --result out/result.json --pdf paper.pdf
cat out/result.json | head -40
```

评测环境（`SKILL.md` 约定的路径）：

```bash
python3 /skills/weekly-skill/scripts/pdf_struct.py --case /input/case.json --out-dir /app/output --debug
```

## 设计要点（详见 `reference/algorithm.md`）

1. **字号表是文档级的**：pdftohtml 的 `fontspec` 只在该字体首次出现时输出，按页解析会丢掉后续页字号，进而毁掉标题/作者识别 —— 初版正因此把所有分节标题降级为正文。
2. **两遍行装配**：先用保守间隙（1.25 em）求栏缝，再用放宽间隙（6 em）回收行内数学与两端对齐空格，且放宽时受"不得跨越栏缝、不得跨越 5% 字号差"双重保护。
3. **栏缝 = 左边距聚类的配对**，不是"空白列"：跨栏标题/通栏表会让空白列不空，但挡不住"左栏右边界的 90 分位—右栏左边距"这一对几何量；再加"左簇 50% 以上在本栏内闭合"以排除单栏页的伪栏缝。
4. **先分带再分栏**：跨栏元素各自成带，带内左栏读完再读右栏；比递归 XY-cut 更抗通栏元素。
5. **先摘公式再建段**：数学字形若留在正文流中会把段落打碎（上下标各自成行）。
6. **图注定位 + 图内标签掩蔽**：从图注向上扫同一 x 带，遇到正文字号长行即停；落在 `<image>` 内的行一律掩蔽。
7. **表格从单元格几何重建**：行按基线合并（通栏表被栏缝切开的两半在此重组），列按单元格区间重叠聚类（居中列也能归位），表头不可判定时用空表头占位、数据一行不丢。

## 实测效果

在 12 页双栏会议论文、15 页单栏论文与 2 页可控合成件上（`reference/measured_results.md` 有完整表格）：

| 指标 | 内容流顺序 | 本 Skill |
| --- | --- | --- |
| 跨栏黏连行占比（三件） | 3.3% / 10.8% / 15.9% | 0.1% / 0% / 0% |
| 栏内倒序占比（三件） | 7.6% / 10.7% / 2.8% | 0% / 0% / 0% |
| 页眉页码残留（条） | 14 / 16 / 4 | 0 / 0 / 0 |

异常分支：加密件 → `reject`；纯图片件 → `cannot_judge` 且写明需 OCR；两者都不产出任何臆造内容。

## 已知边界

- 稠密无框线表格可能出现相邻列合并；表题保留、网格置空并记 `uncertain_items`。
- 图注中段若被大空格拆成独立字形对象且与图件同基线，可能漏接（实测 1 处，漏 6 词）。
- 公式只收带编号的展示公式，上下标以拼接形式给出，不做语义化改写。
- 不做 OCR、不破解加密；扫描件一律 `cannot_judge`。

## 溯源与许可

- 合成测试件由 `examples/gen_synthetic.py` 生成，无第三方版权内容。
- 评测用的真实论文来自公开预印本（arXiv），仅在 `reference/measured_results.md` 中以标题与编号引用，不随仓库分发原文。
- 代码为本次参赛原创，依赖均为开源组件（poppler GPL、pypdf BSD、numpy BSD）。

## 版本记录

| 版本 | 变更 |
| --- | --- |
| 2.0.0 | 重写栏缝检测（左边距聚类 + 闭合比例）、两遍行装配、公式先摘、表格区间聚类、表头占位策略、证据双向自检 |
| 1.x | 初版：全局字表、XY-cut 排序、页眉位置判据（在含通栏标题的页面上失效，已废弃） |
