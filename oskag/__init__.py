"""oskag - 操作系统内核代码分析工具。

读取代码仓库（对 ArceOS-Starry、rCore-Tutorial 等内核家族有专门支持），生成
中文描述与对比文档。事实信息由 tree-sitter / ripgrep 等工具静态提取，并经第二
轮大模型校验，以减少幻觉。
"""

__version__ = "0.0.1"
__all__ = ["__version__"]
