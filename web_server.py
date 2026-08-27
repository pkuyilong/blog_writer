"""Web 页面版 blog_writer:选项控制 + 人工干预(interrupt/resume) + 结果展示.

CLI 的 _interactive_invoke(main.py) 是唯一把图绑在 stdin 的地方;本模块用 FastAPI
提供等价的驱动层:后台 worker 线程跑 graph.invoke,遇到 __interrupt__ 时把大纲存进
任务状态并用 threading.Condition 挂起,前端轮询 /api/status 拿到大纲,经
POST /api/resume 提交 resume 载荷,worker 唤醒后 Command(resume=...) 续跑.
**图逻辑零改动**(agents/human_review.py 的 human_review_node 已用 LangGraph
interrupt 协议;resume 载荷契约 {action: confirm|revise|replace} 与 main.py
的 _resume_payload 同构,但 replace 用显式 action、textarea 是完整大纲原文,
不需要 CLI 的 # 前缀约定).

约束(务必遵守):
- **单任务单槽**:同一时刻只跑一个任务(用户确认"单次任务即可"),done 后状态保留
  供前端持续展示成品,新 run 才覆盖旧任务.
- **uvicorn 必须单 worker**(`uvicorn web_server:app`,禁止 --workers N):单槽注册表
  是进程级全局,多进程会把轮询打散到不同副本.
- **checkpointer 用 MemorySaver(每任务一个)**:web 单任务单进程、无跨进程续跑诉求,
  完全避开 SqliteSaver 的线程安全与 .checkpoints/ 文件锁(避免 web 与 CLI 并发写
  同一 sqlite 报 database is locked).MemorySaver 只在 worker 线程内被触碰.
- **set_default_model 只在 worker 线程开头调用一次**(单写者):模型校验放 /api/run
  端点,生效放 worker,避免与 /api/models 读取产生时序怪异.
- **worker 线程里每次 graph.invoke 会自建事件循环**:绝不把图调用放进 FastAPI 的
  async 端点(ainvoke 会让外部循环与图内部循环纠缠),本模块全用同步 def 端点.
"""
import logging
import os
import threading
import uuid
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import BaseModel

from graph import build_graph
from model_router import MODEL_REGISTRY, ModelRoutingError, get_default_model, set_default_model

logger = logging.getLogger(__name__)

app = FastAPI(title="blog_writer Web 控制台")

INDEX_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
_INTERRUPT_KEY = "__interrupt__"
_INITIAL_DEFAULT_MODEL = get_default_model()  # 模块加载时的全局默认,测试 reset 时恢复

# ---------- 单任务注册表与同步原语 ----------
# _current_task 是唯一"槽位":None 即 idle;done/error 后保留供展示,新 run 才覆盖.
_task_lock = threading.Lock()  # 保护 TaskState 与注册表的全部读写
_cond = threading.Condition(_task_lock)  # interrupt 挂起 / resume 唤醒
_current_task: "TaskState | None" = None
_current_thread: threading.Thread | None = None  # 供测试 join 用


@dataclass
class TaskState:
    """一次运行的任务状态(worker 线程与 HTTP 线程共享,锁内读写)."""

    id: str  # = uuid4().hex,同时用作 LangGraph thread_id
    status: str  # running | waiting | done | error
    topic: str
    model: str  # 实际生效模型名(已在 run 端点校验过在 MODEL_REGISTRY)
    human_review: bool
    outline: str | None = None  # 最近一次 interrupt 展示的大纲(waiting 时有意义)
    resume_payload: dict | None = None  # /api/resume 写入、worker 消费;至多一个
    cancel_requested: bool = False
    final_article: str | None = None
    quality_score: int | None = None
    revision_count: int | None = None
    error: str | None = None


# ---------- 请求体 ----------
class RunRequest(BaseModel):
    topic: str
    model: str | None = None
    human_review: bool = False


class ResumeRequest(BaseModel):
    action: str  # confirm | revise | replace
    feedback: str | None = None
    outline: str | None = None


