# pdf-layout-parse

把学术 PDF 解析成带页码与坐标溯源的结构化 JSON（`result.json`）。用坐标重建双栏 / 多栏阅读顺序，剔除页眉页脚与图内矢量文字，单独归类图题、表题、表格网格、公式编号与参考文献；识别加密、纯扫描、部分页识别失败等异常并给出对应状态。

## 目录结构

```
pdf-layout-parse/
├── SKILL.md                  # Skill 主定义（触发条件、流程、输出 schema、状态判定、禁止行为）
├── scripts/
│   └── pdf_struct.py         # 解析主脚本（无第三方 LLM / 网络调用）
└── docs/
    ├── algorithm.md          # 版面解析算法说明（判据与参数复核）
    ├── output_contract.md    # result.json 字段级输出契约
    ├── usage.md              # 最小调用示例与预期耗时
    └── measured_results.md   # 实测结果与基线对照
```

## 快速开始

```bash
python3 scripts/pdf_struct.py --case /input/case.json --out-dir /app/output --debug
# 或显式指定 PDF
python3 scripts/pdf_struct.py --paper /input/paper.pdf --out-dir /app/output --debug
```

## 运行依赖

- Python 3.10+
- poppler-utils（提供 `pdftohtml`）
- `pypdf`

```bash
pip install pypdf
# Ubuntu / Debian
sudo apt-get install poppler-utils
# macOS
brew install poppler
```

脚本不访问网络、不调用任何大模型；文本一律来自 PDF 文本层，不重打、不改写、不补全。

## 输出

严格 schema 见 [`docs/output_contract.md`](docs/output_contract.md)，字段说明与状态判定口径见 [`SKILL.md`](SKILL.md) 第 5、6 节。
