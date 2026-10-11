"""
API Routes
FastAPI router with consultation, health, and stats endpoints.
Includes timeout handling, concurrency control, and request logging.
"""

import json
import sys
import time
import asyncio
from pathlib import Path
from datetime import datetime

_project_root = Path(__file__).parent.parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.common.observe import trace_span, get_callbacks
from src.common import answer_cache

from src.auth.deps import User, get_current_user
from src.agents.checkpointer import build_thread_id
from src.common.logger import setup_logger
from src.history.store import (
    ConversationNotFoundError,
    ConversationOwnershipError,
    history_store,
)
from src.api.models import (
    ConsultationRequest,
    ConsultationResponse,
    HealthResponse,
    StatsResponse,
    ErrorResponse,
    ConversationListResponse,
    ConversationDetailResponse,
    DeleteConversationResponse
)

logger = setup_logger(__name__, "api.log")

router = APIRouter()

# Timeout configuration
# 安全上限：典型轮次 2-8s；带记忆+多 Agent+LLM 兜底的轮次偶有更慢，故放宽上限。
# 真正的体验优化应走流式输出（见 §5.4 性能优化），此处仅为防失控的天花板。
REQUEST_TIMEOUT = 60  # seconds


# ============================================================
# Request Logging Helper
# ============================================================

def log_request(
    query: str,
    intent: str,
    duration_ms: int,
    has_disclaimer: bool,
    success: bool
):
    """Log request details for monitoring"""
    status = "✅" if success else "❌"
    logger.info(
        f"[REQUEST] {status} "
        f"query='{query[:30]}...' | "
        f"intent={intent} | "
        f"duration={duration_ms}ms | "
        f"disclaimer={'Y' if has_disclaimer else 'N'}"
    )


# ============================================================
# POST /api/consult - Consultation Endpoint
# ============================================================

