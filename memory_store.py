"""长期记忆:基于 LangGraph 官方 SqliteStore 的跨任务持久化(偏好 + 历史写作记录).

背景:项目已有两层"记忆"--search_cache.py(事实素材缓存, 省搜索成本)与 SqliteSaver
checkpointer(执行状态断点续跑). 缺第三层 **agent 行为层记忆**: 每次运行 agent 从零
开始, 不知道用户偏好、不积累历史写作知识. 本模块用 langgraph.store.sqlite.SqliteStore
补上这一层, 提供:
- 用户偏好(prefs): namespace ("prefs",), key "default" -- 影响写作行为(注入 prompts)
- 历史写作记录(topics): namespace ("topics",), key=topic -- 每篇题目终局写回, 跨任务复用

设计要点(实测验证, 详见 CLAUDE.md 决策 #20):
- **嵌套子图节点能访问 store**: 父图 compile(store=...) 传入的 store 经 config/runtime
  一路下传, 任意深度子图节点用 (state, config, *, store) 签名即可拿到.
- **连接必须自己造**: from_conn_string() 拿不到连接, 无法设 PRAGMA journal_mode=WAL
  (WAL 缓解 CLI 与 web 并发写同一库的 database is locked). 连接必须 isolation_level=None
  (autocommit), 否则 SqliteStore 内部 BEGIN 报 "cannot start a transaction within a transaction".
- **TTL 陷阱**: SqliteStore 的 put(ttl=...) 单位是分钟, 且 per-put ttl 实际不生效
  (get/search 不过滤过期行, TTL sweeper 需 store 级 ttl_config 才启动). 本模块**不使用
  TTL**, 记忆长期保留; 需要"记忆过期"语义时再引入 store 级 ttl config + start_ttl_sweeper.
- **节点注解约束**: store 参数注解必须是真实类型 `store: BaseStore | None`(非 future import
  的字符串), 否则 langgraph 的注入匹配不到、节点缺参报 TypeError.
"""
import logging
import os
import sqlite3
from contextlib import contextmanager

from langgraph.store.base import BaseStore
from langgraph.store.sqlite import SqliteStore

logger = logging.getLogger(__name__)

# 默认记忆库位置(与 .cache/、.checkpoints/ 并列, 被 .gitignore 忽略); 测试可覆盖后删库隔离
DB_PATH = os.path.join(".store", "memory.db")

# namespace 约定
PREFS_NS = ("prefs",)  # 用户偏好, key 固定 "default"
TOPICS_NS = ("topics",)  # 历史写作记录, key = 题目

_PREFS_KEY = "default"


