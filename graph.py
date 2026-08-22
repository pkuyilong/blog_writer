from langgraph.graph import START, END, StateGraph

from agents.editor import editor_node
from agents.researcher import researcher_node
from agents.writer import writer_node
from state import ArticleState


def build_graph():
    """组装三节点线性流水线：调研 → 写作 → 审校，并编译成可调用的图。"""
    graph = StateGraph(ArticleState)
    graph.add_node("research", researcher_node)
    graph.add_node("write", writer_node)
    graph.add_node("edit", editor_node)
    graph.add_edge(START, "research")
    graph.add_edge("research", "write")
    graph.add_edge("write", "edit")
    graph.add_edge("edit", END)
    return graph.compile()
