"""确定性验证搜索素材缓存(search_cache.py)两级缓存逻辑.

mock search_cache.web_search(真实搜索函数), 不耗网络. 覆盖:
  T1 query 级: 首次 miss 真实调用, 再次命中不调用
  T2 key 规范化: 首尾空白 / 大小写不敏感命中同一缓存
  T3 TTL 过期: 改 created_at 为过去 → 重新真实搜索
  T4 topic 级: miss → None; store 后 hit; 过期后重新 miss
  T5 clear() 清空两张表并返回条数
  T6 并发单飞: 8 线程同 query, 真实 web_search 调用次数 == 1
  T7 空 query / 空素材不缓存
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import search_cache as SC

passed = []


def check(name, cond, detail=""):
    passed.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, detail)


# 临时库隔离: 覆盖 DB_PATH + 重置 _conn, 避免连真实 .cache/search_cache.db
SC.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_cache.db")
SC._conn = None

real_web_search = SC.web_search
_calls = {"n": 0}
_calls_lock = threading.Lock()


def fake_search(q):
    with _calls_lock:
        _calls["n"] += 1
    return f"结果:{q}"


SC.web_search = fake_search

# ===== T1 query 级: miss → 真实调用; 命中 → 不调用 =====
SC.clear()
_calls["n"] = 0
r1 = SC.cached_search("远程办公")
check("T1 首次搜索 miss, 真实调用 1 次", _calls["n"] == 1, f"n={_calls['n']}")
check("T1 返回搜索结果", r1 == "结果:远程办公", f"r1={r1!r}")
r2 = SC.cached_search("远程办公")
check("T1 第二次命中缓存, 不调真实搜索", _calls["n"] == 1, f"n={_calls['n']}")
check("T1 命中返回同一结果", r2 == r1, "")
print()

# ===== T2 key 规范化 =====
_calls["n"] = 0
r3 = SC.cached_search("  远程办公  ")  # 首尾空白
check("T2 首尾空白规范化, 命中同一缓存", _calls["n"] == 0 and r3 == r2, f"n={_calls['n']}")
r4 = SC.cached_search("REMOTE WORK")
r5 = SC.cached_search("remote work")
check("T2 英文大小写不敏感", r4 == r5 and r4 == "结果:REMOTE WORK", f"r4={r4!r}")
print()

# ===== T3 TTL 过期 =====
_calls["n"] = 0
conn = SC._connect()
conn.execute(
    "UPDATE search_cache SET created_at = ? WHERE query = ?",
    (time.time() - SC.DEFAULT_TTL - 1, "远程办公"),
)
conn.commit()
r6 = SC.cached_search("远程办公")
check("T3 TTL 过期后重新真实搜索", _calls["n"] == 1, f"n={_calls['n']}")
check("T3 重新搜索返回新结果", r6 == "结果:远程办公", "")
print()

# ===== T4 topic 级 =====
SC.clear()
m = SC.get_cached_materials("多智能体")
check("T4 未存过 topic → None", m is None, f"m={m!r}")
SC.store_materials("多智能体", "## 素材\n- 内容够长")
m2 = SC.get_cached_materials("多智能体")
check("T4 store 后命中返回素材", m2 == "## 素材\n- 内容够长", f"m2={m2!r}")
conn.execute(
    "UPDATE topic_materials SET created_at = ? WHERE topic = ?",
    (time.time() - SC.DEFAULT_TTL - 1, "多智能体"),
)
conn.commit()
m3 = SC.get_cached_materials("多智能体")
check("T4 topic 过期后重新 miss", m3 is None, f"m3={m3!r}")
print()

# ===== T5 clear =====
SC.store_materials("话题A", "素材A")
SC.cached_search("查询B")
n1, n2 = SC.clear()
check("T5 clear 返回两张表条数", (n1, n2) == (1, 1), f"n1,n2={n1},{n2}")
check("T5 clear 后 query miss(重新搜索)", SC.cached_search("查询B") == "结果:查询B", "")
check("T5 clear 后 topic miss", SC.get_cached_materials("话题A") is None, "")
print()

# ===== T6 并发单飞: 同 query 只真实搜一次 =====
SC.clear()
_calls["n"] = 0


def worker():
    SC.cached_search("并发查询")


threads = [threading.Thread(target=worker) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("T6 8 线程同 query, 真实搜索次数 == 1", _calls["n"] == 1, f"n={_calls['n']}")
check("T6 并发后缓存已写入, 后续命中", SC.cached_search("并发查询") == "结果:并发查询", "")
print()

# ===== T7 空 key / 空素材不缓存 =====
SC.clear()
_calls["n"] = 0
r7 = SC.cached_search("   ")
check("T7 空 query 不搜索且返回空", _calls["n"] == 0 and r7 == "", f"r7={r7!r}")
SC.store_materials("话题B", "   ")
check("T7 空素材不写库", SC.get_cached_materials("话题B") is None, "")

SC.web_search = real_web_search  # 恢复真实搜索(本进程即退, 保持整洁)

print()
failed = [p for p in passed if not p[1]]
print(f"共 {len(passed)} 项检查, 通过 {len(passed) - len(failed)} 项")
sys.exit(1 if failed else 0)
