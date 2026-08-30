"""多模型路由模块:按角色(role)选择模型 + 调用失败自动 fallback 链.

职责分层:
- **router 管"选哪个模型 + 怎么兜底"**:注册表(MODEL_REGISTRY),角色→模型链
  (ROLE_MODEL_MAP),全局默认模型(--model 可覆盖),OpenAI client 懒加载与缓存,
  单次调用失败按原因退避重试、耗尽再切下一个模型.
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
import time
from dataclasses import dataclass

from openai import (
    APIConnectionError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)

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


# 单模型内失败重试次数: 限流/超时/连接等瞬时错误不切模型, 按失败原因退避后重试
# 同一模型(切模型对限流无效——限流是账号/端点级的, 退避等待往往就好).
PER_SPEC_MAX_RETRIES = 2
# 退避基数(秒); 限流类失败退避翻倍; 单次等待封顶, 避免拖慢整轮.
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 4.0
RATE_LIMIT_BACKOFF = 2.0
# context_length_exceeded 时缩小 max_tokens 的下限(低于此就放弃缩小, 切下一个模型)
MIN_MAX_TOKENS = 512


def _classify_llm_error(exc: Exception) -> str:
    """分类 LLM 调用失败原因, 返回 rate_limit / transient / context_exceeded / fatal.

    - rate_limit(RateLimitError): 限流是账号/端点级的, 换模型无效, 退避(优先
      服务端 retry-after 头)后重试同一模型
    - transient(APIConnectionError 含超时 / InternalServerError): 瞬时故障,
      退避后重试; 注意 openai 里 APITimeoutError 是 APIConnectionError 的子类,
      检查父类即可覆盖
    - context_exceeded(BadRequestError, body.error.code 含 context_length_exceeded
      或 message 含 maximum context length): 请求超上下文上限, 这是可"编辑参数"
      的错误——缩小 max_tokens 再重试同一模型(见 call_with_fallback 的 adjust)
    - fatal(认证/权限/其他参数/未知): 重试无意义或无法自动编辑, 立即切下一个模型
    """
    if isinstance(exc, RateLimitError):
        return "rate_limit"
    if isinstance(exc, (APIConnectionError, InternalServerError)):
        return "transient"
    if isinstance(exc, BadRequestError):
        body = getattr(exc, "body", None)
        err = body.get("error") if isinstance(body, dict) else None
        code = str((err or {}).get("code") or "").casefold()
        msg = str((err or {}).get("message") or "").casefold() if isinstance(err, dict) else ""
        if "context_length_exceeded" in code or "maximum context length" in msg:
            return "context_exceeded"
        return "fatal"  # 其他 400 参数错误(如字段非法), 无法自动编辑
    return "fatal"


def _retry_after_seconds(exc: Exception) -> float | None:
    """从限流错误的响应头读服务端建议的等待秒数(无则 None), 按失败信息调整退避."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    val = headers.get("retry-after")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _retry_delay(attempt: int, kind: str, *, exc: Exception | None = None) -> float:
    """第 attempt 次重试前的等待秒数: 指数退避, 限流翻倍, 封顶.

    限流时优先采用服务端 retry-after 头(它通常更准), 没有才用本地退避.
    """
    if kind == "rate_limit" and exc is not None:
        server = _retry_after_seconds(exc)
        if server is not None:
            return min(server, RETRY_MAX_DELAY)
    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
    if kind == "rate_limit":
        delay *= RATE_LIMIT_BACKOFF
    return min(delay, RETRY_MAX_DELAY)


def call_with_fallback(
    specs: list[ModelSpec],
    func,
    *,
    role: str | None = None,
    retryable_exceptions: tuple = (OpenAIError,),
    adjust: dict | None = None,
):
    """按顺序逐个用 specs 里的模型执行 func(spec),失败按原因调整后切下一个.

    每个模型内先按失败原因调整再重试(PER_SPEC_MAX_RETRIES 次):
    - rate_limit / transient: 退避(限流优先 retry-after 头)后重试同一模型;
    - context_exceeded: **按失败原因编辑参数**——把 adjust["max_tokens"] 减半后
      立即重试同一模型(不退避), 缩到 MIN_MAX_TOKENS 仍超才切下一个;
    - fatal(认证/权限/其他参数/未知): 重试无意义, 立即切下一个模型.
    全部失败抛 ModelRoutingError, 并用 `from last_exc` 保留最后一次异常的
    __cause__,便于排查根因.

    adjust: 可选可变 dict(键 max_tokens), 由 llm.py 传入, 供 context_exceeded
    时缩小请求的 max_tokens 再重试. 不传则 context_exceeded 按 fatal 处理.
    教学取舍:默认只捕获 openai.OpenAIError(含 APITimeoutError/APIStatusError/
    APIConnectionError/RateLimitError 等),不吞代码自身 bug--把"模型失败"和
    "程序 bug"分开,避免悄悄降级掩盖问题.需要吞更多异常可显式传入
    retryable_exceptions=(Exception,).
    """
    last_exc = None
    for i, spec in enumerate(specs):
        for attempt in range(PER_SPEC_MAX_RETRIES + 1):
            try:
                return func(spec)
            except retryable_exceptions as e:
                last_exc = e
                kind = _classify_llm_error(e)
                if kind == "fatal":
                    break  # 重试无意义, 直接切下一个模型
                if attempt >= PER_SPEC_MAX_RETRIES:
                    break  # 当前模型重试耗尽(含 context 缩小次数), 切下一个
                if kind == "context_exceeded":
                    if adjust is not None and adjust["max_tokens"] > MIN_MAX_TOKENS:
                        adjust["max_tokens"] = max(adjust["max_tokens"] // 2, MIN_MAX_TOKENS)
                        logger.warning(
                            f"  ⚠ 模型 {spec.name} 上下文超长，按失败信息把 max_tokens "
                            f"缩小到 {adjust['max_tokens']} 重试…"
                        )
                        continue  # 立即用更小 budget 重试, 不退避
                    break  # 无 adjust 或已缩到最小仍超 → 切下一个模型
                delay = _retry_delay(attempt + 1, kind, exc=e)
                logger.warning(
                    f"  ⚠ 模型 {spec.name} 调用失败（{type(e).__name__}: {e}），"
                    f"按 {kind} 退避 {delay:.1f}s 后重试…"
                )
                time.sleep(delay)
        if i < len(specs) - 1:
            logger.warning(
                f"  ⚠ 模型 {spec.name} 调用失败（{type(last_exc).__name__}: {last_exc}），切下一个…"
            )
        else:
            logger.error(f"  ✗ 模型 {spec.name} 调用失败，role={role} 备用模型已耗尽")
    raise ModelRoutingError(
        f"role={role!r} 的 {len(specs)} 个模型调用全部失败"
    ) from last_exc
