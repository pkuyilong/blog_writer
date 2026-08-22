import os

from langsmith import traceable
from openai import OpenAI

# 通过 OpenAI 兼容端点调用 DeepSeek 官方 API。
# 需要先在环境变量中设置 DEEPSEEK_API_KEY：
#   export DEEPSEEK_API_KEY=sk-...
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

MODEL = "deepseek-v4-flash"


@traceable(run_type="llm", name="deepseek_call_llm")
def call_llm(
    system: str,
    user_content: str,
    max_tokens: int = 16000,
    json_mode: bool = False,
) -> str:
    """统一封装一次 DeepSeek 调用，返回模型输出的文本。

    DeepSeek 是 OpenAI 兼容格式：system 提示作为 messages 中的一条消息，
    而不是顶层参数。json_mode=True 时开启 JSON 模式（prompt 中需出现
    "json" 字样）。
    """
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        **kwargs,
    )
    return response.choices[0].message.content


@traceable(run_type="llm", name="deepseek_chat")
def chat(
    system: str,
    messages: list,
    tools: list | None = None,
    max_tokens: int = 16000,
):
    """需要工具调用或多轮消息时使用的底层调用，返回完整响应对象。

    call_llm 是一次性问答的封装；这里保留原始响应，以便读取
    response.choices[0].message.tool_calls 来实现 ReAct 循环。
    """
    kwargs = {}
    if tools:
        kwargs["tools"] = tools
    return client.chat.completions.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, *messages],
        **kwargs,
    )
