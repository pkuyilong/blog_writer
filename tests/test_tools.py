"""确定性验证 agents/tools.py 的 web_search 失败重试逻辑.

mock agents.tools.DDGS(不耗网络). 覆盖:
  T1 成功: 直接返回 JSON 数组, 不重试
  T2 瞬时失败(超时) → 重试成功: 调用 2 次, sleep 1 次
  T3 瞬时失败重试耗尽 → 返回"暂时不可用", 调用 SEARCH_MAX_RETRIES+1 次
  T4 无结果(No results found) → 不重试, 直接返回"没有返回结果"
  T5 限流退避翻倍: sleep 延迟 >= 2 * RETRY_BASE_DELAY
  T6 未知异常保守重试(同 T2 形态)
  T7 空结果列表(防御分支) → 返回"没有返回结果"
  T8 重试耗尽返回文本带最后一次失败原因
  T9 失败原因分类 _failure_kind
  T10 退避计算 _retry_delay(指数/限流翻倍/封顶)
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

import agents.tools as tools

passed = []


def check(name, cond, detail=""):
    passed.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, detail)


class _FakeDDGS:
    """模拟 DDGS context manager; text() 按 plan 依次执行动作(共享 plan 列表)."""

    def __init__(self, plan, calls, results):
        self.plan = plan
        self.calls = calls
        self.results = results

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def text(self, query, **kw):
        self.calls["n"] += 1
        act = self.plan.pop(0) if isinstance(self.plan, list) else self.plan
        if act == "timeout":
            raise TimeoutException("timed out")
        if act == "ratelimit":
            raise RatelimitException("429: rate limit exceeded")
        if act == "no_results":
            raise DDGSException("No results found.")
        if act == "boom":
            raise RuntimeError("boom")
        return self.results


def _install(plan, results=None):
    """把 tools.DDGS 换成 fake, 返回 (calls, 恢复函数).

    plan 传 list 时每次 text() pop 一个动作(首失败后续成功); 传字符串则每次都相同.
    """
    calls = {"n": 0}
    results = results if results is not None else [
        {"title": "标题A · 标题B", "href": "http://example.com/a", "body": "摘要文本"}
    ]
    real = tools.DDGS

    def factory():
        return _FakeDDGS(plan, calls, results)

    tools.DDGS = factory
    return calls, lambda: setattr(tools, "DDGS", real)


# ===== T1 成功路径: 不重试 =====
calls, restore = _install("ok")
with patch("agents.tools.time") as m:
    r = tools.web_search("远程办公")
items = json.loads(r)
check("T1 成功返回 JSON 数组", isinstance(items, list) and len(items) == 1, f"r={r!r}")
check(
    "T1 字段 title/link/body 正确且标题清洗生效",
    items[0]["title"].startswith("标题A") and items[0]["title"] != "标题A · 标题B"
    and items[0]["link"] == "http://example.com/a" and items[0]["body"] == "摘要文本",
    f"items={items!r}",
)
check("T1 只调用 1 次", calls["n"] == 1, f"n={calls['n']}")
check("T1 不触发重试 sleep", m.sleep.call_count == 0, f"sleep={m.sleep.call_count}")
restore()
print()

# ===== T2 瞬时失败(超时) → 重试成功 =====
calls, restore = _install(["timeout", "ok"])
with patch("agents.tools.time") as m:
    r = tools.web_search("远程办公")
check("T2 瞬时失败重试后成功返回结果", "标题A" in r, f"r={r!r}")
check("T2 调用 2 次", calls["n"] == 2, f"n={calls['n']}")
check("T2 重试前 sleep 1 次", m.sleep.call_count == 1, f"sleep={m.sleep.call_count}")
restore()
print()

# ===== T3 瞬时失败重试耗尽 → 失败文本 =====
calls, restore = _install("timeout")  # 每次都超时
with patch("agents.tools.time") as m:
    r = tools.web_search("远程办公")
check("T3 重试耗尽返回暂时不可用", "搜索暂时不可用" in r, f"r={r!r}")
check(
    "T3 调用 SEARCH_MAX_RETRIES+1 次",
    calls["n"] == tools.SEARCH_MAX_RETRIES + 1,
    f"n={calls['n']}",
)
check("T3 sleep 与重试次数一致", m.sleep.call_count == tools.SEARCH_MAX_RETRIES, f"sleep={m.sleep.call_count}")
restore()
print()

# ===== T4 无结果不重试 =====
calls, restore = _install("no_results")
with patch("agents.tools.time") as m:
    r = tools.web_search("远程办公")
check(
    "T4 无结果直接返回提示",
    "搜索没有返回结果" in r,
    f"r={r!r}",
)
check("T4 无结果不重试(1 次)", calls["n"] == 1, f"n={calls['n']}")
check("T4 无结果不 sleep", m.sleep.call_count == 0, f"sleep={m.sleep.call_count}")
restore()
print()

# ===== T5 限流退避翻倍 =====
calls, restore = _install(["ratelimit", "ok"])
with patch("agents.tools.time") as m:
    tools.web_search("远程办公")
d = m.sleep.call_args[0][0]  # 第 1 次重试前的 sleep 延迟
check("T5 限流退避翻倍", d >= 2 * tools.RETRY_BASE_DELAY, f"delay={d:.2f}")
check("T5 限流重试后成功", calls["n"] == 2, f"n={calls['n']}")
restore()
print()

# ===== T6 未知异常保守重试 =====
calls, restore = _install(["boom", "ok"])
with patch("agents.tools.time") as m:
    r = tools.web_search("远程办公")
check("T6 未知异常保守重试成功", "标题A" in r, f"r={r!r}")
check("T6 调用 2 次", calls["n"] == 2, f"n={calls['n']}")
restore()
print()

# ===== T7 空结果列表(防御分支) =====
calls, restore = _install("ok", results=[])
with patch("agents.tools.time") as m:
    r = tools.web_search("远程办公")
check(
    "T7 空结果返回提示",
    "搜索没有返回结果" in r,
    f"r={r!r}",
)
restore()
print()

# ===== T8 失败文本带最后一次失败原因 =====
calls, restore = _install("boom")  # 每次抛 RuntimeError("boom")
with patch("agents.tools.time") as m:
    r = tools.web_search("远程办公")
check("T8 失败文本带失败原因", "boom" in r, f"r={r!r}")
restore()
print()

# ===== T9 失败原因分类 _failure_kind =====
check("T9 超时=transient", tools._failure_kind(TimeoutException("timed out")) == "transient", "")
check("T9 RatelimitException=ratelimit", tools._failure_kind(RatelimitException("429")) == "ratelimit", "")
check("T9 DDGSException 含 429 文本=ratelimit", tools._failure_kind(DDGSException("429 too many requests")) == "ratelimit", "")
check("T9 DDGSException(No results)=no_results", tools._failure_kind(DDGSException("No results found.")) == "no_results", "")
check("T9 未知异常=transient", tools._failure_kind(RuntimeError("x")) == "transient", "")
print()

# ===== T10 退避计算 _retry_delay =====
check(
    "T10 指数退避",
    tools._retry_delay(1) == 1.0 and tools._retry_delay(2) == 2.0 and tools._retry_delay(3) == 4.0,
    "",
)
check("T10 限流翻倍", tools._retry_delay(1, ratelimited=True) == 2.0, "")
check("T10 封顶 RETRY_MAX_DELAY", tools._retry_delay(3, ratelimited=True) == tools.RETRY_MAX_DELAY, "")
print()

failed = [p for p in passed if not p[1]]
print(f"共 {len(passed)} 项检查, 通过 {len(passed) - len(failed)} 项")
sys.exit(1 if failed else 0)
