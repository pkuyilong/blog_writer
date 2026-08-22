import os

from openai import OpenAI

# 通过 OpenAI 兼容端点调用 DeepSeek 官方 API。
# 需要先在环境变量中设置 DEEPSEEK_API_KEY：
#   export DEEPSEEK_API_KEY=sk-...
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

MODEL = "deepseek-v4-flash"


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
