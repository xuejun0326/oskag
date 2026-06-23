"""oskag.tools — 与外部世界交互的工具层 (rg / tree-sitter / 文件系统).

设计原则:
- 0 LLM 调用 (全程纯确定性)
- 子进程统一走 _subprocess.run_tool, 强制 UTF-8, 拒 shell=True
- 路径越权检查 (workspace 内才允许)
- 工具不可用时 graceful fallback, 写 warnings, 不崩
"""
