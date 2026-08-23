"""人工介入节点：大纲生成后、拆分章节前，让用户确认或修改大纲。

开关：CLI 加 `--human-review` 后 `build_graph(enable_human_review=True)`，
outline 之后的条件边把流程引到这个节点；默认关闭时节点完全不执行。

交互走 stderr（`print(..., file=sys.stderr)`），保持 stdout 只给成品文章——
这是 main.py 的既有约定（`> out.md` 重定向时交互提示不会混进产物）。
"""
import logging
import sys

from llm import call_llm
from prompts import REVISE_OUTLINE_PROMPT
from state import ArticleState

logger = logging.getLogger(__name__)

_EXIT_COMMANDS = {"q", "quit", "exit"}


def _print_outline(outline: str) -> None:
    """把当前大纲打印到 stderr（预览，不是产物）。"""
    print("\n👀 人工审阅大纲（--human-review 已开启）：", file=sys.stderr)
    print("----------------------------------------", file=sys.stderr)
    print(outline, file=sys.stderr)
    print("----------------------------------------", file=sys.stderr)


def _print_help() -> None:
    print("  [回车]          确认大纲，继续写作", file=sys.stderr)
    print("  [输入文字]      作为修改意见，重新生成大纲", file=sys.stderr)
    print("  [# 开头的内容]  视为你粘贴的完整新大纲，直接采用", file=sys.stderr)
    print("  [q]             退出", file=sys.stderr)


def _revise_outline(topic: str, outline: str, feedback: str) -> str:
    """按人工修改意见让 LLM 重写大纲（复用 call_llm，非 json 模式）。"""
    user_content = (
        f"文章题目：{topic}\n\n"
        f"【当前提纲】\n{outline}\n\n"
        f"【人工修改意见】\n{feedback}"
    )
    return call_llm(REVISE_OUTLINE_PROMPT, user_content)


def human_review_node(state: ArticleState) -> dict:
    """人工审阅大纲：打印大纲 → 循环等待确认/修改 → 返回最终 outline。

    返回 `{"outline": outline}` 覆盖 ArticleState.outline（普通 str，直接覆盖）。
    打回重写循环（split ← edit）不经过本节点，人工确认只发生一次。
    """
    logger.info("→ 人工审阅大纲（--human-review）…")
    outline = state.get("outline", "")
    _print_outline(outline)
    _print_help()

    while True:
        try:
            raw = input()
        except EOFError:
            # stdin 被关闭（如管道输入提前结束），按确认处理，避免卡死流程
            break
        cmd = raw.strip()
        if cmd.lower() in _EXIT_COMMANDS:
            sys.exit(0)
        if not cmd:
            break  # 直接回车：确认通过
        if cmd.startswith("#"):
            # 用户粘贴的完整新大纲，直接采用并重新展示供确认
            outline = cmd
            _print_outline(outline)
            _print_help()
            continue
        # 其余输入视为修改意见，交给 LLM 重写大纲后再展示一次
        logger.info("  ✍️ 按人工修改意见重新生成大纲…")
        outline = _revise_outline(state.get("topic", ""), outline, cmd)
        _print_outline(outline)
        _print_help()

    return {"outline": outline}
