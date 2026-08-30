"""确定性验证多模型路由模块(model_router.py + llm.py 委托 + 调用点 role 标记).

mock 底层(patch llm.get_client / llm.call_with_fallback),不耗 token.覆盖:
  T1 role 路由解析 / T2 哨兵→全局默认 / T3 set_default_model 覆盖
  T4 能力过滤(role 链静默跳过 / 显式 model 抛错)
  T5 fallback 链成功 / T6 fallback 耗尽抛错并保留 __cause__
  T7 各调用点 role 正确传递(审校走审核子智能体子图) / T8 旧无参行为不变
  T9 get_client 懒加载 / 缓存 / 缺 env 友好报错
"""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openai
import llm
import model_router as MR

passed = []


def check(name, cond, detail=""):
    passed.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, detail)


# ---------- 全局状态快照/恢复(每轮测试独立,避免污染) ----------
def snapshot():
    return {
        "default": MR._DEFAULT_MODEL,
        "roles": {k: list(v) for k, v in MR.ROLE_MODEL_MAP.items()},
        "registry": dict(MR.MODEL_REGISTRY),
        "cache": dict(MR._client_cache),
        "llm_cwf": llm.call_with_fallback,
        "llm_gc": llm.get_client,
        "openai": MR.OpenAI,
        "env_key": os.environ.get("DEEPSEEK_API_KEY"),
    }


def restore(s):
    MR._DEFAULT_MODEL = s["default"]
    MR.ROLE_MODEL_MAP = {k: list(v) for k, v in s["roles"].items()}
    MR.MODEL_REGISTRY = s["registry"]
    MR._client_cache = s["cache"]
    llm.call_with_fallback = s["llm_cwf"]
    llm.get_client = s["llm_gc"]
    MR.OpenAI = s["openai"]
    if s["env_key"] is not None:
        os.environ["DEEPSEEK_API_KEY"] = s["env_key"]
    else:
        os.environ.pop("DEEPSEEK_API_KEY", None)


def new_spec(name, caps=frozenset()):
    return MR.ModelSpec(
        name=name, provider="fake", model_name=name,
        base_url="https://fake.local", api_key_env="FAKE_KEY",
        capabilities=frozenset(caps),
    )


S = snapshot()

# ===== T1 role 路由解析 =====
MR.ROLE_MODEL_MAP["outline"] = ["deepseek-v4-flash"]
chain = MR.resolve_chain(role="outline")
check("T1 role 路由: 返回 1 个 spec 且名字正确",
      len(chain) == 1 and chain[0].name == "deepseek-v4-flash", f"{[c.name for c in chain]}")
try:
    MR.resolve_chain(role="bogus")
    check("T1 未知 role 抛 ModelRoutingError", False)
except MR.ModelRoutingError:
    check("T1 未知 role 抛 ModelRoutingError", True)
check("T1 ROLES 含 3 个审校角色且不含 edit",
      {"edit_lang", "edit_logic", "edit_fact"} <= MR.ROLES and "edit" not in MR.ROLES,
      f"ROLES={sorted(MR.ROLES)}")

# ===== T2 哨兵→全局默认 =====
chain = MR.resolve_chain(role="edit_lang")  # 默认 map 全哨兵
check("T2 哨兵→全局默认: role 链返回全局默认",
      [c.name for c in chain] == [MR.get_default_model()], f"{[c.name for c in chain]}")
chain2 = MR.resolve_chain(role=None, model=None)
check("T2 无参 resolve 返回全局默认", [c.name for c in chain2] == [MR.get_default_model()], "")

# ===== T3 set_default_model 覆盖 =====
MR.MODEL_REGISTRY["m2"] = new_spec("m2", caps={"json"})
old = MR.set_default_model("m2")
check("T3 set_default_model 返回旧值", old == "deepseek-v4-flash", f"old={old}")
check("T3 切换后 resolve_chain() 返回新模型",
      [c.name for c in MR.resolve_chain()] == ["m2"], "")
