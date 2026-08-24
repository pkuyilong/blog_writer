"""章节写作子智能体：自包含的 LangGraph 子图，负责"写单章 → 自检 → 条件重写"。

作为主图（graph.py）的 write_section 节点挂载，被 fan_out_write 的 Send 并行触发
多次（每章一个实例）。子图读 Send payload（section / topic / feedback），内部自主完成
初稿 → 启发式自检 → 不合格且未到上限则重写，最终一定返回 {"section_drafts": {id: text}}。

自检用启发式（篇幅 / 标题格式），不调 LLM，省 token 且确定性可测：
- 合格 → 直接结束（省掉旧版"无条件自我反思一轮"的那次调用）；
- 不合格且 write_attempt < MAX_SECTION_ATTEMPTS → 重写（自检意见作为反馈塞回生成提示）；
- 不合格且已到上限 → 接受当前结果（warning 兜底）。

子图对外**只输出 section_drafts**（compile 时用 output_schema=SectionWriterOutput 限定），
输入键（section/topic/feedback）与私有键（section_text/write_attempt/self_check_notes）
一律不写回父图。这一限制是 Send 并行触发的**硬要求**：多个子图实例在同一 superstep
并行结束时，若把输入 topic 原样写回父图，父图 topic 是普通键（LastValue），同一
superstep 收到多个值会抛 INVALID_CONCURRENT_GRAPH_UPDATE（langgraph 1.2.11 实测踩坑）。
输出 schema 只暴露 section_drafts 后，父图 apply_writes 只处理该键（reducer 聚合），
冲突根除。若想给子图加新输出，改 SectionWriterOutput 即可。
⚠ 硬约束：子图内部草稿键绝不能命名 draft（父图有 draft 通道，会被单章文本覆盖、
破坏 merge 结果）。本子图一律用 section_text。
"""

import logging
from typing import Annotated, TypedDict

from langgraph.graph import START, END, StateGraph

from llm import call_llm
from prompts import SELF_REVIEW_PROMPT, WRITE_SECTION_PROMPT
from state import _merge_dicts

logger = logging.getLogger(__name__)

# 章节写作重试上限：写 1 次 + 重写 1 次，仍不合格则接受当前结果
MAX_SECTION_ATTEMPTS = 2
# 章节最低长度（字符），低于视为"不合格"触发重写（正常章节约 200-400 字）
MIN_SECTION_LEN = 120


class SectionWriterState(TypedDict):
    """章节写作子图状态。

    输入（来自 fan_out_write 的 Send payload）：section / topic / feedback。
    输出：section_drafts（写回父图经 reducer 聚合，重写覆盖同 id）。
    私有（不写回父图）：section_text / write_attempt / self_check_notes。

    硬约束：私有键与父图 ArticleState 的键（topic/outline/sections/draft/
    passed/...）零重叠，尤其不能用 draft。
    """

    section: dict  # {id, title, points, materials}
    topic: str
    feedback: str  # 上轮审校意见，仅打回重写时非空
    section_text: str  # 私有：当前章节正文
    write_attempt: int  # 私有：写作尝试计数
    self_check_notes: str  # 私有：自检发现的不足；空串 = 合格
    section_drafts: Annotated[dict[str, str], _merge_dicts]  # 输出：章节id → 草稿


class SectionWriterOutput(TypedDict):
    """子图对外输出 schema：只暴露 section_drafts，输入/私有键一律不写回父图。

    compile(output_schema=...) 用它限定 output_channels，从而避免 Send 并行多实例
    把 topic 等输入键原样写回父图造成并发写冲突（见模块 docstring）。
    """

    section_drafts: Annotated[dict[str, str], _merge_dicts]