# ---------- 运行器线程 ----------
def _worker_main(task: TaskState, graph, config: dict, initial_input: dict) -> None:
    """跑图并处理可能多次的 interrupt(CLI _interactive_invoke 的无 stdin 版).

    - revise 会触发 route_review 自环 human_review 再 interrupt 一次,所以 while True
      循环必须保留,第二次中断用新大纲覆盖 task.outline,前端自动刷新确认框.
    - resume 前**不要** graph.invoke(None, config):那是 main.py --resume 跨进程续跑
      专用;同线程循环里直接 Command(resume=...) 续跑(与 test_human_review 一致).
    """
    set_default_model(task.model)  # 全局默认单写者:worker 线程开头固定本次模型
    inp = initial_input
    try:
        while True:
            result = graph.invoke(inp, config)
            if _INTERRUPT_KEY not in result:  # 全跑完
                with _task_lock:
                    task.status = "done"
                    task.final_article = result.get("final_article")
                    task.quality_score = result.get("quality_score")
                    task.revision_count = result.get("revision_count")
                logger.info(f"✅ 任务 {task.id} 完成,质量分 {task.quality_score}")
                return
            payload = result[_INTERRUPT_KEY][0].value  # {"type","topic","outline"}
            with _task_lock:
                task.status = "waiting"
                task.outline = payload.get("outline")
                task.resume_payload = None  # 清位,/api/resume 才能写入
                # wait_for(predicate) 防 missed-wakeup:resume 在进 wait 前已 notify 也会先查 predicate 直接放行
                _cond.wait_for(lambda: task.resume_payload is not None or task.cancel_requested)
                if task.cancel_requested:
                    task.status = "error"
                    task.error = "canceled"
                    logger.info(f"⏹ 任务 {task.id} 已取消")
                    return
                resume = task.resume_payload
                task.resume_payload = None  # 原子消费(payload 置空与 status 置 running 同锁)
                task.status = "running"
            inp = Command(resume=resume)  # 锁外继续 invoke
    except Exception as e:  # 兜底:任何异常都转 error 状态,不让 worker 线程静默退出
        logger.exception(f"任务 {task.id} 运行异常")
        with _task_lock:
            task.status = "error"
            task.error = f"{type(e).__name__}: {e}"


# ---------- 路由(全同步 def 端点,FastAPI 丢线程池跑;HTTP 线程永不进入图执行) ----------
@app.get("/", response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse(INDEX_HTML)


@app.get("/api/models")
def models() -> dict:
    return {"models": sorted(MODEL_REGISTRY), "default": get_default_model()}


@app.post("/api/run", status_code=202)
def run(req: RunRequest) -> dict:
    global _current_task, _current_thread
    topic = req.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="文章题目不能为空")
    model = req.model or get_default_model()
    if model not in MODEL_REGISTRY:
        raise HTTPException(
            status_code=400, detail=f"未知模型 {model!r}(可用: {sorted(MODEL_REGISTRY)})"
        )
    with _task_lock:
        if _current_task is not None and _current_task.status in ("running", "waiting"):
            raise HTTPException(status_code=409, detail="已有任务在运行/等待人工确认")
        task = TaskState(id=uuid.uuid4().hex, status="running", topic=topic, model=model,
                         human_review=req.human_review)
        _current_task = task
    # 锁外构建图并启动 worker:图构建与 invoke 都是分钟级阻塞,绝不进请求线程
    # 构建失败(如缺 API key)会抛异常,必须清槽回 idle,否则槽位被卡死的 running 任务占用
    try:
        checkpointer = MemorySaver()
        graph = build_graph(enable_human_review=req.human_review, checkpointer=checkpointer)
        config = {"configurable": {"thread_id": task.id}}
        # 与 main.py:99-105 的 initial_input 完全一致
        initial_input = {
            "topic": topic,
            "sections": [],
            "section_drafts": {},
            "failed_sections": [],
            "revision_count": 0,
        }
        thread = threading.Thread(target=_worker_main, args=(task, graph, config, initial_input), daemon=True)
        with _task_lock:
            _current_thread = thread
        thread.start()
    except Exception as e:
        logger.exception(f"任务 {task.id} 构建图失败,清槽回 idle")
        with _task_lock:
            _current_task = None
            _current_thread = None
        raise HTTPException(status_code=500, detail=f"任务启动失败: {type(e).__name__}: {e}")
    logger.info(f"🚀 启动任务 {task.id} 题目《{topic}》model={model} human_review={req.human_review}")
    return {"task_id": task.id, "status": "running"}


