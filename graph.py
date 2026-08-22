from langgraph.graph import START, END, StateGraph

from agents.editor import editor_node
from agents.outliner import build_outliner
from agents.writer import writer_node
from langsmith_config import setup_langsmith
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
    """组装流水线：大纲子智能体 → 写作 → 审校，审校不合格时打回写作重写。

    其中 "outline" 是一个编译好的独立子图（agents/outliner.py）：自包含地完成
    搜索素材 → 审查 → 生成提纲 → 自检，素材不足会补搜、提纲不合格会重试，
    保证一定返回可用的 outline。
    LangGraph 会根据环境变量自动向 LangSmith 上报执行过程（见 langsmith_config.py）。
    """
    # 校验 LangSmith 配置并设置项目名（未配置时仅打印提示，不影响运行）
    setup_langsmith(project_name="b_writer")

    graph = StateGraph(ArticleState)
    graph.add_node("outline", build_outliner())  # 大纲子智能体（自包含子图）
    graph.add_node("write", writer_node)
    graph.add_node("edit", editor_node)
    graph.add_edge(START, "outline")
    graph.add_edge("outline", "write")
    graph.add_edge("write", "edit")
    graph.add_conditional_edges(
        "edit",
        should_continue,
        {"rewrite": "write", "end": END},
    )
    return graph.compile()
