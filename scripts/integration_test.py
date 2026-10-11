"""
System Integration Smoke Test
依次验证核心链路（含用户认证，讲义 §7）：
  GET  /api/health          —— 服务与依赖就绪
  GET  /api/stats           —— 知识图谱统计可读
  POST /api/auth/register   —— 用户注册（含重复注册 409）
  POST /api/auth/login      —— 登录签发 JWT（含错误密码 401）
  GET  /api/auth/me         —— 令牌校验（无/伪造令牌 401）
  POST /api/consult         —— 非流式问诊（无令牌 401 + 含免责声明校验）
  GET/DELETE /api/conversations —— 会话归属隔离（他人会话 404）
  POST /api/consult/stream  —— SSE 流式问诊（progress≥1 + answer + done 帧序）

用法：
    python scripts/integration_test.py                    # 默认 http://localhost:8000
    python scripts/integration_test.py --base-url http://127.0.0.1:8000
    python scripts/integration_test.py --skip-consult     # 跳过消耗 LLM token 的两个问诊端点

前置：已启动 API（python scripts/start_api.py）、MySQL 就绪；
     向量索引与 Neo4j 就绪可获得完整覆盖（缺失时 health 为 degraded）。
退出码：0 = 全部通过；非 0 = 存在失败（可接入 CI）。
"""

import sys
import json
import time
import uuid
import argparse
from pathlib import Path

_project_root = Path(__file__).parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import requests

PASS = "✅ PASS"
FAIL = "❌ FAIL"

_results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = ""):
    _results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))


def test_health(base: str):
    try:
        r = requests.get(f"{base}/api/health", timeout=10)
        body = r.json() if r.status_code == 200 else {}
        ok = r.status_code == 200 and body.get("agent_system_ready")
        record("GET /api/health", ok,
               f"status={body.get('status')} neo4j={body.get('neo4j_connected')} "
               f"vector={body.get('vector_index_loaded')} agent={body.get('agent_system_ready')}")
    except Exception as e:
        record("GET /api/health", False, f"连接失败: {e}（请先启动 API）")


def test_stats(base: str):
    try:
        r = requests.get(f"{base}/api/stats", timeout=10)
        data = r.json() if r.status_code == 200 else {}
        kg = data.get("knowledge_graph", {})
        vi = data.get("vector_index", {})
        ok = r.status_code == 200 and "error" not in kg and kg.get("total_nodes", 0) > 0
        record("GET /api/stats", ok,
               f"nodes={kg.get('total_nodes')} relations={kg.get('total_relations')} "
               f"vectors={vi.get('total_vectors')}")
    except Exception as e:
        record("GET /api/stats", False, str(e))


def setup_auth(base: str) -> dict:
    """注册并登录两个一次性测试用户，返回 {alice: {username, token}, bob: {...}}。"""
    suffix = uuid.uuid4().hex[:6]
    password = "IntegTest@123"
    users = {}
    for name in ("alice", "bob"):
        username = f"{name}_{suffix}"
        r = requests.post(f"{base}/api/auth/register",
                          json={"username": username, "password": password}, timeout=15)
        r.raise_for_status()
        r = requests.post(f"{base}/api/auth/login",
                          data={"username": username, "password": password}, timeout=15)
        r.raise_for_status()
        users[name] = {"username": username, "token": r.json()["access_token"]}
    return users


def test_auth(base: str, users: dict):
    token = users["alice"]["token"]
    hdr = {"Authorization": f"Bearer {token}"}

    # 重复注册 → 409
    r = requests.post(f"{base}/api/auth/register",
                      json={"username": users["alice"]["username"], "password": "IntegTest@123"},
                      timeout=15)
    record("POST /api/auth/register (重复注册)", r.status_code == 409, f"HTTP {r.status_code}")

    # 错误密码 → 401
    r = requests.post(f"{base}/api/auth/login",
                      data={"username": users["alice"]["username"], "password": "WrongPass@1"},
                      timeout=15)
    record("POST /api/auth/login (错误密码)", r.status_code == 401, f"HTTP {r.status_code}")

    # /me：有令牌 200；无令牌/伪造令牌 401
    r = requests.get(f"{base}/api/auth/me", headers=hdr, timeout=15)
    ok = r.status_code == 200 and r.json()["username"] == users["alice"]["username"]
    record("GET /api/auth/me (有令牌)", ok, f"HTTP {r.status_code}")

    r = requests.get(f"{base}/api/auth/me", timeout=15)
    record("GET /api/auth/me (无令牌)", r.status_code == 401, f"HTTP {r.status_code}")

    r = requests.get(f"{base}/api/auth/me",
                     headers={"Authorization": "Bearer forged.jwt.token"}, timeout=15)
    record("GET /api/auth/me (伪造令牌)", r.status_code == 401, f"HTTP {r.status_code}")

    # 受保护接口无令牌一律 401
    r = requests.get(f"{base}/api/conversations", timeout=15)
    record("GET /api/conversations (无令牌)", r.status_code == 401, f"HTTP {r.status_code}")
    r = requests.post(f"{base}/api/consult",
                      json={"query": "测试", "session_id": "x"}, timeout=15)
    record("POST /api/consult (无令牌)", r.status_code == 401, f"HTTP {r.status_code}")


def test_consult(base: str, query: str, token: str, session_id: str):
    try:
        t0 = time.time()
        r = requests.post(f"{base}/api/consult",
                          json={"query": query, "session_id": session_id},
                          headers={"Authorization": f"Bearer {token}"},
                          timeout=90)
        d = r.json() if r.status_code == 200 else {}
        # 医疗合规断言：任何回答都必须带免责声明
        ok = (r.status_code == 200 and d.get("answer") and d.get("disclaimers"))
        record("POST /api/consult", bool(ok),
               f"intent={d.get('intent')} answer={len(d.get('answer', ''))}chars "
               f"disclaimers={len(d.get('disclaimers', []))} "
               f"linked={len(d.get('linked_entities', []))} "
               f"duration={time.time() - t0:.1f}s")
    except Exception as e:
        record("POST /api/consult", False, str(e))


