from llm import call_llm
from prompts import RESEARCHER_PROMPT
from state import ArticleState


def researcher_node(state: ArticleState) -> dict:
    """调研/选题节点：根据题目产出文章提纲与关键素材，写入 state["outline"]。"""
    print("→ 调研/选题中…")
    outline = call_llm(RESEARCHER_PROMPT, state["topic"])
    return {"outline": outline}
