"""oskag CLI 入口.

子命令:
- version       展示版本与关键依赖
- config-check  验证 .env 可读 / API key 非空 / DeepSeek 连通 (1-token ping)
- chat          单轮 (带 PROMPT 参数) 或 REPL (无参数)
- facts         扫一个仓库, 输出 facts.json (全部确定性事实)
"""
from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from oskag import __version__
from oskag.config import Settings, load_settings
from oskag.facts.scan import scan as scan_facts
from oskag.llm import ChatResult, DeepSeekClient
from oskag.logging_setup import get_logger, setup_logging

console = Console()
err_console = Console(stderr=True)
log = get_logger("oskag.cli")

# ---- 公共字面量
_OK = "[green]OK[/green]"
_FAIL_ERR_PANEL_BORDER = "red"
_REPL_HISTORY_LIMIT = 20
_TABLE_HEADER_STYLE = "bold cyan"

app = typer.Typer(
    name="oskag",
    help="OS-Kernel-Agent: 分析 OS 内核代码并生成中文描述/比对文档.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    add_completion=False,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _bootstrap() -> Settings:
    """加载配置 + 初始化日志 (CLI 子命令首条调用)."""
    settings = load_settings()
    setup_logging(level=settings.log_level, log_dir=settings.log_dir)
    return settings


def _make_client(settings: Settings, no_cache: bool = False) -> "DeepSeekClient":
    """统一构造 DeepSeekClient. no_cache=True 时禁用 LLM 磁盘缓存."""
    return DeepSeekClient(settings, cache=not no_cache)


def _pkg_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "<未安装>"


def _format_token_summary(result: ChatResult) -> str:
    return (
        f"tokens: prompt={result.prompt_tokens} "
        f"completion={result.completion_tokens} "
        f"reasoning={result.reasoning_tokens} "
        f"cached={result.cached_tokens} "
        f"latency={result.latency_ms}ms "
        f"req={result.request_id or '-'}"
    )


def _truncate_history(messages: list[dict], limit: int = _REPL_HISTORY_LIMIT) -> list[dict]:
    """REPL 历史截断: 保留 system 消息(若有) + 最近 N 条; 不能切断 assistant tool_calls
    与对应 tool 回复的配对.
    """
    if len(messages) <= limit:
        return messages

    # 分离 system 消息
    system_msgs = [m for m in messages if m.get("role") == "system"]
    others = [m for m in messages if m.get("role") != "system"]

    # 取最后 limit 条 (留点空间给 system)
    keep = limit - len(system_msgs)
    if keep < 1:
        # system 太多就只保留 system 加最后 1 条
        keep = 1
    tail = others[-keep:]

    # 修复 tool_calls 配对: 若 tail 第一条是 role=tool, 但前面没有 tool_calls 的 assistant,
    # 把它丢掉 (避免 OpenAI 端报 "tool_call_id without matching assistant.tool_calls")
    while tail and tail[0].get("role") == "tool":
        tail.pop(0)
    # 反向: 若最后一条 assistant 有 tool_calls 但 tail 里没有对应 tool, 也截掉它
    if tail and tail[-1].get("role") == "assistant" and tail[-1].get("tool_calls"):
        tail.pop()

    return system_msgs + tail


# --------------------------------------------------------------------------- #
# version
# --------------------------------------------------------------------------- #


@app.command()
def version() -> None:
    """展示 oskag 与关键依赖版本."""
    table = Table(title="oskag 版本信息", show_header=True, header_style=_TABLE_HEADER_STYLE)
    table.add_column("组件")
    table.add_column("版本")
    table.add_row("oskag", __version__)
    table.add_row("Python", ".".join(map(str, sys.version_info[:3])))
    for pkg in ("openai", "typer", "rich", "structlog",
                "pydantic", "pydantic-settings", "python-dotenv",
                "jinja2"):
        table.add_row(pkg, _pkg_version(pkg))
    console.print(table)


# --------------------------------------------------------------------------- #
# config-check
# --------------------------------------------------------------------------- #


def _config_check_table_basic(settings: Settings) -> Table:
    """组装基础信息表 (不含 ping)."""
    table = Table(title="oskag 配置检查", show_header=True, header_style=_TABLE_HEADER_STYLE)
    table.add_column("检查项", style="bold")
    table.add_column("状态")
    table.add_column("详情")

    if settings.env_source == "json":
        table.add_row("✓ .env", _OK, f"JSON 配置文件 @ {settings.env_path}")
    elif settings.env_source == "dotenv":
        table.add_row("✓ .env", _OK, f"标准 dotenv @ {settings.env_path}")
    else:
        table.add_row("⚠ .env", "[yellow]未找到[/yellow]", "将仅依赖进程环境变量")

    if settings.api_key.get_secret_value():
        table.add_row("✓ DEEPSEEK_API_KEY", "[green]已配置[/green]", settings.api_key_masked)
    else:
        table.add_row("✗ DEEPSEEK_API_KEY", "[red]缺失[/red]", "请在 .env 中提供")
        return table

    table.add_row("✓ base_url", _OK, settings.base_url)
    table.add_row("✓ model_flash", _OK, settings.model_flash)
    table.add_row("✓ model_pro", _OK, settings.model_pro)
    table.add_row("✓ thinking", _OK, settings.thinking)
    return table


def _print_missing_key_panel() -> None:
    console.print(
        Panel.fit(
            "[red]config-check 失败: 缺少 DEEPSEEK_API_KEY[/red]\n\n"
            "在 .env 写: [bold]DEEPSEEK_API_KEY=sk-...[/bold]\n"
            "或 JSON 配置文件: [bold]env.ANTHROPIC_AUTH_TOKEN=sk-...[/bold]",
            title="❌",
            border_style=_FAIL_ERR_PANEL_BORDER,
        )
    )


def _print_ping_failure_panel(exc: BaseException) -> None:
    console.print(
        Panel.fit(
            f"[red]ping 失败:[/red] {type(exc).__name__}\n"
            f"详情: {str(exc)[:200]}\n\n"
            "排查:\n"
            "  1. API key 是否正确, 余额是否充足\n"
            "  2. base_url 是否能访问 (代理 / 防火墙)\n"
            "  3. 模型 ID 是否合法 (检查是否带 [1m] 后缀)",
            title="❌",
            border_style=_FAIL_ERR_PANEL_BORDER,
        )
    )


@app.command("config-check")
def config_check(
    skip_ping: Annotated[
        bool,
        typer.Option("--skip-ping", help="跳过 DeepSeek 连通测试."),
    ] = False,
) -> None:
    """验证 .env 可读 / API key 非空 / DeepSeek 连通."""
    settings = _bootstrap()
    table = _config_check_table_basic(settings)

    if not settings.api_key.get_secret_value():
        console.print(table)
        _print_missing_key_panel()
        raise typer.Exit(code=1)

    if skip_ping:
        table.add_row("⊘ ping", "[yellow]已跳过[/yellow]", "--skip-ping")
        console.print(table)
        return

    try:
        client = DeepSeekClient(settings)
        result = client.ping()
    except Exception as exc:
        table.add_row(
            "✗ ping DeepSeek",
            "[red]失败[/red]",
            f"{type(exc).__name__}: {str(exc)[:120]}",
        )
        console.print(table)
        _print_ping_failure_panel(exc)
        raise typer.Exit(code=1) from exc

    table.add_row(
        "✓ ping DeepSeek",
        _OK,
        f"latency={result.latency_ms}ms model={result.model} req_id={result.request_id or '-'}",
    )
    console.print(table)
    console.print(Panel.fit("[green]✓ 配置检查全部通过[/green]", border_style="green"))


# --------------------------------------------------------------------------- #
# chat (single-shot + REPL)
# --------------------------------------------------------------------------- #


def _print_assistant_panel(result: ChatResult) -> None:
    if result.reasoning_content:
        console.print(
            Panel(
                result.reasoning_content,
                title="[dim]thinking[/dim]",
                border_style="grey50",
                style="dim",
            )
        )
    body = result.content or "[grey]<空回复>[/grey]"
    console.print(Panel(body, title=f"DeepSeek · {result.model}", border_style="cyan"))
    err_console.print(f"[dim]{_format_token_summary(result)}[/dim]")


def _chat_single(
    client: DeepSeekClient,
    prompt: str,
    *,
    model_role: str = "flash",
    thinking: str | None = None,
) -> None:
    """单轮 chat: 用户给一条 prompt, 输出回复."""
    model_id = client.settings.resolve_model(model_role)
    messages = [{"role": "user", "content": prompt}]
    result = client.chat(messages=messages, model=model_id, thinking=thinking)
    _print_assistant_panel(result)


# ---- REPL: 多轮交互 ---- #


class _ReplState:
    """REPL 内部状态. 抽出来让 _chat_repl 复杂度可控."""

    def __init__(self, settings: Settings, model_role: str, thinking: str | None):
        self.settings = settings
        self.model_role = model_role
        self.model_id = settings.resolve_model(model_role)
        self.thinking = thinking
        self.messages: list[dict] = []

    def append_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
        self.messages = _truncate_history(self.messages)

    def append_assistant(self, raw: dict) -> None:
        self.messages.append(raw)


def _repl_print_banner(state: _ReplState) -> None:
    console.print(
        Panel.fit(
            f"[{_TABLE_HEADER_STYLE}]oskag chat[/{_TABLE_HEADER_STYLE}] · model=[bold]{state.model_id}[/bold] · "
            f"thinking={state.thinking or state.settings.thinking}\n"
            "命令: [yellow]/clear[/yellow]  [yellow]/save <path>[/yellow]  "
            "[yellow]/model flash|pro[/yellow]  [yellow]Ctrl+C 退出[/yellow]",
            border_style="cyan",
        )
    )


def _repl_handle_command(state: _ReplState, line: str) -> bool:
    """处理 /command. 返回 True = 已处理, False = 不是命令."""
    if not line.startswith("/"):
        return False
    cmd, _, arg = line[1:].partition(" ")
    if cmd == "clear":
        state.messages.clear()
        console.print("[dim]history cleared[/dim]")
    elif cmd == "save":
        _repl_save(state, arg.strip())
    elif cmd == "model":
        _repl_switch_model(state, arg.strip())
    else:
        console.print(f"[yellow]未知命令: /{cmd}[/yellow]")
    return True


def _repl_save(state: _ReplState, arg: str) -> None:
    if not arg:
        console.print("[yellow]用法: /save <path>[/yellow]")
        return
    p = Path(arg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for m in state.messages:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    console.print(f"[dim]saved {len(state.messages)} messages → {p}[/dim]")


def _repl_switch_model(state: _ReplState, arg: str) -> None:
    if arg in ("flash", "pro"):
        state.model_role = arg
        state.model_id = state.settings.resolve_model(arg)
        console.print(f"[dim]model → {state.model_id}[/dim]")
    else:
        console.print("[yellow]用法: /model flash 或 /model pro[/yellow]")


def _repl_step(client: DeepSeekClient, state: _ReplState, user_in: str) -> None:
    """单轮 REPL 交互: 用户输入 → LLM 调用 → 显示."""
    state.append_user(user_in)
    try:
        result = client.chat(
            messages=state.messages,
            model=state.model_id,
            thinking=state.thinking,
        )
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]LLM 调用失败:[/red] {type(e).__name__}: {str(e)[:160]}")
        state.messages.pop()
        return
    except Exception as e:  # 兜底: openai 各类网络/SDK 异常都不让 REPL 崩
        console.print(f"[red]LLM 调用失败:[/red] {type(e).__name__}: {str(e)[:160]}")
        state.messages.pop()
        return
    _print_assistant_panel(result)
    state.append_assistant(result.raw_message)


def _chat_repl(
    client: DeepSeekClient,
    *,
    model_role: str = "flash",
    thinking: str | None = None,
) -> None:
    """多轮交互 REPL.

    命令:
      /clear          清空历史
      /save <path>    把当前 messages 存到 jsonl
      /model flash|pro   切换模型
      Ctrl+C / Ctrl+D 退出
    """
    state = _ReplState(client.settings, model_role, thinking)
    _repl_print_banner(state)

    while True:
        try:
            user_in = console.input("[bold green]you ›[/bold green] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]bye[/dim]")
            return
        if not user_in:
            continue
        if _repl_handle_command(state, user_in):
            continue
        _repl_step(client, state, user_in)


@app.command()
def chat(
    prompt: Annotated[
        Optional[str],
        typer.Argument(
            help="一句话 prompt; 不提供则进入交互 REPL.",
            show_default=False,
        ),
    ] = None,
    model_role: Annotated[
        str,
        typer.Option(
            "--model", "-m",
            help="模型角色: flash / pro / chat=flash / reasoner=pro, 也接受完整 model id.",
        ),
    ] = "flash",
    thinking: Annotated[
        Optional[str],
        typer.Option(
            "--thinking", "-t",
            help="思考开关: enabled / disabled. 默认沿用配置.",
            show_default=False,
        ),
    ] = None,
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="禁用 LLM 磁盘缓存 (cache/llm/), 强制每次真调 API."),
    ] = False,
) -> None:
    """与 DeepSeek 对话 (单轮或 REPL)."""
    settings = _bootstrap()
    if not settings.api_key.get_secret_value():
        err_console.print(
            "[red]缺少 DEEPSEEK_API_KEY. 跑 [bold]oskag config-check[/bold] 排查.[/red]"
        )
        raise typer.Exit(code=1)
    client = _make_client(settings, no_cache=no_cache)
    if prompt:
        _chat_single(client, prompt, model_role=model_role, thinking=thinking)
    else:
        _chat_repl(client, model_role=model_role, thinking=thinking)


