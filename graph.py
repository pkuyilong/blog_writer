import logging
from functools import partial

from langgraph.graph import START, END, StateGraph

from agents.human_review import human_review_node
from agents.outliner import build_outliner
from agents.review import build_review_agent
from agents.writing import build_writing_agent
from langsmith_config import setup_langsmith
from state import ArticleState

logger = logging.getLogger(__name__)

# 最多打回重写几次(超出即接受当前结果,防止死循环)
MAX_REVISIONS = 2


def should_continue(state: ArticleState) -> str:
    """条件边:审校通过或达到次数上限 → end;否则打回写作节点重写."""
    if state["passed"] or state.get("revision_count", 0) >= MAX_REVISIONS:
        if not state["passed"]:
            logger.warning(f"  ⚠ 已达最大重写次数（{MAX_REVISIONS}），接受当前结果")
        return "end"
    return "rewrite"


# 大纲后的人工介入:开关开启 → human_review(确认/修改大纲)→ writing;关闭 → 直接 writing
def route_outline(state: ArticleState, enable: bool = False) -> str:
    return "human_review" if enable else "writing"


def route_review(state: ArticleState) -> str:
    """条件边(human_review 后):有修改意见(outline_review_feedback)→ 回环重写再确认;否则 → writing.

    多轮 HITL 的关键:用户输入修改意见时,human_review_node 把意见写进
    state["outline_review_feedback"],本边把流程引回 human_review 节点(节点开头会按
    意见 LLM 重写大纲再 interrupt 一次);确认后 feedback 被清空,放行 writing.
    """
    return "human_review" if state.get("outline_review_feedback") else "writing"


def build_graph(enable_human_review: bool = False, checkpointer=None):
    """组装流水线:大纲子智能体 →(可选人工介入)→ 写作子 Agent → 审核子智能体.

    其中 "outline" 是一个编译好的独立子图(agents/outliner.py):自包含地完成
    搜索素材 → 审查 → 生成提纲 → 自检,素材不足会补搜,提纲不合格会重试,
    保证一定返回可用的 outline.

    "writing" 是写作子 Agent 子图(agents/writing.py):内部完成 拆章 → Send 并行触发
    章节写作子智能体(agents/section_writer.py)×N → 按 id 合并,产出整篇 draft.
    审校不合格打回时主图再次进入 writing,子图内部只重写 failed_sections 里的
    问题章节,其余章节草稿保留(共享通道跨循环流动).

    "edit" 是审核子智能体(agents/review.py):3 个审校角色(语言/逻辑/事实)各自独立
    打分,多数表决(显式通过票 >= 2/3)判定是否合格,产出 final_article(=draft,取消润色)/
    quality_score/passed/failed_sections/revision_count 写回父图.

    enable_human_review=True 时,大纲生成后会在 human_review 节点停下,
    由人工确认/修改大纲再继续(对应 CLI 的 --human-review 开关);默认关闭,
    图与全自动版本完全一致(human_review 节点不执行).

    LangGraph 会根据环境变量自动向 LangSmith 上报执行过程(见 langsmith_config.py).

    checkpointer(MemorySaver/SqliteSaver 等)传给 compile():它提供 checkpoint 持久化,
    interrupt() 人工介入,--resume 断点续跑都依赖它.传 None 时图无状态(不能中断/续跑).
    """
    # 校验 LangSmith 配置并设置项目名(未配置时仅打印提示,不影响运行)
    setup_langsmith(project_name="blog_writer")

    graph = StateGraph(ArticleState)
    graph.add_node("outline", build_outliner())  # 大纲子智能体(自包含子图)
    graph.add_node("human_review", human_review_node)  # 可选:人工确认/修改大纲
    graph.add_node("writing", build_writing_agent())  # 写作子 Agent(自包含子图:拆章+并行写章+合并)
    graph.add_node("edit", build_review_agent())  # 审核子智能体(自包含子图:3 角色并行打分+多数表决)

    graph.add_edge(START, "outline")

    graph.add_conditional_edges(
        "outline",
        partial(route_outline, enable=enable_human_review),
        {"human_review": "human_review", "writing": "writing"},
    )
    graph.add_conditional_edges(
        "human_review",
        route_review,
        {"human_review": "human_review", "writing": "writing"},
    )
    graph.add_edge("writing", "edit")
    graph.add_conditional_edges(
        "edit",
        should_continue,
        {"rewrite": "writing", "end": END},
    )
    return graph.compile(checkpointer=checkpointer)
