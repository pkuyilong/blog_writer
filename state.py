from typing import TypedDict


class ArticleState(TypedDict):
    """在三个 Agent 之间共享的状态。

    draft 由写作节点写入；审校节点会读 draft 并写入
    quality_score / passed / revision_feedback / final_article，
    因此循环（打回重写）时这几个字段会反复更新。
    """

    topic: str               # 用户输入的题目
    outline: str             # 调研 Agent 产出的提纲与素材
    draft: str               # 写作 Agent 产出的草稿
    final_article: str       # 审校 Agent 润色后的文章（合格时为成品）
    quality_score: int       # 审校 Agent 给出的质量分（0-100）
    passed: bool             # 审校是否通过
    revision_feedback: str   # 打回时的修改意见（空串表示无需修改）
    revision_count: int      # 已审校的次数（用于限制循环上限）