@router.post(
    "/consult",
    response_model=ConsultationResponse,
    responses={
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
        504: {"model": ErrorResponse}
    },
    tags=["问诊"],
    summary="智慧问诊接口",
    description="接收用户问题，返回 AI 生成的医疗建议（需登录；60秒超时，5并发限制）"
)
async def consult(req: ConsultationRequest, current_user: User = Depends(get_current_user)):
    """
    Process user consultation request

    - Accepts user question
    - Runs through Agent system with timeout and concurrency control
    - Returns medical advice with disclaimers
    - Requires login; conversation is bound to the current user
    """
    from src.api.main import app_state

    # Check if agent system is ready
    if not app_state.agent_system_ready:
        raise HTTPException(
            status_code=503,
            detail="Agent system not ready. Please try again later."
        )

    start_time = time.time()
    app_state.total_requests += 1
    request_id = uuid.uuid4().hex[:8]

    logger.info(
        f"[API] Consultation request: '{req.query[:50]}...' "
        f"(session: {req.session_id}, user: {current_user.username}, rid: {request_id})"
    )

    # 生产 tracing：callbacks 透传 LangGraph（Langfuse 模式补内层调用树；
    # LangSmith 模式靠 env 自动 trace，返回空列表）；env 未配置时整体 no-op 零开销
    callbacks = get_callbacks()

    try:
      # 请求级 span：异常穿越 with 时也会 flush（trace_span 内部 try/finally）
      with trace_span(
          "consult", request_id=request_id, session_id=req.session_id,
          user_id=current_user.id, query=req.query[:200],
      ) as span:
        # Acquire semaphore for concurrency control
        async with app_state.request_semaphore:
            # Call Agent system with timeout
            from src.agents.graph import run

            # 加载多轮上下文（先归属裁决，再三级命中：Redis 热缓存 → MySQL 回填）
            try:
                history = history_store.load_context(req.session_id, current_user.id)
            except ConversationOwnershipError:
                # 越权访问他人会话：统一 404，不泄露会话存在性
                raise HTTPException(status_code=404, detail="Conversation not found")

            # 答案缓存（三分域，全用户隔离），命中即直返（跳过 Agent 全链路）：
            # Cacheability Rule 按域路由——上下文依赖追问走 L1 会话域
            # （su:{user}:{session}，同会话内复用）；自包含问题走 L1 用户精确
            # → L2 用户语义（同一用户跨会话共享）。所有键含 user_id，绝不跨用户
            cache_hit = answer_cache.lookup(
                req.query, user_id=current_user.id, session_id=req.session_id
            )
            if cache_hit:
                level, payload = cache_hit
                duration_ms = int((time.time() - start_time) * 1000)
                response = ConsultationResponse.model_validate({
                    **payload,
                    "cached": True,
                    "duration_ms": duration_ms,
                    "timestamp": datetime.now().isoformat(),
                })
                log_request(
                    query=req.query,
                    intent=response.intent,
                    duration_ms=duration_ms,
                    has_disclaimer=len(response.disclaimers) > 0,
                    success=True
                )
                logger.info(f"[API] Answer cache hit {level} (rid: {request_id})")
                # 命中也要落对话历史，否则下一轮上下文断裂
                try:
                    history_store.persist_turn(
                        req.session_id, "user", req.query, user_id=current_user.id
                    )
                    history_store.persist_turn(
                        req.session_id, "assistant", response.answer,
                        intent=response.intent, user_id=current_user.id
                    )
                except (ConversationNotFoundError, ConversationOwnershipError) as e:
                    logger.warning(f"[API] Skip persist due to ownership conflict: {e}")
                span.update(
                    intent=response.intent, duration_ms=duration_ms,
                    cache=level, ok=True,
                )
                return response

            try:
                # Run in thread pool to not block event loop
                # thread_id 含 user_id：执行态按用户隔离（契约见 src/agents/checkpointer.py）
                result = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, run, req.query, history, callbacks,
                        build_thread_id(current_user.id, req.session_id),
                    ),
                    timeout=REQUEST_TIMEOUT
                )

                duration_ms = int((time.time() - start_time) * 1000)

                # Build response
                disclaimers = result.get("disclaimers", [])
                response = ConsultationResponse(
                    answer=result.get("final_answer", ""),
                    intent=result.get("intent", ""),
                    symptoms=result.get("symptoms", []),
                    departments=result.get("departments", []),
                    medications=result.get("medications", []),
                    disclaimers=disclaimers,
                    warnings=result.get("warnings", []),
                    linked_entities=result.get("linked_entities", []),
                    duration_ms=duration_ms
                )

                # Log request
                log_request(
                    query=req.query,
                    intent=response.intent,
                    duration_ms=duration_ms,
                    has_disclaimer=len(disclaimers) > 0,
                    success=True
                )

                # 双写对话历史（先 MySQL 再 Redis；任一失败降级，不使请求失败）。
                # 首条消息落库时惰性创建会话并绑定当前用户（归属冲突仅记日志降级）。
                try:
                    history_store.persist_turn(
                        req.session_id, "user", req.query, user_id=current_user.id
                    )
                    history_store.persist_turn(
                        req.session_id, "assistant", response.answer,
                        intent=response.intent, user_id=current_user.id
                    )
                except (ConversationNotFoundError, ConversationOwnershipError) as e:
                    logger.warning(f"[API] Skip persist due to ownership conflict: {e}")

                # 写缓存（后台线程异步，不占请求延迟）：
                # store_async 内按 Cacheability Rule 分域——追问写会话域，自包含写用户域
                answer_cache.store_async(
                    req.query, response.model_dump(),
                    user_id=current_user.id, session_id=req.session_id,
                )

                span.update(
                    intent=response.intent, duration_ms=duration_ms,
                    n_disclaimers=len(disclaimers), ok=True,
                )
                return response

            except asyncio.TimeoutError:
                duration_ms = int((time.time() - start_time) * 1000)
                app_state.total_errors += 1

                log_request(
                    query=req.query,
                    intent="timeout",
                    duration_ms=duration_ms,
                    has_disclaimer=False,
                    success=False
                )

                span.update(intent="timeout", duration_ms=duration_ms, ok=False)
                raise HTTPException(
                    status_code=504,
                    detail=f"Request timeout after {REQUEST_TIMEOUT}s. Please try a simpler question."
                )

    except HTTPException:
        raise
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        app_state.total_errors += 1

        log_request(
            query=req.query,
            intent="error",
            duration_ms=duration_ms,
            has_disclaimer=False,
            success=False
        )

        logger.error(f"[API] Consultation failed: {e}")

        err_answer = "抱歉，系统暂时无法处理您的请求。请稍后重试，或拨打医疗咨询热线 12320。"
        # 记录本轮，避免上下文断裂（归属冲突时静默跳过，不影响错误响应）
        try:
            history_store.persist_turn(req.session_id, "user", req.query, user_id=current_user.id)
            history_store.persist_turn(
                req.session_id, "assistant", err_answer, intent="error", user_id=current_user.id
            )
        except (ConversationNotFoundError, ConversationOwnershipError):
            pass

        # Return friendly error
        return ConsultationResponse(
            answer=err_answer,
            intent="error",
            disclaimers=["🔴 重要声明：本建议仅供参考，不能替代专业医疗诊断。"],
            duration_ms=duration_ms
        )


