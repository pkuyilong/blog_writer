"""确定性验证 Web 页面版(web_server.py)的任务状态机与 API 契约.

mock 所有 LLM(outliner.chat+cached_search / writer.call_llm / section_writer.call_llm /
review.call_llm / human_review.call_llm),不耗 token.用 FastAPI TestClient(httpx)驱动,
后台 worker 线程跑图,覆盖:
  - /api/models / 参数校验(空 topic / 未知模型 / 非法 action)
  - 全自动(human_review=false):run → 直达 done,不出现 waiting
  - 人工确认:run → waiting(大纲) → resume(confirm) → done
  - revise 二次中断:revise → 再次 waiting(重写后大纲) → confirm → done
  - replace 无二次中断:replace → 直接 done
  - 409(waiting 期间再 run / 非 waiting resume)/ 400(resume 缺参)
  - cancel:waiting 时 cancel → error=canceled
  - 异常兜底:fake LLM 抛异常 → error 状态

注意:web_server 的单槽注册表是进程级全局,每个用例开头必须 _reset_for_tests() 复位
(取消旧任务、join worker、清槽、恢复默认模型),否则会拿到上个任务的状态.
"""
import json
import os
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.outliner as O
import agents.writing as W
import agents.section_writer as SW
import agents.review as R
import agents.human_review as HR
from fastapi.testclient import TestClient

import web_server
from web_server import app

client = TestClient(app)

# 长期记忆 store 隔离:web_server 现在强制挂全局 store(懒加载 _get_store), 会把
# .store/memory.db 写进真实记忆库, 污染后续真实运行。这里把 _store_path 指向临时库;
# _reset_for_tests() 每次关闭连接并置 None, _get_store() 下次按当前 _store_path 懒重建,
# 33 项检查互不串库、不写真库。脚本末尾 rmtree 清理临时目录。
_TMP_MEMORY = tempfile.mkdtemp(prefix="bw_web_memory_")
web_server._store_path = os.path.join(_TMP_MEMORY, "memory.db")

passed = []


def check(name, cond, detail=""):
    passed.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, detail)