# --------------------------------------------------------------------------- #
# facts: 扫描一个 OS 内核仓库, 输出 facts.json
# --------------------------------------------------------------------------- #


def _facts_summary_table(facts_dict: dict) -> Table:
    """从 facts dict 提一份单页摘要展示给用户."""
    meta = facts_dict.get("meta", {})
    profile = facts_dict.get("profile", {})
    repo_stats = facts_dict.get("repo_stats", {})
    cargo = facts_dict.get("cargo", {})
    syscalls = facts_dict.get("syscalls", {})
    boot = facts_dict.get("boot", {})

    table = Table(title=f"facts · {meta.get('repo_name', '?')}",
                  show_header=True, header_style=_TABLE_HEADER_STYLE)
    table.add_column("子节点", style="bold")
    table.add_column("摘要")

    table.add_row("meta", f"family={meta.get('family')} · "
                  f"errors={len(meta.get('errors', []))} · "
                  f"warnings={len(meta.get('warnings', []))}")
    table.add_row("profile", f"syscall={profile.get('syscall_style')} · "
                  f"boot={profile.get('boot_style')} · "
                  f"signals={len(profile.get('detection_signals', []))}")
    loc = repo_stats.get("loc", {})
    table.add_row("repo_stats", f"files={loc.get('total_files', 0)} · "
                  f"code_lines={loc.get('total_code', 0)} · "
                  f"backend={loc.get('backend')} · "
                  f"makefile={len(repo_stats.get('makefile_targets', []))} targets")
    table.add_row("cargo", f"workspace={cargo.get('is_workspace', False)} · "
                  f"members={len(cargo.get('members', []))} · "
                  f"deps={len(cargo.get('all_deps_union', []))}")
    table.add_row("syscalls", f"count={syscalls.get('count', 0)} · "
                  f"by_domain={syscalls.get('by_domain', {})}")
    table.add_row("boot", f"entry={boot.get('entry_file')}#{boot.get('entry_line', 0)} · "
                  f"fn={boot.get('entry_fn')} · "
                  f"chain={len(boot.get('call_chain', []))} · "
                  f"handlers={len(boot.get('handlers', []))} · "
                  f"asm={len(boot.get('assembly_files', []))}")
    rmap = facts_dict.get("repomap", "") or ""
    table.add_row("repomap", f"chars={len(rmap)}")
    return table