# ============================================================
# POST /api/consult/stream - Streaming Consultation Endpoint (SSE)
# ============================================================

# 流结束哨兵：next(gen, _STREAM_DONE) 避免 StopIteration 穿越线程边界
# 进入 async 生成器（PEP 479 会把 async 生成器内的 StopIteration 转为 RuntimeError）
_STREAM_DONE = object()


def _sse_format(data: str, event: str | None = None) -> str:
    """把一条消息格式化为 SSE 帧：event:/data: 行 + 空行结尾（WHATWG SSE 规范）。"""
    lines = []
    if event is not None:
        lines.append(f"event: {event}")
    for line in data.splitlines() or [""]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def _facts_from_delta(delta: dict) -> dict:
    """从节点 state 增量中提取可展示给用户的部分事实（progress 帧用）。"""
    facts = {}
    if delta.get("intent"):
        facts["intent"] = delta["intent"]
    if delta.get("symptoms"):
        facts["symptoms"] = delta["symptoms"]
    if delta.get("departments"):
        facts["departments"] = delta["departments"]
    if delta.get("linked_entities"):
        facts["linked_entities"] = [lk.get("matched_entity") for lk in delta["linked_entities"]]
    if delta.get("needs_clarification"):
        facts["needs_clarification"] = True
    return facts


def _build_response_from_state(state: dict, start_time: float) -> ConsultationResponse:
    """从终态构建标准响应（/consult 与 /consult/stream 共用字段口径）。"""
    return ConsultationResponse(
        answer=state.get("final_answer", ""),
        intent=state.get("intent", ""),
        symptoms=state.get("symptoms", []),
        departments=state.get("departments", []),
        medications=state.get("medications", []),
        disclaimers=state.get("disclaimers", []),
        warnings=state.get("warnings", []),
        linked_entities=state.get("linked_entities", []),
        duration_ms=int((time.time() - start_time) * 1000),
    )


