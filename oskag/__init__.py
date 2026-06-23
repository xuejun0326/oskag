"""oskag - 操作系统内核代码分析工具。

读取内核仓库（RISC-V / LoongArch / ArceOS / rCore-Tutorial 等），生成中文描述
与对比文档。事实信息由 tree-sitter / ripgrep 等工具静态提取，并经第二轮大模型
校验，以减少幻觉。
"""

__version__ = "0.0.1"
__all__ = ["__version__"]
