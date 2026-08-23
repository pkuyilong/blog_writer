import logging
from functools import partial

from langgraph.graph import START, END, StateGraph

from agents.editor import editor_node
from agents.human_review import human_review_node
from agents.outliner import build_outliner
from agents.writer import fan_out_write, merge_sections, split_sections, write_section
from langsmith_config import setup_langsmith
from state import ArticleState

logger = logging.getLogger(__name__)

# 最多打回重写几次（超出即接受当前结果，防止死循环）
MAX_REVISIONS = 2


def should_continue(state: ArticleState) -> str:
    """条件边：审校通过或达到次数上限 → end；否则打回写作节点重写。"""
    if state["passed"] or state.get("revision_count", 0) >= MAX_REVISIONS:
        if not state["passed"]:
            logger.warning(f"  ⚠ 已达最大重写次数（{MAX_REVISIONS}），接受当前结果")
        return "end"
    return "rewrite"


# 大纲后的人工介入：开关开启 → human_review（确认/修改大纲）→ split；关闭 → 直接 split
def route_outline(state: ArticleState, enable: bool = False) -> str:
    return "human_review" if enable else "split"


def build_graph(enable_human_review: bool = False):
    """组装流水线：大纲子智能体 →（可选人工介入）→ 拆分章节 → 并发写作 → 合并 → 审校。

    其中 "outline" 是一个编译好的独立子图（agents/outliner.py）：自包含地完成
    搜索素材 → 审查 → 生成提纲 → 自检，素材不足会补搜、提纲不合格会重试，
    保证一定返回可用的 outline。

    enable_human_review=True 时，大纲生成后会在 human_review 节点停下，
    由人工确认/修改大纲再继续（对应 CLI 的 --human-review 开关）；默认关闭，
    图与全自动版本完全一致（human_review 节点不执行）。

    写作阶段按章节并行（Send map-reduce）：split 拆章节 → fan_out 条件边
    并行触发 write_section 多次 → merge 按序拼装成 draft。审校不合格打回时
    fan_out 只重写 failed_sections 里的问题章节，其余章节草稿保留。
    LangGraph 会根据环境变量自动向 LangSmith 上报执行过程（见 langsmith_config.py）。
    """
    # 校验 LangSmith 配置并设置项目名（未配置时仅打印提示，不影响运行）
    setup_langsmith(project_name="blog_writer")

    graph = StateGraph(ArticleState)
    graph.add_node("outline", build_outliner())  # 大纲子智能体（自包含子图）
    graph.add_node("human_review", human_review_node)  # 可选：人工确认/修改大纲
    graph.add_node("split", split_sections)
    graph.add_node("write_section", write_section)
    graph.add_node("merge", merge_sections)
    graph.add_node("edit", editor_node)

    graph.add_edge(START, "outline")

    graph.add_conditional_edges(
        "outline",
        partial(route_outline, enable=enable_human_review),
        {"human_review": "human_review", "split": "split"},
    )
    graph.add_edge("human_review", "split")
    graph.add_conditional_edges("split", fan_out_write)  # 返回 [Send(...)] 或 "merge"
    graph.add_edge("write_section", "merge")
    graph.add_edge("merge", "edit")
    graph.add_conditional_edges(
        "edit",
        should_continue,
        {"rewrite": "split", "end": END},
    )
    return graph.compile()