try:
    MR.set_default_model("不存在的")
    check("T3 未知名模型抛错", False)
except MR.ModelRoutingError:
    check("T3 未知名模型抛错", True)
try:
    MR.set_default_model(MR.DEFAULT_MODEL)  # 哨兵不能作为真实模型名
    check("T3 哨兵被拒绝", False)
except MR.ModelRoutingError:
    check("T3 哨兵被拒绝", True)
restore(S)

# ===== T4 能力过滤 =====
MR.MODEL_REGISTRY["no-json"] = new_spec("no-json")  # 无 json 能力
MR.ROLE_MODEL_MAP["split"] = ["no-json", MR.DEFAULT_MODEL]  # no-json 应被静默跳过
chain = MR.resolve_chain(role="split", required_capabilities={"json"})
check("T4 role 链能力不符静默跳过", [c.name for c in chain] == [MR.get_default_model()], f"{[c.name for c in chain]}")
try:
    MR.resolve_chain(model="no-json", required_capabilities={"json"})
    check("T4 显式 model 能力不符抛错", False)
except MR.ModelRoutingError:
    check("T4 显式 model 能力不符抛错", True)
restore(S)

# ===== T5 fallback 链成功 =====
MR.MODEL_REGISTRY["m1"] = new_spec("m1", caps={"json"})
MR.MODEL_REGISTRY["m2"] = new_spec("m2", caps={"json"})
MR.ROLE_MODEL_MAP["edit_lang"] = ["m1", "m2"]


def fake_client_result(text):
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kw: SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
                )
            )
        )
    )


def fake_get_fallback(spec):
    if spec.name == "m1":
        raise openai.OpenAIError("boom")
    return fake_client_result("第二个模型成功")


llm.get_client = fake_get_fallback
out = llm.call_llm("sys", "user", role="edit_lang")
check("T5 fallback: 主模型失败切第二个成功", out == "第二个模型成功", f"out={out!r}")
restore(S)

# ===== T6 fallback 耗尽 =====
def fake_get_dead(spec):
    raise openai.OpenAIError("一直失败")


llm.get_client = fake_get_dead
try:
    llm.call_llm("sys", "user", role="edit_lang")  # 默认 map 只 1 个模型
    check("T6 fallback 耗尽抛 ModelRoutingError", False)
except MR.ModelRoutingError as e:
    check("T6 fallback 耗尽抛 ModelRoutingError", True)
    check("T6 保留最后一次异常 __cause__", isinstance(e.__cause__, openai.OpenAIError),
          f"cause={type(e.__cause__).__name__}")
restore(S)

# ===== T7 调用点 role 正确传递(patch llm.call_with_fallback 为 spy) =====
import agents.outliner as O
import agents.writing as W
import agents.section_writer as SW
import agents.review as R
import agents.human_review as HR

calls = []
res_n = {"n": 0}


def resp(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))])


def resp_with_tools():
    """带 1 个工具调用:让 search 进入"执行搜索→审查"分支,触发第二次 chat."""
    tc = SimpleNamespace(
        id="t1",
        function=SimpleNamespace(name="web_search", arguments=json.dumps({"query": "远程办公"})),
    )
    msg = SimpleNamespace(content="", tool_calls=[tc])
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def spy(specs, func, *, role=None, **kw):
    calls.append(role)
    if role == "research":
        res_n["n"] += 1
        # 第一次(搜索意图):带 tool_calls → 走搜索+审查;第二次(审查):直接出结果
        return resp_with_tools() if res_n["n"] == 1 else resp("ok")
    if role == "outline":
        return resp("ok")  # chat 路径返回完整 response 对象
    if role == "split":
        return json.dumps({"sections": [{"title": "引言", "points": [], "materials": []}]}, ensure_ascii=False)
    if role in ("edit_lang", "edit_logic", "edit_fact"):
        return json.dumps({"score": 90, "passed": True, "failed_sections": []}, ensure_ascii=False)
    if role == "write":
        return "## 章节\n\n正文内容足够长。"
    if role == "revise_outline":
        return "重写后的大纲"
    return "罐头"