@app.command()
def facts(
    repo_path: Annotated[
        Path,
        typer.Argument(
            help="待扫描的 OS 内核仓库路径.",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ],
    out: Annotated[
        Optional[Path],
        typer.Option(
            "--out", "-o",
            help="输出 JSON 路径 (默认: reports/<repo_name>-facts.json).",
            show_default=False,
        ),
    ] = None,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="不打印摘要表 (CI 友好)."),
    ] = False,
) -> None:
    """扫描一个内核仓库, 提取确定性事实并输出 facts.json."""
    _bootstrap()

    out_path = out or (Path("reports") / f"{repo_path.name}-facts.json")
    facts_dict = scan_facts(repo_path, out=out_path)

    if not quiet:
        console.print(_facts_summary_table(facts_dict))
        size_kb = out_path.stat().st_size / 1024 if out_path.is_file() else 0
        console.print(
            Panel.fit(
                f"[green]facts written[/green] · {out_path} · {size_kb:.1f} KB",
                border_style="green",
            )
        )

    errors = facts_dict.get("meta", {}).get("errors", [])
    if errors:
        err_console.print(f"[red]facts has {len(errors)} errors[/red]")
        for e in errors[:5]:
            err_console.print(f"  [red]·[/red] {e}")
        raise typer.Exit(code=2)


