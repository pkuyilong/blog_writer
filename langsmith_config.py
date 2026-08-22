"""
LangSmith 可观测性配置模块。

LangSmith 用于追踪 agent 执行过程、状态变化、token 使用情况等。

配置方式：只要设置环境变量，LangGraph 就会自动上报追踪，无需手动创建 tracer：
  - LANGCHAIN_TRACING_V2=true
  - LANGCHAIN_API_KEY=lsv2_xxx...
  - LANGCHAIN_PROJECT=b_writer（可选，指定项目名）

两种设置环境变量的方式任选其一：
  1. 在 shell 里 export（见 LANGSMITH.md）
  2. 复制 .env.example 为 .env 并填写（本模块启动时会自动读取）

首次使用需去 https://smith.langchain.com 注册并获取 API Key。
不配置时不会产生任何追踪开销。
"""

import os
from pathlib import Path


def _load_env_file() -> None:
    """读取项目根目录的 .env 文件并注入环境变量（已存在的环境变量不覆盖）。

    这样把 Key 写在 .env 里即可，不必每次在 shell 里手动 export。
    .env 已被 .gitignore 忽略，不会提交泄露。
    """
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # 跳过空行与注释
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # 只有环境变量未设置时才从 .env 注入，避免覆盖外部配置
        if key and key not in os.environ:
            os.environ[key] = value


def setup_langsmith(project_name: str | None = None) -> None:
    """校验 LangSmith 配置并设置项目名；LangGraph 会根据环境变量自动上报。

    Args:
        project_name: 项目名，会写入 LANGCHAIN_PROJECT 环境变量。
    """
    # 先尝试从项目根目录的 .env 文件加载配置
    _load_env_file()

    tracing = os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true"
    api_key = os.getenv("LANGCHAIN_API_KEY")

    if project_name:
        os.environ["LANGCHAIN_PROJECT"] = project_name

    if not tracing:
        print("⚠ LangSmith 未启用：请设置 LANGCHAIN_TRACING_V2=true")
        print("   最简单的方式：复制 .env.example 为 .env 并填写（详见 LANGSMITH.md）")
        return
    if not api_key:
        print("⚠ LangSmith 未启用：请在 .env 或环境变量中设置 LANGCHAIN_API_KEY=lsv2_xxx")
        print("   （在 https://smith.langchain.com/settings 生成 API Key）")
        return

    project = os.getenv("LANGCHAIN_PROJECT", project_name or "default")
    print(f"✓ LangSmith 追踪已启用：项目 = {project}")
    print("  访问 https://smith.langchain.com 查看追踪数据")


def is_langsmith_enabled() -> bool:
    """检查 LangSmith 是否已启用。"""
    return (
        os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true"
        and os.getenv("LANGCHAIN_API_KEY") is not None
    )
