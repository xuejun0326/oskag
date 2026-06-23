"""oskag.pipelines — 起的高层流水线 (compare 等).

设计:
- 的 describe 流水线放在 oskag/describe/pipeline.py (历史原因)
- 起新流水线归 oskag/pipelines/ (compare.py, _diff.py 等)
- _diff.py: 0 LLM 调用的纯算法模块 (集合差异 / Jaccard / SimRank)
"""