llm.call_with_fallback = spy
_real_web = O.cached_search
O.cached_search = lambda q: f"结果:{q}"  # mock,防真实联网(v2.3 起 _run_search 走 cached_search)
# topic 级缓存读写也 mock 掉(测试隔离, 不连真库; 否则命中真实缓存会改变调用次数)
_real_gc = O.get_cached_materials
O.get_cached_materials = lambda t: None
_real_gs = O.store_materials
O.store_materials = lambda t, m: None
try:
    O.search({"topic": "远程办公"})  # 2 次 chat:搜索意图 + 素材审查
    check("T7 outliner.search → research ×2", calls[-2:] == ["research", "research"], f"{calls[-2:]}")
    O.generate({"topic": "T", "materials": "素材"})
    check("T7 outliner.generate → outline", calls[-1] == "outline", f"{calls[-1:]}")
    O.fallback({"topic": "T"})
    check("T7 outliner.fallback → outline", calls[-1] == "outline", f"{calls[-1:]}")
    W.split_sections({"topic": "T", "outline": "提纲"})
    check("T7 writer.split_sections → split", calls[-1] == "split", f"{calls[-1:]}")
    SW.write({"section": {"id": 1, "title": "引言", "points": [], "materials": []}, "topic": "T"})
    check("T7 section_writer.write → write", calls[-1] == "write", f"{calls[-1:]}")
    R.build_review_agent().invoke({"draft": "文章草稿"})
    check("T7 review 子图 → edit_lang/edit_logic/edit_fact 各调一次",
          set(calls[-3:]) == {"edit_lang", "edit_logic", "edit_fact"}, f"{calls[-3:]}")
    HR._revise_outline("T", "大纲", "意见")
    check("T7 human_review._revise_outline → revise_outline", calls[-1] == "revise_outline", f"{calls[-1:]}")
finally:
    O.cached_search = _real_web
    O.get_cached_materials = _real_gc
    O.store_materials = _real_gs
restore(S)

# ===== T8 旧无参行为不变 =====
seen = []


def spy_plain(specs, func, *, role=None, **kw):
    seen.append((role, [s.name for s in specs]))
    return "x"


llm.call_with_fallback = spy_plain
llm.call_llm("sys", "user")
check("T8 无参调用: role=None、走全局默认", seen[-1] == (None, ["deepseek-v4-flash"]), f"{seen[-1]}")
restore(S)

# ===== T9 get_client 懒加载/缓存/缺 env =====
spec_ds = MR.MODEL_REGISTRY["deepseek-v4-flash"]
os.environ.pop("DEEPSEEK_API_KEY", None)
MR._client_cache.clear()
try:
    MR.get_client(spec_ds)
    check("T9 缺 env 抛 ModelRoutingError 且消息含 env 名", False)
except MR.ModelRoutingError as e:
    check("T9 缺 env 抛 ModelRoutingError 且消息含 env 名",
          "DEEPSEEK_API_KEY" in str(e), f"{str(e)[:50]}")

os.environ["DEEPSEEK_API_KEY"] = "fake-key"
count = {"n": 0}


class FakeOpenAI:
    def __init__(self, *a, **kw):
        count["n"] += 1


MR.OpenAI = FakeOpenAI
MR._client_cache.clear()
c1 = MR.get_client(spec_ds)
c2 = MR.get_client(spec_ds)
check("T9 懒加载: 相同 (base_url, env) 只构造一次 client", count["n"] == 1, f"n={count['n']}")
check("T9 缓存: 两次返回同一实例", c1 is c2, "")
restore(S)

# ===== T10 限流退避重试成功(同模型, 不切) =====
MR.MODEL_REGISTRY["m1"] = new_spec("m1", caps={"json"})


