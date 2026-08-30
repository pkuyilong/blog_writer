"""搜索素材缓存:query 级(web_search 原始结果) + topic 级(审查后素材)跨进程复用.

背景:outliner 的 search 节点每次运行都真实联网搜索 + LLM 审查, 同一题目/相似关键词
反复消耗. 本模块用 SQLite(.cache/search_cache.db)持久化两级缓存, 跨进程复用:
- search_cache 表: query -> web_search 原始结果(相似关键词跨题目共享命中)
- topic_materials 表: topic -> 审查后素材(同一题目完全跳过 搜索+审查 两个环节)

设计要点(见 CLAUDE.md 决策 #15):
- 大纲不缓存: LLM 生成内容, 缓存会失去每次运行的多样性
- 锁只在 SQLite 读写处, 不在 web_search 上(保持 outliner 的并行搜索不串行化)
- TTL 惰性失效: 读时发现过期即删即 miss, 无后台清理任务
"""
import logging
import os
import sqlite3
import threading
import time

from agents.tools import web_search

logger = logging.getLogger(__name__)

# 默认缓存库位置(被 .gitignore 忽略, 不入库); 测试可覆盖后重置 _conn 以隔离
DB_PATH = os.path.join(".cache", "search_cache.db")
# 缓存有效期: 7 天(秒). 科普素材有时效性, 网页会变
DEFAULT_TTL = 7 * 24 * 3600

_lock = threading.Lock()
# 单飞:key -> 是否有线程正在真实搜索该 query. 同 key 并发时后到线程在 Condition 上
# 等待, 真正搜索只发生一次(否则 8 个并行线程会重复搜同一 query 8 次).
_searching: dict[str, bool] = {}
_cond = threading.Condition(_lock)
_conn = None  # 懒加载单连接(check_same_thread=False + Lock 保护)


def _connect() -> sqlite3.Connection:
    """懒加载 SQLite 连接并建表(模仿 main.py 的 CHECKPOINT_DB 做法)."""
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS search_cache (
                query TEXT PRIMARY KEY,
                result TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS topic_materials (
                topic TEXT PRIMARY KEY,
                materials TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        _conn.commit()
    return _conn


def _norm(s: str) -> str:
    """缓存 key 规范化:去首尾空白 + 大小写不敏感(中文无影响, 英文关键词受益)."""
    return (s or "").strip().casefold()


def _expired(created_at: float, ttl: float) -> bool:
    return time.time() - created_at > ttl


def cached_search(query: str, *, ttl: float = DEFAULT_TTL) -> str:
    """搜索 query 并走 query 级缓存:命中未过期直接返回, 否则真实搜索后写库.

    单飞:同 key 并发时, 后到线程在 _cond 上等待, 由第一个线程真正搜索并写库,
    然后复用其结果(避免 N 个并行线程重复搜索同一 query). web_search 始终在锁外,
    保持 outliner 的多查询并行; 写库用 INSERT OR REPLACE 幂等.
    """
    key = _norm(query)
    if not key:
        return ""
    conn = _connect()
    with _cond:
        while _searching.get(key):
            _cond.wait()  # 同 key 正在搜索:等它写完缓存
        row = conn.execute(
            "SELECT result, created_at FROM search_cache WHERE query = ?", (key,)
        ).fetchone()
        if row is not None:
            result, created_at = row
            if not _expired(created_at, ttl):
                return result
            conn.execute("DELETE FROM search_cache WHERE query = ?", (key,))  # 惰性失效
            conn.commit()
        _searching[key] = True
    try:
        result = web_search(query)
    except Exception:
        # 搜索失败:清"正在搜索"标志并唤醒等待线程,不写库;异常向上抛
        with _cond:
            _searching[key] = False
            _cond.notify_all()
        raise
    # 先写库再唤醒:保证被唤醒的等待线程 re-check 时能读到结果. 否则 notify 与
    # INSERT 之间的窗口会让等待线程误判 miss 再次搜索, 单飞失效(真实并发偶发
    # 重复搜索, 由 tests/test_search_cache.py T6 复现)
    with _cond:
        conn.execute(
            "INSERT OR REPLACE INTO search_cache (query, result, created_at) VALUES (?, ?, ?)",
            (key, result, time.time()),
        )
        conn.commit()
        _searching[key] = False
        _cond.notify_all()  # 唤醒等待同 key 的线程
    return result


def get_cached_materials(topic: str, *, ttl: float = DEFAULT_TTL) -> str | None:
    """取整题审查后素材:命中未过期返回素材, 过期/未命中返回 None(由调用方走完整搜索)."""
    key = _norm(topic)
    if not key:
        return None
    conn = _connect()
    with _lock:
        row = conn.execute(
            "SELECT materials, created_at FROM topic_materials WHERE topic = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        materials, created_at = row
        if _expired(created_at, ttl):
            conn.execute("DELETE FROM topic_materials WHERE topic = ?", (key,))
            conn.commit()
            return None
        return materials


def store_materials(topic: str, materials: str) -> None:
    """写入整题审查后素材(供下次同题命中). 空 key/空素材不写."""
    key = _norm(topic)
    if not key or not (materials or "").strip():
        return
    conn = _connect()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO topic_materials (topic, materials, created_at) VALUES (?, ?, ?)",
            (key, materials, time.time()),
        )
        conn.commit()


def clear() -> tuple[int, int]:
    """清空两级缓存, 返回 (query 条数, topic 条数). 供 --clear-search-cache 使用."""
    conn = _connect()
    with _lock:
        n1 = conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0]
        n2 = conn.execute("SELECT COUNT(*) FROM topic_materials").fetchone()[0]
        conn.execute("DELETE FROM search_cache")
        conn.execute("DELETE FROM topic_materials")
        conn.commit()
        return n1, n2