# ---------- mock outliner:搜索+审查+提纲(v2.3 起走 cached_search,CLAUDE.md 决策 #15) ----------
def make_msg(content="", tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def make_tc(tc_id, query):
    return SimpleNamespace(
        id=tc_id,
        function=SimpleNamespace(name="web_search", arguments=json.dumps({"query": query})),
    )


def make_resp(msg):
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


OUTLINE = (
    "## 文章提纲\n"
    "一、引言:为什么远程办公越来越普遍\n"
    "二、远程办公的历史与技术推动\n"
    "三、效率与工作生活平衡\n"
    "四、混合办公的新趋势\n"
    "五、面临的挑战\n"
    "六、结语\n\n"
    "## 关键素材\n"
    "- 远程办公兴起与数据(来源:麦肯锡)\n"
    "- 混合办公成为主流(来源:报告)"
)
REVIEW = (
    "## 素材审查结果\n### 保留\n"
    "- 远程办公数据增长(来源:麦肯锡)\n- 混合办公 3+2 模式(来源:报告)"
)


def fake_outliner_chat(system, messages, tools=None, **kw):
    if tools:
        return make_resp(make_msg("", [make_tc("t1", "远程办公 数据")]))
    if system == O.MATERIAL_REVIEW_PROMPT:
        return make_resp(make_msg(REVIEW))
    if system == O.OUTLINER_PROMPT:
        return make_resp(make_msg(OUTLINE))
    if system == O.FALLBACK_OUTLINE_PROMPT:
        return make_resp(make_msg("兜底:" + OUTLINE))
    raise AssertionError(f"未知 outliner system: {system[:30]}")


def fake_search(q):
    return f"结果:{q}"


O.chat = fake_outliner_chat
O.cached_search = fake_search
O.get_cached_materials = lambda topic: None  # topic 级缓存恒 miss,防连真库
O.store_materials = lambda topic, materials: None  # 不写真库


# ---------- mock writer.call_llm(只负责 split;写章节已下放到 section_writer) ----------
SECTIONS = {
    "sections": [
        {"title": "引言:远程办公为何兴起", "points": [], "materials": ["数据1"]},
        {"title": "效率与平衡", "points": [], "materials": ["数据2"]},
        {"title": "混合办公趋势", "points": [], "materials": ["数据3"]},
    ]
}


def fake_writer_call_llm(system, user_content, json_mode=False, **kw):
    if system == W.SPLIT_PROMPT:
        return json.dumps(SECTIONS, ensure_ascii=False)
    raise AssertionError(f"未知 writer system: {system[:30]}")


W.call_llm = fake_writer_call_llm


# ---------- mock section_writer.call_llm(每章写足够长 + ## 开头,通过启发式自检) ----------
def fake_sw_call_llm(system, user_content, json_mode=False, **kw):
    if system in (SW.WRITE_SECTION_PROMPT, SW.SELF_REVIEW_PROMPT):
        title = [l for l in user_content.splitlines() if l.startswith("【本章节】")][0]
        title = title.split("标题：", 1)[1]
        return f"## {title}\n\n(正文)" + "(内容扩充)" * 30
    raise AssertionError(f"未知 section_writer system: {system[:30]}")


SW.call_llm = fake_sw_call_llm


# ---------- mock review.call_llm(三角色一律判通过) ----------
def fake_review_call_llm(system, user_content, json_mode=False, **kw):
    return json.dumps({"score": 90, "passed": True, "failed_sections": []}, ensure_ascii=False)


R.call_llm = fake_review_call_llm


# ---------- mock human_review.call_llm(_revise_outline 用) ----------
def fake_revise(system, user_content, **kw):
    return "重写版:" + OUTLINE + "\n五、补充背景数据章节"


HR.call_llm = fake_revise


# ---------- 轮询辅助 ----------
def wait_status(want, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get("/api/status").json()
        if s["status"] == want:
            return s
        time.sleep(0.05)
    return client.get("/api/status").json()


def run_until(pred, timeout=10.0):
    """轮询 status 直到 pred(s) 为真,返回 (最终 s, 期间观察到的 status 序列).

    序列可用于断言"不再出现某状态"(如 replace 后不二次 waiting)——比只等终点
    wait_status("done") 更严格,能把中间态的变化也守护住.
    """
    seq = []
    deadline = time.time() + timeout
    s = None
    while time.time() < deadline:
        s = client.get("/api/status").json()
        seq.append(s["status"])
        if pred(s):
            return s, seq
        time.sleep(0.05)
    return s, seq


# ===== 1. /api/models 与参数校验 =====
s = client.get("/api/models").json()
check("models: 列出注册表并带默认值",
      "deepseek-v4-flash" in s["models"] and s["default"] == "deepseek-v4-flash", f"={s}")

web_server._reset_for_tests()
r = client.post("/api/run", json={"topic": "   "})
check("run: 空 topic → 400", r.status_code == 400, f"code={r.status_code}")
r = client.post("/api/run", json={"topic": "T", "model": "nope"})
check("run: 未知模型 → 400", r.status_code == 400, f"code={r.status_code}")
r = client.post("/api/resume", json={"action": "confirm"})
check("resume: 无任务时 → 409", r.status_code == 409, f"code={r.status_code}")
r = client.post("/api/cancel")
check("cancel: 无任务时 → 409", r.status_code == 409, f"code={r.status_code}")
r = client.get("/api/status")
check("status: 无任务 → idle", r.json()["status"] == "idle", f"={r.json()}")

# ===== 2. 全自动:run → 直达 done,不出现 waiting =====
web_server._reset_for_tests()
r = client.post("/api/run", json={"topic": "远程办公", "human_review": False})
check("全自动: run → 202 running", r.status_code == 202 and r.json()["status"] == "running",
      f"code={r.status_code}")
seen_waiting = False
deadline = time.time() + 10
s = None
while time.time() < deadline:
    s = client.get("/api/status").json()
    if s["status"] == "waiting":
        seen_waiting = True
    if s["status"] == "done":
        break
    time.sleep(0.05)
check("全自动: 直达 done", s is not None and s["status"] == "done", f"={s and s['status']}")
check("全自动: 全程不出现 waiting", not seen_waiting, "")
check("全自动: final_article 非空 + quality_score 存在",
      bool(s and s["final_article"]) and s and s["quality_score"] is not None, "")

# ===== 3. 人工确认:run → waiting → confirm → done =====
web_server._reset_for_tests()
client.post("/api/run", json={"topic": "远程办公", "human_review": True})
s = wait_status("waiting")
check("人工确认: 进入 waiting", s["status"] == "waiting", f"={s['status']}")
check("人工确认: outline 含大纲", bool(s["outline"]) and "提纲" in s["outline"], f"head={str(s['outline'])[:20]!r}")
r = client.post("/api/resume", json={"action": "confirm"})
check("人工确认: confirm → 202", r.status_code == 202, f"code={r.status_code}")
s = wait_status("done")
check("人工确认: confirm 后 done + 有成品", s["status"] == "done" and bool(s["final_article"]), "")

# ===== 4. revise 二次中断:revise → 再次 waiting(重写后大纲) → confirm → done =====
web_server._reset_for_tests()
client.post("/api/run", json={"topic": "远程办公", "human_review": True})
wait_status("waiting")
r = client.post("/api/resume", json={"action": "revise", "feedback": "补充背景数据"})
check("revise: → 202", r.status_code == 202, f"code={r.status_code}")
# 谓词等待:worker 异步消费 payload,若只等 wait_status("waiting") 可能拿到"提交前旧 waiting"
# (outline 还是原大纲);必须等 status==waiting 且 outline 已是重写版才算二次中断真到达.
s2, seq2 = run_until(lambda s: s["status"] == "waiting" and "重写版" in (s["outline"] or ""))
check("revise: 再次进入 waiting(二次中断)", s2["status"] == "waiting", f"seq={seq2}")
check("revise: 展示重写后大纲", bool(s2["outline"]) and "重写版" in s2["outline"], f"head={str(s2['outline'])[:20]!r}")
client.post("/api/resume", json={"action": "confirm"})
s3 = wait_status("done")
check("revise: 二次确认后 done", s3["status"] == "done" and bool(s3["final_article"]), "")

# ===== 5. replace 无二次中断:replace → 直接 done =====
web_server._reset_for_tests()
client.post("/api/run", json={"topic": "远程办公", "human_review": True})
wait_status("waiting")
r = client.post("/api/resume", json={"action": "replace", "outline": "# 用户粘贴的全新大纲\n一、A\n二、B"})
check("replace: → 202", r.status_code == 202, f"code={r.status_code}")
# 先等 worker 消费 payload 离开第一次 waiting(提交后可能残留几帧旧 waiting),再等 done,
# 断言离开后的状态序列里没有再出现 waiting——才真正守护"replace 无二次中断".
s, _ = run_until(lambda s: s["status"] != "waiting")
check("replace: 提交已生效(离开 waiting)", s["status"] != "waiting", f"={s['status']}")
s_done, seq_done = run_until(lambda s: s["status"] == "done")
check("replace: 直接 done 不二次 waiting",
      s_done["status"] == "done" and "waiting" not in seq_done, f"seq={seq_done}")

# ===== 6. 409:waiting 期间再 run / 非 waiting resume =====
web_server._reset_for_tests()
client.post("/api/run", json={"topic": "T", "human_review": True})
wait_status("waiting")
r = client.post("/api/run", json={"topic": "T2"})
check("409: waiting 期间再 run → 409", r.status_code == 409, f"code={r.status_code}")

web_server._reset_for_tests()
client.post("/api/run", json={"topic": "T", "human_review": False})
wait_status("done")
r = client.post("/api/resume", json={"action": "confirm"})
check("409: 非 waiting resume → 409", r.status_code == 409, f"code={r.status_code}")

# ===== 7. 400:resume 缺参 / 非法 action =====
web_server._reset_for_tests()
client.post("/api/run", json={"topic": "T", "human_review": True})
wait_status("waiting")
r = client.post("/api/resume", json={"action": "revise", "feedback": "   "})
check("400: revise 无 feedback → 400", r.status_code == 400, f"code={r.status_code}")
r = client.post("/api/resume", json={"action": "replace", "outline": ""})
check("400: replace 无 outline → 400", r.status_code == 400, f"code={r.status_code}")
r = client.post("/api/resume", json={"action": "bogus"})
check("400: 未知 action → 400", r.status_code == 400, f"code={r.status_code}")

# ===== 8. cancel:waiting 时取消 → error=canceled =====
web_server._reset_for_tests()
client.post("/api/run", json={"topic": "T", "human_review": True})
wait_status("waiting")
r = client.post("/api/cancel")
check("cancel: → 202", r.status_code == 202, f"code={r.status_code}")
s = wait_status("error")
check("cancel: error=canceled", s["error"] == "canceled", f"error={s['error']}")

# ===== 9. 异常兜底:fake LLM 抛异常 → error 状态 =====
def fake_bad_review(system, user_content, json_mode=False, **kw):
    raise RuntimeError("boom")


web_server._reset_for_tests()
orig_review = R.call_llm
R.call_llm = fake_bad_review
try:
    client.post("/api/run", json={"topic": "T", "human_review": False})
    s = wait_status("error", timeout=15)
    check("异常: 兜底到 error 状态", s["status"] == "error" and "RuntimeError" in (s["error"] or ""),
          f"status={s['status']} error={s['error']}")
finally:
    R.call_llm = orig_review

# ===== 10. run 端点 build_graph 失败(如缺 API key)→ 500 且槽位清空回 idle =====
web_server._reset_for_tests()
orig_build = web_server.build_graph


def bad_build(*a, **k):
    raise RuntimeError("boom")


web_server.build_graph = bad_build
try:
    r = client.post("/api/run", json={"topic": "T"})
    check("run: build_graph 失败 → 500", r.status_code == 500, f"code={r.status_code}")
    s = client.get("/api/status").json()
    check("run: 失败后槽位清空回 idle", s["status"] == "idle", f"={s['status']}")
finally:
    web_server.build_graph = orig_build
# 槽位未卡死:恢复 build 后能正常再起新任务(此时 build_graph 已还原)
r2 = client.post("/api/run", json={"topic": "T", "human_review": False})
check("run: 失败后可立即再起新任务", r2.status_code == 202, f"code={r2.status_code}")
web_server._reset_for_tests()

# ===== 11. cancel running → 409(running 阶段 invoke 原子执行不可中断) =====
web_server._reset_for_tests()
orig_worker = web_server._worker_main


def slow_worker(task, graph, config, initial_input):
    time.sleep(1)  # 让任务稳定停留在 running 态,供 cancel 断言


web_server._worker_main = slow_worker
try:
    client.post("/api/run", json={"topic": "T", "human_review": False})
    time.sleep(0.3)  # 等 worker 进入 running
    r = client.post("/api/cancel")
    check("cancel: running → 409", r.status_code == 409, f"code={r.status_code}")
finally:
    web_server._worker_main = orig_worker

web_server._reset_for_tests()
shutil.rmtree(_TMP_MEMORY, ignore_errors=True)  # 清临时记忆库(_reset 已关连接)

print()
failed = [p for p in passed if not p[1]]
print(f"共 {len(passed)} 项检查,通过 {len(passed) - len(failed)} 项")
sys.exit(1 if failed else 0)
