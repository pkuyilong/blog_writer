from llm import call_llm
from prompts import EDITOR_PROMPT
from state import ArticleState


def editor_node(state: ArticleState) -> dict:
    """审校/润色节点：审校草稿并润色，写入 state["final_article"]。"""
    print("→ 审校/润色中…")
    user_content = f"请审校并润色下面这篇中文文章草稿：\n\n{state['draft']}"
    final_article = call_llm(EDITOR_PROMPT, user_content)
    return {"final_article": final_article}
