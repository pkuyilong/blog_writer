"""写作子 Agent:自包含 LangGraph 子图,把"拆章 → 并行写章 → 合并"封装成主图单个节点.

作为主图 graph.py 的 writing 节点挂载:读父图共享通道(topic/outline/sections/
section_drafts/failed_sections/revision_count),内部 拆章 → Send 并行触发章节写作
子智能体(section_writer 子图)×N → 按 id 合并,产出整篇 draft 写回父图.
审校(edit)留在主图;打回重写时主图再次进入本子图,只重写 failed_sections 问题章节.

与父图共享的键(sections/section_drafts)跨重写循环必须双向流动,既是输入也是输出,
须同时列入 WritingState(子图读写)与 WritingOutput(output_schema 限定写回):
- 首次进入:split 拆章 → 并行写章填充 section_drafts;
- 打回重写:split 复用(revision_count>0 不调 LLM) → fan_out 只 Send 问题章节 → merge 重组,
  并把更新后的 sections/section_drafts 写回父图供下一轮保留.

双层 output_schema 是 Send 并行的硬要求(沿 section_writer 的既有结论下推一层):
- section_writer 子图 output_schema 只暴露 section_drafts → 并行实例只写 reducer 聚合键;
- 本子图 output_schema 只暴露 draft/sections/section_drafts → 父图只收到这三键,
  topic/outline/revision_count 等不会原样写回,根除 INVALID_CONCURRENT_GRAPH_UPDATE.
本子图由主图单实例顺序调用(与 outline 一样),不会被 Send 并行触发;内部 Send 并行
发生在下一层(section_writer 实例),父图不会出现并发写.
"""

import logging
from typing import Annotated, TypedDict

from langgraph.graph import START, END, StateGraph
from langgraph.types import Send

from agents.section_writer import build_section_writer
from llm import call_llm
from output_validation import SplitOutput, call_json_model
from prompts import SPLIT_PROMPT
from state import Section, _merge_dicts

logger = logging.getLogger(__name__)


class WritingState(TypedDict):
    """写作子 Agent 状态:与父图共享的通道 + 内部产出 draft.

    这些键同时存在于父图 ArticleState(同名通道),进入本子图时由父图注入,
    结束时经 output_schema 写回父图.
    """

    topic: str  # 共享:读(拆章/分发用)
    outline: str  # 共享:读(拆章用)
    sections: list[Section]  # 共享:split 产出/重写时复用;写回父图
    section_drafts: Annotated[dict[str, str], _merge_dicts]  # 共享:并行聚合;写回父图
    failed_sections: list[dict]  # 共享:读(只重写问题章节)
    revision_count: int  # 共享:读(split 复用/fan_out 判定)
    draft: str  # 本子图产出:merge 拼装;写回父图


class WritingOutput(TypedDict):
    """写作子 Agent 对外输出 schema:只暴露跨循环需要保留/新产出的三个键.

    compile(output_schema=...) 用它限定 output_channels,父图只收到这三个键.
    """

    draft: str
    sections: list[Section]
    section_drafts: Annotated[dict[str, str], _merge_dicts]


def split_sections(state: WritingState) -> dict:
    """把 outline 文本拆成结构化章节列表,写入 state["sections"].

    打回重写时(revision_count > 0 且已有 sections)直接复用,不重复调 LLM.
    输出经 SplitOutput 强校验,校验失败把具体字段错误反馈给模型重试;耗尽回退单章节.
    """
    if state.get("revision_count", 0) > 0 and state.get("sections"):
        logger.info("→ 复用已拆分的章节…")
        return {}
    logger.info("→ 拆分章节…")
    out = call_json_model(
        SPLIT_PROMPT,
        f"文章题目：{state['topic']}\n\n【提纲】\n{state['outline']}",
        SplitOutput,
        role="split",
        retry_prefix="请重新拆分并重新输出",
        llm_call=call_llm,
    )
    sections = out.model_dump()["sections"] if out is not None else []
    if not sections:
        # 兜底:拆章失败时至少给一个章节,避免并发分支为空
        logger.warning("  ⚠ 章节拆分失败，回退为单章节")
        sections = [{"title": state["topic"], "points": [], "materials": []}]
    # 给每个章节补 id(enumerate 顺序编号):section_drafts 以它作 key,打回时按 id 匹配
    sections = [dict(s, id=i) for i, s in enumerate(sections)]
    logger.info(f"  📑 已拆分为 {len(sections)} 个章节")
    return {"sections": sections}


def fan_out_write(state: WritingState):
    """条件边(挂在 split 后):返回 Send 列表触发并行写章节,或 "merge" 直接合并.

    首次写作:Send 全部章节;打回重写:只 Send failed_sections 里的问题章节
    (按 section["id"] 匹配),并把该章节专属的审校意见(feedback)一并传给
    write_section,各章互不串味.
    """
    sections = state.get("sections", [])
    if not sections:
        return "merge"
    if state.get("revision_count", 0) > 0 and state.get("failed_sections"):
        failed = state["failed_sections"]
        by_id = {sec["id"]: sec for sec in sections}
        feedback_map = {
            item["id"]: item.get("feedback", "")
            for item in failed
            if isinstance(item, dict) and "id" in item
        }
        targets = []
        for fid in feedback_map:
            sec = by_id.get(fid)
            if sec is not None:
                targets.append(sec)
        if not targets:
            return "merge"
        logger.info(f"  🔁 只重写问题章节：{[sec['id'] for sec in targets]}")
    else:
        feedback_map = {}
        targets = sections

    return [
        Send(
            "write_section",
            {
                "section": sec,
                "topic": state.get("topic", ""),
                "feedback": feedback_map.get(sec["id"], ""),
            },
        )
        for sec in targets
    ]


def merge_sections(state: WritingState) -> dict:
    """按 id 升序拼装各章节草稿,生成整篇 draft.

    sections 由 split 的 enumerate 编号(列表序 == id 序),这里显式按 id 排序,
    不依赖列表顺序,更稳健.缺章节草稿时补占位文本.
    """
    parts = []
    for sec in sorted(state.get("sections", []), key=lambda s: s.get("id", 0)):
        text = state.get("section_drafts", {}).get(str(sec["id"]))
        if text is None:
            text = f"## {sec.get('title', '')}\n\n（本章未生成）"
        parts.append(text)
    draft = "\n\n".join(parts).strip()
    logger.info("→ 合并章节完成")
    return {"draft": draft}


def build_writing_agent():
    """构造写作子 Agent 子图.

    START → split →(fan_out: [Send(...)×N] 或 "merge")→ write_section(章节写作子智能体,并行×N) → merge → END
    """
    g = StateGraph(WritingState, output_schema=WritingOutput)
    g.add_node("split", split_sections)
    g.add_node("write_section", build_section_writer())  # 章节写作子智能体(自包含子图)
    g.add_node("merge", merge_sections)

    g.add_edge(START, "split")
    g.add_conditional_edges("split", fan_out_write)  # 返回 [Send(...)] 或 "merge"
    g.add_edge("write_section", "merge")
    g.add_edge("merge", END)
    return g.compile()
