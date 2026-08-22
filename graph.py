from langgraph.graph import START, END, StateGraph

from agents.editor import editor_node
from agents.researcher import researcher_node
from agents.writer import writer_node
from state import ArticleState

# 最多打回重写几次（超出即接受当前结果，防止死循环）
MAX_REVISIONS = 2


def should_continue(state: ArticleState) -> str:
    """条件边：审校通过或达到次数上限 → end；否则打回写作节点重写。"""
    if state["passed"] or state.get("revision_count", 0) >= MAX_REVISIONS:
        if not state["passed"]:
            print(f"  ⚠ 已达最大重写次数（{MAX_REVISIONS}），接受当前结果")
        return "end"
    return "rewrite"


def build_graph():
    """组装流水线：调研 → 写作 → 审校，审校不合格时打回写作重写。"""
    graph = StateGraph(ArticleState)
    graph.add_node("research", researcher_node)
    graph.add_node("write", writer_node)
    graph.add_node("edit", editor_node)
    graph.add_edge(START, "research")
    graph.add_edge("research", "write")
    graph.add_edge("write", "edit")
    graph.add_conditional_edges(
        "edit",
        should_continue,
        {"rewrite": "write", "end": END},
    )
    return graph.compile()