def _rl_err(retry_after=None):
    """构造真实的 openai.RateLimitError(带可选 retry-after 响应头)."""
    req = httpx.Request("POST", "https://fake.local/chat/completions")
    hdrs = {"retry-after": retry_after} if retry_after is not None else {}
    resp = httpx.Response(429, headers=hdrs, request=req)
    return openai.RateLimitError(
        "429 Too Many Requests", response=resp, body={"error": {"code": "rate_limit_error"}}
    )


def fake_rl_spec_ok(spec):
    n = getattr(fake_rl_spec_ok, "n", 0)
    fake_rl_spec_ok.n = n + 1
    if n == 0:
        raise _rl_err()
    return fake_client_result("限流退避后成功")


MR.ROLE_MODEL_MAP["edit_lang"] = ["m1"]
llm.get_client = fake_rl_spec_ok
with patch("model_router.time") as m:
    out = llm.call_llm("sys", "user", role="edit_lang")
check("T10 限流退避重试后成功", out == "限流退避后成功", f"out={out!r}")
check("T10 限流同模型调 2 次", fake_rl_spec_ok.n == 2, f"n={fake_rl_spec_ok.n}")
check("T10 限流退避 sleep 1 次", m.sleep.call_count == 1, f"sleep={m.sleep.call_count}")
restore(S)

# ===== T11 限流优先采用服务端 retry-after 头 =====
MR.MODEL_REGISTRY["m1"] = new_spec("m1", caps={"json"})


def fake_rl_ra(spec):
    n = getattr(fake_rl_ra, "n", 0)
    fake_rl_ra.n = n + 1
    if n == 0:
        raise _rl_err(retry_after="3")
    return fake_client_result("ok")


MR.ROLE_MODEL_MAP["edit_lang"] = ["m1"]
llm.get_client = fake_rl_ra
with patch("model_router.time") as m:
    llm.call_llm("sys", "user", role="edit_lang")
d = m.sleep.call_args[0][0]
check("T11 限流退避采用 retry-after 头", d == 3.0, f"delay={d}")
restore(S)

# ===== T12 瞬时(超时)退避重试成功 =====
MR.MODEL_REGISTRY["m1"] = new_spec("m1", caps={"json"})


def fake_to_ok(spec):
    n = getattr(fake_to_ok, "n", 0)
    fake_to_ok.n = n + 1
    if n == 0:
        req = httpx.Request("POST", "https://fake.local/chat/completions")
        raise openai.APITimeoutError(request=req)
    return fake_client_result("超时重试后成功")


MR.ROLE_MODEL_MAP["edit_lang"] = ["m1"]
llm.get_client = fake_to_ok
with patch("model_router.time") as m:
    out = llm.call_llm("sys", "user", role="edit_lang")
check("T12 超时退避重试后成功", out == "超时重试后成功", f"out={out!r}")
check("T12 超时调 2 次", fake_to_ok.n == 2, f"n={fake_to_ok.n}")
check("T12 超时 sleep 1 次", m.sleep.call_count == 1, f"sleep={m.sleep.call_count}")
restore(S)

# ===== T13 瞬时重试耗尽后切下一个模型, 仍失败抛错 =====
MR.MODEL_REGISTRY["m1"] = new_spec("m1", caps={"json"})
MR.MODEL_REGISTRY["m2"] = new_spec("m2", caps={"json"})


def fake_to_dead(spec):
    n = getattr(fake_to_dead, "n", 0)
    fake_to_dead.n = n + 1
    req = httpx.Request("POST", "https://fake.local/chat/completions")
    raise openai.APITimeoutError(request=req)


MR.ROLE_MODEL_MAP["edit_lang"] = ["m1", "m2"]
llm.get_client = fake_to_dead
with patch("model_router.time") as m:
    try:
        llm.call_llm("sys", "user", role="edit_lang")
        check("T13 两模型瞬时全失败抛 ModelRoutingError", False)
    except MR.ModelRoutingError:
        check("T13 两模型瞬时全失败抛 ModelRoutingError", True)
