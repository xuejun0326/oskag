# oskag

## 题目

- **题目 ID**：proj18
- **题目内容**：设计智能体（Agent）对历史上的操作系统比赛内核赛道作品进行描述；将新提交的作品和历史上的作品进行比较。描述和比较都要求生成对人类友好的描述/比较文档。

## 简介

oskag 是一个命令行代码分析工具。给定一个代码仓库，它会读取源码并借助大模型生成一份中文分析报告，报告中的每条结论都标注了出处（文件名与行号），并经过一轮自动校验，以减少大模型的凭空臆造。除了分析单个仓库，它也支持对比两个仓库，识别其中的相似与差异。

oskag 对 Rust 编写的操作系统内核仓库（如 ArceOS-Starry、rCore 系列）做了专门适配，会按启动、内存、调度、文件系统等固定维度展开分析；面对其他类型的项目（编译器、库、应用等），则根据目录结构自动组织章节。

## 功能

- **describe**：`oskag describe <仓库>` 生成单个仓库的分析报告，包含整体定位与评分、各模块的实现思路与不足、关键数据结构和接口，以及一份引用索引（列出每条结论对应的代码片段）。
- **compare**：`oskag compare <仓库A> <仓库B>` 对比两个仓库。相似度由代码静态计算得出——分别比对函数签名、调用图、syscall 列表和依赖项，大模型仅负责将结果组织成文字。

报告中凡是能由代码静态得出的信息（代码行数、函数列表、依赖关系、调用图等），均由 ripgrep、tokei、tree-sitter 等工具计算，不交给大模型估算。大模型给出的每条判断都必须附带文件名与行号，随后的校验环节会重新读取对应代码，确认结论成立与否，未通过的会在报告中标记。

## 安装

需要 Python 3.11 或更高版本，并预先安装两个命令行工具：ripgrep（代码搜索）和 tokei（代码行统计）。

Windows：

```powershell
winget install BurntSushi.ripgrep.MSVC
winget install XAMPPRocky.Tokei
```

macOS：

```bash
brew install ripgrep tokei
```

安装后确认 `rg --version` 和 `tokei --version` 均能正常输出，随后安装 Python 包：

```bash
cd oskag
pip install -e .
```

如需运行测试或代码检查，安装时附带 dev 依赖：

```bash
pip install -e ".[dev]"
```

## 配置

oskag 使用 DeepSeek API，需先到 platform.deepseek.com 申请一个 API key。

在 oskag 目录的上一级新建 `.env` 文件并填入 key（可参考随附的 `.env.example`）：

```ini
DEEPSEEK_API_KEY=sk-你的key
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

完成后运行 `oskag config-check`，它会检查 key 是否可读、API 是否连通。

## 使用

分析一个仓库分为两步，先抽取事实再生成报告：

```bash
oskag facts /path/to/repo       # 扫描仓库，不调用大模型，速度快
oskag describe /path/to/repo    # 生成报告，内核仓库约需二三十分钟
```

报告输出到 `reports/` 目录，其中 `.md` 供阅读，`.json` 供 compare 使用。

对比两个仓库时，先分别对它们运行过 describe，再执行：

```bash
oskag compare /path/to/A /path/to/B
```

describe 的常用参数：

| 参数 | 说明 |
| --- | --- |
| `--module <列表>` | 只分析指定模块，逗号分隔。用于调试或重跑某个模块 |
| `--out <路径>` | 指定报告输出位置 |
| `--no-cache` | 忽略缓存，重新生成 |
| `--max-verifier 0` | 跳过校验环节，明显加快速度 |

内核仓库的模块名为：boot、mm、task、fs、signal、ipc、net、drivers、syscall。完整的参数与命令说明见 [USAGE.md](USAGE.md)。

## 工作流程

一次完整的 describe 分为四个阶段：

1. **facts**：扫描仓库，计算代码行数、函数签名、依赖、syscall 表、调用图等信息，保存为 `facts.json`。此阶段不调用大模型。
2. **describe**：对每个模块单独处理，大模型可主动读取相关文件取证，产出带引用的分析。
3. **校验**：抽取报告中的引用，重新读取对应代码，核对结论是否成立，不成立的予以标记。
4. **综合**：汇总为整体评价与评分，最终渲染为 markdown。

大模型偶尔会输出格式损坏的 JSON（中途截断、多余逗号、代码片段中引号未转义等）。针对这种情况，解析采用逐级降级：先按原样解析，失败则修复结构，再失败则用正则提取正文，最后才退回重新生成一段摘要。因此单个模块的输出损坏通常不会导致整段分析丢失。

## 项目结构

```text
oskag/
├── README.md
├── USAGE.md                 完整使用说明
├── pyproject.toml
├── .env.example             配置示例
└── oskag/                   Python 包源码
    ├── cli.py               命令行入口
    ├── llm.py               DeepSeek 客户端（thinking / 重试 / 缓存）
    ├── agent.py             工具循环
    ├── config.py            .env 加载
    ├── cache.py             本地缓存
    ├── logging_setup.py     日志
    ├── tools/               大模型可调用的工具（文件读取、代码行、语法解析）
    ├── facts/               确定性事实抽取
    │   ├── profiles.py      内核家族画像
    │   ├── project_type.py  项目类型识别
    │   ├── scan.py          文件扫描与代码行统计
    │   ├── cargo.py         Cargo 依赖
    │   ├── syscalls.py      syscall 表抽取
    │   ├── signatures.py    函数签名
    │   ├── call_graph.py    调用图（Jaccard / SimRank）
    │   ├── repomap.py       仓库地图
    │   └── boot.py          启动流程
    ├── describe/            describe 流水线
    │   ├── pipeline.py      主编排与 JSON 容错
    │   ├── _modules.py      模块发现与命名
    │   └── token_budget.py  上下文预算控制
    ├── pipelines/           compare 流水线
    ├── eval/                引用校验
    ├── prompts/             中文提示词模板
    └── render/              markdown 渲染
```

## 技术栈

- 语言与 CLI：Python 3.11、typer、rich
- 代码解析：tree-sitter、ripgrep、tokei
- 调用图：networkx（Jaccard、SimRank）
- 大模型：DeepSeek，日常分析用 flash，综合评价用 pro；agent 工具循环为自行实现
- 配置 / 缓存 / 日志：pydantic-settings、diskcache、structlog

## 致谢

仓库地图（RepoMap）的实现参考了 [aider](https://github.com/Aider-AI/aider)，agent 循环的设计参考了 [gptme](https://github.com/ErikBjare/gptme)。内核基座相关项目包括 oscomp/arceos、oscomp/starry-next 与 rCore-Tutorial-v3。