@app.get("/api/status")
def status() -> dict:
    with _task_lock:
        task = _current_task
        if task is None:
            return {"task_id": None, "status": "idle", "topic": None, "model": None,
                    "human_review": None, "outline": None, "final_article": None,
                    "quality_score": None, "revision_count": None, "error": None}
        return {
            "task_id": task.id,
            "status": task.status,
            "topic": task.topic,
            "model": task.model,
            "human_review": task.human_review,
            "outline": task.outline,
            "final_article": task.final_article,
            "quality_score": task.quality_score,
            "revision_count": task.revision_count,
            "error": task.error,
        }


@app.post("/api/resume", status_code=202)
def resume(req: ResumeRequest) -> dict:
    with _task_lock:
        task = _current_task
        if task is None or task.status != "waiting":
            raise HTTPException(status_code=409, detail="当前没有等待人工确认的任务")
        if task.resume_payload is not None:
            raise HTTPException(status_code=409, detail="上一次 resume 尚未被消费(请勿重复提交)")
        action = req.action
        if action == "confirm":
            payload = {"action": "confirm"}
        elif action == "revise":
            if not (req.feedback or "").strip():
                raise HTTPException(status_code=400, detail="revise 需要非空 feedback")
            payload = {"action": "revise", "feedback": req.feedback}
        elif action == "replace":
            if not (req.outline or "").strip():
                raise HTTPException(status_code=400, detail="replace 需要非空 outline")
            payload = {"action": "replace", "outline": req.outline}
        else:
            raise HTTPException(
                status_code=400, detail=f"未知 action {action!r}(可用: confirm/revise/replace)"
            )
        # 写 payload 与 notify 在锁内原子完成:worker 消费后才允许下一次 resume(双条件 409)
        task.resume_payload = payload
        _cond.notify()
    return {"status": "running"}


@app.post("/api/cancel", status_code=202)
def cancel() -> dict:
    """取消任务.仅 waiting(挂起在 interrupt 上)可取消:
    running 阶段 graph.invoke 是原子执行、无法中途打断,取消会静默无效,
    前端因此只在 waiting 状态显示取消按钮.
    """
    with _task_lock:
        task = _current_task
        if task is None:
            raise HTTPException(status_code=409, detail="当前没有任务")
        if task.status != "waiting":
            raise HTTPException(status_code=409, detail="仅等待人工确认时可取消(running 无法中断)")
        task.cancel_requested = True
        _cond.notify()
    return {"status": "canceling"}


# ---------- 测试 teardown 辅助 ----------
def _reset_for_tests() -> None:
    """测试用:cancel 当前任务、join worker 线程、清槽、恢复默认模型.

    单槽注册表是进程级全局,必须在用例间复位,否则后续用例拿到上一个任务的状态.
    """
    global _current_task, _current_thread
    with _task_lock:
        task = _current_task
        thread = _current_thread
        if task is not None and task.status in ("running", "waiting"):
            task.cancel_requested = True
            _cond.notify()
    if thread is not None:
        thread.join(timeout=5)
    try:
        set_default_model(_INITIAL_DEFAULT_MODEL)  # 恢复测试期间可能被 worker 改掉的全局默认
    except ModelRoutingError:
        pass
    with _task_lock:
        _current_task = None
        _current_thread = None