check("T13 总调用 = 2 模型 × 3 次", fake_to_dead.n == 6, f"n={fake_to_dead.n}")
check("T13 sleep = 2 模型 × 2 重试", m.sleep.call_count == 4, f"sleep={m.sleep.call_count}")
restore(S)

# ===== T14 致命错误不重试, 直接切下一个模型 =====
MR.MODEL_REGISTRY["m1"] = new_spec("m1", caps={"json"})
MR.MODEL_REGISTRY["m2"] = new_spec("m2", caps={"json"})


def fake_fatal(spec):
    n = getattr(fake_fatal, "n", 0)
    fake_fatal.n = n + 1
    if spec.name == "m1":
        req = httpx.Request("POST", "https://fake.local/chat/completions")
        resp = httpx.Response(401, request=req)
        raise openai.AuthenticationError("invalid key", response=resp, body={"error": {}})
    return fake_client_result("第二个模型成功")


MR.ROLE_MODEL_MAP["edit_lang"] = ["m1", "m2"]
llm.get_client = fake_fatal
with patch("model_router.time") as m:
    out = llm.call_llm("sys", "user", role="edit_lang")
check("T14 致命错误不重试直接切下一个", out == "第二个模型成功", f"out={out!r}")
check("T14 致命错误只调 2 次(无重试)", fake_fatal.n == 2, f"n={fake_fatal.n}")
check("T14 致命错误不 sleep", m.sleep.call_count == 0, f"sleep={m.sleep.call_count}")
restore(S)

def _ctx_err():
    """构造真实的 context_length_exceeded BadRequestError."""
    req = httpx.Request("POST", "https://fake.local/chat/completions")
    resp = httpx.Response(400, request=req)
    body = {
        "error": {
            "code": "context_length_exceeded",
            "message": "This model's maximum context length is 64000 tokens",
        }
    }
    return openai.BadRequestError("400 context length exceeded", response=resp, body=body)


def _other400():
    """构造一个无法自动编辑的普通 400 参数错误."""
    req = httpx.Request("POST", "https://fake.local/chat/completions")
    resp = httpx.Response(400, request=req)
    body = {"error": {"code": "invalid_request_error", "message": "bad param"}}
    return openai.BadRequestError("400", response=resp, body=body)


# ===== T15 失败原因分类 _classify_llm_error =====
_req = httpx.Request("POST", "https://fake.local/chat/completions")
_resp500 = httpx.Response(500, request=_req)
check("T15 RateLimitError=rate_limit", MR._classify_llm_error(_rl_err()) == "rate_limit", "")
check("T15 APITimeoutError=transient", MR._classify_llm_error(openai.APITimeoutError(request=_req)) == "transient", "")
check("T15 APIConnectionError=transient", MR._classify_llm_error(openai.APIConnectionError(request=_req)) == "transient", "")
check("T15 InternalServerError=transient", MR._classify_llm_error(openai.InternalServerError("500", response=_resp500, body={})) == "transient", "")
check("T15 认证错误=fatal", MR._classify_llm_error(openai.AuthenticationError("401", response=httpx.Response(401, request=_req), body={})) == "fatal", "")
check("T15 未知 OpenAIError=fatal", MR._classify_llm_error(openai.OpenAIError("x")) == "fatal", "")
check("T15 context_length_exceeded=context_exceeded", MR._classify_llm_error(_ctx_err()) == "context_exceeded", "")
check("T15 其他 400 参数错误=fatal", MR._classify_llm_error(_other400()) == "fatal", "")

