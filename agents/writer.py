import json
import logging

from langgraph.types import Send

from llm import call_llm
from prompts import SELF_REVIEW_PROMPT, SPLIT_PROMPT, WRITE_SECTION_PROMPT
from state import ArticleState

logger = logging.getLogger(__name__)


def split_sections(state: ArticleState) -> dict:
    """把 outline 文本拆成结构化章节列表，写入 state["sections"]。

    打回重写时（revision_count > 0 且已有 sections）直接复用，不重复调 LLM。
    """
    if state.get("revision_count", 0) > 0 and state.get("sections"):
        logger.info("→ 复用已拆分的章节…")
        return {}
    logger.info("→ 拆分章节…")
    raw = call_llm(
        SPLIT_PROMPT,
        f"文章题目：{state['topic']}\n\n【提纲】\n{state['outline']}",
        json_mode=True,
    )
    try:
        sections = json.loads(raw).get("sections", [])
    except json.JSONDecodeError:
        sections = []
    if not sections:
        # 兜底：拆章失败时至少给一个章节，避免并发分支为空
        logger.warning("  ⚠ 章节拆分失败，回退为单章节")
        sections = [{"title": state["topic"], "points": [], "materials": []}]
    # 给每个章节补 id（enumerate 顺序编号）：section_drafts 以它作 key，打回时按 id 匹配
    sections = [dict(s, id=i) for i, s in enumerate(sections)]
    logger.info(f"  📑 已拆分为 {len(sections)} 个章节")
    return {"sections": sections}


def fan_out_write(state: ArticleState):
    """条件边（挂在 split 后）：返回 Send 列表触发并行写章节，或 "merge" 直接合并。

    首次写作：Send 全部章节；打回重写：只 Send failed_sections 里的问题章节
    （按 section["id"] 匹配），并把该章节专属的审校意见（feedback）一并传给
    write_section，各章互不串味。
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


def write_section(state: dict) -> dict:
    """写单个章节（被 Send 并行调用多次）。返回 {"section_drafts": {id: text}}。

    state 由 fan_out_write 的 Send payload 注入，只含三个键：
    section / topic / feedback，不是完整 ArticleState；章节 id 在
    state["section"]["id"] 里。打回重写时 state["feedback"] 是该章节
    专属的审校意见（首次写作时为空串）。

    写完初稿后做一轮**自我反思**（SELF_REVIEW_PROMPT）：让模型审视自己的
    输出（内容扎实度/语言自然度/科普效果/衔接），直接输出改进后的章节，
    提升初稿质量，减少外部审校打回次数。
    """
    section = state["section"]
    sid = section["id"]
    logger.info(f"→ 写作章节[{sid}]：{section['title']}…")
    user_content = (
        f"文章主题：{state.get('topic', '')}\n\n"
        f"【本章节】标题：{section['title']}\n"
        f"要点：{section.get('points', [])}\n"
        f"素材：{section.get('materials', [])}"
    )
    feedback = state.get("feedback", "")
    if feedback:
        user_content += f"\n\n【上轮审校意见】{feedback}\n请逐条针对意见修改本章节。"

    # 第一轮：生成初稿
    text = call_llm(WRITE_SECTION_PROMPT, user_content)

    # 第二轮：自我反思改进（审视自己刚写的章节 → 输出改进版）
    logger.info(f"  🔍 自我反思章节[{sid}]…")
    review_input = (
        f"文章主题：{state.get('topic', '')}\n\n"
        f"【本章节】标题：{section['title']}\n"
        f"要点：{section.get('points', [])}\n\n"
        f"【初稿】\n{text}"
    )
    if feedback:
        review_input += f"\n\n【上轮审校意见】{feedback}\n请确保改进后仍满足这些要求。"
    text = call_llm(SELF_REVIEW_PROMPT, review_input)

    return {"section_drafts": {str(sid): text}}


def merge_sections(state: ArticleState) -> dict:
    """按 sections 顺序拼装各章节草稿，生成整篇 draft。"""
    parts = []
    for sec in state.get("sections", []):
        text = state.get("section_drafts", {}).get(str(sec["id"]))
        if text is None:
            text = f"## {sec.get('title', '')}\n\n（本章未生成）"
        parts.append(text)
    draft = "\n\n".join(parts).strip()
    logger.info("→ 合并章节完成")
    return {"draft": draft}
