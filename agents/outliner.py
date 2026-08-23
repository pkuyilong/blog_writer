"""大纲子智能体：自包含的 LangGraph 子图，负责"检索素材 + 生成可用提纲"。

作为主图（graph.py）的 outline 节点挂载：子图读父图 state 中的 topic，
内部自主完成 搜索素材 → 审查素材 → 生成提纲 → 自检，并保证最终一定返回
一份可用的 outline。

失败分类路由（素材问题与提纲问题分开处理）：
- 素材不足（搜索失败/审查结果太差）且未到补搜上限 → 回到 search 补搜；
- 素材不足且已到补搜上限 → 直接自身知识兜底；
- 提纲不合格且未到重试上限 → 回到 generate 换提示重试；
- 提纲不合格且已到重试上限 → 自身知识兜底。

补搜 / 重试计数（search_round / outline_attempt）都是子图私有键，不会泄漏
回父图 state（已对 langgraph 1.2.11 实测验证）。
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TypedDict

from langgraph.graph import START, END, StateGraph

from llm import chat
from prompts import (
    FALLBACK_OUTLINE_PROMPT,
    OUTLINER_PROMPT,
    RESEARCHER_PROMPT,
    MATERIAL_REVIEW_PROMPT,
)
from agents.tools import WEB_SEARCH_TOOL, web_search

logger = logging.getLogger(__name__)

# 提纲重试上限：生成 1 次 + 重试 1 次，仍未达标则自身知识兜底
MAX_ATTEMPTS = 2
# 搜索轮次上限：先搜 1 轮 + 补搜 1 轮，素材仍不足则自身知识兜底
MAX_SEARCH_ROUNDS = 2
# 单轮内搜索查询的并发上限（DuckDuckGo 免费 API 有限流风险，不宜过大）
MAX_PARALLEL_SEARCHES = 4
# 提纲最低长度（字符），低于视为"不可用"，触发重试/兜底
MIN_OUTLINE_LEN = 60
# 素材最低长度（字符），低于视为"素材不足"，触发补搜
MIN_MATERIALS_LEN = 40
# 素材/审查结果中代表"没搜到可用内容"的特征词
FAILURE_MARKERS = (
    "没有返回结果",
    "暂时不可用",
    "基于自身知识",
    "（无）",
    "(无)",
    "无可用素材",
    "无可用的素材",
    "未找到",
    "没有找到",
    "未检索到",
)


class OutlineState(TypedDict):
    topic: str
    materials: str
    outline: str
    outline_attempt: int  # 私有：提纲重试计数
    search_round: int  # 私有：搜索轮次计数


def _usable(outline: str) -> bool:
    """提纲可用标准：非空且达到最低长度。"""
    return bool(outline.strip()) and len(outline.strip()) >= MIN_OUTLINE_LEN


def _materials_ok(materials: str) -> bool:
    """素材可用标准：非空、够长、且不包含"没搜到"的特征词。"""
    t = (materials or "").strip()
    if len(t) < MIN_MATERIALS_LEN:
        return False
    return not any(m in t for m in FAILURE_MARKERS)


def _run_search(tc) -> tuple[str, str]:
    """执行单个搜索查询，返回 (tool_call_id, 搜索结果文本)。供线程池并行调用。"""
    query = json.loads(tc.function.arguments).get("query", "")
    logger.info(f"    🔍 搜索：{query}")
    return tc.id, web_search(query)


def search(state: OutlineState) -> dict:
    """搜索素材并审查：让模型规划查询 → 执行搜索 → 审查筛选，产出 materials。"""
    round_n = state.get("search_round", 0) + 1
    logger.info(f"  🔍 大纲子智能体搜索素材（第 {round_n} 轮）…")
    messages = [
        {"role": "user", "content": f"文章题目：{state['topic']}\n请开始调研。"}
    ]

    # 阶段一：让模型规划搜索（一次可请求多个查询），并执行全部搜索
    response = chat(RESEARCHER_PROMPT, messages, tools=[WEB_SEARCH_TOOL])
    msg = response.choices[0].message
    if not msg.tool_calls:
        # 模型认为不需要搜索：直接把它给出的内容当作素材
        materials = msg.content or ""
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
        # 并行执行全部查询；executor.map 保持输入顺序返回结果，tool_call_id 一一对应
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_SEARCHES) as ex:
            for tc_id, content in ex.map(_run_search, msg.tool_calls):
                messages.append(
                    {"role": "tool", "tool_call_id": tc_id, "content": content}
                )

        # 阶段二：让模型审查搜索结果，筛选出可靠素材
        logger.info("  🧐 审查搜索结果素材…")
        reviewed = chat(
            MATERIAL_REVIEW_PROMPT,
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
        if _materials_ok(reviewed_text):
            materials = reviewed_text
        else:
            # 审查结果不可用：退回原始搜索结果（可能仍被判为素材不足 → 补搜）
            materials = "\n\n".join(
                m.get("content", "") for m in messages if m.get("role") == "tool"
            )

    return {"materials": materials, "search_round": round_n}


def should_search_again(state: OutlineState) -> str:
    """素材不足判断：够用→generate；不足且可补搜→search；不足且到上限→fallback。"""
    if _materials_ok(state.get("materials", "")):
        return "generate"
    if state.get("search_round", 0) >= MAX_SEARCH_ROUNDS:
        return "fallback"
    return "search"


def generate(state: OutlineState) -> dict:
    """基于素材生成/重试提纲；outline_attempt 自增，重试时提示模型补全。"""
    attempt = state.get("outline_attempt", 0) + 1
    logger.info(f"  📋 大纲子智能体生成提纲（第 {attempt} 次）…")
    user_content = (
        f"文章题目：{state['topic']}\n\n【素材】\n{state.get('materials', '')}"
    )
    if attempt > 1:
        user_content += "\n\n（上一版提纲不够完整/可用，请基于素材输出一份更完整、可直接支撑写作的提纲。）"
    resp = chat(OUTLINER_PROMPT, [{"role": "user", "content": user_content}], tools=[])
    outline = resp.choices[0].message.content or ""
    return {"outline": outline, "outline_attempt": attempt}


def should_retry(state: OutlineState) -> str:
    """提纲自检：可用→end；已达重试上限→fallback；否则→retry（回到 generate）。"""
    if _usable(state.get("outline", "")):
        return "end"
    if state.get("outline_attempt", 0) >= MAX_ATTEMPTS:
        return "fallback"
    return "retry"


def fallback(state: OutlineState) -> dict:
    """兜底：素材/提纲均不可用时，基于模型自身知识直接输出可用提纲。"""
    logger.warning("  ⚠ 大纲子智能体基于自身知识兜底…")
    resp = chat(
        FALLBACK_OUTLINE_PROMPT,
        [{"role": "user", "content": f"文章题目：{state['topic']}\n请直接输出提纲。"}],
        tools=[],
    )
    return {"outline": resp.choices[0].message.content or ""}


def build_outliner():
    """构造大纲子智能体子图。

    START → search →（素材不足？）→ 补搜 search / 直接兜底 fallback / 生成 generate
    generate →（提纲不合格？）→ 重试 generate / 兜底 fallback / 结束 END
    fallback → END
    """
    g = StateGraph(OutlineState)
    g.add_node("search", search)
    g.add_node("generate", generate)
    g.add_node("fallback", fallback)
    g.add_edge(START, "search")
    g.add_conditional_edges(
        "search",
        should_search_again,
        {"search": "search", "generate": "generate", "fallback": "fallback"},
    )
    g.add_conditional_edges(
        "generate",
        should_retry,
        {"retry": "generate", "fallback": "fallback", "end": END},
    )
    g.add_edge("fallback", END)
    return g.compile()
