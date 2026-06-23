"""oskag.eval — 评估子包 .

子模块:
- citation_validator: 扫 markdown 报告里的 `file:line` 引用, 验证文件存在 / 行号合理.
- verdict_stats: 4 仓 describe 的 verifier_verdicts 聚合统计.
- ground_truth: 2 对人工标注的 ground truth 与 oskag 输出做 recall/precision/F1.
"""
from __future__ import annotations
