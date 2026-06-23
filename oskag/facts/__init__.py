"""oskag.facts — 确定性事实抽取层 (全程 0 LLM 调用).

子模块:
- profiles  家族探针 + KernelProfile
- cargo     workspace + dependency 解析 (cargo metadata 或 toml fallback)
- syscalls  双家族 syscall 抽取 + 6 域分类
- boot      启动入口与调用链
- repomap   aider RepoMap 紧凑表示
- scan      统筹各子模块, 组装 facts.json
"""
