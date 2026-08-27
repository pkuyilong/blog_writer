"""审核子智能体:自包含 LangGraph 子图,内部 3 个审校角色并行独立打分 + 多数表决聚合.

作为主图 graph.py 的 edit 节点挂载:读父图 draft/revision_count,内部
START →(fan_out_reviewers: Send×3)→ review_role(并行×3) → aggregate(多数表决) → END,
产出 final_article(=draft,取消润色)/quality_score/passed/failed_sections/revision_count 写回父图.
审校不合格打回时主图再次进入 writing,只重写 failed_sections 里的问题章节(语义与旧版 editor 一致).

3 个审校角色(语言编辑/逻辑结构/事实准确性)各自独立调一次 LLM 打分,互不干扰:
- 各自输出 {score, passed, failed_sections},经 Send 并行写入私有 reducer 聚合键 role_reviews;
- aggregate 按"多数表决"聚合:显式通过票达到多数(3 角色即 >= 2)才算整篇通过;
- 角色 JSON 解析失败重试耗尽 → 弃权(passed=None 不投通过票,也不污染分数均值),
  避免坏角色把多数表决带偏;3 角色全弃权才保守通过(沿 CLAUDE.md 决策 #5 的"绝不卡死"语义).

与父图共享的键:
- 输入:draft / revision_count
- 输出(aggregate 单点,output_schema 限定):final_article / quality_score / passed / failed_sections / revision_count
内部 Send 并行实例只写 role_reviews(reducer 聚合),父图不会出现并发写
(沿决策 #12/#13 的 Send 并行硬要求,langgraph 1.2.11 实测).
"""

import json
import logging
from typing import Annotated, TypedDict

from langgraph.graph import START, END, StateGraph
from langgraph.types import Send

from llm import call_llm
from prompts import REVIEW_FACT_PROMPT, REVIEW_LANG_PROMPT, REVIEW_LOGIC_PROMPT
from state import _merge_dicts

logger = logging.getLogger(__name__)

# 每个审校角色每次最多调 LLM 次数:1 次正式 + 1 次重试;重试耗尽该角色弃权
REVIEW_MAX_RETRIES = 2
# 重试提示:JSON 模式要求 prompt 里出现 "json" 字样(各 REVIEW_*_PROMPT 已用小写 json 满足)
REVIEW_RETRY_NOTE = (
    "\n\n【警告】上一次输出不是合法 json（已丢弃）。"
    "请严格只输出一个符合要求的 json 对象，不要夹杂任何其他文字。"
)
# 审校角色表:role 名(model_router 注册的 role / prompts 里的角色) -> (prompt, 中文标签)
REVIEW_ROLES = {
    "edit_lang": (REVIEW_LANG_PROMPT, "语言编辑"),
    "edit_logic": (REVIEW_LOGIC_PROMPT, "逻辑结构"),
    "edit_fact": (REVIEW_FACT_PROMPT, "事实准确性"),
}
# 固定顺序(供 Send 列表与聚合遍历稳定;测试也按此断言)
REVIEW_ROLE_NAMES = tuple(REVIEW_ROLES)


class ReviewState(TypedDict):
    """审核子图状态.

    输入(与父图共享):draft / revision_count.
    输出(aggregate 单点,output_schema 限定写回):final_article / quality_score /
    passed / failed_sections / revision_count.
    Send 并行实例的私有键:role_name / role_reviews(与父图 ArticleState 零重叠;
    父图的 draft/passed/quality_score/failed_sections/final_article 只能当共享输入
    或 aggregate 单点输出,绝不能当并行实例的私有写键).
    """

    draft: str  # 输入:全文章稿(与父图共享)
    revision_count: int  # 输入/输出:已审校次数(跨重写循环共享)
    role_name: str  # 私有:Send payload,当前实例的角色名
    role_reviews: Annotated[dict, _merge_dicts]  # 私有:并行聚合 role_name -> {score, passed, failed_sections}
    final_article: str  # 输出:恒 = draft(取消润色,合格时即成品)
    quality_score: int  # 输出:有效票 score 均值
    passed: bool  # 输出:多数表决结果
    failed_sections: list[dict]  # 输出:合并后的问题章节(通过时 [])


class ReviewOutput(TypedDict):
    """审核子图对外输出 schema:只暴露写回父图的 5 个键.

    compile(output_schema=...) 用它限定 output_channels,输入键(draft)与私有键
    (role_name/role_reviews)一律不写回父图.
    """

    final_article: str
    quality_score: int
    passed: bool
    failed_sections: list[dict]
    revision_count: int


def _parse_failed_sections(failed) -> list[dict]:
    """把审校 JSON 里的 failed_sections 解析成 [{id, feedback}] 列表.

    支持两种输入:
    - 新格式:[{"id": 0, "feedback": "该章节的具体修改意见"}, ...]
    - 兼容旧格式:纯编号 [0, 2](feedback 留空)
    无法解析的条目直接丢弃,保证返回的都是合法结构.
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


def _parse_review_output(raw: str) -> tuple[int, bool, list[dict]]:
    """解析单个角色的审校 json;返回 (score, passed, failed_sections).

    任一字段不合法都会抛 (json.JSONDecodeError, ValueError),由 review_role 的
    重试循环调用:解析失败就重试,重试耗尽该角色弃权.
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        # 模型输出 JSON 数组/裸值/null 时 data.get 会抛 AttributeError/TypeError,
        # 归一成 ValueError 让重试循环接管(否则会击穿整图,见 CLAUDE.md 决策 #5)
        raise ValueError("审校输出不是 json 对象，无法解析")
    raw_score = data.get("score", 60)
    if raw_score is None:
        raise ValueError("score 字段为空，无法解析")
    score = int(raw_score)  # 非整数值会抛 ValueError,触发重试
    passed = bool(data.get("passed", True))  # 沿用旧宽松默认 True
    failed_sections = _parse_failed_sections(data.get("failed_sections", []))
    return score, passed, failed_sections