# ===== T16 退避计算 _retry_delay(指数/限流翻倍/封顶/retry-after 优先) =====
check("T16 瞬时指数退避", MR._retry_delay(1, "transient") == 1.0 and MR._retry_delay(2, "transient") == 2.0, "")
check("T16 限流退避翻倍", MR._retry_delay(1, "rate_limit") == 2.0, "")
check("T16 封顶 RETRY_MAX_DELAY", MR._retry_delay(5, "rate_limit") == MR.RETRY_MAX_DELAY, "")
check("T16 限流 retry-after 优先", MR._retry_delay(1, "rate_limit", exc=_rl_err(retry_after="3")) == 3.0, "")
check("T16 瞬时忽略 retry-after", MR._retry_delay(1, "transient", exc=_rl_err(retry_after="3")) == 1.0, "")
print()

# ===== T17 context_length_exceeded → 按失败信息缩小 max_tokens 重试成功 =====
MR.MODEL_REGISTRY["m1"] = new_spec("m1", caps={"json"})


def fake_recording(seen):
    """记录每次 create 实际收到的 max_tokens."""
    def create(**kw):
        seen.append(kw.get("max_tokens"))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="缩小后成功"))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def fake_ctx_ok(spec):
    n = getattr(fake_ctx_ok, "n", 0)
    fake_ctx_ok.n = n + 1
    if n == 0:
        raise _ctx_err()
    return fake_recording(seen)


seen = []
MR.ROLE_MODEL_MAP["edit_lang"] = ["m1"]
llm.get_client = fake_ctx_ok
with patch("model_router.time") as m:
    out = llm.call_llm("sys", "user", role="edit_lang")  # 默认 max_tokens=16000
check("T17 context 超长缩小 max_tokens 重试成功", out == "缩小后成功", f"out={out!r}")
check("T17 同模型调 2 次", fake_ctx_ok.n == 2, f"n={fake_ctx_ok.n}")
check("T17 第二次 max_tokens 减半为 8000", seen == [8000], f"seen={seen}")
check("T17 参数编辑重试不退避", m.sleep.call_count == 0, f"sleep={m.sleep.call_count}")
restore(S)

# ===== T18 context 超长缩到最小仍超 → 切下一个模型 =====
MR.MODEL_REGISTRY["m1"] = new_spec("m1", caps={"json"})


def fake_ctx_dead(spec):
    raise _ctx_err()


adjust = {"max_tokens": 16000}
with patch("model_router.time") as m:
    try:
        MR.call_with_fallback([MR.MODEL_REGISTRY["m1"]], fake_ctx_dead, adjust=adjust)
        check("T18 缩到最小仍超抛 ModelRoutingError", False)
    except MR.ModelRoutingError:
        check("T18 缩到最小仍超抛 ModelRoutingError", True)
check("T18 缩小 2 次后 max_tokens=4000", adjust["max_tokens"] == 4000, f"adjust={adjust}")
check("T18 参数编辑重试不退避", m.sleep.call_count == 0, f"sleep={m.sleep.call_count}")
restore(S)

# ===== T19 其他 400 参数错误无法自动编辑 → fatal 直接切下一个模型 =====
MR.MODEL_REGISTRY["m1"] = new_spec("m1", caps={"json"})
MR.MODEL_REGISTRY["m2"] = new_spec("m2", caps={"json"})


def fake_other400(spec):
    n = getattr(fake_other400, "n", 0)
    fake_other400.n = n + 1
    if spec.name == "m1":
        raise _other400()
    return fake_client_result("第二个模型成功")


MR.ROLE_MODEL_MAP["edit_lang"] = ["m1", "m2"]
llm.get_client = fake_other400
with patch("model_router.time") as m:
    out = llm.call_llm("sys", "user", role="edit_lang")
check("T19 其他 400 参数错误=fatal 切下一个", out == "第二个模型成功", f"out={out!r}")
check("T19 不缩 max_tokens 只调 2 次", fake_other400.n == 2, f"n={fake_other400.n}")
restore(S)

failed = [p for p in passed if not p[1]]
print(f"共 {len(passed)} 项检查，通过 {len(passed) - len(failed)} 项")
sys.exit(1 if failed else 0)
