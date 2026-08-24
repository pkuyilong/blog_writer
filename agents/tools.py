"""联网搜索工具:用 DuckDuckGo 搜索真实资料,无需 API Key."""
from ddgs import DDGS

MAX_RESULTS = 5
REGION = "cn-zh"  # 中文区域,能让维基百科等中文权威源更靠前


def _clean_title(title: str) -> str:
    """清洗 DDGS 返回的标题.

    DuckDuckGo 在结果不足时会**把多个站点的标题拼接成一串**(如
    "A_B百度百科B - WikiwandC一篇就够了. - 知乎D架构_百度百科...").
    这类脏字符串没有意义,截断到第一个明显的拼接点,避免污染模型输入.
    """
    if not title:
        return ""
    # 截断到第一个标题分隔符(· 是常见的标题连接符,多出现一次即视为拼接)
    parts = title.split("·")
    if len(parts) > 1:
        # 保留第一段 + 还原一个分隔符,丢弃后续拼接内容
        title = parts[0] + "…"
    return title.strip()


def web_search(query: str) -> str:
    """搜索 query,返回前 MAX_RESULTS 条结果(标题+链接+摘要)的纯文本.

    网络/被限流失败时返回提示,让模型回退到自身知识,避免把流程卡死.
    """
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, region=REGION, max_results=MAX_RESULTS))
    except Exception as e:
        return f"搜索暂时不可用（{e}）。请基于自身知识作答。"

    if not results:
        return "搜索没有返回结果，请换个关键词或基于自身知识作答。"

    lines = []
    for i, r in enumerate(results[:MAX_RESULTS], 1):
        title = _clean_title(r.get("title", ""))
        if not title:
            continue  # 清洗后为空则跳过该条
        href = r.get("href", "")
        body = (r.get("body", "") or "")[:500]  # 截断摘要,避免工具结果超长
        lines.append(f"{i}. {title}\n   链接：{href}\n   摘要：{body}")
    return "\n\n".join(lines) if lines else "搜索没有返回结果，请换个关键词或基于自身知识作答。"


# 工具定义(OpenAI 兼容的 function schema),传给 chat.completions 的 tools 参数
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "在互联网上搜索给定关键词，返回相关网页的标题、链接与摘要，用于收集写文章所需的真实素材。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要搜索的中文关键词或短语",
                }
            },
            "required": ["query"],
        },
    },
}