# --------------------------------------------------------------------------- #
# describe
# --------------------------------------------------------------------------- #


def _parse_modules_arg(s: Optional[str]) -> Optional[list[str]]:
    if not s:
        return None
    out = [m.strip() for m in s.split(",") if m.strip()]
    return out or None


@app.command()
def describe(
    repo_path: Annotated[
        Path,
        typer.Argument(
            help="待描述的 OS 内核仓库路径 (须先跑过 oskag facts).",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ],
    out: Annotated[
        Optional[Path],
        typer.Option(
            "--out", "-o",
            help="输出 markdown 路径 (默认: reports/<repo_name>-describe.md).",
            show_default=False,
        ),
    ] = None,
    facts_path: Annotated[
        Optional[Path],
        typer.Option(
            "--facts",
            help="facts.json 路径 (默认: reports/<repo_name>-facts.json).",
            show_default=False,
        ),
    ] = None,
    module: Annotated[
        Optional[str],
        typer.Option(
            "--module", "-m",
            help="只跑指定模块 (逗号分隔, 如 mm,fs); 默认全部 9 个模块.",
        ),
    ] = None,
    deep: Annotated[
        bool,
        typer.Option(
            "--deep",
            help="synthesize 阶段切到 v4-pro + thinking (更慢, 引用更精).",
        ),
    ] = False,
    max_turns: Annotated[
        int,
        typer.Option(
            "--max-turns",
            help="单模块 agent tool-loop 最大轮数 (默认抬高到 10, 让 LLM 主动 read_file 跨文件分析).",
        ),
    ] = 10,
    max_verifier: Annotated[
        int,
        typer.Option(
            "--max-verifier",
            help="verifier 阶段校验的 evidence 上限 (越大越准, 但 token 开销线性).",
        ),
    ] = 30,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="不打印摘要面板."),
    ] = False,
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="禁用 LLM 磁盘缓存, 强制每次真调 API (调试用)."),
    ] = False,
) -> None:
    """读取 facts.json, 调 LLM 产出 5000-8000 字中文 Markdown 内核分析报告."""
    settings = _bootstrap()

    # 描述链路依赖较重, 这里 import (避免 cli 启动时 import 整个 describe 包)
    from oskag.describe.pipeline import describe as run_describe
    from oskag.render.markdown import render_describe

    facts_path_resolved = facts_path or (Path("reports") / f"{repo_path.name}-facts.json")
    out_path = out or (Path("reports") / f"{repo_path.name}-describe.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not facts_path_resolved.is_file():
        err_console.print(
            f"[red]facts.json 不存在: {facts_path_resolved}[/red]\n"
            f"  请先跑: [cyan]oskag facts {repo_path}[/cyan]"
        )
        raise typer.Exit(code=2)

    client = _make_client(settings, no_cache=no_cache)
    modules_to_run = _parse_modules_arg(module)

    console.print(
        Panel.fit(
            f"[cyan]describe[/cyan] · {repo_path.name}\n"
            f"facts: {facts_path_resolved}\n"
            f"modules: {modules_to_run or '(all)'}  · deep={deep}  · max_turns={max_turns}",
            title="oskag describe",
            border_style="cyan",
        )
    )

    report = run_describe(
        repo_path,
        facts_path=facts_path_resolved,
        client=client,
        modules_to_run=modules_to_run,
        deep_synthesize=deep,
        max_turns=max_turns,
        max_verifier=max_verifier,
    )

    md = render_describe(report)
    out_path.write_text(md, encoding="utf-8")

    # 同时落盘结构化 JSON, 供 oskag compare 直接读取 (避免重新跑 LLM).
    json_path = out_path.with_suffix(".json")
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if not quiet:
        passed = sum(1 for v in report.verifier_verdicts
                     if v.verdict in ("support", "partial"))
        rate = (passed / len(report.verifier_verdicts) * 100) if report.verifier_verdicts else 0.0

        table = Table(title="describe summary", header_style=_TABLE_HEADER_STYLE)
        table.add_column("metric", style="bold")
        table.add_column("value")
        table.add_row("仓库", report.repo_name)
        table.add_row("家族", report.family)
        table.add_row("模块数", str(len(report.modules)))
        unimpl = sum(1 for m in report.modules if m.unimplemented)
        table.add_row("未实现模块", str(unimpl))
        table.add_row("evidence 校验", f"{passed}/{len(report.verifier_verdicts)} ({rate:.0f}%)")
        table.add_row("prompt tokens", str(report.total_prompt_tokens))
        table.add_row("completion tokens", str(report.total_completion_tokens))
        table.add_row("reasoning tokens", str(report.total_reasoning_tokens))
        table.add_row("耗时 (秒)", f"{report.duration_sec:.1f}")
        table.add_row("文档字数", str(len(md)))
        table.add_row("输出", str(out_path))
        console.print(table)

    if report.errors:
        err_console.print(f"[red]describe encountered {len(report.errors)} module-level errors[/red]")
        for e in report.errors[:5]:
            err_console.print(f"  [red]·[/red] {e}")
        raise typer.Exit(code=2)


# --------------------------------------------------------------------------- #
# compare
# --------------------------------------------------------------------------- #


@app.command()
def compare(
    a_repo: Annotated[
        Path,
        typer.Argument(
            help="基准仓库路径 A (须先跑过 oskag facts + oskag describe).",
            exists=True, file_okay=False, dir_okay=True, resolve_path=True,
        ),
    ],
    b_repo: Annotated[
        Path,
        typer.Argument(
            help="待评仓库路径 B.",
            exists=True, file_okay=False, dir_okay=True, resolve_path=True,
        ),
    ],
    out: Annotated[
        Optional[Path],
        typer.Option(
            "--out", "-o",
            help="输出 markdown 路径 (默认: reports/<a>__vs__<b>-compare.md).",
            show_default=False,
        ),
    ] = None,
    a_facts: Annotated[
        Optional[Path],
        typer.Option("--a-facts", help="A 仓 facts.json 路径 (默认 reports/<a>-facts.json).",
                     show_default=False),
    ] = None,
    b_facts: Annotated[
        Optional[Path],
        typer.Option("--b-facts", help="B 仓 facts.json 路径.", show_default=False),
    ] = None,
    a_desc: Annotated[
        Optional[Path],
        typer.Option("--a-describe", help="A 仓 describe.json 路径 (默认 reports/<a>-describe.json).",
                     show_default=False),
    ] = None,
    b_desc: Annotated[
        Optional[Path],
        typer.Option("--b-describe", help="B 仓 describe.json 路径.", show_default=False),
    ] = None,
    skip_llm: Annotated[
        bool,
        typer.Option("--skip-llm",
                     help="跳过 LLM 步骤 (只跑确定性差异 + Jaccard, 用于快速验证 / 离线评分)."),
    ] = False,
    skip_callgraph: Annotated[
        bool,
        typer.Option("--skip-callgraph",
                     help="跳过调用图抽取 (大仓很慢, 用于快速 dry-run)."),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="不打印摘要面板."),
    ] = False,
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="禁用 LLM 磁盘缓存, 强制每次真调 API (调试用)."),
    ] = False,
) -> None:
    """对比两个 OS 内核仓库, 输出 ≥6000 字中文 Markdown 报告 (确定性差异 + LLM 语义对照)."""
    settings = _bootstrap()

    from oskag.pipelines.compare import compare as run_compare
    from oskag.render.compare_markdown import render_compare

    paths = _resolve_compare_paths(a_repo, b_repo,
                                    a_facts, b_facts, a_desc, b_desc, out)
    _validate_compare_inputs(paths, skip_llm, a_repo, b_repo)

    client = None if skip_llm else _make_client(settings, no_cache=no_cache)
    _print_compare_panel(paths, a_repo, b_repo, skip_llm, skip_callgraph)

    report = run_compare(
        a_repo, b_repo,
        a_facts=paths["a_facts"] if paths["a_facts"].is_file() else {},
        b_facts=paths["b_facts"] if paths["b_facts"].is_file() else {},
        a_describe=paths["a_desc"] if paths["a_desc"].is_file() else {},
        b_describe=paths["b_desc"] if paths["b_desc"].is_file() else {},
        client=client,
        skip_llm=skip_llm,
        skip_callgraph=skip_callgraph,
    )

    md = render_compare(report)
    paths["out"].write_text(md, encoding="utf-8")
    json_path = paths["out"].with_suffix(".json")
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if not quiet:
        _print_compare_summary(report, md, paths["out"])

    if report.errors:
        err_console.print(f"[red]compare encountered {len(report.errors)} errors[/red]")
        for e in report.errors[:5]:
            err_console.print(f"  [red]·[/red] {e}")
        raise typer.Exit(code=2)


