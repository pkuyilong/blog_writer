import json
import logging

from llm import call_llm
from prompts import EDITOR_PROMPT
from state import ArticleState

logger = logging.getLogger(__name__)

# 每次审校最多调 LLM 的次数：1 次正式 + 1 次重试；重试耗尽才保守按通过处理
EDITOR_MAX_RETRIES = 2
# 重试提示：JSON 模式要求 prompt 里出现 "json" 字样（EDITOR_PROMPT 已满足）
EDITOR_RETRY_NOTE = (
    "\n\n【警告】上一次输出不是合法 JSON（已丢弃）。"
    "请严格只输出一个符合要求的 JSON 对象，不要夹杂任何其他文字。"
)


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


def _parse_editor_output(raw: str, fallback_draft: str):
    """解析审校 JSON；任一字段不合法都会抛 (json.JSONDecodeError, ValueError)。

    由 editor_node 的重试循环调用：解析失败就重试，重试耗尽才保守降级。
    """
    data = json.loads(raw)
    revised = str(data.get("revised_article", fallback_draft))
    passed = bool(data.get("passed", True))
    score = int(data.get("score", 60))  # 非整数值会抛 ValueError，触发重试
    failed_sections = _parse_failed_sections(data.get("failed_sections", []))
    return revised, passed, score, failed_sections


def editor_node(state: ArticleState) -> dict:
    """审校/润色节点：审校草稿，输出 JSON（质量分/是否合格/润色全文/问题章节）。

    写入多个 state 字段；passed 与 revision_count 供条件边决定是否打回重写。
    failed_sections 是 [{id, feedback}]，每个问题章节自带专属修改意见，
    打回时每个章节只会看到自己的意见，互不串味。

    JSON 解析失败时重试（最多 EDITOR_MAX_RETRIES 次），重试提示附"上次不是
    合法 JSON"；重试耗尽才保守按通过处理（沿用 CLAUDE.md 决策 #5 的降级语义，
    避免把流程卡进死循环）。revision_count 只在此节点每次执行时 +1，不因重试多计。
    """
    logger.info("→ 审校/润色中…")
    base_content = f"请审校并润色下面这篇中文文章草稿：\n\n{state['draft']}"
    user_content = base_content

    for attempt in range(1, EDITOR_MAX_RETRIES + 1):
        raw = call_llm(EDITOR_PROMPT, user_content, json_mode=True)
        try:
            revised, passed, score, failed_sections = _parse_editor_output(
                raw, state["draft"]
            )
            break
        except (json.JSONDecodeError, ValueError):
            logger.warning(f"  ⚠ 审校输出解析失败（第 {attempt} 次）")
            if attempt < EDITOR_MAX_RETRIES:
                user_content = base_content + EDITOR_RETRY_NOTE
            else:
                # 重试耗尽：保守按通过处理，避免把流程卡进死循环
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