def _self_check(section: dict, draft: str) -> tuple[bool, str]:
    """启发式自检：篇幅 / 标题格式。返回 (是否合格, 不足说明；合格为空串)。

    不做"要点覆盖"子串检查——split 生成的 points 是描述性写作指令（如"用生活化
    场景对比早高峰通勤与在家办公的差异"），正文几乎不可能逐字复述，子串匹配在
    真实 e2e 中 100% 误判（两次运行 7 章全部被误判"未覆盖要点"、白白各多付一次
    重写调用）。要点覆盖属内容层，与 LLM 自由表达不兼容，交给外部 editor 审校。
    """
    t = draft.strip()
    problems = []
    if len(t) < MIN_SECTION_LEN:
        problems.append(f"篇幅不足（{len(t)}字 < {MIN_SECTION_LEN}字）")
    if not t.startswith("## "):
        problems.append("缺少章节标题（应以 ## 开头）")
    return (not problems), "；".join(problems)


def write(state: SectionWriterState) -> dict:
    """初稿或按自检/审校意见重写；write_attempt 自增。

    首写用 WRITE_SECTION_PROMPT（基于要点与素材生成）；重写用 SELF_REVIEW_PROMPT，
    把上一版草稿 + 自检意见 + 审校意见一并给模型，让它审视后直接输出改进版。
    """
    attempt = state.get("write_attempt", 0) + 1
    sec = state["section"]
    sid = sec.get("id")
    logger.info(f"  ✍️ 章节写作子智能体写章节[{sid}]（第 {attempt} 次）…")

    if attempt == 1:
        user_content = (
            f"文章主题：{state.get('topic', '')}\n\n"
            f"【本章节】标题：{sec.get('title', '')}\n"
            f"要点：{sec.get('points', [])}\n"
            f"素材：{sec.get('materials', [])}"
        )
        prompt = WRITE_SECTION_PROMPT
    else:
        user_content = (
            f"文章主题：{state.get('topic', '')}\n\n"
            f"【本章节】标题：{sec.get('title', '')}\n"
            f"要点：{sec.get('points', [])}\n\n"
            f"【当前草稿】\n{state.get('section_text', '')}"
        )
        if state.get("self_check_notes"):
            user_content += f"\n\n【自检意见】{state['self_check_notes']}"
        prompt = SELF_REVIEW_PROMPT

    if state.get("feedback"):
        user_content += f"\n\n【上轮审校意见】{state['feedback']}\n请逐条针对意见修改本章节。"

    text = call_llm(prompt, user_content, role="write")
    return {"section_text": text, "write_attempt": attempt}


def self_check(state: SectionWriterState) -> dict:
    """启发式自检当前草稿，把不足写进 self_check_notes（空串 = 合格）。"""
    ok, notes = _self_check(state["section"], state.get("section_text", ""))
    if not ok:
        logger.info(f"  🧐 章节[{state['section'].get('id')}]自检不通过：{notes}")
    return {"self_check_notes": notes}


def should_rewrite(state: SectionWriterState) -> str:
    """自检结论路由：合格→end；不合格且达上限→end（接受）；否则→rewrite。"""
    if not state.get("self_check_notes"):
        return "end"
    if state.get("write_attempt", 0) >= MAX_SECTION_ATTEMPTS:
        logger.warning(
            f"  ⚠ 章节[{state['section'].get('id')}]已达最大写作次数，接受当前结果"
        )
        return "end"
    return "rewrite"


def emit(state: SectionWriterState) -> dict:
    """输出章节草稿：{"section_drafts": {str(id): text}}，写回父图经 reducer 聚合。"""
    return {
        "section_drafts": {
            str(state["section"]["id"]): state.get("section_text", "")
        }
    }


def build_section_writer():
    """构造章节写作子智能体子图。

    START → write → self_check → should_rewrite →(rewrite)→ write
                                     │(end)
                                     └→ emit → END
    """
    g = StateGraph(SectionWriterState, output_schema=SectionWriterOutput)
    g.add_node("write", write)
    g.add_node("self_check", self_check)
    g.add_node("emit", emit)

    g.add_edge(START, "write")
    g.add_edge("write", "self_check")
    g.add_conditional_edges(
        "self_check",
        should_rewrite,
        {"rewrite": "write", "end": "emit"},
    )
    g.add_edge("emit", END)
    return g.compile()