def review_role(state: ReviewState) -> dict:
    """单个审校角色:按 role_name 选 prompt,json_mode 调用,解析失败重试,耗尽弃权.

    由 fan_out_reviewers 的 Send 并行触发多次(每个角色一个实例);只写私有 reducer
    聚合键 role_reviews(role_name -> {score, passed, failed_sections}).
    解析失败重试 REVIEW_MAX_RETRIES 次(重试提示附"上次不是合法 json");耗尽弃权
    passed=None -- 不投通过票,也不污染分数均值(多数表决不拉偏,见 CLAUDE.md 决策 #5).
    """
    role_name = state["role_name"]
    prompt, label = REVIEW_ROLES[role_name]
    base_content = f"请从【{label}】的角度审校下面这篇中文文章草稿：\n\n{state['draft']}"
    user_content = base_content
    review = {"score": 0, "passed": None, "failed_sections": []}  # 默认:弃权
    for attempt in range(1, REVIEW_MAX_RETRIES + 1):
        raw = call_llm(prompt, user_content, json_mode=True, role=role_name)
        try:
            score, passed, failed_sections = _parse_review_output(raw)
            review = {
                "score": score,
                "passed": passed,
                "failed_sections": failed_sections,
            }
            break
        except (json.JSONDecodeError, ValueError):
            logger.warning(f"  ⚠ 【{label}】审校输出解析失败（第 {attempt} 次）")
            if attempt < REVIEW_MAX_RETRIES:
                user_content = base_content + REVIEW_RETRY_NOTE
    return {"role_reviews": {role_name: review}}


def fan_out_reviewers(state: ReviewState):
    """START 条件边:返回 [Send("review_role", {role_name, draft})×N] 或 "aggregate".

    Send 并行触发每个审校角色一个实例(角色表为空时直接走 aggregate 兜底).
    """
    if not REVIEW_ROLE_NAMES:
        return "aggregate"
    logger.info(f"→ 审核子智能体审校中（{len(REVIEW_ROLE_NAMES)} 角色并行打分）…")
    return [
        Send("review_role", {"role_name": rn, "draft": state["draft"]})
        for rn in REVIEW_ROLE_NAMES
    ]


def _merge_failed_sections(valid: dict) -> list[dict]:
    """把各失败角色的 failed_sections 按 id 合并;同 id 多条 feedback 用【角色名】前缀拼接.

    只聚合 passed 为 False 的角色的问题章节(通过角色契约上应为空,即使模型多给也忽略).
    返回按 id 升序排列的 [{id, feedback}],供 writing 打回时按 id 匹配章节.
    """
    by_id: dict[int, dict] = {}
    for role_name, rv in valid.items():
        if rv["passed"] is True:
            continue
        _, label = REVIEW_ROLES[role_name]
        for item in rv.get("failed_sections", []):
            sid = item["id"]
            feedback = str(item.get("feedback", "")).strip()
            part = f"【{label}】{feedback}" if feedback else f"【{label}】判定不通过"
            entry = by_id.setdefault(sid, {"id": sid, "feedback": ""})
            entry["feedback"] = (entry["feedback"] + "\n" if entry["feedback"] else "") + part
    return [by_id[k] for k in sorted(by_id)]


def aggregate(state: ReviewState) -> dict:
    """多数表决聚合:弃权不投票,显式通过票达到多数才 passed;全弃权保守通过.

    - passed = (显式通过票 >= len(REVIEW_ROLE_NAMES)//2 + 1),3 角色即 >= 2;
    - quality_score = round(有效票 score 均值),无有效票取 0;
    - failed_sections = 各失败角色问题章节按 id 合并(passed 时强制 []);
    - final_article = draft(取消润色,合格时即成品);
    - revision_count 只在此 +1,不因角色重试/并行多计.
    """
    reviews = state.get("role_reviews", {})
    valid = {rn: rv for rn, rv in reviews.items() if rv["passed"] is not None}
    if not valid:
        # 全弃权:保守通过,避免把流程卡进死循环(沿决策 #5)
        passed, score, failed = True, 0, []
    else:
        majority = len(REVIEW_ROLE_NAMES) // 2 + 1
        pass_votes = sum(1 for rv in valid.values() if rv["passed"] is True)
        passed = pass_votes >= majority
        score = round(sum(rv["score"] for rv in valid.values()) / len(valid))
        failed = [] if passed else _merge_failed_sections(valid)
    return {
        "final_article": state.get("draft", ""),
        "quality_score": score,
        "passed": passed,
        "failed_sections": failed,
        "revision_count": state.get("revision_count", 0) + 1,
    }


def build_review_agent():
    """构造审核子智能体子图.

    START →(fan_out_reviewers: [Send(...)×N] 或 "aggregate")→ review_role(并行×N) → aggregate → END
    """
    g = StateGraph(ReviewState, output_schema=ReviewOutput)
    g.add_node("review_role", review_role)
    g.add_node("aggregate", aggregate)

    g.add_conditional_edges(START, fan_out_reviewers)  # 返回 [Send(...)×N] 或 "aggregate"
    g.add_edge("review_role", "aggregate")  # 屏障:所有角色实例都写完才聚合
    g.add_edge("aggregate", END)
    return g.compile()