@router.post(
    "/consult/stream",
    tags=["问诊"],
    summary="流式问诊接口（SSE）",
    description="与 /consult 相同的结果，但以 Server-Sent Events 流式推送："
                "event: progress（每个 Agent 节点完成的进度）→ event: answer（完整响应 JSON）"
                "→ event: done（[DONE] 结束标记）。客户端断开时提前终止。需登录。"
)
async def consult_stream(
    req: ConsultationRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """SSE 流式问诊。

    桥接设计：LangGraph app.stream 是同步生成器，本端点是 async def。
    用 asyncio.to_thread(lambda: next(gen, _STREAM_DONE)) 逐个取值——
    每个节点一次线程调度，既能在每步之间检查客户端断开，又不长时间占用线程池。
    """
    from src.api.main import app_state

    if not app_state.agent_system_ready:
        raise HTTPException(
            status_code=503,
            detail="Agent system not ready. Please try again later."
        )

    # 归属裁决 + 上下文加载放在流开始之前：越权访问直接 404，
    # 避免 StreamingResponse 已经发出后再也无法返回 HTTP 状态码。
    try:
        history = history_store.load_context(req.session_id, current_user.id)
    except ConversationOwnershipError:
        raise HTTPException(status_code=404, detail="Conversation not found")

    start_time = time.time()
    app_state.total_requests += 1
    request_id = uuid.uuid4().hex[:8]
    logger.info(
        f"[API] Streaming consultation request: '{req.query[:50]}...' "
        f"(session: {req.session_id}, user: {current_user.username}, rid: {request_id})"
    )
    callbacks = get_callbacks()

    # 答案缓存：与 /consult 同规则（三分域，全用户隔离；Cacheability Rule 分域）。
    # 命中时流里发一个缓存命中 progress 帧 + 完整 answer 帧，前端渲染零改动
    cache_hit = answer_cache.lookup(
        req.query, user_id=current_user.id, session_id=req.session_id
    )

    async def event_generator():
        from src.agents.graph import run_stream, NODE_LABELS_ZH

        final_state = None
        # 请求级 span：生成器结束（含异常/客户端断开提前 return）时 flush
        with trace_span(
            "consult_stream", request_id=request_id, session_id=req.session_id,
            user_id=current_user.id, query=req.query[:200],
        ) as span:
            try:
                if cache_hit:
                    level, payload = cache_hit
                    response = ConsultationResponse.model_validate({
                        **payload,
                        "cached": True,
                        "duration_ms": int((time.time() - start_time) * 1000),
                        "timestamp": datetime.now().isoformat(),
                    })
                    log_request(
                        query=req.query,
                        intent=response.intent,
                        duration_ms=response.duration_ms,
                        has_disclaimer=len(response.disclaimers) > 0,
                        success=True,
                    )
                    logger.info(f"[API] Stream answer cache hit {level} (rid: {request_id})")
                    # 命中也要落对话历史，否则下一轮上下文断裂
                    try:
                        history_store.persist_turn(
                            req.session_id, "user", req.query, user_id=current_user.id
                        )
                        history_store.persist_turn(
                            req.session_id, "assistant", response.answer,
                            intent=response.intent, user_id=current_user.id
                        )
                    except (ConversationNotFoundError, ConversationOwnershipError) as e:
                        logger.warning(f"[API] Skip stream persist due to ownership conflict: {e}")
                    span.update(
                        intent=response.intent, duration_ms=response.duration_ms,
                        cache=level, ok=True,
                    )
                    yield _sse_format(
                        json.dumps(
                            {"node": "answer_cache", "label": f"缓存命中（{level}）", "facts": {}},
                            ensure_ascii=False,
                        ),
                        event="progress",
                    )
                    yield _sse_format(response.model_dump_json(), event="answer")
                    yield _sse_format("[DONE]", event="done")
                    return

                async with app_state.request_semaphore:
                    # thread_id 含 user_id：与非流式 /consult 同口径的用户级执行态隔离
                    gen = run_stream(
                        req.query, history, callbacks,
                        build_thread_id(current_user.id, req.session_id),
                    )
                    while True:
                        # 先查客户端断开，再取下一块
                        if await request.is_disconnected():
                            logger.info("[API] Stream client disconnected, stopping early")
                            return
                        try:
                            chunk = await asyncio.wait_for(
                                asyncio.to_thread(lambda: next(gen, _STREAM_DONE)),
                                timeout=REQUEST_TIMEOUT,
                            )
                        except asyncio.TimeoutError:
                            app_state.total_errors += 1
                            logger.error(f"[API] Stream timeout after {REQUEST_TIMEOUT}s")
                            span.update(intent="timeout", ok=False)
                            yield _sse_format(
                                json.dumps({"message": f"处理超时（{REQUEST_TIMEOUT}s），请简化问题后重试"},
                                           ensure_ascii=False),
                                event="error",
                            )
                            return
                        if chunk is _STREAM_DONE:
                            break

                        kind = chunk[0]
                        if kind == "progress":
                            _, node_name, delta = chunk
                            payload = {
                                "node": node_name,
                                "label": NODE_LABELS_ZH.get(node_name, node_name),
                                "facts": _facts_from_delta(delta),
                            }
                            yield _sse_format(json.dumps(payload, ensure_ascii=False), event="progress")
                        else:  # kind == "values"：完整快照，最后一个即终态
                            final_state = chunk[1]
            except Exception as e:
                app_state.total_errors += 1
                logger.error(f"[API] Streaming consultation failed: {e}")
                span.update(ok=False, error=str(e)[:200])
                yield _sse_format(
                    json.dumps({"message": f"系统错误：{e}"}, ensure_ascii=False),
                    event="error",
                )
                return

            # 终答帧：与 /consult 同口径的完整响应
            if final_state is not None:
                response = _build_response_from_state(final_state, start_time)
                log_request(
                    query=req.query,
                    intent=response.intent,
                    duration_ms=response.duration_ms,
                    has_disclaimer=len(response.disclaimers) > 0,
                    success=True,
                )
                # 双写对话历史（与 /consult 一致；归属冲突仅记日志，不打断流）
                try:
                    history_store.persist_turn(
                        req.session_id, "user", req.query, user_id=current_user.id
                    )
                    history_store.persist_turn(
                        req.session_id, "assistant", response.answer,
                        intent=response.intent, user_id=current_user.id
                    )
                except (ConversationNotFoundError, ConversationOwnershipError) as e:
                    logger.warning(f"[API] Skip stream persist due to ownership conflict: {e}")
                # 写缓存（后台线程异步，不占流路径延迟）：按 Cacheability Rule 分域写入
                answer_cache.store_async(
                    req.query, response.model_dump(),
                    user_id=current_user.id, session_id=req.session_id,
                )
                span.update(
                    intent=response.intent, duration_ms=response.duration_ms,
                    n_disclaimers=len(response.disclaimers), ok=True,
                )
                yield _sse_format(response.model_dump_json(), event="answer")

            yield _sse_format("[DONE]", event="done")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 防反向代理（Nginx 等）缓冲 SSE
        },
    )


