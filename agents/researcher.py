import json

from llm import chat
from prompts import RESEARCHER_PROMPT
from state import ArticleState

from agents.tools import WEB_SEARCH_TOOL, web_search


def researcher_node(state: ArticleState) -> dict:
    """调研/选题节点：联网搜索真实资料，产出提纲与素材。

    两阶段流程：
    1. 让模型规划并请求搜索（可一次并行请求多个查询，覆盖不同角度）；
    2. 执行全部搜索后，去掉工具，强制模型基于结果直接输出提纲。

    这样既保证搜到真实素材，又保证收敛——模型不会被允许反复搜索、
    无限拖延，也就不再出现"搜索轮次用尽"的兜底路径。
    """
    print("→ 调研/选题中…（可联网搜索）")
    messages = [{"role": "user", "content": f"文章题目：{state['topic']}\n请开始调研。"}]

    outline = ""
    # 阶段一：让模型规划搜索（一次可请求多个查询），并执行全部搜索
    response = chat(RESEARCHER_PROMPT, messages, tools=[WEB_SEARCH_TOOL])
    msg = response.choices[0].message

    if not msg.tool_calls:
        # 模型认为不需要搜索，直接给出提纲
        outline = msg.content or ""
    else:
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

    # 阶段二：去掉工具，强制模型基于已搜到的资料直接输出提纲
    if not outline:
        print("  基于搜索结果整理提纲…")
        forced = chat(
            RESEARCHER_PROMPT
            + "\n\n注意：现在请直接输出最终提纲，不要再调用任何工具。",
            messages,
            tools=[],
        )
        outline = forced.choices[0].message.content or ""

    if not outline:
        print("  ⚠ 调研未产出有效提纲")
    return {"outline": outline}