@contextmanager
def open_store(path: str = DB_PATH):
    """打开记忆库, 返回 SqliteStore 实例(跨任务持久化).

    context manager: 用 with 解包, 退出时关闭连接. 自建连接以便设置
    journal_mode=WAL(缓解跨进程写同一库的 database is locked)与
    isolation_level=None(SqliteStore 内部自管事务的硬要求, 见模块 docstring).
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield SqliteStore(conn)
    finally:
        conn.close()


# 进程级 store 单例:get_store 懒加载同一实例, close_store 关闭连接供测试重置.
# web_server 常驻进程用单例复用同一 sqlite 连接; main.py 仍走 with open_store()(一次性,
# 不共享单例). 上下文生命周期必须留在模块级: 若 open_store(path).__enter__() 拿到的
# context manager 被 GC, 生成器的 finally: conn.close() 立即执行, store 连接被关
# (实测踩坑, 见 CLAUDE.md 决策 #20).
_singleton = None
_singleton_cm = None


def get_store(path: str = DB_PATH) -> BaseStore:
    """进程级懒加载单例 SqliteStore(供 web_server 等常驻进程复用同一连接)."""
    global _singleton, _singleton_cm
    if _singleton is None:
        _singleton_cm = open_store(path)
        _singleton = _singleton_cm.__enter__()
    return _singleton


def close_store() -> None:
    """关闭并重置单例(测试 teardown 用); 下次 get_store() 按当前 path 懒重建."""
    global _singleton, _singleton_cm
    if _singleton_cm is not None:
        _singleton_cm.__exit__(None, None, None)  # 触发 open_store 的 finally: conn.close()
        _singleton_cm = None
    _singleton = None


def parse_prefs_arg(raw: str) -> dict:
    """解析 --prefs 参数为 dict: "风格:轻松口语,篇幅:3000字" -> {"风格": "轻松口语", ...}.

    兼容中英文冒号/逗号; 空段(无冒号)跳过; 全空返回空 dict.
    """
    prefs = {}
    for part in (raw or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "：" in part:
            k, v = part.split("：", 1)
        elif ":" in part:
            k, v = part.split(":", 1)
        else:
            continue
        k, v = k.strip(), v.strip()
        if k:
            prefs[k] = v
    return prefs


def save_prefs(store: BaseStore | None, prefs: dict) -> None:
    """写入/覆盖用户偏好(幂等). store 为 None 时跳过(无记忆模式)."""
    if store is None:
        return
    if prefs:
        store.put(PREFS_NS, _PREFS_KEY, prefs)
        logger.info(f"🧠 已写入用户偏好: {prefs}")


def _fmt_prefs(prefs: dict) -> str:
    """偏好 dict → 注入/显示文本(中文冒号, 分号分隔). load_prefs 与 dump_memory 共用."""
    return "；".join(f"{k}：{v}" for k, v in prefs.items())


def load_prefs(store: BaseStore | None) -> str | None:
    """读用户偏好, 格式化为注入文本; 无记录/store 为 None 返回 None(不注入)."""
    if store is None:
        return None
    item = store.get(PREFS_NS, _PREFS_KEY)
    if item is None:
        return None
    return _fmt_prefs(item.value)


def prefs_block(store: BaseStore | None) -> str:
    """返回【写作偏好】注入块(含标签), 无偏好/store 为 None 时返回空串.

    供 outliner.generate / section_writer.write 等各 agent 拼进 user_content,
    标签文案与拼接逻辑只此一份.
    """
    prefs = load_prefs(store)
    if not prefs:
        return ""
    return f"\n\n【写作偏好（来自长期记忆）】{prefs}"


def _fmt_topic_record(v: dict) -> str:
    """单条题目记录的质量信号片段, load_topic_history 与 dump_memory 共用."""
    return (
        f"质量分 {v.get('quality_score', '?')}/100，"
        f"审校 {v.get('revision_count', '?')} 次，"
        f"通过 {'是' if v.get('passed') else '否'}"
    )


def load_topic_history(store: BaseStore | None, topic: str) -> str | None:
    """读某题目的历史写作记录, 格式化为提示文本; 无记录/store 为 None 返回 None."""
    if store is None or not topic:
        return None
    item = store.get(TOPICS_NS, topic)
    if item is None:
        return None
    return f"上次写作此主题：{_fmt_topic_record(item.value)}。请参考上次的经验把这篇写得更好。"


def save_topic_result(store: BaseStore | None, state: dict) -> None:
    """终局写回长期记忆:抽取 state 的质量信号, 以 topic 为 key 写入 TOPICS_NS.

    字段抽取与写入集中于此(graph.remember 只调本函数), upsert 幂等覆盖同 topic 旧记录;
    store 为 None 或 topic 为空时跳过(无记忆模式 / 异常态). 记录以 key 为唯一题目来源,
    value 不重复存 topic.
    """
    if store is None:
        return
    topic = state.get("topic", "")
    if not topic:
        return
    store.put(
        TOPICS_NS,
        topic,
        {
            "quality_score": state.get("quality_score"),
            "passed": bool(state.get("passed")),
            "revision_count": state.get("revision_count", 0),
            "draft_tail": (state.get("final_article") or "")[:200],
        },
    )
    logger.info(f"  🧠 已写入长期记忆: 《{topic}》质量分 {state.get('quality_score')}/100")


def dump_memory(store: BaseStore | None) -> str:
    """打印 store 全部记忆内容(--show-memory 用): 用户偏好 + 历史写作记录."""
    if store is None:
        return "（未启用长期记忆：build_graph 未传入 store）"
    lines = ["🧠 长期记忆（LangGraph SqliteStore 内容）", "=" * 30]
    prefs = load_prefs(store)
    if prefs:
        lines.append("【用户偏好】")
        lines.append(prefs)
    else:
        lines.append("【用户偏好】未设置（可用 --prefs \"风格:轻松口语\" 写入）")
    items = store.search(TOPICS_NS, limit=20)
    lines.append("")
    lines.append(f"【历史写作记录】共 {len(items)} 条")
    for it in items:
        line = f"- 《{it.key}》：{_fmt_topic_record(it.value)}"
        tail = (it.value.get("draft_tail") or "").strip()  # 文末片段, 展示成品开头, 供人工快速回看
        if tail:
            line += f"\n    文末：{tail}"
        lines.append(line)
    return "\n".join(lines)


def clear(path: str = DB_PATH) -> int:
    """清空记忆库(删主库 + WAL/SHM 附属文件), 返回删除的文件数. 供 --clear-memory."""
    removed = 0
    for p in (path, path + "-wal", path + "-shm"):
        try:
            if os.path.exists(p):
                os.remove(p)
                removed += 1
        except OSError as e:
            logger.warning(f"删除记忆库文件失败: {p} ({e})")
    if removed:
        logger.info(f"🧹 已清空长期记忆库: {path}")
    return removed
