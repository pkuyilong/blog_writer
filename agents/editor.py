import json

from llm import call_llm
from prompts import EDITOR_PROMPT
from state import ArticleState


def editor_node(state: ArticleState) -> dict:
    """审校/润色节点：审校草稿，输出 JSON（质量分/是否合格/修改意见/润色全文）。

    写入多个 state 字段；passed 与 revision_count 供条件边决定是否打回重写。
    """
    print("→ 审校/润色中…")
    user_content = f"请审校并润色下面这篇中文文章草稿：\n\n{state['draft']}"
    raw = call_llm(EDITOR_PROMPT, user_content, json_mode=True)

    try:
        data = json.loads(raw)
        revised = str(data.get("revised_article", state["draft"]))
        passed = bool(data.get("passed", True))
        score = int(data.get("score", 60))
        feedback = str(data.get("feedback", "无需修改"))
    except (json.JSONDecodeError, ValueError):
        # 解析失败时保守处理：接受当前草稿，避免把流程卡进死循环
        print("  ⚠ 审校输出解析失败，按通过处理")
        revised, passed, score, feedback = state["draft"], True, 0, "审校输出解析失败"

    return {
        "final_article": revised,
        "quality_score": score,
        "passed": passed,
        "revision_feedback": feedback,
        "revision_count": state.get("revision_count", 0) + 1,
    }
