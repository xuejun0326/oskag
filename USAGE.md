# 使用说明

这份文档讲清楚 oskag 怎么装、怎么配、怎么用。

oskag 是个分析代码仓库的命令行工具，给它一个项目目录，它会生成中文的分析报告，报告里的结论都带代码出处。它对 Rust 操作系统内核仓库有专门支持，也能分析其他类型的项目（编译器、库、应用等），后者会按目录结构自动组织章节。

## 环境要求

- Python 3.11 或更高（3.11、3.12 都行）
- Windows 10/11 配 Git Bash，或者 macOS、Linux
- 能连上 api.deepseek.com
- 一个 DeepSeek API key（按用量付费，分析一个项目大概一块多）
- 装依赖会占大概 800MB，主要是 tree-sitter 那个语言包比较大

## 安装

### 1. 装系统工具

oskag 要用到两个外部命令：ripgrep 搜代码，tokei 数代码行。

Windows：

```powershell
winget install BurntSushi.ripgrep.MSVC
winget install XAMPPRocky.Tokei
```

macOS：

```bash
brew install ripgrep tokei
```

Debian / Ubuntu：

```bash
sudo apt install ripgrep
cargo install tokei        # apt 源里 tokei 版本旧，用 cargo 装更稳
```

装完确认一下：

```bash
rg --version
tokei --version
```

### 2. 装 Python 包

进到 oskag 目录里装：

```bash
cd oskag
pip install -e .
```

嫌 pip 慢的话可以用 uv，先 `pip install uv`，然后 `uv pip install -e .`，快很多。

## 配置 API key

到 platform.deepseek.com 注册，建个 API key。账户里充个一二十块钱足够跑十几个项目了。

新建一个 `.env` 文件，写上 key。文件放哪都行，oskag 会按这个顺序找：你运行命令时所在的目录、oskag 文件夹的上一级、oskag 文件夹本身。最常见的是放在上一级。

```ini
DEEPSEEK_API_KEY=sk-你的key
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

仓库里有个 `.env.example` 可以照着改。

填好之后验一下：

```bash
oskag config-check
```

key 能读到、API 能 ping 通，就配好了。

## 上手

分析一个仓库，先抽事实，再生成报告：

```bash
oskag facts /path/to/project       # 扫仓库，不花钱，十几秒
oskag describe /path/to/project    # 生成报告，内核仓库要二三十分钟
```

报告在 `reports/` 里：

- `<项目名>-describe.md`，给人看的报告
- `<项目名>-describe.json`，结构化数据，compare 会读它

直接跑 describe 不先跑 facts 也行，它内部会自己抽，只是不单独留 facts 文件。

对比两个项目，先各自跑过 facts 和 describe，再：

```bash
oskag compare /path/to/A /path/to/B
```

对比报告在 `reports/<A>__vs__<B>-compare.md`。

## 命令说明

### facts

扫仓库抽事实，不调用大模型，写到 `reports/<名字>-facts.json`。

```bash
oskag facts /path/to/project
oskag facts /path/to/project -o 自定义路径.json
```

抽出来的有：代码行数、文件结构、Cargo 依赖、syscall 列表、调用图、启动入口这些。这步免费、快，可以单独跑。

### describe

读 facts 生成报告。

```bash
oskag describe /path/to/project

# 只看一个模块，调试或重跑失败模块时用
oskag describe /path/to/project --module mm

# 一次看几个模块
oskag describe /path/to/project --module boot,mm,task

# 换输出路径
oskag describe /path/to/project --out reports/我的报告.md

# 不走缓存重新生成
oskag describe /path/to/project --no-cache
```

参数：

- `--module, -m` 指定模块，逗号分隔，默认全部。内核模块名见下面
- `--out, -o` 报告输出路径，默认 `reports/<名字>-describe.md`
- `--facts` 指定 facts.json 路径，默认自动找
- `--max-turns` 单个模块里大模型最多读几轮文件，默认 10
- `--max-verifier` 校验多少条引用，设成 0 就跳过校验，快一些
- `--deep` 综合评价那步换更强的模型，质量高但更慢
- `--no-cache` 不用缓存
- `--quiet, -q` 少打日志

内核仓库的模块名：boot（启动）、mm（内存）、task（调度）、fs（文件系统）、signal（信号）、ipc（进程间通信）、net（网络）、drivers（驱动）、syscall（系统调用层）。

### compare

对比两个仓库，出查重和差异报告。

```bash
oskag compare /path/to/A /path/to/B

# 只算相似度，不调用大模型，非常快
oskag compare /path/to/A /path/to/B --skip-llm

# 仓库太大、调用图算得慢的话，跳过它
oskag compare /path/to/A /path/to/B --skip-callgraph
```

compare 会优先读两个仓库已经生成的 describe.json 和 facts.json，所以最好先给两个仓库都跑过 facts 和 describe。

### chat

直接和大模型聊一句，用来快速测 API 通不通。

```bash
oskag chat "用一句话介绍 RISC-V"
oskag chat              # 不带参数进交互模式
```

### version / config-check

看版本，检查配置和连通性。

## 报告里有什么

内核仓库的报告大致是这些章节（别的项目按实际目录来，标题自动生成）：

整体定位和评分（完整度、创新性、代码质量），综合评价（设计取舍、创新点、完成度），然后是启动流程、内存管理、进程调度、文件系统、信号、进程间通信、网络、驱动、系统调用层这几块的逐一分析，最后一张表列出所有代码引用的校验结果。

报告里每条结论后面都跟着来源的文件和行号，引用索引里还附了对应的代码片段，方便对着原仓库核对。

## 花费和缓存

facts 不花钱。describe 单个内核仓库（九个模块）大概一块多，compare 两个仓库一毛钱左右。

大模型的返回会缓存在本地的 `cache/` 目录里，同一个仓库第二次跑会命中缓存，不再花钱。想强制重新生成就加 `--no-cache`。

## 遇到问题

**rg 或 tokei 找不到。** 工具没装好或者不在 PATH 里，重新装一遍，确认 `rg --version`、`tokei --version` 有输出。

**tree_sitter_language_pack 装不上。** 先升级 pip 再试：`pip install --upgrade pip`。Windows 上如果报路径太长，看本节最后一条。

**提示找不到 .env 或 API key。** 确认 `.env` 放对了地方（见上面配置那节），文件名得是 `.env` 而不是 `env.txt` 之类。可以用 `oskag config-check` 排查。

**报告里某个章节写着"该模块描述失败"。** 把那个模块单独重跑一下就行：

```bash
oskag describe /path/to/repo --module 模块名 --no-cache
```

**太慢。** 一个完整内核仓库九个模块是串着跑的，每个模块还要让大模型多轮读代码，二三十分钟算正常。只关心某几个模块就用 `--module` 限定，或者加 `--max-verifier 0` 跳过校验那一步。

**Windows 上路径太长，或者中文文件名乱码。** 用管理员身份开 PowerShell 开启长路径，再设一下 UTF-8：

```bash
reg add "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled /t REG_DWORD /d 1 /f
echo 'export PYTHONUTF8=1' >> ~/.bashrc
echo 'export PYTHONIOENCODING=utf-8' >> ~/.bashrc
```
