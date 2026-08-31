"""共享的指数退避延时:LLM 重试(model_router)与搜索重试(agents/tools)共用同一公式.

两处此前各自实现一份相同的指数退避(基数 * 2^(attempt-1), 限流翻倍, 封顶),
公式与常量集中到此, 避免策略漂移. model_router 额外读服务端 retry-after 头,
那是 provider 特有逻辑, 留在 model_router.
"""

# 指数退避基数(秒); 限流类失败退避翻倍; 单次等待封顶, 避免拖慢整轮.
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 4.0
# 限流比普通瞬时错误需要更长退避的倍率.
RATE_LIMIT_FACTOR = 2.0


def backoff_delay(
    attempt: int,
    *,
    ratelimited: bool = False,
    base: float = RETRY_BASE_DELAY,
    max_delay: float = RETRY_MAX_DELAY,
) -> float:
    """第 attempt 次重试前的等待秒数: 指数退避 base * 2^(attempt-1).

    ratelimited=True 时额外乘 RATE_LIMIT_FACTOR, 结果封顶到 max_delay,
    防止无限拉长. 调用方可在返回值上叠加 jitter.
    """
    delay = base * (2 ** (attempt - 1))
    if ratelimited:
        delay *= RATE_LIMIT_FACTOR
    return min(delay, max_delay)
