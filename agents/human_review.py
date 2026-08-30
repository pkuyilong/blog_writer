"""人工介入节点:大纲生成后,拆分章节前,用 LangGraph 的 interrupt() 协议暂停,
把大纲交给客户端(main.py 的交互循环)确认/修改,再经 Command(resume=...) 续跑.

架构(LangGraph 1.2.11 HITL 正统写法):
- 节点内调用 `interrupt({...})` 暂停图,invoke 返回 {"__interrupt__": (Interrupt(value=...),)}
- 客户端读取 payload,交互后以 Command(resume={...}) 续跑;resume 后节点从头重执行
- resume 载荷三种 action:
    confirm  → 确认大纲,继续写作
    replace  → 用户粘贴的完整新大纲,直接采用
    revise   → 一段修改意见;本节点把 outline_review_feedback 写入 state,经条件边
               回环重新进入本节点 → 按意见 LLM 重写大纲 → 再次 interrupt 展示确认
- 多轮修改 = interrupt → resume(revise) → 回环 → interrupt → resume(confirm) 的循环,
  完全由 LangGraph 控制,不再用节点内同步 input()(那会阻塞整个图,无法断点续跑).

注意:interrupt 必须配 checkpointer(graph.py 里 compile(checkpointer=...)),
否则会直接报错.交互提示由 main.py 统一走 stderr,保持 stdout 只给成品文章.
"""
import logging

from langgraph.types import interrupt

from llm import call_llm
from prompts import REVISE_OUTLINE_PROMPT
from state import ArticleState

logger = logging.getLogger(__name__)


def _revise_outline(topic: str, outline: str, feedback: str) -> str:
    """按人工修改意见让 LLM 重写大纲(复用 call_llm,显式 json_mode=False)."""
    user_content = (
        f"文章题目：{topic}\n\n"
        f"【当前提纲】\n{outline}\n\n"
        f"【人工修改意见】\n{feedback}"
    )
    return call_llm(REVISE_OUTLINE_PROMPT, user_content, role="revise_outline", json_mode=False)


def human_review_node(state: ArticleState) -> dict:
    """人工审阅大纲:按需重写 → interrupt 暂停展示 → 按 resume 载荷决定下一步.

    返回 dict 写入主图 state;outline_review_feedback 非 None 时条件边 route_review
    会把流程引回本节点再做一轮确认,None 则放行到 split.
    """
    logger.info("→ 人工审阅大纲（--human-review）…")
    outline = state["outline"]
    feedback = state.get("outline_review_feedback")
    if feedback:
        # 第二次(或更多次)进入:先按上轮修改意见重写大纲,再展示确认
        logger.info("  ✍️ 按人工修改意见重新生成大纲…")
        outline = _revise_outline(state["topic"], outline, feedback)

    decision = interrupt(
        {
            "type": "outline_review",
            "topic": state["topic"],
            "outline": outline,
        }
    )
    action = decision.get("action", "confirm")
    if action == "confirm":
        return {"outline": outline, "outline_review_feedback": None}
    if action == "replace":
        # 用户粘贴的完整新大纲,直接采用(不调 LLM)
        return {"outline": decision.get("outline", outline), "outline_review_feedback": None}
    # revise:把意见写进 state,条件边回环到本节点重写后再确认
    return {"outline": outline, "outline_review_feedback": decision.get("feedback")}
