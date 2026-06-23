#!/usr/bin/env python3
"""检查发行版源码是否残留内部/AI 痕迹。

用法:
    python check_traces.py            # 扫描整个 oskag/ 包
    python check_traces.py oskag/facts/scan.py   # 只扫指定文件

发现痕迹会列出文件名、行号和原文，方便你手动清理。
退出码: 有痕迹返回 1, 干净返回 0。
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 需要警惕的痕迹模式。key 是说明，value 是正则。
PATTERNS = {
    "里程碑标记 (M1-M5 / S数字)": re.compile(r"\bM[1-9]\b|M[1-9]\.S[0-9]+"),
    "Claude / Claude Code": re.compile(r"[Cc]laude"),
    "SiliconFlow / RAG / 嵌入死配置": re.compile(r"[Ss]ilicon[Ff]low|bge-m3|\bBM25\b|rank_bm25|lancedb"),
    "代码检查工具内部标注": re.compile(r"SonarQube|S[0-9]{3,}"),
    "比赛 / 内部词": re.compile(r"大赛|参赛|赛题|里程碑|oskag dev"),
    # 注意: 这里只匹配作为产品名出现的 Cursor (前后有空格/标点), 不匹配
    # tree-sitter 的 cursor / QueryCursor 等变量名, 避免误报。
    "其他 AI 工具名": re.compile(r"[Cc]opilot|[Cc]hat\s?GPT|(?<![A-Za-z])Cursor(?![A-Za-z])|GPT-[34]"),
    "开发文档引用": re.compile(r"STEPS\.md|PLAN\.md|WORKFLOW\.md"),
}

# 误报白名单：这些词包含上面的模式但其实是正常代码/技术术语
WHITELIST = re.compile(r"MAX|MODULE|MERMAID|Markdown|markdown|MarkupSafe|FROM|format|Make|HMAC|S1192-not-real")


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    hits = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return hits
    for i, line in enumerate(text.splitlines(), 1):
        for label, pat in PATTERNS.items():
            for m in pat.finditer(line):
                # 跳过白名单误报
                ctx = line[max(0, m.start() - 3): m.end() + 3]
                if WHITELIST.search(ctx) and label.startswith(("里程碑", "代码检查")):
                    continue
                hits.append((i, label, line.strip()))
                break  # 同一行命中一个就够，不重复报
    return hits


def main() -> int:
    if len(sys.argv) > 1:
        targets = [Path(a) for a in sys.argv[1:]]
    else:
        pkg = HERE / "oskag"
        targets = list(pkg.rglob("*.py")) + list(pkg.rglob("*.j2"))
        targets += [HERE / "pyproject.toml", HERE / "README.md", HERE / "USAGE.md", HERE / ".env.example"]

    total = 0
    for f in targets:
        if "__pycache__" in str(f) or not f.exists():
            continue
        hits = scan_file(f)
        if hits:
            print(f"\n[{f}]")
            for line_no, label, content in hits:
                print(f"  L{line_no}  ({label})")
                print(f"      {content[:90]}")
            total += len(hits)

    print("\n" + "=" * 50)
    if total:
        print(f"发现 {total} 处疑似痕迹，请检查并清理上面列出的位置。")
        return 1
    print("干净，未发现痕迹。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
