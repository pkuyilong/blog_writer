"""多模型路由模块:按角色(role)选择模型 + 调用失败自动 fallback 链.

职责分层:
- **router 管"选哪个模型 + 怎么兜底"**:注册表(MODEL_REGISTRY),角色→模型链
  (ROLE_MODEL_MAP),全局默认模型(--model 可覆盖),OpenAI client 懒加载与缓存,
  单次调用失败自动切下一个模型重试.
- **llm.py 管"消息形状 + @traceable"**:组装 messages,json_mode/tools,委托本模块.

即使目前只注册一个模型(deepseek-v4-flash),路由/fallback 架构是完整的:
将来加第二个模型只需在 MODEL_REGISTRY 注册一个 ModelSpec,并(可选)在
ROLE_MODEL_MAP 里给某个 role 配置 fallback 链,无需改动任何调用点.

教学取舍:call_with_fallback 默认只捕获 `openai.OpenAIError`(含超时/状态错误/连接
错误/限流),而不是吞掉所有 Exception--避免把代码自身 bug 误当成"模型失败"而悄悄
降级.更激进或更严格的策略可按需传入 retryable_exceptions.
"""

import logging
import os
from dataclasses import dataclass

from openai import OpenAI, OpenAIError

logger = logging.getLogger(__name__)

# 哨兵:ROLE_MODEL_MAP 里出现它表示"跟随全局默认模型".
# 这是 --model 能一处切换所有 role 的关键--若各 role 链硬编码具体模型名,
# --model 就只对未指定 role 的调用生效.哨兵字符串不会与真实模型名冲突
# (set_default_model 与注册时都有 guard 拒绝它).
DEFAULT_MODEL = "__default__"

# 全部角色(对齐 10 个 LLM 调用点的用途).给某个调用点传 role 时必须在此集合内.
# 审校已是 3 个独立角色(语言/逻辑/事实),各自走自己的 role 链,便于将来单独配更强模型.
ROLES = frozenset({
    "research", "outline", "split", "write", "revise_outline",
    "edit_lang", "edit_logic", "edit_fact",
})


@dataclass(frozen=True)
class ModelSpec:
    """一个可路由的模型规格(不可变).

    capabilities 为将来"按能力过滤"预留:若某模型不支持 json_mode 或 tools,
    对应需求下 resolve_chain 会在 role 链里静默跳过它(显式指定则报错).
    """

    name: str  # 注册表键,如 "deepseek-v4-flash"
    provider: str  # 如 "deepseek"(将来可区分 openai/ollama/...)
    model_name: str  # 传给 API 的模型 id(通常与 name 相同)
    base_url: str  # OpenAI 兼容端点
    api_key_env: str  # 读哪个环境变量,如 "DEEPSEEK_API_KEY"
    capabilities: frozenset[str] = frozenset()
    timeout: float = 300.0  # 单次请求超时(秒);超时抛 APITimeoutError 驱动 fallback
    default_max_tokens: int = 16000


# 模型注册表:默认只注册 DeepSeek.加第二个模型在此追加一个 ModelSpec 即可
# (provider/base_url/api_key_env/capabilities 各自独立,按 provider 懒加载 client).
MODEL_REGISTRY: dict[str, ModelSpec] = {
    "deepseek-v4-flash": ModelSpec(
        name="deepseek-v4-flash",
        provider="deepseek",
        model_name="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        capabilities=frozenset({"json", "tools"}),
    ),
}


# 角色 → 候选模型链(第一个为主选,失败自动切下一个).
# 默认全部指向哨兵 DEFAULT_MODEL(= 跟随全局默认模型).
# 想让某环节用更强的模型:{"edit_fact": ["deepseek-reasoner", DEFAULT_MODEL]}.
ROLE_MODEL_MAP: dict[str, list[str]] = {role: [DEFAULT_MODEL] for role in ROLES}

# 全局默认模型(可被 main.py 的 --model 或 set_default_model() 覆盖)
_DEFAULT_MODEL = "deepseek-v4-flash"

# client 缓存:(base_url, api_key_env) -> OpenAI 实例.多个 provider 时各自独立,
# 同 provider 不同模型共享同一个 client.
_client_cache: dict[tuple[str, str], OpenAI] = {}


class ModelRoutingError(Exception):
    """模型路由/调用失败(未知角色,未知模型,缺 key,fallback 耗尽等)."""


