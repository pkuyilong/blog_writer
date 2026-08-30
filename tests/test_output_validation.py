"""确定性验证 output_validation.call_json_model 的 JSON Schema 强约束 + 校验失败反馈重试.

直接传 llm_call=fake,不耗 token 不连真实 LLM. 覆盖:
  V1 合法输出 → 返回校验通过的 model, 只调 1 次
  V2 非法 JSON → 第二次成功, 第二次 user_content 含校验反馈与"根对象"路径
  V3 缺字段 → 反馈含字段名与"缺少该字段"
  V4 类型错(score:"abc") → 反馈含"score"与"应为整数"
  V5 范围错(score:500) → 反馈含"score"与"高于允许上限"
  V6 非 dict(JSON 数组) → 反馈含"应为 json 对象"
  V7 llm_call 返回 None(content 为 None 防御) → 走 json_invalid 重试后成功
  V8 全失败 → 返回 None, 调用次数 == max_retries
  V9 retry_prefix 透传生效
  V10 SplitOutput 嵌套数组类型错 → 反馈含嵌套路径 sections.0.points
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from output_validation import ReviewOutput, SplitOutput, call_json_model

passed = []


def check(name, cond, detail=""):
    passed.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, detail)


def _make_llm(responses):
    """返回 (fake llm_call, calls): 按调用次数依次返回 responses 里的响应串(超出用最后一个).

    记录每次 user_content 供断言"第二次是否带具体校验反馈".
    """
    calls = {"n": 0, "user_contents": []}

    def fake(system, user_content, **kw):
        calls["n"] += 1
        calls["user_contents"].append(user_content)
        idx = min(calls["n"] - 1, len(responses) - 1)
        return responses[idx]

    return fake, calls


REVIEW_OK = json.dumps(
    {"score": 85, "passed": True, "failed_sections": []}, ensure_ascii=False
)

# ===== V1 合法输出 =====
fake, calls = _make_llm([REVIEW_OK])
out = call_json_model("system", "请审校", ReviewOutput, role="edit_lang", llm_call=fake)
check("V1 合法输出返回 model", out is not None and out.score == 85 and out.passed is True and out.failed_sections == [], "")
check("V1 只调 1 次", calls["n"] == 1, f"n={calls['n']}")
print()

# ===== V2 非法 JSON → 重试成功 =====
fake, calls = _make_llm(["not json", REVIEW_OK])
out = call_json_model("system", "请审校", ReviewOutput, role="edit_lang", llm_call=fake)
check("V2 非法 JSON 重试后成功", out is not None and out.score == 85, "")
check("V2 调 2 次", calls["n"] == 2, f"n={calls['n']}")
check("V2 第二次含校验反馈", "没有通过 json 结构校验" in calls["user_contents"][1], "")
check("V2 反馈含根对象路径", "根对象" in calls["user_contents"][1], "")
print()

# ===== V3 缺字段 =====
missing = json.dumps({"score": 85, "passed": True}, ensure_ascii=False)
fake, calls = _make_llm([missing, REVIEW_OK])
out = call_json_model("system", "请审校", ReviewOutput, role="edit_lang", llm_call=fake)
check("V3 缺字段重试后成功", out is not None and out.score == 85, "")
check("V3 反馈含字段名", "failed_sections" in calls["user_contents"][1], "")
check("V3 反馈含'缺少该字段'", "缺少该字段" in calls["user_contents"][1], "")
print()

# ===== V4 类型错(score 是字符串) =====
bad_type = json.dumps({"score": "abc", "passed": True, "failed_sections": []}, ensure_ascii=False)
fake, calls = _make_llm([bad_type, REVIEW_OK])
out = call_json_model("system", "请审校", ReviewOutput, role="edit_lang", llm_call=fake)
check("V4 类型错重试后成功", out is not None and out.score == 85, "")
check("V4 反馈含字段名", "score" in calls["user_contents"][1], "")
check("V4 反馈含'应为整数'", "应为整数" in calls["user_contents"][1], "")
print()

# ===== V5 范围错(score 超上限) =====
bad_range = json.dumps({"score": 500, "passed": True, "failed_sections": []}, ensure_ascii=False)
fake, calls = _make_llm([bad_range, REVIEW_OK])
out = call_json_model("system", "请审校", ReviewOutput, role="edit_lang", llm_call=fake)
check("V5 范围错重试后成功", out is not None and out.score == 85, "")
check("V5 反馈含'高于允许上限'", "高于允许上限" in calls["user_contents"][1], "")
print()

# ===== V6 非 dict(JSON 数组) =====
not_obj = json.dumps([{"score": 60}], ensure_ascii=False)
fake, calls = _make_llm([not_obj, REVIEW_OK])
out = call_json_model("system", "请审校", ReviewOutput, role="edit_lang", llm_call=fake)
check("V6 非 dict 重试后成功", out is not None and out.score == 85, "")
check("V6 反馈含'应为 json 对象'", "应为 json 对象" in calls["user_contents"][1], "")
print()

# ===== V7 llm_call 返回 None(content 为 None 防御) =====
fake, calls = _make_llm([None, REVIEW_OK])
out = call_json_model("system", "请审校", ReviewOutput, role="edit_lang", llm_call=fake)
check("V7 None 防御重试后成功", out is not None and out.score == 85, "")
check("V7 调 2 次", calls["n"] == 2, f"n={calls['n']}")
print()

# ===== V8 全失败 → 耗尽返回 None =====
fake, calls = _make_llm(["bad", "bad"])
out = call_json_model("system", "请审校", ReviewOutput, role="edit_lang", max_retries=2, llm_call=fake)
check("V8 全失败返回 None", out is None, "")
check("V8 调用次数 == max_retries", calls["n"] == 2, f"n={calls['n']}")
check("V8 最后一次不重试(无第 3 次调用)", len(calls["user_contents"]) == 2, f"n={len(calls['user_contents'])}")
print()

# ===== V9 retry_prefix 透传 =====
fake, calls = _make_llm(["bad", REVIEW_OK])
out = call_json_model(
    "system", "请审校", ReviewOutput, role="edit_lang",
    retry_prefix="请重新审校并重新输出", llm_call=fake,
)
check("V9 retry_prefix 透传", "【请重新审校并重新输出】" in calls["user_contents"][1], "")
check("V9 透传后仍成功", out is not None and out.score == 85, "")
print()

# ===== V10 SplitOutput 嵌套数组类型错 → 嵌套路径 =====
bad_split = json.dumps(
    {"sections": [{"title": "T", "points": "不是数组", "materials": []}]}, ensure_ascii=False
)
split_ok = json.dumps(
    {"sections": [{"title": "T", "points": ["P1"], "materials": ["M1"]}]}, ensure_ascii=False
)
fake, calls = _make_llm([bad_split, split_ok])
out = call_json_model("system", "请拆分", SplitOutput, role="split", llm_call=fake)
check("V10 SplitOutput 嵌套校验重试后成功", out is not None and out.sections[0].points == ["P1"], "")
check("V10 反馈含嵌套路径", "sections.0.points" in calls["user_contents"][1], "")
print()

failed = [p for p in passed if not p[1]]
print(f"共 {len(passed)} 项检查, 通过 {len(passed) - len(failed)} 项")
sys.exit(1 if failed else 0)
