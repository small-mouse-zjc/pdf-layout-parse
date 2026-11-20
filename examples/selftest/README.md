# 自测记录（不是案例答案）

本目录是一次**自测**的原始记录：输入是本仓库 `examples/gen_synthetic.py` 生成的双栏样张，
不是评测平台的任何固定案例；产物也不得被当作案例结果。

| 文件 | 内容 |
| --- | --- |
| `run.log` | 完整运行记录：解析 → 自检 → 基线对照，含时间戳与工具版本 |
| `result.json` | 该合成样张的解析产物（示例） |
| `selfcheck.json` | 自检结果（PASS + 4 项检查） |
| `layout_bench.md` | 同一样张上"内容流顺序 vs 本 Skill 顺序"的对照 |

复现：

```bash
python3 examples/gen_synthetic.py
python3 scripts/pdf_struct.py --pdf synthetic_two_column.pdf --out-dir /tmp/st --debug
python3 scripts/selfcheck.py  --result /tmp/st/result.json --pdf synthetic_two_column.pdf
```