def _resolve_compare_paths(
    a_repo: Path, b_repo: Path,
    a_facts: Optional[Path], b_facts: Optional[Path],
    a_desc: Optional[Path], b_desc: Optional[Path],
    out: Optional[Path],
) -> dict[str, Path]:
    """compare 命令的所有输入路径补全 + 输出路径计算."""
    out_path = out or (Path("reports") / f"{a_repo.name}__vs__{b_repo.name}-compare.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return {
        "a_facts": a_facts or (Path("reports") / f"{a_repo.name}-facts.json"),
        "b_facts": b_facts or (Path("reports") / f"{b_repo.name}-facts.json"),
        "a_desc": a_desc or (Path("reports") / f"{a_repo.name}-describe.json"),
        "b_desc": b_desc or (Path("reports") / f"{b_repo.name}-describe.json"),
        "out": out_path,
    }


def _validate_compare_inputs(paths: dict[str, Path], skip_llm: bool,
                              a_repo: Path, b_repo: Path) -> None:
    """检查 4 个输入文件是否齐全; 缺失且非 skip_llm 时报错并退出."""
    missing: list[str] = []
    for label, key in [("A facts", "a_facts"), ("B facts", "b_facts"),
                       ("A describe", "a_desc"), ("B describe", "b_desc")]:
        p = paths[key]
        if not p.is_file():
            missing.append(f"{label}: {p}")
    if missing and not skip_llm:
        err_console.print("[red]缺少必要的输入文件:[/red]")
        for m in missing:
            err_console.print(f"  · {m}")
        err_console.print(
            "\n[yellow]提示[/yellow]: 先依次跑\n"
            f"  oskag facts {a_repo}\n"
            f"  oskag describe {a_repo}\n"
            f"  oskag facts {b_repo}\n"
            f"  oskag describe {b_repo}\n"
            "或者用 --skip-llm 只跑确定性差异部分."
        )
        raise typer.Exit(code=2)


