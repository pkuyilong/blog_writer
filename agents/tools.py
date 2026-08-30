"""联网搜索工具:用 DuckDuckGo 搜索真实资料,无需 API Key.

返回 JSON 格式:成功返回 JSON 数组 [{title, link, body}, ...] 给模型审查
(结构清晰);失败/无结果返回 {"error": "..."}——保留"搜索暂时不可用"/"没有
返回结果"等关键词,使 outliner 的 FAILURE_MARKERS 仍能识别素材不足.

失败重试:单次搜索因瞬时故障(超时/限流/网络抖动)失败时, 获取失败原因并按
原因调整退避时间重试同一 query, 避免一次瞬时故障就把整轮搜索降级成"自身
知识兜底"(DDG 免费接口限流很常见, 退避后重试大概率能成功).
"""
import json
import logging
import time

from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

logger = logging.getLogger(__name__)

MAX_RESULTS = 5
REGION = "cn-zh"  # 中文区域,能让维基百科等中文权威源更靠前

# 重试策略:初次调用失败后再重试 SEARCH_MAX_RETRIES 次
SEARCH_MAX_RETRIES = 2
# 指数退避基数(秒);限流类失败退避翻倍;单次等待封顶,避免拖慢整轮并行搜索
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 4.0


def _failure_kind(exc: Exception) -> str:
    """按失败原因分类, 返回 transient(可重试)/ratelimit(限流, 退避更久)/no_results(重试无意义).

    依赖 ddgs 库的异常形态:
    - 超时: 抛 TimeoutException, 或 message 含 timeout/timed out
    - 限流: 可能抛 RatelimitException, 但更常被 _search_sync 吞进 err 后包成
      DDGSException(message 含 429/rate limit), 所以类型与文本都要看
    - 无结果: DDGSException("No results found.") 不是故障, 换关键词才有意义, 重试浪费
    - 其他: 保守归 transient, 重试一次无妨
    """
    msg = str(exc)
    lowered = msg.casefold()
    if isinstance(exc, TimeoutException) or "timed out" in lowered or "timeout" in lowered:
        return "transient"
    if (
        isinstance(exc, RatelimitException)
        or "429" in msg
        or "ratelimit" in lowered
        or "rate limit" in lowered
    ):
        return "ratelimit"
    if isinstance(exc, DDGSException) and "no results" in lowered:
        return "no_results"
    return "transient"


def _retry_delay(attempt: int, *, ratelimited: bool = False) -> float:
    """第 attempt 次重试前的等待秒数: 指数退避, 限流翻倍, 封顶 RETRY_MAX_DELAY."""
    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
    if ratelimited:
        delay *= 2
    return min(delay, RETRY_MAX_DELAY)


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
    """搜索 query,返回前 MAX_RESULTS 条结果的 JSON 字符串.

    成功返回 JSON 数组 [{title, link, body}, ...](title 清洗、body 截断 500 字);
    失败/无结果返回 {"error": "..."}——保留"搜索暂时不可用"/"没有返回结果"
    关键词,使 outliner 的 FAILURE_MARKERS 仍能识别素材不足.

    失败重试:瞬时故障(超时/限流/网络抖动)按失败原因调整退避时间重试同一
    query SEARCH_MAX_RETRIES 次;"无结果"不是故障, 不重试(换关键词才有意义).
    重试耗尽返回失败提示, 让模型回退到自身知识, 避免把流程卡死.
    """
    results = None
    for attempt in range(SEARCH_MAX_RETRIES + 1):
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, region=REGION, max_results=MAX_RESULTS))
            break  # 成功, 退出重试循环
        except Exception as e:
            kind = _failure_kind(e)
            if kind == "no_results":
                return json.dumps(
                    {"error": "搜索没有返回结果，请换个关键词或基于自身知识作答。"},
                    ensure_ascii=False,
                )
            if attempt >= SEARCH_MAX_RETRIES:
                logger.error(
                    f"  ✗ 搜索失败（第 {attempt + 1} 次仍失败）: {type(e).__name__}: {e}"
                )
                return json.dumps(
                    {"error": f"搜索暂时不可用（{e}）。请基于自身知识作答。"},
                    ensure_ascii=False,
                )
            delay = _retry_delay(attempt + 1, ratelimited=(kind == "ratelimit"))
            # 确定性 jitter: 让不同 query 的并发重试错开, 避免整轮同时打 DDG
            # (同 query 并发由 search_cache 单飞保证只搜一次, 不依赖此 jitter)
            delay += (hash(query) % 10) / 10
            logger.warning(
                f"  ⚠ 搜索失败（第 {attempt + 1} 次）: {type(e).__name__}: {e}, "
                f"{delay:.1f}s 后重试…"
            )
            time.sleep(delay)

    if not results:
        return json.dumps(
            {"error": "搜索没有返回结果，请换个关键词或基于自身知识作答。"},
            ensure_ascii=False,
        )

    items = []
    for r in results[:MAX_RESULTS]:
        title = _clean_title(r.get("title", ""))
        if not title:
            continue  # 清洗后为空则跳过该条
        items.append(
            {
                "title": title,
                "link": r.get("href", ""),
                "body": (r.get("body", "") or "")[:500],  # 截断摘要,避免工具结果超长
            }
        )
    if not items:
        return json.dumps(
            {"error": "搜索没有返回结果，请换个关键词或基于自身知识作答。"},
            ensure_ascii=False,
        )
    return json.dumps(items, ensure_ascii=False)


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