def test_conversation_isolation(base: str, users: dict, cid: str):
    """会话归属：consult 已用 alice 令牌创建 cid 会话，
    bob 读/删都应 404，alice 本人可读。"""
    hdr_a = {"Authorization": f"Bearer {users['alice']['token']}"}
    hdr_b = {"Authorization": f"Bearer {users['bob']['token']}"}

    r = requests.get(f"{base}/api/conversations/{cid}", headers=hdr_b, timeout=15)
    record("GET 他人会话 (bob→alice)", r.status_code == 404, f"HTTP {r.status_code}")

    r = requests.delete(f"{base}/api/conversations/{cid}", headers=hdr_b, timeout=15)
    record("DELETE 他人会话 (bob→alice)", r.status_code == 404, f"HTTP {r.status_code}")

    r = requests.get(f"{base}/api/conversations/{cid}", headers=hdr_a, timeout=15)
    ok = r.status_code == 200 and len(r.json().get("messages", [])) >= 2
    record("GET 本人会话 (alice)", ok, f"HTTP {r.status_code} msgs={len(r.json().get('messages', []))}")

    r = requests.get(f"{base}/api/conversations", headers=hdr_a, timeout=15)
    ids = [c["id"] for c in r.json().get("conversations", [])]
    record("GET 会话列表隔离 (alice 只见到自己的)", cid in ids, f"count={len(ids)}")


def test_consult_stream(base: str, query: str, token: str, session_id: str):
    """SSE 流式端点：断言帧序 progress≥1 + answer=1 + done=1 + error=0。

    帧解析假设：本协议每帧恰为一行 event: + 一行 data:（后端 _sse_format 保证
    JSON 数据单行），故逐行解析、data 行到达即派发，无需等待空行分帧。
    """
    events = {"progress": 0, "answer": 0, "error": 0, "done": 0}
    answer = {}
    t0 = time.time()
    try:
        with requests.post(f"{base}/api/consult/stream",
                           json={"query": query, "session_id": session_id},
                           headers={"Authorization": f"Bearer {token}"},
                           stream=True, timeout=90) as r:
            if r.status_code != 200:
                record("POST /api/consult/stream", False, f"HTTP {r.status_code}: {r.text[:100]}")
                return
            event = "message"
            for raw in r.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                if raw.startswith("event:"):
                    event = raw[6:].strip()
                elif raw.startswith("data:"):
                    data = raw[5:].strip()
                    if event in events:
                        events[event] += 1
                    if event == "answer":
                        try:
                            answer = json.loads(data)
                        except json.JSONDecodeError:
                            pass
                    event = "message"  # 派发后复位
        ok = (events["progress"] >= 1 and events["answer"] == 1
              and events["done"] == 1 and events["error"] == 0 and answer.get("answer"))
        record("POST /api/consult/stream", ok,
               f"progress={events['progress']} answer={events['answer']} "
               f"done={events['done']} error={events['error']} "
               f"intent={answer.get('intent')} duration={time.time() - t0:.1f}s")
    except Exception as e:
        record("POST /api/consult/stream", False, str(e))


def main():
    ap = argparse.ArgumentParser(description="系统集成冒烟测试")
    # 默认 127.0.0.1 而非 localhost：部分 Windows 机 localhost 先解析到 ::1，
    # 会引入 ~2s 的 IPv6 连接超时回退，干扰延迟观测
    ap.add_argument("--base-url", default="http://127.0.0.1:8000", help="API 基地址")
    ap.add_argument("--query", default="我最近头痛、头晕，应该挂什么科？", help="测试问题")
    ap.add_argument("--skip-consult", action="store_true",
                    help="跳过两个消耗 LLM token 的问诊端点（仅测 health/stats/auth）")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    print("=" * 60)
    print(f"INTEGRATION SMOKE TEST → {base}")
    print("=" * 60)

    test_health(base)
    test_stats(base)

    # 认证链路（不消耗 LLM token）
    try:
        users = setup_auth(base)
        test_auth(base, users)
    except Exception as e:
        record("AUTH SETUP", False, f"注册/登录失败: {e}（请确认 MySQL 已启动）")
        users = None

    if not args.skip_consult and users:
        token = users["alice"]["token"]
        # 会话属主 = 本次运行的一次性用户，session_id 必须每次运行唯一，
        # 否则上一次运行遗留的同名会话会因归属校验拒绝本次写入（正确行为，但干扰测试）。
        sess = f"integ-{uuid.uuid4().hex[:10]}"
        sess_stream = f"{sess}-s"
        test_consult(base, args.query, token, sess)
        test_conversation_isolation(base, users, sess)
        test_consult_stream(base, args.query, token, sess_stream)
        # 清理：删除本次运行产生的两个会话（best effort，不影响断言结果）
        for cid in (sess, sess_stream):
            try:
                requests.delete(f"{base}/api/conversations/{cid}",
                                headers={"Authorization": f"Bearer {token}"}, timeout=15)
            except Exception:
                pass
    elif args.skip_consult:
        print("  （--skip-consult：跳过问诊端点与会话隔离用例）")

    failed = [n for n, ok, _ in _results if not ok]
    print("=" * 60)
    if failed:
        print(f"[FAILED] {len(failed)} 项未通过：{failed}")
        sys.exit(1)
    print(f"[SUCCESS] 全部 {len(_results)} 项通过")
    sys.exit(0)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
