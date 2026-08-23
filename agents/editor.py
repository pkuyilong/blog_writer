import json
import logging

from llm import call_llm
from prompts import EDITOR_PROMPT
from state import ArticleState

logger = logging.getLogger(__name__)


def _parse_failed_sections(failed) -> list[dict]:
    """把审校 JSON 里的 failed_sections 解析成 [{id, feedback}] 列表。

    支持两种输入：
    - 新格式：[{"id": 0, "feedback": "该章节的具体修改意见"}, ...]
    - 兼容旧格式：纯编号 [0, 2]（feedback 留空）
    无法解析的条目直接丢弃，保证返回的都是合法结构。
    """
    result = []
    if not isinstance(failed, list):
        return result
    for item in failed:
        if isinstance(item, dict) and item.get("id") is not None:
            try:
                sid = int(item["id"])
            except (TypeError, ValueError):
                continue
            feedback = str(item.get("feedback", "")).strip()
            result.append({"id": sid, "feedback": feedback})
        elif isinstance(item, (int, float)) or (isinstance(item, str) and item.isdigit()):
            result.append({"id": int(item), "feedback": ""})
    return result


def editor_node(state: ArticleState) -> dict:
    """审校/润色节点：审校草稿，输出 JSON（质量分/是否合格/润色全文/问题章节）。

    写入多个 state 字段；passed 与 revision_count 供条件边决定是否打回重写。
    failed_sections 是 [{id, feedback}]，每个问题章节自带专属修改意见，
    打回时每个章节只会看到自己的意见，互不串味。
    """
    logger.info("→ 审校/润色中…")
    user_content = f"请审校并润色下面这篇中文文章草稿：\n\n{state['draft']}"
    raw = call_llm(EDITOR_PROMPT, user_content, json_mode=True)

    try:
        data = json.loads(raw)
        revised = str(data.get("revised_article", state["draft"]))
        passed = bool(data.get("passed", True))
        score = int(data.get("score", 60))
        failed_sections = _parse_failed_sections(data.get("failed_sections", []))
    except (json.JSONDecodeError, ValueError):
        # 解析失败时保守处理：接受当前草稿，避免把流程卡进死循环
        logger.warning("  ⚠ 审校输出解析失败，按通过处理")
        revised, passed, score, failed_sections = (
            state["draft"], True, 0, []
        )

    return {
        "final_article": revised,
        "quality_score": score,
        "passed": passed,
        "failed_sections": failed_sections,
        "revision_count": state.get("revision_count", 0) + 1,
    }