def _print_compare_panel(paths: dict, a_repo: Path, b_repo: Path,
                          skip_llm: bool, skip_callgraph: bool) -> None:
    console.print(
        Panel.fit(
            f"[cyan]compare[/cyan] · A={a_repo.name}  B={b_repo.name}\n"
            f"a_facts: {paths['a_facts']}\nb_facts: {paths['b_facts']}\n"
            f"a_describe: {paths['a_desc']}\nb_describe: {paths['b_desc']}\n"
            f"skip_llm={skip_llm}  skip_callgraph={skip_callgraph}",
            title="oskag compare",
            border_style="cyan",
        )
    )


def _print_compare_summary(report, md: str, out_path: Path) -> None:
    sim = report.similarity
    table = Table(title="compare summary", header_style=_TABLE_HEADER_STYLE)
    table.add_column("metric", style="bold")
    table.add_column("value")
    table.add_row("A 仓", report.a_repo)
    table.add_row("B 仓", report.b_repo)
    if sim:
        table.add_row("综合相似度", f"{sim.overall} ({sim.label})")
        table.add_row("函数签名 Jaccard", str(sim.sig_jaccard))
        table.add_row("syscall Jaccard", str(sim.syscall_jaccard))
        table.add_row("依赖 Jaccard", str(sim.deps_jaccard))
        table.add_row("调用图综合", str(sim.callgraph_score))
        table.add_row("目录 Jaccard", str(sim.dir_jaccard))
    table.add_row("模块对照数", str(len(report.module_compares)))
    diffs_n = len(report.novelty.get("diffs", [])) if report.novelty else 0
    table.add_row("novelty 差异点数", str(diffs_n))
    table.add_row("prompt tokens", str(report.total_prompt_tokens))
    table.add_row("completion tokens", str(report.total_completion_tokens))
    table.add_row("reasoning tokens", str(report.total_reasoning_tokens))
    table.add_row("耗时 (秒)", f"{report.duration_sec:.1f}")
    table.add_row("文档字数", str(len(md)))
    table.add_row("输出", str(out_path))
    console.print(table)

    if report.warnings:
        console.print(f"[yellow]warnings: {len(report.warnings)}[/yellow]")
        for w in report.warnings[:3]:
            console.print(f"  · {w}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


if __name__ == "__main__":
    app()