# ============================================================
# GET /api/health - Health Check Endpoint
# ============================================================

@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["系统"],
    summary="健康检查",
    description="检查服务状态、Neo4j 连接、模型加载状态"
)
async def health():
    """
    Health check endpoint

    Returns status of all system components.
    """
    from src.api.main import app_state

    # Determine overall status
    if app_state.agent_system_ready and app_state.vector_index_ready and app_state.neo4j_ready:
        status = "healthy"
    elif app_state.agent_system_ready:
        status = "degraded"
    else:
        status = "unhealthy"

    return HealthResponse(
        status=status,
        neo4j_connected=app_state.neo4j_ready,
        vector_index_loaded=app_state.vector_index_ready,
        agent_system_ready=app_state.agent_system_ready
    )


# ============================================================
# GET /api/stats - System Statistics Endpoint
# ============================================================

@router.get(
    "/stats",
    response_model=StatsResponse,
    tags=["系统"],
    summary="系统统计",
    description="返回知识图谱、向量索引、API 统计信息"
)
async def stats():
    """
    System statistics endpoint

    Returns knowledge graph stats, vector index stats, and API stats.
    """
    from src.api.main import app_state

    # Knowledge graph stats
    kg_stats = {}
    try:
        from src.knowledge_graph.graph_queries import GraphQueries
        queries = GraphQueries()
        kg_data = queries.get_statistics()
        kg_stats = {
            "total_nodes": kg_data.get("total_nodes", 0),
            "total_relations": kg_data.get("total_relations", 0),
            "nodes_by_type": kg_data.get("nodes", {}),
            "relations_by_type": kg_data.get("relations", {}),
            "density": round(kg_data.get("density", 0), 2)
        }
        queries.close()
    except Exception as e:
        kg_stats = {"error": str(e)}

    # Vector index stats
    vi_stats = {}
    try:
        from config.paths import DATA_INDEXES_DIR
        import json

        index_file = DATA_INDEXES_DIR / "faiss.index"
        entities_file = DATA_INDEXES_DIR / "entities.json"

        if index_file.exists():
            import faiss
            index = faiss.read_index(str(index_file))
            vi_stats["total_vectors"] = index.ntotal
            vi_stats["dimension"] = index.d

        if entities_file.exists():
            with open(entities_file, encoding="utf-8") as f:
                entities = json.load(f)
            vi_stats["total_entities"] = len(entities)

    except Exception as e:
        vi_stats = {"error": str(e)}

    # API stats
    api_stats = {
        "version": "1.0.0",
        "total_requests": app_state.total_requests,
        "total_errors": app_state.total_errors,
        "concurrency_limit": 5,
        "request_timeout": REQUEST_TIMEOUT
    }

    return StatsResponse(
        knowledge_graph=kg_stats,
        vector_index=vi_stats,
        api=api_stats
    )


