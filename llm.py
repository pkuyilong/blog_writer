"""LLM 调用统一封装：消息形状 + @traceable，模型选择交给 model_router。

职责分层（见 model_router.py docstring）：
- **本模块管"消息形状 + 可观测性"**：把 system/user 组装成 OpenAI 兼容 messages、
  json_mode 时加 response_format、保留 @traceable（一个逻辑调用 = 一个 LLM span，
  fallback 链发生在 span 内部）。
- **model_router 管"选哪个模型 + 怎么兜底"**：按 role 路由到候选模型链、单次失败
  自动切下一个、client 懒加载。

调用方式：
  call_llm(system, user_content, json_mode=True, role="split")
  chat(system, messages, tools=[...], role="research")
  call_llm(system, user_content, model="deepseek-v4-flash")  # 显式指定，跳过路由

不传 role/model 时行为与旧版一致：走全局默认模型（main.py --model 可覆盖）。
新参数 model/role 是 keyword-only，放在既有位置参数之后，测试 fake 的 **kw 可吸收。
"""

from langsmith import traceable

from model_router import (
    call_with_fallback,
    get_client,
    resolve_chain,
)

# 兼容别名：旧代码/文档引用 llm.MODEL 时仍可用；真正生效的默认由 model_router 管理
# （全局默认模型 + main.py --model 覆盖）。
MODEL = "deepseek-v4-flash"


@traceable(run_type="llm", name="call_llm")
def call_llm(
    system: str,
    user_content: str,
    max_tokens: int = 16000,
    json_mode: bool = False,
    *,
    model: str | None = None,
    role: str | None = None,
) -> str:
    """统一封装一次 LLM 调用，返回模型输出的文本。

    DeepSeek 是 OpenAI 兼容格式：system 提示作为 messages 中的一条消息，
    而不是顶层参数。json_mode=True 时开启 JSON 模式（prompt 中需出现
    "json" 字样）。

    model/role：见模块 docstring。json_mode=True 时要求模型具备 json 能力
    （role 链里不具备的模型会被静默跳过，显式指定 model 则直接报错）。
    """
    specs = resolve_chain(
        role=role,
        model=model,
        required_capabilities={"json"} if json_mode else None,
    )
    return call_with_fallback(
        specs,
        lambda spec: _create(spec, system, user_content, max_tokens, json_mode),
        role=role,
    )


@traceable(run_type="llm", name="chat")
def chat(
    system: str,
    messages: list,
    tools: list | None = None,
    max_tokens: int = 16000,
    *,
    model: str | None = None,
    role: str | None = None,
):
    """需要工具调用或多轮消息时使用的底层调用，返回完整响应对象。

    call_llm 是一次性问答的封装；这里保留原始响应，以便读取
    response.choices[0].message.tool_calls 来实现 ReAct 循环。

    tools 非空时要求模型具备 tools 能力（role 链跳过 / 显式 model 报错，同上）。
    """
    specs = resolve_chain(
        role=role,
        model=model,
        required_capabilities={"tools"} if tools else None,
    )
    return call_with_fallback(
        specs,
        lambda spec: _chat_create(spec, system, messages, tools, max_tokens),
        role=role,
    )


def _create(spec, system: str, user_content: str, max_tokens: int, json_mode: bool) -> str:
    """用指定 ModelSpec 发起一次 OpenAI 兼容问答，返回文本。"""
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    response = get_client(spec).chat.completions.create(
        model=spec.model_name,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        **kwargs,
    )
    return response.choices[0].message.content


def _chat_create(spec, system: str, messages: list, tools, max_tokens: int):
    """用指定 ModelSpec 发起带 tools/多轮消息的调用，返回完整响应。"""
    kwargs = {}
    if tools:
        kwargs["tools"] = tools
    return get_client(spec).chat.completions.create(
        model=spec.model_name,
        max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, *messages],
        **kwargs,
    )
