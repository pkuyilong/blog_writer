import json

from llm import chat
from prompts import RESEARCHER_PROMPT, REVIEWER_PROMPT
from state import ArticleState

from agents.tools import WEB_SEARCH_TOOL, web_search


def researcher_node(state: ArticleState) -> dict:
    """调研/选题节点：联网搜索真实资料，产出提纲与素材。

    三阶段流程：
    1. 搜索：让模型规划并请求搜索（一次并行提出多个具体查询），执行全部搜索；
    2. 素材审查：用 REVIEWER_PROMPT 让模型逐条评估搜索结果，剔除低质/营销/宽泛
       内容，保留真实可靠的科普素材；
    3. 生成提纲：基于审查后的素材，让模型输出文章提纲。

    搜索被限制在单轮内，避免模型反复搜索拖延；审查保证下游写作基于可靠素材。
    """
    print("→ 调研/选题中…（可联网搜索）")
    messages = [{"role": "user", "content": f"文章题目：{state['topic']}\n请开始调研。"}]

    # ---- 阶段一：让模型规划搜索（一次可请求多个查询），并执行全部搜索 ----
    response = chat(RESEARCHER_PROMPT, messages, tools=[WEB_SEARCH_TOOL])
    msg = response.choices[0].message

    if not msg.tool_calls:
        # 模型认为不需要搜索，直接给出提纲（仍走一次审查，保证输出一致）
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

        # ---- 阶段二：让模型审查搜索结果，筛选出可靠素材 ----
        print("  🧐 审查搜索结果素材…")
        reviewed = chat(
            REVIEWER_PROMPT,
            messages
            + [
                {
                    "role": "user",
                    "content": f"请审查上面这些针对《{state['topic']}》的搜索结果，输出保留/剔除清单。",
                }
            ],
            tools=[],
        )
        reviewed_text = reviewed.choices[0].message.content or ""

        # ---- 阶段三：基于审查后的素材生成提纲 ----
        print("  基于审查后的素材整理提纲…")
        messages.append({"role": "user", "content": "审查结果：\n" + reviewed_text})
        forced = chat(
            RESEARCHER_PROMPT
            + "\n\n注意：现在请直接基于上面的搜索结果与审查结果，输出最终提纲，不要再调用任何工具。",
            messages,
            tools=[],
        )
        outline = forced.choices[0].message.content or ""

    if not outline:
        print("  ⚠ 调研未产出有效提纲")
    return {"outline": outline}
