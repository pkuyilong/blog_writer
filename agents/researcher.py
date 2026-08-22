import json

from llm import chat
from prompts import RESEARCHER_PROMPT
from state import ArticleState

from agents.tools import WEB_SEARCH_TOOL, web_search

MAX_SEARCH_ROUNDS = 3  # 最多几个模型回合（每个回合模型可请求多次搜索）


def researcher_node(state: ArticleState) -> dict:
    """调研/选题节点：通过 ReAct 循环联网搜索真实资料，产出提纲与素材。

    循环逻辑：把对话交给模型（带 web_search 工具）→ 若模型请求搜索就执行
    并把结果放回对话再交给模型，直到模型不再请求工具、直接给出提纲。
    """
    print("→ 调研/选题中…（可联网搜索）")
    messages = [{"role": "user", "content": f"文章题目：{state['topic']}\n请开始调研。"}]

    outline = ""
    for _ in range(MAX_SEARCH_ROUNDS):
        response = chat(RESEARCHER_PROMPT, messages, tools=[WEB_SEARCH_TOOL])
        msg = response.choices[0].message

        if not msg.tool_calls:
            # 模型不再要工具，其输出就是最终提纲
            outline = msg.content or ""
            break

        # 把含工具调用的助手消息放回对话，逐条执行搜索
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
        )
        for tc in msg.tool_calls:
            query = json.loads(tc.function.arguments).get("query", "")
            print(f"  🔍 搜索：{query}")
            result = web_search(query)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    if not outline:
        print("  ⚠ 调研未产出有效提纲")
    return {"outline": outline}
