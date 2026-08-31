from typing import Annotated, TypedDict


def _merge_dicts(a: dict, b: dict) -> dict:
    """reducer:合并两个 dict(后写覆盖同 key).用于聚合并行章节草稿."""
    return {**a, **b}


def initial_state(topic: str) -> dict:
    """构造图初始输入:题目 + 空中间产物. main.py 与 web_server.py 共用同一份."""
    return {
        "topic": topic,
        "sections": [],
        "section_drafts": {},
        "failed_sections": [],
        "revision_count": 0,
    }


class Section(TypedDict):
    """split 拆分出的单个章节结构.id 由 split_sections 程序补(enumerate 编号)."""
    id: int
    title: str
    points: list[str]
    materials: list[str]


class ArticleState(TypedDict):
    """在多个 Agent 之间共享的状态.

    draft 由 merge 节点写入;审核子智能体(agents/review.py)会读 draft 并写入
    quality_score / passed / final_article / failed_sections,
    因此循环(打回重写)时这几个字段会反复更新.

    sections / section_drafts / failed_sections 是"按章节并发写作"的中间产物:
    - sections       由 split 节点产出(结构化章节列表)
    - section_drafts 由 write_section 并行写入,reducer 聚合(重写覆盖同 id)
    - failed_sections 由审核子智能体指出问题章节(含 id 与专属修改意见),供打回时只重写这些章节
    """

    topic: str                          # 用户输入的题目
    outline: str                        # 大纲子智能体产出的可用提纲
    outline_review_feedback: str | None         # 人工修改意见;human_review 收到后先重写大纲再二次确认(HITL 自环)
    sections: list[Section]             # split 拆出的章节 [{title, points, materials}]
    section_drafts: Annotated[dict[str, str], _merge_dicts]   # 章节id → 草稿
    failed_sections: list[dict]         # 打回时需重写的章节 [{id, feedback}](审核子智能体写入)
    draft: str                          # merge 拼装后的全文章稿
    final_article: str                  # 审核子智能体写出,恒等于当前 draft(取消润色,合格时即成品)
    quality_score: int                  # 审核子智能体的质量分:各有效角色 score 均值(0-100)
    passed: bool                        # 审核子智能体的多数表决结果(是否通过)
    revision_count: int                 # 已审校的次数(用于限制循环上限)