def get_default_model() -> str:
    """返回全局默认模型名(哨兵被 resolve 时替换为它)."""
    return _DEFAULT_MODEL


def set_default_model(name: str) -> str:
    """把全局默认模型切到 name,返回旧值(便于测试恢复)."""
    if name == DEFAULT_MODEL or name not in MODEL_REGISTRY:
        raise ModelRoutingError(
            f"未知模型 {name!r}（已注册: {sorted(MODEL_REGISTRY)}）"
        )
    global _DEFAULT_MODEL
    old, _DEFAULT_MODEL = _DEFAULT_MODEL, name
    return old


def get_client(spec: ModelSpec) -> OpenAI:
    """按 (base_url, api_key_env) 懒加载并缓存 OpenAI client.

    api_key 在首次真实调用时才读环境变量(模块导入不读 env,缺 key 报友好错误
    而非 import 时 KeyError).
    """
    key = (spec.base_url, spec.api_key_env)
    client = _client_cache.get(key)
    if client is None:
        api_key = os.environ.get(spec.api_key_env)
        if not api_key:
            raise ModelRoutingError(
                f"缺少环境变量 {spec.api_key_env}（模型 {spec.name} 需要它）"
            )
        client = OpenAI(api_key=api_key, base_url=spec.base_url, timeout=spec.timeout)
        _client_cache[key] = client
    return client


def resolve_chain(
    *,
    role: str | None = None,
    model: str | None = None,
    required_capabilities: set[str] | None = None,
) -> list[ModelSpec]:
    """解析本次调用应使用的候选模型链(含 fallback 顺序).

    优先级:显式 model > 按 role 查链 > 全局默认.
    能力过滤:role 链里能力不符的成员**静默跳过**(换下一个更合适);
    显式 model 能力不符**直接抛错**(用户点名要这个模型,不能用别的顶替).
    """
    if model is not None:
        names = [model]  # ① 显式指定最高优先(单个,无 fallback 链)
    elif role is not None:
        if role not in ROLES:
            raise ModelRoutingError(f"未知 role {role!r}（可用: {sorted(ROLES)}）")
        names = ROLE_MODEL_MAP.get(role, [DEFAULT_MODEL])  # ② role 链
    else:
        names = [DEFAULT_MODEL]  # ③ 全局默认

    specs = []
    for n in names:
        if n == DEFAULT_MODEL:
            n = get_default_model()
        spec = MODEL_REGISTRY.get(n)
        if spec is None:
            raise ModelRoutingError(f"未知模型 {n!r}（已注册: {sorted(MODEL_REGISTRY)}）")
        if required_capabilities and not required_capabilities <= spec.capabilities:
            if model is not None:
                raise ModelRoutingError(
                    f"模型 {n!r} 不具备能力 {required_capabilities}（无法满足本次调用）"
                )
            continue  # role 链里能力不符的成员静默跳过
        specs.append(spec)

    if not specs:
        raise ModelRoutingError(
            f"role={role} model={model} 能力过滤后无可用模型"
        )
    return specs


def call_with_fallback(
    specs: list[ModelSpec],
    func,
    *,
    role: str | None = None,
    retryable_exceptions: tuple = (OpenAIError,),
):
    """按顺序逐个用 specs 里的模型执行 func(spec),失败切下一个.

    全部失败抛 ModelRoutingError,并用 `from last_exc` 保留最后一次异常的
    __cause__,便于排查根因.

    教学取舍:默认只捕获 openai.OpenAIError(含 APITimeoutError/APIStatusError/
    APIConnectionError/RateLimitError 等),不吞代码自身 bug--把"模型失败"和
    "程序 bug"分开,避免悄悄降级掩盖问题.需要吞更多异常可显式传入
    retryable_exceptions=(Exception,).
    """
    last_exc = None
    for i, spec in enumerate(specs):
        try:
            return func(spec)
        except retryable_exceptions as e:
            last_exc = e
            if i < len(specs) - 1:
                logger.warning(
                    f"  ⚠ 模型 {spec.name} 调用失败（{type(e).__name__}: {e}），切下一个…"
                )
            else:
                logger.error(
                    f"  ✗ 模型 {spec.name} 调用失败，role={role} 备用模型已耗尽"
                )
    raise ModelRoutingError(
        f"role={role!r} 的 {len(specs)} 个模型调用全部失败"
    ) from last_exc