# ============================================================
# 对话历史管理（Redis 热缓存 + MySQL 冷存储 + 用户归属，讲义 §6 / §7）
# 历史接口用 def 同步路由：FastAPI 自动放入线程池，匹配
# store 层"MySQL 每请求新开连接、用完即关"的连接管理约定。
# 归属原则：只返回/操作当前登录用户自己的会话；越权访问统一 404，
# 不泄露会话是否存在（防 session_id 猜测枚举）。
# ============================================================

@router.get(
    "/conversations",
    response_model=ConversationListResponse,
    tags=["对话历史"],
    summary="对话历史列表",
    description="按 updated_at 倒序分页列出【当前用户】的对话（LIMIT ≤ 200），读 MySQL"
)
def list_conversations(limit: int = 50, current_user: User = Depends(get_current_user)):
    """对话历史列表（左侧历史栏数据源，仅本人会话）"""
    return ConversationListResponse(
        conversations=history_store.list_conversations(
            current_user.id, limit=min(max(limit, 1), 200)
        )
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
    tags=["对话历史"],
    summary="恢复单个对话",
    description="返回该对话全部消息（时间正序），仅限属主；读 MySQL"
)
def get_conversation(conversation_id: str, current_user: User = Depends(get_current_user)):
    """恢复单个对话（单击历史栏项时调用，仅属主可访问）"""
    try:
        messages = history_store.get_messages(conversation_id, current_user.id)
    except (ConversationNotFoundError, ConversationOwnershipError):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationDetailResponse(
        conversation_id=conversation_id,
        messages=messages,
    )


@router.delete(
    "/conversations/{conversation_id}",
    response_model=DeleteConversationResponse,
    tags=["对话历史"],
    summary="删除对话",
    description="同步清空 MySQL 两表 + 对应 Redis key，仅限属主"
)
def delete_conversation(conversation_id: str, current_user: User = Depends(get_current_user)):
    """删除对话（仅属主可删；MySQL 与 Redis 同步清空）"""
    try:
        history_store.delete_conversation(conversation_id, current_user.id)
    except (ConversationNotFoundError, ConversationOwnershipError):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return DeleteConversationResponse(deleted=conversation_id)
