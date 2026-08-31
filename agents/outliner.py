"""大纲子智能体:自包含的 LangGraph 子图,负责"检索素材 + 生成可用提纲".

作为主图(graph.py)的 outline 节点挂载:子图读父图 state 中的 topic,
内部自主完成 搜索素材 → 审查素材 → 生成提纲 → 自检,并保证最终一定返回
一份可用的 outline.

失败分类路由(素材问题与提纲问题分开处理):
- 素材不足(搜索失败/审查结果太差)且未到补搜上限 → 回到 search 补搜;
- 素材不足且已到补搜上限 → 直接自身知识兜底;
- 提纲不合格且未到重试上限 → 回到 generate 换提示重试;
- 提纲不合格且已到重试上限 → 自身知识兜底.

补搜 / 重试计数(search_round / outline_attempt)都是子图私有键,不会泄漏
回父图 state(已对 langgraph 1.2.11 实测验证).
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TypedDict

from langgraph.graph import START, END, StateGraph
from langgraph.store.base import BaseStore
from pydantic import ValidationError

from llm import chat
from memory_store import load_prefs, load_topic_history
from prompts import (
    FALLBACK_OUTLINE_PROMPT,
    OUTLINER_PROMPT,
    RESEARCHER_PROMPT,
    MATERIAL_REVIEW_PROMPT,
)
from agents.tools import WEB_SEARCH_TOOL, WebSearchArgs
from output_validation import format_tool_arg_errors
from search_cache import cached_search, get_cached_materials, store_materials

logger = logging.getLogger(__name__)

# 提纲重试上限:生成 1 次 + 重试 1 次,仍未达标则自身知识兜底
MAX_ATTEMPTS = 2
# 搜索轮次上限:先搜 1 轮 + 补搜 1 轮,素材仍不足则自身知识兜底
MAX_SEARCH_ROUNDS = 2
# 单轮内搜索查询的并发上限(DuckDuckGo 免费 API 有限流风险,不宜过大)
MAX_PARALLEL_SEARCHES = 4
# 工具参数强校验重试上限:首次规划 + 最多修正 MAX_TOOL_ARGS_ATTEMPTS 次
MAX_TOOL_ARGS_ATTEMPTS = 2
# 提纲最低长度(字符),低于视为"不可用",触发重试/兜底
MIN_OUTLINE_LEN = 60
# 素材最低长度(字符),低于视为"素材不足",触发补搜
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
    outline_attempt: int  # 私有:提纲重试计数
    search_round: int  # 私有:搜索轮次计数
    search_history: list[str]  # 私有:已执行过的查询关键词(供补搜轮带上下文,避免重复盲搜)


def _usable(outline: str) -> bool:
    """提纲可用标准:非空且达到最低长度."""
    return bool(outline.strip()) and len(outline.strip()) >= MIN_OUTLINE_LEN


def _materials_ok(materials: str) -> bool:
    """素材可用标准:非空,够长,且不包含"没搜到"的特征词."""
    t = (materials or "").strip()
    if len(t) < MIN_MATERIALS_LEN:
        return False
    return not any(m in t for m in FAILURE_MARKERS)


def _run_search(tc) -> tuple[str, str]:
    """执行单个搜索查询,返回 (tool_call_id, 搜索结果文本).供线程池并行调用.

    参数强校验:WebSearchArgs.model_validate_json 解析并校验模型生成的工具参数,
    非法参数返回错误文本而非抛异常(正常路径由 search 节点提前校验兜底, 这里作防御);
    合法时走 cached_search:query 级缓存命中直接返回, 未命中真实搜索并写库(见 search_cache.py).
    """
    try:
        args = WebSearchArgs.model_validate_json(tc.function.arguments)
    except ValidationError as exc:
        return tc.id, json.dumps(
            {"error": "工具参数校验失败: " + format_tool_arg_errors(tc.function.arguments, exc)},
            ensure_ascii=False,
        )
    query = args.query
    logger.info(f"    🔍 搜索：{query}")
    return tc.id, cached_search(query)


def search(state: OutlineState) -> dict:
    """搜索素材并审查:让模型规划查询 → 执行搜索 → 审查筛选,产出 materials.

    开头先查 topic 级缓存:同一题目命中则跳过整个 搜索+审查 子流程(知识复用), 直接
    产出素材. 命中时 search_round 照常 +1(不重置为 1), 防补搜阶段命中缓存后死循环
    (素材不足 → 补搜 → 又命中同一缓存 → 永远到不了补搜上限).
    """
    hit = get_cached_materials(state["topic"])
    if hit is not None:
        logger.info("  ⚡ 命中素材缓存, 跳过搜索+审查")
        return {"materials": hit, "search_round": state.get("search_round", 0) + 1}
    round_n = state.get("search_round", 0) + 1
    logger.info(f"  🔍 大纲子智能体搜索素材（第 {round_n} 轮）…")
    # 补搜轮(round_n>1)把已执行过的查询列表以纯文本带进 user 消息:让模型避免
    # 重复搜索、针对上一轮缺失的角度定向补搜。只塞进 user 消息、不追加 tool 消息,
    # 保持"孤儿 tool 消息"不变量(见 CLAUDE.md 决策 #1)。
    history = list(state.get("search_history") or [])
    history_extra = ""
    if history:
        history_extra = (
            "\n\n【上一轮搜索记录】\n"
            "以下关键词在上一轮已搜索过，本次是补充搜索：\n"
            + "\n".join(f"- {q}" for q in history)
            + "\n请避免重复搜索上述关键词，针对上一轮素材缺失的角度补充新的查询。"
        )
    messages = [
        {
            "role": "user",
            "content": f"文章题目：{state['topic']}\n请开始调研。{history_extra}",
        }
    ]

    # 阶段一:让模型规划搜索(一次可请求多个查询)。模型生成的工具参数经 WebSearchArgs
    # 强校验(类型/范围/格式):非法调用不静默丢弃/不崩溃, 把具体字段错误(字段+期望+实际值)
    # 作为 tool 消息反馈给模型重新生成; 合法调用立即并行执行。MAX_TOOL_ARGS_ATTEMPTS 次
    # 仍非法则跳过。所有 tool_calls 都有配对 tool 消息, 维持"孤儿 tool 消息"不变量
    # (CLAUDE.md 决策 #1:重试是同一 search 节点内的多轮规划, 跨 search 节点仍重建干净对话).
    valid_tcs: list = []  # 通过校验且已执行的 tool_call(跨重试轮累积)
    materials: str | None = None
    for arg_attempt in range(MAX_TOOL_ARGS_ATTEMPTS + 1):
        response = chat(RESEARCHER_PROMPT, messages, tools=[WEB_SEARCH_TOOL], role="research")
        msg = response.choices[0].message
        if not msg.tool_calls:
            # 模型认为不需要搜索:直接把它给出的内容当作素材
            materials = msg.content or ""
            break
        # 把含工具调用的助手消息放回对话(其后的 tool 结果/错误消息与之配对)
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
        good, bad = [], []
        for tc in msg.tool_calls:
            try:
                WebSearchArgs.model_validate_json(tc.function.arguments)
                good.append(tc)
            except ValidationError as exc:
                bad.append((tc, exc))
        # 合法调用立即并行执行(结果作为 tool 消息);非法调用反馈具体校验错误
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_SEARCHES) as ex:
            for tc_id, content in ex.map(_run_search, good):
                messages.append(
                    {"role": "tool", "tool_call_id": tc_id, "content": content}
                )
        for tc, exc in bad:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(
                        {
                            "error": "工具参数校验失败: "
                            + format_tool_arg_errors(tc.function.arguments, exc)
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        valid_tcs.extend(good)
        if not bad:
            break
        if arg_attempt < MAX_TOOL_ARGS_ATTEMPTS:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "以上工具调用的参数未通过 json 结构校验，请按上述字段路径逐一修正后"
                        "重新生成工具调用，已成功的查询不要重复。"
                    ),
                }
            )
        else:
            logger.warning(
                f"  ⚠ 工具参数校验 {MAX_TOOL_ARGS_ATTEMPTS + 1} 次仍失败，"
                f"跳过 {len(bad)} 条非法调用，本轮已执行 {len(good)} 条合法调用"
            )
            break

    if valid_tcs:
        # 记录本轮实际执行的查询(去重保序),写回私有键供下一轮补搜参考
        new_queries = [
            WebSearchArgs.model_validate_json(tc.function.arguments).query for tc in valid_tcs
        ]
        history = list(dict.fromkeys(history + new_queries))
        # 阶段二:让模型审查搜索结果,筛选出可靠素材
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
            role="research",
        )
        reviewed_text = reviewed.choices[0].message.content or ""
        if _materials_ok(reviewed_text):
            materials = reviewed_text
        else:
            # 审查结果不可用:退回原始搜索结果(可能仍被判为素材不足 → 补搜)
            materials = "\n\n".join(
                m.get("content", "") for m in messages if m.get("role") == "tool"
            )
    elif materials is None:
        # 模型未搜索或全部参数非法且无内容:素材空 → 触发补搜/兜底
        materials = ""

    # 素材可用才写入 topic 缓存(不足素材不缓存, 避免下次命中坏缓存再触发补搜)
    if _materials_ok(materials):
        store_materials(state["topic"], materials)
    return {"materials": materials, "search_round": round_n, "search_history": history}


def should_search_again(state: OutlineState) -> str:
    """素材不足判断:够用→generate;不足且可补搜→search;不足且到上限→fallback."""
    if _materials_ok(state.get("materials", "")):
        return "generate"
    if state.get("search_round", 0) >= MAX_SEARCH_ROUNDS:
        return "fallback"
    return "search"


def generate(state: OutlineState, config, *, store: BaseStore | None) -> dict:
    """基于素材生成/重试提纲;outline_attempt 自增,重试时提示模型补全.

    store 参数由 langgraph 注入(父图 compile(store=...) 传入, 见 memory_store.py):
    注入用户偏好 + 该题目的历史写作记录(长期记忆, 跨任务复用), store 为 None 跳过.
    """
    attempt = state.get("outline_attempt", 0) + 1
    logger.info(f"  📋 大纲子智能体生成提纲（第 {attempt} 次）…")
    user_content = (
        f"文章题目：{state['topic']}\n\n【素材】\n{state.get('materials', '')}"
    )
    if attempt > 1:
        user_content += "\n\n（上一版提纲不够完整/可用，请基于素材输出一份更完整、可直接支撑写作的提纲。）"
    prefs = load_prefs(store)
    if prefs:
        user_content += f"\n\n【写作偏好（来自长期记忆）】{prefs}"
    history = load_topic_history(store, state["topic"])
    if history:
        user_content += f"\n\n【历史写作记录】{history}"
    resp = chat(OUTLINER_PROMPT, [{"role": "user", "content": user_content}], tools=[], role="outline")
    outline = resp.choices[0].message.content or ""
    return {"outline": outline, "outline_attempt": attempt}


def should_retry(state: OutlineState) -> str:
    """提纲自检:可用→end;已达重试上限→fallback;否则→retry(回到 generate)."""
    if _usable(state.get("outline", "")):
        return "end"
    if state.get("outline_attempt", 0) >= MAX_ATTEMPTS:
        return "fallback"
    return "retry"


def fallback(state: OutlineState) -> dict:
    """兜底:素材/提纲均不可用时,基于模型自身知识直接输出可用提纲."""
    logger.warning("  ⚠ 大纲子智能体基于自身知识兜底…")
    resp = chat(
        FALLBACK_OUTLINE_PROMPT,
        [{"role": "user", "content": f"文章题目：{state['topic']}\n请直接输出提纲。"}],
        tools=[],
        role="outline",
    )
    return {"outline": resp.choices[0].message.content or ""}


def build_outliner():
    """构造大纲子智能体子图.

    START → search →(素材不足?)→ 补搜 search / 直接兜底 fallback / 生成 generate
    generate →(提纲不合格?)→ 重试 generate / 兜底 fallback / 结束 END
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
