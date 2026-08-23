import json

from langgraph.types import Send

from llm import call_llm
from prompts import SPLIT_PROMPT, WRITE_SECTION_PROMPT
from state import ArticleState


def split_sections(state: ArticleState) -> dict:
    """把 outline 文本拆成结构化章节列表，写入 state["sections"]。

    打回重写时（revision_count > 0 且已有 sections）直接复用，不重复调 LLM。
    """
    if state.get("revision_count", 0) > 0 and state.get("sections"):
        print("→ 复用已拆分的章节…")
        return {}
    print("→ 拆分章节…")
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
        print("  ⚠ 章节拆分失败，回退为单章节")
        sections = [{"title": state["topic"], "points": [], "materials": []}]
    print(f"  📑 已拆分为 {len(sections)} 个章节")
    return {"sections": sections}


def fan_out_write(state: ArticleState):
    """条件边（挂在 split 后）：返回 Send 列表触发并行写章节，或 "merge" 直接合并。

    首次写作：Send 全部章节；打回重写：只 Send failed_sections 里的问题章节，
    并把该章节专属的审校意见（feedback）一并传给 write_section，各章互不串味。
    """
    sections = state.get("sections", [])
    if not sections:
        return "merge"
    if state.get("revision_count", 0) > 0 and state.get("failed_sections"):
        failed = state["failed_sections"]
        feedback_map = {
            item["id"]: item.get("feedback", "")
            for item in failed
            if isinstance(item, dict) and "id" in item
        }
        targets = [i for i in feedback_map if 0 <= i < len(sections)]
        if not targets:
            return "merge"
        print(f"  🔁 只重写问题章节：{targets}")
    else:
        feedback_map = {}
        targets = list(range(len(sections)))

    return [
        Send(
            "write_section",
            {
                "section_id": i,
                "section": sections[i],
                "topic": state.get("topic", ""),
                "feedback": feedback_map.get(i, ""),
            },
        )
        for i in targets
    ]


def write_section(state: ArticleState) -> dict:
    """写单个章节（被 Send 并行调用多次）。返回 {"section_drafts": {id: text}}。

    打回重写时 state["feedback"] 是该章节专属的审校意见（首次写作时为空串）。
    """
    sid = state["section_id"]
    section = state["section"]
    print(f"→ 写作章节[{sid}]：{section['title']}…")
    user_content = (
        f"文章主题：{state.get('topic', '')}\n\n"
        f"【本章节】标题：{section['title']}\n"
        f"要点：{section.get('points', [])}\n"
        f"素材：{section.get('materials', [])}"
    )
    feedback = state.get("feedback", "")
    if feedback:
        user_content += f"\n\n【上轮审校意见】{feedback}\n请逐条针对意见修改本章节。"
    text = call_llm(WRITE_SECTION_PROMPT, user_content)
    return {"section_drafts": {str(sid): text}}


def merge_sections(state: ArticleState) -> dict:
    """按 sections 顺序拼装各章节草稿，生成整篇 draft。"""
    parts = []
    for i, sec in enumerate(state.get("sections", [])):
        text = state.get("section_drafts", {}).get(str(i))
        if text is None:
            text = f"## {sec.get('title', '')}\n\n（本章未生成）"
        parts.append(text)
    draft = "\n\n".join(parts).strip()
    print("→ 合并章节完成")
    return {"draft": draft}
