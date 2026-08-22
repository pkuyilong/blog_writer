from typing import TypedDict


class ArticleState(TypedDict):
    """在三个 Agent 之间共享的状态。

    线性流水线中每个字段只由一个节点写入，因此无需 reducer。
    """

    topic: str          # 用户输入的题目
    outline: str        # 调研 Agent 产出的提纲与素材
    draft: str          # 写作 Agent 产出的草稿
    final_article: str  # 审校 Agent 产出的成品
