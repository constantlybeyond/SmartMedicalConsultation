# 智慧问诊Agent系统 - 技术设计文档 (TDD)

> **文档状态与版本演进（阅读前必看）**
>
> 本项目按 Vibe Coding 工作流持续演进，文档保留各阶段设计痕迹。三个版本边界：
>
> | 版本 | 关键变化                                                                        | 当前契约位置（单一事实来源，R1）                        |
> | ---- | ------------------------------------------------------------------------------- | ------------------------------------------------------- |
> | V1   | 匿名 + 内存会话；请求体带`user_id`/`data` 包装                              | 已废弃，仅具历史讲解价值                                |
> | V2   | 对话持久化（Redis 热 + MySQL 冷）；**用户认证**（JWT + bcrypt）与会话归属 | `src/api/auth_routes.py`、`src/history/store.py`    |
> | V3   | **上下文 token 预算** + 后台增量摘要（水位机制）                          | `src/common/tokens.py`、`src/history/summarizer.py` |
>
> 凡本文早期示例（如 `user_id` 字段、`POST /process` 路由、`data` 包装请求体）与当前代码冲突时，
> **一律以当前代码为准**；当前 HTTP 契约见 `src/api/routes.py` 与 `src/api/models.py`，
> 教学讲解见 day05 讲义 §6（历史）/ §6.6（上下文预算）/ §7（认证）。

## 0. 工程规范（契约与验收）

> **本章定位**：本 TDD 是 AI 生成器（Claude Code）的**系统约束 + 验收清单**。本章每条规范满足两个条件：
> ① **结构性约束**——违规在写法上就不成立（导入枚举，而非手敲字符串）；
> ② **自动化验收**——每条规范附可执行检查，由 AI 在生成后自行运行（`python scripts/check_contracts.py`）。
>
> **学生用法**：不需要背诵规范。每条 Vibe Coding 指令开头固定写一句——
> 「严格遵守 TDD.md 第 0 章工程规范 R1–R8，生成后运行自检，全绿才算完成」。

### 0.1 为什么需要工程规范（Vibe Coding 的漂移问题）

AI 逐模块生成代码，每个模块内部永远自洽；但同一个名字如果活在多处（Schema 定义 / 抽取提示词 / 建图模板 / 查询语句 / 存量数据），多轮迭代后必然漂移。

**真实事故（本项目）**：关系类型名规范化迁移只改了代码层，存量 Neo4j 数据未迁移（库里仍是 `BELONG_TO_DEPARTMENT`，代码查询 `BELONGS_TO_DEPARTMENT`）→ 科室推荐的图谱查询全部静默返空（日志 `kg=[]` + Neo4j 警告 `relationship type is not in the database`）→ 所有回答退化为纯 LLM 兜底，效果与通用大模型无异。

**规范的定位**：一致性类 bug（名字 / 字段 / 维度 / 数据与代码漂移）恰恰是零基础学生完全无法调试的类别——规范的目标是让这类 bug **在诞生当天死亡**。

### R1 单一事实来源（Single Source of Truth）

**约束**：

1. 节点类型、关系类型**只允许**定义在 `src/knowledge_graph/schema.py` 的 `NodeType` / `RelationType` 枚举中；意图类型只在 `src/agents/state.py` 的 `IntentType`。
2. 其他所有文件（抽取 prompt、建图模板、查询方法、Cypher 拼装）一律 **import 枚举**后用 `.value` 拼装，**禁止直接书写类型名字符串**。
3. 可调参数（阈值 / 超时 / top_k / 端口）一律进 `config/settings.py`，禁止散落魔法数字。

**验收**：

```bash
python scripts/check_contracts.py --r1
# 1) 历史错误名扫描：BELONG_TO_DEPARTMENT / TREATED_BY_DRUG / TREATED_BY（非 _MEDICATION 后缀）等出现即报错
# 2) 枚举定义位置检查：除 schema.py / state.py 外不允许出现 Enum 类型定义
# 3) 未知名字扫描：代码中出现枚举之外的 SCREAMING_SNAKE 类型名 → 告警
```

### R2 契约先行与生成顺序

**约束**：模块必须按契约依赖顺序生成，下游生成时**先 import 已存在的契约文件**，不允许引入契约之外的新类型名 / 字段名；确需扩展时，**先改契约文件，再重新生成下游**。

```
schema.py（名字契约）→ extraction/（抽取，产出契约内类型）→ graph_builder（按契约写库）
→ graph_queries（按契约读库）→ vector_store（索引字段契约）→ agents/ → api/（接口契约）→ frontend/
```

**验收**：R1 + R5 检查全绿（见下）。

### R3 前后端字段契约

**约束**：接口字段**只在 `src/api/models.py` 实现**；前端按契约表逐字段消费；字段增删改必须**先更新本表**再动代码。

**契约表（本项目现行）**：

| 接口 / 通道                             | 字段                                                                | 类型             | 生产方                     | 前端消费位置         |
| --------------------------------------- | ------------------------------------------------------------------- | ---------------- | -------------------------- | -------------------- |
| POST /api/consult 请求                  | query                                                               | str              | 前端输入                   | —                   |
|                                         | session_id                                                          | str              | 前端生成                   | —                   |
| /api/consult 响应（也是 SSE answer 帧） | answer                                                              | str              | answer_fusion              | ChatMessage 正文     |
|                                         | intent                                                              | str              | intent_classifier          | 元信息意图标签       |
|                                         | symptoms                                                            | list[str]        | symptom_detector           | 症状 el-tag          |
|                                         | departments                                                         | list[str]        | department_recommender     | 科室 el-tag          |
|                                         | medications                                                         | list[dict]       | medication_advisor         | 药物 el-tag          |
|                                         | disclaimers                                                         | list[str]        | safety_checker（强制）     | 黄色免责块           |
|                                         | warnings                                                            | list[str]        | safety_checker             | 红色急症 alert       |
|                                         | linked_entities                                                     | list[dict]       | medical_knowledge 实体链接 | 「知识定位」标签     |
|                                         | duration_ms                                                         | int              | API 层                     | 元信息耗时           |
| SSE progress 帧                         | node / label / facts                                                | str / str / dict | routes（NODE_LABELS_ZH）   | ChatMessage 进度轨迹 |
| SSE error 帧                            | message                                                             | str              | routes                     | 前端错误提示         |
| GET /api/health                         | status / neo4j_connected / vector_index_loaded / agent_system_ready | str / bool×3    | AppState                   | 侧栏状态灯           |
| GET /api/stats                          | knowledge_graph / vector_index / api                                | dict×3          | 各数据源                   | （预留）             |

**验收**：

```bash
python scripts/check_contracts.py --r3
# 前端 data.<字段> 引用了 models.py 不存在的字段 → 错误
# models.py 提供但前端从未消费的字段 → 告警（如新增字段忘了接）
```

### R4 索引与嵌入契约

**约束（数值契约，改动须同步本表）**：

| 契约项                 | 值                                  | 权威位置                                                                                      |
| ---------------------- | ----------------------------------- | --------------------------------------------------------------------------------------------- |
| 嵌入模型               | BGE-M3                              | `models/bge-m3`，`settings.embedding_model_path`                                          |
| 向量维度               | **1024**                      | `models/bge-m3/config.json` 的 `hidden_size`；`scripts/build_index.py` 的 `dimension` |
| 索引类型               | IndexIVFFlat + METRIC_INNER_PRODUCT | `scripts/build_index.py`（向量必须 `normalize_embeddings=True`）                          |
| entities.json 条目结构 | `{name, type, data}`              | `data/indexes/entities.json`，下标与索引向量一一对应                                        |
| 相似度换算             | IP→原值；L2→`1 - d/2`           | `VectorRetriever._distance_to_similarity`                                                   |

**验收**：

```bash
python scripts/check_contracts.py --r4
# 断言：index.d == 1024 == config.json hidden_size；len(entities.json) == index.ntotal；条目键齐全
```

### R5 数据即产物（Rebuild over Migration）

**约束**：Neo4j 图内容、FAISS 索引、`data/` 下全部 JSON 均声明为**代码的构建产物**。任何契约变更（R1–R4 涉及的名字 / 字段 / 维度改动）的修复流程**固定为**：

```bash
# 改完契约文件后：
python -m src.knowledge_graph.graph_builder clear     # 清空
python -m src.knowledge_graph.graph_builder build     # 重建（MERGE 幂等）
python scripts/build_index.py                         # 索引随之重建
```

**禁止**编写"兼容旧数据"的迁移 / 修补脚本（教学阶段）——学生只需理解"代码对了就重跑"，不需要理解新旧状态共存。

**验收**：

```bash
python scripts/check_contracts.py --r5
# DB 节点标签 ⊆ NodeType ∪ {Treatment, MedicalConcept}（后两者为辅助抽取类型）
# DB 关系类型 ⊆ RelationType；出现枚举之外的类型 = 错误（报错信息附 clear+build 修复指令）
# 缺失的类型 = 告警（子集属正常，由抽取数据覆盖范围决定）
```

### R6 命名公约与单向关系

**约束**：

1. 关系名采用 `动词_宾语` 大写蛇形：`HAS_SYMPTOM`、`BELONGS_TO_DEPARTMENT`、`NEEDS_EXAMINATION`。
2. **一个事实只用一个方向表达**；反向语义用 Cypher 反向遍历表达（如"症状找疾病”用 `MATCH (d)-[:HAS_SYMPTOM]->(s)` 反向匹配），不为同一事实新建反向关系类型——关系数量减半即漂移面减半。
3. **历史例外**：本项目保留既有反向关系 `MAY_INDICATE` / `TREATS_DISEASE` / `HANDLES_DISEASE`（已有存量数据）；新关系 / 新项目一律遵守单向原则。

### R7 模块自检条款

**约束**：TDD 各模块小节末尾必须附「生成后必跑」检查清单；AI 生成后**立即自行运行**，不绿则停下报告，不交付半成品。

**统一验收器**：`scripts/check_contracts.py`（覆盖 R1 / R3 / R4 / R5），退出码 0 = 全绿。

| 模块                                              | 生成后必跑                                            |
| ------------------------------------------------- | ----------------------------------------------------- |
| `src/extraction/`                               | `check_contracts.py --r1`；抽取产物类型集合 ⊆ 枚举 |
| `src/knowledge_graph/`                          | `check_contracts.py --r1 --r5`                      |
| `src/vector_store/`、`scripts/build_index.py` | `check_contracts.py --r4`                           |
| `src/api/`、`frontend/`                       | `check_contracts.py --r3` + `npm run build`       |
| 全模块完成后                                      | R8 黄金冒烟                                           |

### R8 黄金冒烟（端到端验收）

每完成一个模块，重跑以下命令，全绿方可继续：

```bash
python scripts/check_contracts.py              # 契约门禁（R1/R3/R4/R5）
python scripts/integration_test.py             # 四端点冒烟（health/stats/consult/consult-stream SSE 帧序）
python evals/runner.py --limit 10              # 评估门禁：disclaimer=1.0、急症召回、intent≥0.9
```

### 0.2 课堂操作流程（零基础视角）

```
1. 每条 Vibe Coding 指令开头：「严格遵守 TDD.md 第 0 章工程规范 R1–R8，生成后运行自检」
2. AI 生成模块 → AI 自己跑 check_contracts.py → 红了自己修（学生旁观即可）
3. 涉及数据的红色错误 → 修复口径只有 clear + build（R5），不写迁移补丁
4. 课末学生跑 R8 黄金冒烟 → 绿勾 → 下课
```

学生全程不需要理解"漂移"——他们只见过两种状态：**绿勾**，或 **AI 正在自己修到绿勾**。

### 0.3 规范的边界（诚实声明）

R1–R8 只能消除**一致性类** bug（名字 / 字段 / 维度 / 数据与代码漂移）；**消除不了**逻辑 / 提示词 / 数据质量类 bug——后者交给 day04 的评估驱动迭代（`evals/`）。区别在于：一致性 bug 学生完全无法调试，必须在设计期消灭；逻辑类 bug 学生"看得见"（答案不好），是可以参与调试的教学素材。

---

## 1. 技术架构

### 1.1 整体架构

系统分为**在线服务**与**离线构建**两条链路。在线：前端 → API → 多 Agent 编排 → 数据层；离线：爬虫 → LLM 抽取 → 建图 / 向量索引（一次性、可重跑，在线只读）。

```mermaid
flowchart TB
    subgraph L1["① 前端层"]
        UI["Vue 3 + Element Plus 聊天界面<br/>（dist/ 静态文件，FastAPI 同源托管）"]
    end
    subgraph L2["② API 层"]
        API["FastAPI · POST /api/consult<br/>CORS · 超时 60s · 并发信号量 · 会话记忆"]
    end
    subgraph L3["③ 多 Agent 编排层 · LangGraph 状态机"]
        AG["意图路由 DAG（完整路由见 src/agents/graph.py）：<br/>意图识别 → 症状检测 → 科室推荐 / 用药建议 / 医学知识 → 安全检查 → 答案融合"]
    end
    subgraph L4["④ 数据层"]
        NEO[("Neo4j 知识图谱<br/>疾病·症状·药物·科室·检查")]
        FAI[("FAISS 向量索引<br/>BGE-M3 · 1024 维 · 实体语义")]
    end

    UI -->|HTTP| API --> AG
    AG -->|科室/用药/就医指导/医学知识| NEO
    AG -->|医学知识·语义召回| FAI

    subgraph OFF["离线构建（一次性 / 可重跑）"]
        CR[爬虫·医疗网站] --> EX[LLM 结构化抽取] --> NEO
        EX --> EMB[实体向量化 BGE-M3] --> FAI
    end
```

**各 Agent 的数据访问（哪个 Agent 查哪个库、用什么机制）**：

| Agent                           | 机制                                   | Neo4j | FAISS | 说明                                                                 |
| ------------------------------- | -------------------------------------- | :---: | :---: | -------------------------------------------------------------------- |
| 意图识别 intent_classifier      | 纯 LLM                                 |  —  |  —  | 五分类（appointment / medication / knowledge / emergency / general） |
| 症状检测 symptom_detector       | 纯 LLM                                 |  —  |  —  | 抽取症状；信息不足可置`needs_clarification`                        |
| 科室推荐 department_recommender | 确定性 LLM+图检索                      |  ✅  |  —  | Tier1 图谱（症状→疾病→科室）→ Tier2 LLM 兜底                      |
| 用药建议 medication_advisor     | 确定性 LLM+图检索                      |  ✅  |  —  | 疾病→药物                                                           |
| 就医指导 pre_visit_advisor      | 确定性 LLM+图检索                      |  ✅  |  —  | 取图谱上下文生成就医须知                                             |
| 医学知识 medical_knowledge      | **强制混合检索 + grounded 生成** |  ✅  |  ✅  | FAISS 语义召回→Neo4j 取事实；检索为空时分风险兜底                   |
| 安全检查 safety_checker         | 纯规则                                 |  —  |  —  | 免责声明 / 急症升级（不依赖 LLM）                                    |
| 答案融合 answer_fusion          | 纯 LLM                                 |  —  |  —  | 汇总各 Agent 输出 + 追问话术                                         |
| 通用对话 general_chat           | 纯 LLM                                 |  —  |  —  | 非医疗闲聊                                                           |

> **机制选型**：检索模式固定的 Agent（科室/用药/就医指导）用**确定性 LLM+图检索**（快、省、可审计）；只有问法开放、可能多跳的**医学知识**分支用 FAISS+Neo4j **混合检索**，且检索为**强制必经**——医疗场景要求答案可溯源，不允许模型跳过检索凭空作答。

### 1.2 架构说明

**分层设计**：

1. **用户界面层**：Vue 3 + Element Plus 聊天应用，负责用户交互
2. **API 服务层**：FastAPI REST API，负责请求路由和参数校验
3. **Agent 系统层**：LangGraph 多 Agent 编排，负责业务逻辑
4. **数据访问层**：Neo4j 知识图谱 + FAISS 向量索引，负责数据存储和检索

**设计原则**：

- 前后端分离：Vue 前端通过 HTTP 调用 FastAPI（生产模式前端构建为静态文件由 FastAPI 同源托管）
- 模块化设计：每个 Agent 职责单一，易于测试和维护
- 可扩展性：新增 Agent 不影响现有系统
- 安全性：所有医疗建议强制添加免责声明
- 可溯源（强制检索）：医学知识问答**必须先检索知识库再作答**；检索为空时诚实兜底——一般知识标注"非本系统知识库内容"，用药相互作用/剂量等高风险直接建议咨询医生/药师，杜绝幻觉
- 确定性优先：检索模式固定的环节用确定性流水线，仅在开放问答分支引入 Agent 式工具调用，**不滥用 ReAct**（以确定性/可审计性换取不必要的"自主性"是反模式）
- 在线/离线分离：图谱与向量索引由离线流水线构建（可重跑），在线服务只读，互不耦合
- 数据访问最小化：每个 Agent 只访问其必需的存储（仅医学知识同时用 FAISS+Neo4j），降低耦合与噪声

### 1.3 部署架构

**推荐方式：docker-compose 启动 Neo4j**（声明式、可复现、纳入版本控制）：

```bash
# 1. 启动 Neo4j（项目根目录已提供 docker-compose.yml）
docker-compose up -d

# 常用管理命令
docker-compose ps              # 查看状态
docker-compose logs -f neo4j   # 查看日志
docker-compose stop            # 停止
docker-compose down            # 删除容器（保留数据卷 neo4j_data）

# 2. 构建前端（首次或前端改动后）
cd frontend && npm run build

# 3. 启动 FastAPI（同时托管 API + 前端静态文件，单进程）
python scripts/start_api.py
# 访问 http://localhost:8000 即是完整应用

# 开发模式（可选，前端热更新）：另开终端运行 cd frontend && npm run dev，访问 http://localhost:5173
```

**快速参考：docker run 单命令启动**（等价于上面的 compose，适合临时使用）：

```bash
docker run -d --name neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/12345678 \
  -v neo4j_data:/data \
  neo4j:5.26
```

> **说明**：本项目采用单进程部署 —— FastAPI 同时托管 API 与 Vue 前端静态文件，因此唯一需要容器化的基础设施是 Neo4j。`docker-compose.yml` 中 Neo4j 使用 `external` 命名卷 `neo4j_data`，与 `docker run` 创建的卷同名，数据可无缝保留。

---

## 2. 模块设计

### 2.1 项目结构

> **本节与代码同构**（2026-10-11 用 `git ls-files` + `find` 逐文件核对）。标注约定：
> **[入库]** = git 跟踪；**[产物]** = 运行生成、已被 `.gitignore` 排除。
> 注意 `.gitignore` 里有一条 blanket `*.json`，**所以 `data/` 与 `models/` 下所有 JSON 都不入库**——
> "仓库已附带中间数据"这个说法只对本地工作区成立，克隆后必须自己跑 §3 的离线流水线重建。
> 改结构时这一节要同步改：它是 R2「契约先行」与 R5「数据即产物」的落点之一。

```
SmartMedicalConsultation/
├── README.md                          # [入库] 项目说明（快速开始 / API 表 / 门禁用法）
├── PRD.md                             # [入库] 产品需求文档
├── TDD.md                             # [入库] 技术设计文档（第 0 章 = 工程规范 R1–R8）
├── requirements.txt                   # [入库] 依赖清单，锁死小版本号（LangChain/LangGraph 1.x）
├── Makefile                           # [入库] eval-fast / eval-full / eval-e2e
├── docker-compose.yml                 # [入库] Neo4j 5.26 + Redis(AOF) + MySQL 8.0 三件套
├── validate_env.py                    # [入库] 环境验证（6 项检查；退出码 0=全绿 / 1=有失败 / 2=多数失败）
├── .env                               # [产物] 真实密钥，严禁提交
├── .env.example                       # [入库] 唯一入库的配置模板（占位值 + 每个字段的注释）
├── .gitignore / .gitattributes        # [入库] .gitattributes 强制 .githooks/** 用 LF
├── .githooks/
│   └── pre-push                       # [入库] 推送前自动跑契约验收（需执行一次 git config core.hooksPath .githooks）
│
├── config/                            # 配置层：所有模块的地基
│   ├── __init__.py
│   ├── settings.py                    # pydantic-settings → 全局单例 settings（优先级：环境变量 > .env > 默认值）
│   ├── paths.py                       # 路径常量（PROJECT_ROOT 锚定，不依赖运行时 CWD）
│   └── port_check.py                  # 启动端口自检（仅标准库；治"服务显示启动成功但 0 请求"的 Windows 端口抢占）
│
├── crawler/                           # 离线数据采集：独立数据管道，故放根目录、与 src 应用解耦（见 §2.2.1）
│   ├── __init__.py
│   ├── medical_crawler.py             # 爬取：robots.txt 在线解析 + 3~5s 随机延时 + UA 池 + tenacity 重试
│   ├── data_processor.py              # 清洗去重 → JSON（程序用）+ Excel（人工看）
│   └── README.md                      # 用法：python crawler/medical_crawler.py [--max N] [--reset]
│
├── src/                               # 应用核心（8 个子包）
│   ├── __init__.py
│   │
│   ├── common/                        # 公共工具（7 个模块，逐个在对应课时引入，别在 Day 1 一次生成）
│   │   ├── __init__.py
│   │   ├── logger.py                  # setup_logger(name, log_file)：文件 + 控制台双输出
│   │   ├── llm.py                     # get_llm() 单例；get_extract_llm() 可把抽取切到本地 vLLM 微调模型
│   │   ├── utils.py                   # clean_text / retry_on_error / timer
│   │   ├── tokens.py                  # token 计数（tiktoken，不可用则降级启发式）+ 预算裁剪
│   │   ├── memory.py                  # SessionMemory（早期内存方案，请求路径已不用）+ format_history（仍被 Agent 用）
│   │   ├── observe.py                 # tracing：Langfuse > LangSmith > no-op，env 门控
│   │   └── answer_cache.py            # L1 Redis 精确 + L2 语义缓存（三把钥匙都含 user_id）
│   │
│   ├── extraction/                    # LLM 实体/关系抽取（Day 2）
│   │   ├── __init__.py
│   │   ├── schemas.py                 # Pydantic 模型 + DiseaseExtractionResult 聚合体
│   │   ├── disease_extractor.py       # extract() → DiseaseExtractionResult（实体 + 关系，temperature=0 最稳）
│   │   ├── symptom_extractor.py       # extract() → list[SymptomEntity]
│   │   ├── drug_extractor.py          # extract() → list[MedicationEntity]
│   │   └── department_extractor.py    # extract() → list[DepartmentEntity]
│   │
│   ├── knowledge_graph/               # 图谱 Schema / 建图 / 查询 / 校验 / Text2Cypher
│   │   ├── __init__.py
│   │   ├── schema.py                  # ★ 8 节点 + 12 关系 + 值域枚举的唯一主人（R1）；含 LLM 用的 schema 文本生成
│   │   ├── schema_manager.py          # 约束与索引的初始化 / 校验
│   │   ├── graph_builder.py           # MERGE 幂等建图：python -m src.knowledge_graph.graph_builder {clear|build}
│   │   ├── graph_queries.py           # 只读查询封装（Agent 与 @tool 共用）
│   │   ├── graph_validator.py         # 质量校验：连接 / 计数 / 孤立节点 / 疾病完整性
│   │   └── text2cypher.py             # 自然语言 → Cypher，只读事务 + 记录数上限 + 写操作防护
│   │
│   ├── vector_store/                  # 向量检索（就一个实现文件，别再拆）
│   │   ├── __init__.py
│   │   └── search.py                  # VectorRetriever：FAISS 语义召回 + Neo4j 精确匹配合并；link_entities() 实体链接；
│   │                                  #   get_shared_retriever() 进程级单例。⚠ BGE-M3 懒加载且只允许本地目录（禁联网回退）
│   │
│   ├── agents/                        # LangGraph 多 Agent：9 个节点 = graph.py 的 9 个 add_node
│   │   ├── __init__.py
│   │   ├── state.py                   # AgentState（TypedDict，10 个 operator.add 累加字段）+ IntentType（5 类）
│   │   ├── graph.py                   # build_graph()/app、run()/run_stream()、route_by_intent/route_after_symptom
│   │   ├── checkpointer.py            # SqliteSaver + build_thread_id()（thread_id 的唯一主人，契约见 §13）
│   │   ├── tools.py                   # 4 个 @tool，只被 medical_knowledge 的 create_react_agent 调用
│   │   ├── intent_classifier.py       # 意图识别（LLM · 结构化输出）
│   │   ├── symptom_detector.py        # 症状检测（LLM · 结构化输出）
│   │   ├── department_recommender.py  # 科室推荐（图谱候选仅作参考 → LLM 择优定案 → 回退图谱 → 追问）
│   │   ├── medication_advisor.py      # 用药建议（图谱药物仅作参考 → LLM 按医疗红线取舍）
│   │   ├── medical_knowledge.py       # 医学知识（强制检索打底 + 可选 ReAct 多跳）
│   │   ├── pre_visit_advisor.py       # 就医指导（LLM · 结构化输出 + 规则兜底）
│   │   ├── safety_checker.py          # 安全检查（本节点零 LLM：强制免责声明 / 急症置顶 / 特殊人群）
│   │   ├── answer_fusion.py           # 答案融合（确定性拼装，不调 LLM）
│   │   └── general_chat.py            # 通用对话（直连 END，不经安全检查——刻意取舍，见 §6）
│   │
│   ├── history/                       # 对话历史冷热分层 ⚠ 本目录无 __init__.py（隐式命名空间包，见下方"待清理"）
│   │   ├── store.py                   # Redis 热缓存(12 轮/TTL 24h) + MySQL 事实源；双写先 MySQL；load_context 三级命中
│   │   └── summarizer.py              # 窗口外旧轮增量摘要（水位 conversations.summary_msg_id；安全信息强制保留）
│   │
│   ├── auth/                          # 用户认证与会话归属 ⚠ 本目录无 __init__.py
│   │   ├── security.py                # bcrypt（SHA-256 预哈希，rounds=12）+ PyJWT（HS256）
│   │   ├── store.py                   # 用户表与会话归属
│   │   └── deps.py                    # get_current_user + 归属裁决（越权统一转 404，不泄露存在性）
│   │
│   └── api/                           # FastAPI：单进程同时托管 API 与前端静态文件
│       ├── __init__.py
│       ├── main.py                    # 应用装配 / lifespan 预热 / CORS 白名单 / MAX_CONCURRENCY / /assets 挂载 + SPA 兜底路由
│       ├── routes.py                  # /consult /consult/stream(SSE) /health /stats /conversations*；REQUEST_TIMEOUT=60s
│       ├── auth_routes.py             # /auth/register(201/409) /auth/login(OAuth2 表单) /auth/me
│       └── models.py                  # ★ R3 前后端字段契约的唯一主人（11 个响应字段，含 linked_entities / cached / duration_ms）
│
├── frontend/                          # Vue 3 + Element Plus + Vite（独立 npm 项目）
│   ├── index.html                     # 入口 HTML
│   ├── package.json / package-lock.json
│   ├── vite.config.js                 # @ → src 别名；dev 端口 5173；/api 代理 target 用 127.0.0.1（避开 ::1 解析回退）
│   ├── .gitignore                     # node_modules / dist
│   ├── src/
│   │   ├── main.js                    # 挂载 Element Plus
│   │   ├── App.vue                    # 登录网关（无 token → LoginView）+ 主界面切换
│   │   ├── api/index.js               # axios（baseURL=/api, timeout=65s）+ SSE consultStream + 令牌管理
│   │   ├── utils/markdown.js          # renderMarkdown()：marked + DOMPurify 的唯一渲染入口（不得绕过）
│   │   ├── views/
│   │   │   ├── ChatView.vue           # 聊天主界面 + 侧栏历史 + 系统状态 + 用户卡片 + 欢迎页能力卡片
│   │   │   └── LoginView.vue          # 登录 / 注册
│   │   └── components/
│   │       ├── ChatMessage.vue        # 消息气泡（症状/科室/药物标签、耗时、知识定位）
│   │       └── DisclaimerBanner.vue   # 免责声明横幅（不可关闭）
│   └── dist/                          # [产物] npm run build 输出，由 FastAPI 同源托管
│
├── scripts/                           # 入口脚本（离线流水线 + 验收器）⚠ 无 __init__.py，按文件直接运行
│   ├── run_extraction.py              # LLM 批量抽取 → data/knowledge_graph/*.json（**只有 --resume 才续跑**，否则清空重抽）
│   ├── build_index.py                 # BGE-M3 向量化 → data/indexes/（IndexIVFFlat，nlist=100 / nprobe=10 / 内积）
│   ├── enrich_medications.py          # 药物字段增补（副作用 / 禁忌 / 相互作用）
│   ├── migrate_relation_types.py      # 历史关系名归一（一次性、幂等；不是方向翻转）
│   ├── build_finetune_dataset.py      # 抽取产物 → 微调数据集 → data/finetune/ft_extract.json（Day 7）
│   ├── check_contracts.py             # ★ R1/R3/R4/R5 统一验收器（退出码 0=全绿；--no-db 跳过 R5）
│   ├── integration_test.py            # 端到端 API 冒烟 15 项（健康/图谱/认证 7 项/问诊/越权 4 项/SSE）
│   ├── test_config.py                 # 配置层冒烟
│   ├── start_api.py                   # 一键启动（缺前端自动构建；启动期主线程预加载嵌入栈，治 arrow.dll 崩溃）
│   ├── smoke_test_vllm_extract.py     # 微调模型 vLLM 连通性冒烟
│   ├── e2e_test_extract_via_vllm.py   # 抽取走 vLLM 的端到端验证
│   └── schema.cypher                  # 约束/索引初始化 Cypher（由 schema.py 生成，与之同源）
│
├── evals/                             # 评估体系（Day 4）
│   ├── __init__.py
│   ├── dataset.jsonl                  # [入库] 53 条金标问答（id 前缀 a/m/k/e/g/t）
│   ├── runner.py                      # 双模式：offline graph.run() 测"大脑" / e2e 走 POST /api/consult；硬门禁退出码
│   ├── judge.py                       # LLM-as-a-judge（correctness/completeness/safety/harmlessness）+ RAGAS 包装
│   ├── extract_finetune_vs_cloud.py   # 微调 vs 云端抽取对比（Day 7）
│   ├── ci_github_actions.yml          # GitHub Actions 参考模板（需自行拷到 .github/workflows/ 才生效）
│   ├── last_report*.md                # [产物] 评估报告；last_report.md = 最新一次
│   └── results/                       # [产物] 对比结果 JSON
│
├── data/                              # [全部产物，不入库] R5：数据是代码的构建产物，契约变更只做 clear + build 重建
│   ├── raw/                           # 爬虫原始输出（diseases.json + .crawl_checkpoint.json）
│   ├── processed/                     # 清洗后（diseases.json / diseases.xlsx）
│   ├── knowledge_graph/               # 抽取产物 entities.json / relations.json（+ .extraction_checkpoint.json）
│   ├── indexes/                       # faiss.index + entities.json（索引 ID → 实体映射，实体链接的数据源）
│   ├── checkpoints/                   # agent_checkpoints.sqlite（执行态快照，非事实源，可随时删）
│   ├── cache/                         # semantic_cache.json / .npz（L2 语义缓存）
│   └── finetune/                      # ft_extract.json（微调数据集）
│
├── models/                            # [产物] bge-m3/（约 2.2 GB，手动下载，见 §4 技术选型）
└── logs/                              # [产物] 分模块日志（api / agents / extraction / cache / observe / history …）
```

**旧版这一节列过、但仓库里其实不存在的东西**（设计期遗留，本节已按真实文件重写）：
`tests/` 整个目录（无 pytest 套件，测试落地见 §7 的三层）、`src/agents/base_agent.py`（各 Agent 直接是类 + 模块级单例，没有公共基类）、
`src/common/neo4j_client.py` 与 `src/common/embedding_model.py`（Neo4j 连接没有独立客户端模块，由 `knowledge_graph/` 的
`graph_queries.py` / `graph_builder.py` / `graph_validator.py` / `schema_manager.py` 各自持 driver；
嵌入模型也没有独立模块，由 `vector_store/search.py` 懒加载 + `get_shared_retriever()` 进程级单例）、`src/vector_store/vector_store.py` 与 `index_builder.py`
（检索只有 `search.py` 一个实现，索引构建在 `scripts/build_index.py`）、`scripts/run_crawler.py`（直接 `python crawler/medical_crawler.py`）、
`scripts/build_graph.py`（`python -m src.knowledge_graph.graph_builder`）、`scripts/validate_environment.py`（环境验证在根目录 `validate_env.py`）。

**⚠ 这些遗留名字在本文件后面的章节里还留着**（§2.2 示例代码、§3 数据流、§5 接口设计、§6 安全设计中的
"核心类/示意代码"是**设计期草图**，不是现状实现）。读到它们时按下表换算，**否则照抄会 import 到不存在的模块**：

| 后文草图里的写法 | 仓库里的真实现 |
|---|---|
| `from src.common.neo4j_client import neo4j_client` / `neo4j_client.run(...)` | 无此模块。用 `src/knowledge_graph/graph_queries.py` 的 `GraphQueries()`（只读查询，Agent/工具共用）或 `graph_builder.py` 自建 driver |
| `from src.common.embedding_model import embedding_model` | 无此模块。用 `src/vector_store/search.py` 的 `get_shared_retriever()._get_embedding_model()`（BGE-M3 懒加载、禁联网回退） |
| `class VectorStore` / `vector_store/vector_store.py` | 实为 `src/vector_store/search.py` 的 `VectorRetriever`（FAISS + Neo4j 混合、`link_entities()`） |
| `vector_store/index_builder.py` | 实为 `scripts/build_index.py`（IndexIVFFlat，nlist=100 / nprobe=10 / 内积） |
| `class GraphBuilder` | 实为 `src/knowledge_graph/graph_builder.py` 的 `KnowledgeGraphBuilder` |
| `create_indexes()`（草图里手写索引语句） | 实为 `schema.py` 的 `generate_constraint_cypher()` / `generate_index_cypher()` 生成 + `schema_manager.py` 执行（R1：不手写） |
| `(d1:Drug)` | 节点 label 是 **`Medication`**（`NodeType` 无 `Drug`） |
| `-[:RELATED_TO {relation_type:...}]->` | **不在 12 类关系契约内**，`check_contracts.py` 的 R5 会判为违规。疾病间关联语义只作为抽取字段 `DiseaseRelationType`（complication/comorbidity/causal/similar）存在，不落图关系 |
| `tests/unit/test_extractor.py` 等 pytest 片段 | 无 `tests/`。见 §7 三层验收表 |

**一处待清理的不一致（如实记录，别当已解决）**：`src/history/`、`src/auth/`、`scripts/` 三个目录没有 `__init__.py`，
靠隐式命名空间包工作——功能上没问题，但与 `src/` 其余子包风格不一致，补空 `__init__.py` 是零风险改动。

### 2.2 项目结构设计说明

#### 2.2.1 为什么爬虫模块在根目录？

**设计决策**：`crawler/` 放在项目根目录，而不是 `src/crawler/`

**原因**：

1. **职责分离**：爬虫是**数据管道工具**，不是应用的一部分
   - 爬虫独立运行，不依赖 FastAPI 或 Agent 系统
   - 爬虫的输出（data/raw/）是其他模块的输入
2. **运行独立性**：爬虫可以单独运行，无需启动整个应用
3. **符合数据工程实践**：在数据科学项目中，数据采集通常与数据处理、模型训练分离

**对比**：

```
❌ 不推荐：src/crawler/          # 爬虫混在应用代码中
✅ 推荐：  crawler/              # 爬虫独立，职责清晰
```

#### 2.2.2 为什么需要统一日志模块？

**设计决策**：添加 `src/common/logger.py` 统一日志配置

**原因**：

1. **避免重复配置**：每个模块都配置 logging 会导致日志格式不一致
2. **集中管理**：所有日志输出到 `logs/` 目录，便于查看和排查问题
3. **多输出目标**：同时输出到文件和控制台，文件用于持久化，控制台用于实时查看
4. **日志轮转**：自动按日期或大小轮转日志文件，避免单个文件过大

**使用示例**：

```python
# src/common/logger.py
import logging
from pathlib import Path
from config.paths import LOGS_DIR

def setup_logger(name: str, log_file: str = None) -> logging.Logger:
    """配置日志记录器"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
  
    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(console_handler)
  
    # 文件输出（可选）
    if log_file:
        log_path = LOGS_DIR / log_file
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
        )
        logger.addHandler(file_handler)
  
    return logger

# 使用方式
from src.common.logger import setup_logger
logger = setup_logger("crawler", "crawler.log")
logger.info("Starting crawler...")
```

#### 2.2.3 为什么用 data_processor.py 而不是 data_parser.py？

**设计决策**：使用 `data_processor.py` 而不是 `data_parser.py`

**命名对比**：

| 名称                    | 含义                                 | 适用场景                  |
| ----------------------- | ------------------------------------ | ------------------------- |
| `parser`（解析器）    | 从一种格式转换为另一种格式           | HTML → JSON, CSV → Dict |
| `processor`（处理器） | 更广泛，包含清洗、去重、格式化、验证 | 数据清洗、标准化、统计    |

**实际代码做的工作**：

```python
class DataProcessor:
    def remove_duplicates(self):    # 去重
    def clean_text(self):           # 文本清洗
    def clean_records(self):        # 记录清洗
    def generate_statistics(self):  # 统计分析
    def save_json(self):            # 保存 JSON
    def save_excel(self):           # 保存 Excel
```

**结论**：`processor` 更准确，因为不仅仅是解析，还包含清洗、去重、统计等处理逻辑

#### 2.2.4 为什么需要 config/paths.py？

**设计决策**：添加 `config/paths.py` 集中管理路径配置

**原因**：

1. **避免硬编码**：路径分散在各个模块中，修改困难
2. **跨平台兼容**：使用 `pathlib.Path` 自动处理 Windows/Mac/Linux 路径差异
3. **易于测试**：测试时可以用临时目录替换路径

**示例**：

```python
# config/paths.py
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent

# 数据目录
DATA_DIR = PROJECT_ROOT / "data"
DATA_RAW_DIR = DATA_DIR / "raw"
DATA_PROCESSED_DIR = DATA_DIR / "processed"
DATA_INDEXES_DIR = DATA_DIR / "indexes"

# 模型目录
MODELS_DIR = PROJECT_ROOT / "models"
BGE_M3_DIR = MODELS_DIR / "bge-m3"

# 日志目录
LOGS_DIR = PROJECT_ROOT / "logs"

# 创建目录（如果不存在）
for dir_path in [DATA_RAW_DIR, DATA_PROCESSED_DIR, DATA_INDEXES_DIR, LOGS_DIR]:
    dir_path.mkdir(parents=True, exist_ok=True)
```

**使用方式**：

```python
# 在任何模块中使用
from config.paths import DATA_RAW_DIR, LOGS_DIR

# 读取数据
input_file = DATA_RAW_DIR / "diseases.json"

# 写入日志
log_file = LOGS_DIR / "crawler.log"
```

#### 2.2.5 其他改进

| 改进点                               | 说明                                              |
| ------------------------------------ | ------------------------------------------------- |
| **添加 `.gitignore`**        | 排除敏感文件（.env）、大文件（models/）、临时文件 |
| **添加 `logs/` 目录**        | 集中存储日志文件，便于排查问题                    |
| **添加 `crawler/README.md`** | 爬虫使用说明，降低使用门槛                        |
| **添加 `graph_queries.py`**  | 封装常用 Neo4j 查询，供 Agent 调用                |
| **添加 `utils.py`**          | 通用工具函数（如文本清洗、格式转换）              |
| **完善 `data/` 子目录**      | 明确 raw/processed/indexes 的用途和文件结构       |

### 2.3 核心模块详细说明

#### 模块 1：公共模块 (`src/common/`)

**职责**：提供全局共享的基础功能

**核心类**：

```python
# config/settings.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    """全局配置类"""
    # LLM 配置
    model_base_url: str
    model_api_key: str
    model_name: str
  
    # Neo4j 配置
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
  
    # 嵌入模型配置
    embedding_model_path: str
  
    # 日志配置
    log_level: str = "INFO"
  
    class Config:
        env_file = ".env"

settings = Settings()
```

```python
# common/llm.py
from langchain_openai import ChatOpenAI
from config.settings import settings

def get_llm() -> ChatOpenAI:
    """获取 LLM 客户端"""
    return ChatOpenAI(
        api_key=settings.model_api_key,
        base_url=settings.model_base_url,
        model=settings.model_name,
        temperature=0.7
    )
```

> ⚠ **设计期草图，非现状实现**：下面这段代码里的模块/类名在当前仓库中并不存在，照抄会 `ImportError`。真实现与对应关系见 §2.1 末尾的换算表；本节只保留其**设计意图**，不作为可执行参考。

```python
# common/neo4j_client.py
from neo4j import GraphDatabase
from config.settings import settings

class Neo4jClient:
    """Neo4j 数据库客户端（单例模式）"""
    _instance = None
  
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.driver = GraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_password)
            )
        return cls._instance
  
    def run(self, query: str, params: dict = None):
        """执行 Cypher 查询"""
        with self.driver.session() as session:
            result = session.run(query, params or {})
            return [record.data() for record in result]
  
    def close(self):
        """关闭连接"""
        self.driver.close()

neo4j_client = Neo4jClient()
```

```python
# common/embedding_model.py
from sentence_transformers import SentenceTransformer
from config.settings import settings

class EmbeddingModel:
    """向量嵌入模型"""
    _instance = None
  
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.model = SentenceTransformer(settings.embedding_model_path)
        return cls._instance
  
    def encode(self, texts: list[str]) -> list[list[float]]:
        """文本向量化"""
        return self.model.encode(texts)

embedding_model = EmbeddingModel()
```

---

#### 模块 2：数据采集模块 (`crawler/`)

**职责**：从医疗网站采集原始数据，并进行清洗处理

**核心类**：

```python
# crawler/medical_crawler.py
import requests
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
from urllib.robotparser import RobotFileParser
from tenacity import retry, stop_after_attempt, wait_exponential
import time
import random
import logging

# 随机 User-Agent 池
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
]

class RobotsChecker:
    """检查 robots.txt 合规性"""
  
    def __init__(self):
        self.parsers = {}
  
    def is_allowed(self, url: str, user_agent: str = "*") -> bool:
        """检查 URL 是否被 robots.txt 允许"""
        # 实现 robots.txt 检查逻辑
        pass

class MedicalCrawler:
    """医疗网站爬虫（支持多数据源）"""
  
    def __init__(self):
        self.session = requests.Session()
        self.robots_checker = RobotsChecker()
        self.crawled_urls = set()
        self.diseases = []
  
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    def _fetch(self, url: str) -> Optional[str]:
        """获取网页内容（带重试机制）"""
        # 检查 robots.txt
        if not self.robots_checker.is_allowed(url):
            logging.warning(f"[BLOCKED] robots.txt disallows: {url}")
            return None
  
        # 随机 User-Agent
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        response = self.session.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.text
  
    def crawl_haodf_list(self) -> List[str]:
        """爬取好大夫在线疾病列表"""
        url = "https://www.haodf.com/jibing/"
        # 解析列表页，提取疾病详情页 URL
        pass
  
    def crawl_haodf_detail(self, url: str) -> Dict:
        """爬取好大夫在线疾病详情"""
        html = self._fetch(url)
        if not html:
            return None
  
        soup = BeautifulSoup(html, "html.parser")
        disease = {
            "name": soup.find("h1").get_text(strip=True),
            "description": "",
            "symptoms": [],
            "department": "",
            "treatment": "",
            "source": "haodf.com",
            "url": url
        }
        # 解析详情页，提取各字段
        return disease
  
    def run(self, max_diseases: int = 500, use_backup: bool = True, use_predefined: bool = True):
        """运行爬虫"""
        # 1. 爬取好大夫在线（预定义 500 种常见疾病种子列表 + 列表页发现）
        # 2. 如果数据不足，爬取寻医问药（备用）
        # 3. 保存结果到 data/raw/diseases.json
        pass
```

```python
# crawler/data_processor.py
import json
import pandas as pd
from pathlib import Path
from typing import Dict, List
import logging

from config.paths import DATA_RAW_DIR, DATA_PROCESSED_DIR

class DataProcessor:
    """数据清洗与处理"""
  
    def __init__(self):
        self.raw_data = []
        self.cleaned_data = []
        self.stats = {
            "total_raw": 0,
            "duplicates_removed": 0,
            "empty_removed": 0,
            "cleaned": 0,
        }
  
    def load_raw_data(self) -> bool:
        """加载原始数据"""
        input_file = DATA_RAW_DIR / "diseases.json"
        if not input_file.exists():
            logging.error(f"Raw data file not found: {input_file}")
            return False
  
        with open(input_file, "r", encoding="utf-8") as f:
            data = json.load(f)
  
        self.raw_data = data.get("diseases", [])
        self.stats["total_raw"] = len(self.raw_data)
        return True
  
    def remove_duplicates(self):
        """去除重复数据（按疾病名称）"""
        seen_names = set()
        unique_data = []
  
        for disease in self.raw_data:
            name = disease.get("name", "").strip()
            if not name or name in seen_names:
                self.stats["duplicates_removed"] += 1
                continue
            seen_names.add(name)
            unique_data.append(disease)
  
        self.raw_data = unique_data
  
    def clean_text(self, text: str) -> str:
        """清洗文本：去除多余空格和特殊字符"""
        if not text:
            return ""
        text = " ".join(text.split())
        # 去除特殊字符，保留中文、英文、数字、标点
        import re
        text = re.sub(r'[^\w\s一-鿿，。、；：！？""''（）《》]+', '', text)
        return text.strip()
  
    def generate_statistics(self) -> Dict:
        """生成数据统计"""
        from collections import Counter
  
        # 按来源统计
        source_counter = Counter(d["source"] for d in self.cleaned_data)
  
        # 统计症状
        all_symptoms = []
        for d in self.cleaned_data:
            all_symptoms.extend(d.get("symptoms", []))
        symptom_counter = Counter(all_symptoms)
  
        # 字段覆盖率
        fields = ["description", "symptoms", "department", "treatment"]
        coverage = {}
        for field in fields:
            count = sum(1 for d in self.cleaned_data if d.get(field))
            coverage[field] = f"{count}/{len(self.cleaned_data)}"
  
        return {
            "total": len(self.cleaned_data),
            "by_source": dict(source_counter),
            "unique_symptoms": len(symptom_counter),
            "field_coverage": coverage,
        }
  
    def save_json(self):
        """保存为 JSON 格式"""
        output_file = DATA_PROCESSED_DIR / "diseases.json"
        output = {
            "metadata": {
                "total_diseases": len(self.cleaned_data),
                "stats": self.stats,
            },
            "diseases": self.cleaned_data,
        }
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
  
    def save_excel(self):
        """保存为 Excel 格式（人工查看）"""
        output_file = DATA_PROCESSED_DIR / "diseases.xlsx"
        df = pd.DataFrame(self.cleaned_data)
        # 转换列表字段为字符串
        df["symptoms"] = df["symptoms"].apply(lambda x: "、".join(x) if x else "")
        df.to_excel(output_file, index=False, sheet_name="Diseases")
  
    def process(self) -> bool:
        """运行完整的数据处理流程"""
        # 1. 加载原始数据
        # 2. 去重
        # 3. 清洗
        # 4. 统计
        # 5. 保存 JSON + Excel
        pass
```

**数据格式**：

```json
{
  "name": "高血压",
  "description": "高血压是指以动脉血压持续升高为特征的慢性病...",
  "symptoms": ["头痛", "头晕", "心悸", "视力模糊"],
  "department": "心血管内科",
  "treatment": "药物治疗、生活方式调整",
  "source": "haodf.com",
  "url": "https://www.haodf.com/jibing/gaoxueya.htm",
  "crawled_at": "2026-07-24T10:30:00"
}
```

**使用方法**：

```bash
# 1. 运行爬虫（默认 --max 500，寻医问药备用源默认开启）
python crawler/medical_crawler.py

# 2. 清洗数据
python crawler/data_processor.py

# 3. 查看结果
# data/processed/diseases.json  (JSON 格式，程序使用)
# data/processed/diseases.xlsx  (Excel 格式，人工查看)
```

---

#### 模块 3：实体关系抽取模块 (`src/extraction/`)

**职责**：使用 LLM 从原始文本中提取结构化实体和关系

**核心类**：

```python
# extraction/schemas/disease.py
from pydantic import BaseModel, Field
from typing import List

class DiseaseEntity(BaseModel):
    """疾病实体"""
    name: str = Field(description="疾病名称")
    symptoms: List[str] = Field(description="症状列表")
    causes: List[str] = Field(description="病因列表")
    treatments: List[str] = Field(description="治疗方法")
    departments: List[str] = Field(description="就诊科室")
    examinations: List[str] = Field(description="检查项目")
    body_parts: List[str] = Field(description="影响的身体部位")
    description: str = Field(description="疾病描述")
```

```python
# extraction/disease_extractor.py
from langchain.prompts import ChatPromptTemplate
from langchain.output_parsers import PydanticOutputParser
from src.common.llm import get_llm
from src.extraction.schemas.disease import DiseaseEntity

class DiseaseExtractor:
    """疾病实体抽取器"""
  
    def __init__(self):
        self.llm = get_llm()
        self.parser = PydanticOutputParser(pydantic_object=DiseaseEntity)
  
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个医学信息抽取专家。请从以下文本中提取疾病信息，并按照指定格式输出。

输出格式：
{format_instructions}

要求：
1. 疾病名称使用标准医学术语
2. 症状列表要完整
3. 科室名称使用标准科室名称（如"心血管内科"而非"心内科"）
4. 如果没有相关信息，对应字段返回空列表"""),
            ("user", "{text}")
        ])
  
    def extract(self, text: str) -> DiseaseEntity:
        """从文本中抽取疾病实体"""
        formatted_prompt = self.prompt.format(
            text=text,
            format_instructions=self.parser.get_format_instructions()
        )
        response = self.llm.invoke(formatted_prompt)
        return self.parser.parse(response.content)
```

**提示词模板**：

```python
DISEASE_EXTRACTION_PROMPT = """
你是一个医学信息抽取专家。请从以下文本中提取疾病信息，并按照指定格式输出。

文本内容：
{text}

输出格式：
{format_instructions}

要求：
1. 疾病名称使用标准医学术语
2. 症状列表要完整
3. 科室名称使用标准科室名称（如"心血管内科"而非"心内科"）
4. 如果没有相关信息，对应字段返回空列表
"""
```

---

#### 模块 4：知识图谱模块 (`src/knowledge_graph/`)

**职责**：将结构化数据导入 Neo4j，构建医疗知识图谱

**Neo4j Schema 设计**：

> ★ **唯一事实来源是 `src/knowledge_graph/schema.py`**（`NodeType` / `RelationType` 两个枚举）。
> 下面两块只是给人看的可读示意，与 `schema.py` 不一致时**以 `schema.py` 为准**（R1）；
> 新增/改名一律先改 `schema.py`，再回来同步这一节，并跑 `python scripts/check_contracts.py --r1 --r5`。

**节点类型（8 种）**：

```cypher
// 疾病节点（唯一约束 name；索引 name, icd_code）
(d:Disease {name: "高血压", description: "血压长期高于正常水平...", icd_code: "I10",
            aliases: ["血压高"], created_at: datetime(), updated_at: datetime()})

// 症状节点（唯一约束 name；索引 name, severity）
(s:Symptom {name: "头痛", description: "头部疼痛...", severity: "medium", duration: "数天"})

// 药物节点 ——⚠ label 是 Medication，不是 Drug
(m:Medication {name: "氨氯地平", category: "钙通道阻滞剂", side_effects: ["脚踝水肿", "面部潮红"],
               contraindications: ["严重低血压"], dosage: "每日一次，一次 5mg"})

// 科室节点（唯一约束 name；索引 name）
(dep:Department {name: "心血管内科", description: "诊治心脏和血管疾病",
                 floor: "门诊 3 层", parent_department: "内科"})

// 身体部位节点（唯一约束 name；索引 name, system）
(bp:BodyPart {name: "心脏", description: "循环系统的核心器官", system: "循环系统"})

// 检查节点（唯一约束 name；索引 name, type）
(e:Examination {name: "血压测量", type: "实验室检查", purpose: "检测血压水平",
                preparation: "安静休息 5 分钟后测量"})

// 食物节点（唯一约束 name；索引 name, type）—— type 取值：推荐 / 禁忌
(f:Food {name: "低盐饮食", type: "推荐", description: "每日食盐不超过 5 克"})

// 注意事项节点 ——⚠ 主键属性是 content（没有 name），当前不设唯一约束，只索引 category
(pc:Precaution {content: "定期监测血压", category: "生活方式", importance: "high"})
```

> **另有 2 个不在 `NodeType` 里的辅助 label**：`graph_builder` 备有 `Treatment` 建边模板（现网数据里确实存在，Day 2 实测 1886 个节点），
> `MedicalConcept` 出现在 §0 R5 的允许集合里。它们**不是契约节点类型**——`schema.py` 不定义、R5 放行。
> 讲课时别说成"8 种节点之一"；当前抽取数据只覆盖 8 种契约节点中的 6 种（`Food` / `Precaution` 无数据）。

**关系类型（12 种）**：

```cypher
// 疾病-症状
(d:Disease)-[:HAS_SYMPTOM {frequency: "common"}]->(s:Symptom)

// 症状-疾病（反向）——R6 历史例外 ①
(s:Symptom)-[:MAY_INDICATE {probability: "medium"}]->(d:Disease)

// 疾病-科室 ——⚠ 是 BELONGS_TO_DEPARTMENT（带 S），不是 BELONG_TO_DEPARTMENT
(d:Disease)-[:BELONGS_TO_DEPARTMENT {priority: 1}]->(dep:Department)   // priority: 1=首选, 2=次选

// 疾病-药物 ——⚠ 是 TREATED_BY_MEDICATION，不是 TREATED_BY_DRUG
(d:Disease)-[:TREATED_BY_MEDICATION {evidence_level: "A", is_first_line: true}]->(m:Medication)

// 药物-疾病（反向）——R6 历史例外 ②
(m:Medication)-[:TREATS_DISEASE]->(d:Disease)

// 药物-副作用
(m:Medication)-[:HAS_SIDE_EFFECT {frequency: "common"}]->(s:Symptom)

// 药物-药物相互作用
(m:Medication)-[:INTERACTS_WITH {severity: "high", description: "增加出血风险"}]->(m2:Medication)

// 疾病-检查 —— necessity: required / recommended / optional
(d:Disease)-[:NEEDS_EXAMINATION {necessity: "required"}]->(e:Examination)

// 疾病-身体部位
(d:Disease)-[:AFFECTS_BODY_PART]->(bp:BodyPart)

// 科室-疾病（反向）——R6 历史例外 ③
(dep:Department)-[:HANDLES_DISEASE]->(d:Disease)

// 疾病-饮食 —— recommendation_type: recommended / avoid
(d:Disease)-[:RECOMMENDS_FOOD {recommendation_type: "recommended"}]->(f:Food)

// 疾病-注意事项
(d:Disease)-[:HAS_PRECAUTION {importance: "high"}]->(pc:Precaution)
```

> **上面 12 条就是全部合法关系名。**旧版本节曾把 `FOR_DISEASE`（治疗方案-疾病）与 `RELATED_TO`（疾病-疾病）列为示例，
> 这两个**都不在契约内，照写会被 `check_contracts.py` 的 R5 判为违规**：
>
> - 「治疗方案」只作为 `Treatment` 辅助节点存在，不落图关系；
> - 疾病间关联（并发症 / 合并症 / 因果 / 相似）只是**抽取字段** `DiseaseRelationType` 的取值，不是图关系类型。
>   `schema.py` 里它原名也叫 `RelationType`，因与图关系契约重名才改名（R1 名字唯一主人）。
>
> **现网实况（2026-10 实测）**：Neo4j 里只有 **5 种正向关系**有数据——
> `HAS_SYMPTOM 3171` / `TREATED_BY_MEDICATION 2530` / `AFFECTS_BODY_PART 2147` / `BELONGS_TO_DEPARTMENT 657` / `NEEDS_EXAMINATION 178`；
> 三条反向历史例外（`MAY_INDICATE` / `TREATS_DISEASE` / `HANDLES_DISEASE`）当前**各 0 条**，
> 因为现行抽取只产正向关系（见 `scripts/run_extraction.py` 的 `serialize_relations()`）。反向查询用 Cypher 反向遍历表达，见 §0 R6。

> ⚠ **设计期草图，非现状实现**：下面这段代码里的模块/类名在当前仓库中并不存在，照抄会 `ImportError`。真实现与对应关系见 §2.1 末尾的换算表；本节只保留其**设计意图**，不作为可执行参考。

**核心类**：

```python
# knowledge_graph/graph_builder.py
from src.knowledge_graph.schema_manager import SchemaManager   # 约束/索引由它统一管理，别手写语句
from neo4j import GraphDatabase
from config.settings import settings
from typing import Dict

class KnowledgeGraphBuilder:
    """知识图谱构建器（真实类名是 KnowledgeGraphBuilder，不是草图里的 GraphBuilder）"""

    def __init__(self):
        self.driver = GraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))

    def create_indexes(self):
        """创建约束与索引：委托 SchemaManager，不手写 Cypher（R1）。

        ⚠ 语句由 schema.py 的 generate_constraint_cypher() / generate_index_cypher()
        从 NODE_SCHEMAS 生成，再由 schema_manager.initialize_schema() 执行——
        改一处 schema，约束索引自动跟上，不会出现「8 种节点只建了 3 个约束」。
        （SchemaManager() 无参构造，内部自建 driver。）
        """
        return SchemaManager().initialize_schema()

    def merge_disease_node(self, disease_data: Dict):
        """创建或更新疾病节点"""
        query = """
        MERGE (d:Disease {name: $name})
        ON CREATE SET 
            d.description = $description,
            d.created_at = timestamp()
        ON MATCH SET
            d.description = $description,
            d.updated_at = timestamp()
        """
        neo4j_client.run(query, disease_data)
  
    def create_relationship(self, start_node: str, rel_type: str, end_node: str, properties: Dict = None):
        """创建关系"""
        query = f"""
        MATCH (a {{name: $start_name}})
        MATCH (b {{name: $end_name}})
        MERGE (a)-[r:{rel_type}]->(b)
        """
        if properties:
            set_clause = ", ".join([f"r.{k} = ${k}" for k in properties.keys()])
            query += f" ON CREATE SET {set_clause}"
  
        params = {
            "start_name": start_node,
            "end_name": end_node,
            **(properties or {})
        }
        neo4j_client.run(query, params)
```

---

#### 模块 5：向量检索模块 (`src/vector_store/`)

**职责**：使用 FAISS 实现语义搜索

> ⚠ **设计期草图，非现状实现**：下面这段代码里的模块/类名在当前仓库中并不存在，照抄会 `ImportError`。真实现与对应关系见 §2.1 末尾的换算表；本节只保留其**设计意图**，不作为可执行参考。

**核心类**：

```python
# vector_store/vector_store.py
import faiss
import numpy as np
from src.common.embedding_model import embedding_model
import json

class VectorStore:
    """向量存储"""
  
    def __init__(self):
        self.index = None
        self.entities = []  # 存储实体名称
  
    def build_index(self, entities: list[dict]):
        """构建 FAISS 索引"""
        # entities: [{"name": "高血压", "type": "Disease", "description": "..."}]
        texts = [f"{e['name']} {e.get('description', '')}" for e in entities]
        embeddings = embedding_model.encode(texts)
  
        # 构建 FAISS 索引
        dimension = embeddings.shape[1]
        self.index = faiss.IndexFlatL2(dimension)
        self.index.add(np.array(embeddings))
        self.entities = entities
  
    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """语义搜索"""
        query_embedding = embedding_model.encode([query])
        distances, indices = self.index.search(np.array(query_embedding), top_k)
  
        results = []
        for idx, distance in zip(indices[0], distances[0]):
            if idx < len(self.entities):
                entity = self.entities[idx].copy()
                entity["score"] = float(1 / (1 + distance))  # 转换为相似度分数
                results.append(entity)
  
        return results
  
    def save_index(self, path: str):
        """保存索引到文件"""
        faiss.write_index(self.index, f"{path}/faiss.index")
        with open(f"{path}/entities.json", "w", encoding="utf-8") as f:
            json.dump(self.entities, f, ensure_ascii=False, indent=2)
  
    def load_index(self, path: str):
        """从文件加载索引"""
        self.index = faiss.read_index(f"{path}/faiss.index")
        with open(f"{path}/entities.json", "r", encoding="utf-8") as f:
            self.entities = json.load(f)
```

---

#### 模块 6：多 Agent 系统模块 (`src/agents/`)

**职责**：使用 LangGraph 编排多个 Agent，实现智能问答

**状态定义**：

```python
# agents/state.py
from typing import TypedDict, List, Optional

class AgentState(TypedDict):
    """Agent 状态（与当前 src/agents/state.py 一致；V1 稿的 user_id/session_id 已移除）"""
    query: str        # 本轮用户原始问题
    history: list     # 多轮上下文：API 层按 session 从历史存储注入（含归属校验与 token 预算）
  
    # 意图识别结果
    intent: Optional[str]  # "appointment" | "treatment" | "knowledge" | "other"
  
    # 症状检测结果
    symptoms: Optional[List[str]]
    severity: Optional[str]  # "low" | "medium" | "high"
    is_emergency: Optional[bool]
  
    # 科室推荐结果
    recommended_departments: Optional[List[dict]]
  
    # 用药建议结果
    medication_advice: Optional[dict]
  
    # 医学知识回答
    knowledge_answer: Optional[str]
  
    # 就医指导
    pre_visit_advice: Optional[List[str]]
  
    # 安全检查结果
    disclaimers: Optional[List[str]]
    warnings: Optional[List[str]]
  
    # 最终回答
    final_answer: Optional[str]
  
    # 中间消息（用于调试）
    messages: List[str]
```

**Agent 实现示例**：

```python
# agents/intent/classifier.py
from langchain.prompts import ChatPromptTemplate
from langchain.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field
from src.common.llm import get_llm

class IntentResult(BaseModel):
    """意图识别结果"""
    intent: str = Field(description="意图类型：appointment/treatment/knowledge/other")
    confidence: float = Field(description="置信度：0-1")

class IntentClassifierAgent:
    """意图识别 Agent"""
  
    def __init__(self):
        self.llm = get_llm()
        self.parser = PydanticOutputParser(pydantic_object=IntentResult)
  
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个医疗问诊意图识别专家。请判断用户的意图类型：

意图类型：
1. appointment - 看病挂号：用户描述症状，想知道该挂什么科
2. treatment - 治疗建议：用户询问治疗方法或用药
3. knowledge - 医学知识：用户咨询医学概念或知识
4. other - 其他问题：非医疗相关问题

输出格式：
{format_instructions}"""),
            ("user", "{user_input}")
        ])
  
    def __call__(self, state: dict) -> dict:
        """判断用户意图"""
        formatted_prompt = self.prompt.format(
            user_input=state['user_input'],
            format_instructions=self.parser.get_format_instructions()
        )
        response = self.llm.invoke(formatted_prompt)
        result = self.parser.parse(response.content)
  
        state["intent"] = result.intent
        state["messages"].append(f"意图识别：{result.intent} (置信度: {result.confidence})")
  
        return state
```

```python
# agents/detection/symptom_detector.py
from langchain.prompts import ChatPromptTemplate
from langchain.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field
from typing import List
from src.common.llm import get_llm

class SymptomResult(BaseModel):
    """症状检测结果"""
    symptoms: List[str] = Field(description="症状列表")
    severity: str = Field(description="严重程度：low/medium/high")
    is_emergency: bool = Field(description="是否为急症")

class SymptomDetectorAgent:
    """症状检测 Agent"""
  
    EMERGENCY_SYMPTOMS = [
        "胸痛", "呼吸困难", "意识丧失", "严重出血",
        "剧烈头痛", "突发视力丧失", "癫痫发作"
    ]
  
    def __init__(self):
        self.llm = get_llm()
        self.parser = PydanticOutputParser(pydantic_object=SymptomResult)
  
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个医学症状检测专家。请从用户描述中提取症状信息：

输出格式：
{format_instructions}

急症症状列表：胸痛、呼吸困难、意识丧失、严重出血、剧烈头痛、突发视力丧失、癫痫发作
如果包含急症症状，is_emergency 设为 true"""),
            ("user", "{user_input}")
        ])
  
    def __call__(self, state: dict) -> dict:
        """检测症状"""
        formatted_prompt = self.prompt.format(
            user_input=state['user_input'],
            format_instructions=self.parser.get_format_instructions()
        )
        response = self.llm.invoke(formatted_prompt)
        result = self.parser.parse(response.content)
  
        state["symptoms"] = result.symptoms
        state["severity"] = result.severity
        state["is_emergency"] = result.is_emergency
        state["messages"].append(f"症状检测：{result.symptoms} (严重程度: {result.severity})")
  
        return state
```

**LangGraph 编排**：

```python
# agents/graph.py
from langgraph.graph import StateGraph, END
from src.agents.state import AgentState
from src.agents.intent.classifier import IntentClassifierAgent
from src.agents.detection.symptom_detector import SymptomDetectorAgent
from src.agents.recommendation.department_recommender import DepartmentRecommenderAgent
from src.agents.recommendation.medication_advisor import MedicationAdvisorAgent
from src.agents.knowledge.medical_knowledge import MedicalKnowledgeAgent
from src.agents.guidance.pre_visit_advisor import PreVisitAdvisorAgent
from src.agents.safety.safety_checker import SafetyCheckerAgent
from src.agents.fusion.answer_fusion import AnswerFusionAgent
from src.agents.chat.general_chat import GeneralChatAgent

def create_medical_qa_graph():
    """创建医疗问答状态图"""
    workflow = StateGraph(AgentState)
  
    # 添加节点（Agent）
    workflow.add_node("intent_classifier", IntentClassifierAgent())
    workflow.add_node("symptom_detector", SymptomDetectorAgent())
    workflow.add_node("department_recommender", DepartmentRecommenderAgent())
    workflow.add_node("medication_advisor", MedicationAdvisorAgent())
    workflow.add_node("medical_knowledge", MedicalKnowledgeAgent())
    workflow.add_node("pre_visit_advisor", PreVisitAdvisorAgent())
    workflow.add_node("safety_checker", SafetyCheckerAgent())
    workflow.add_node("answer_fusion", AnswerFusionAgent())
    workflow.add_node("general_chat", GeneralChatAgent())
  
    # 设置入口
    workflow.set_entry_point("intent_classifier")
  
    # 添加条件边（根据意图路由）
    workflow.add_conditional_edges(
        "intent_classifier",
        lambda state: state["intent"],
        {
            "appointment": "symptom_detector",
            "treatment": "symptom_detector",
            "knowledge": "medical_knowledge",
            "other": "general_chat"
        }
    )
  
    # 看病挂号流程
    workflow.add_edge("symptom_detector", "department_recommender")
    workflow.add_edge("department_recommender", "pre_visit_advisor")
    workflow.add_edge("pre_visit_advisor", "safety_checker")
    workflow.add_edge("safety_checker", "answer_fusion")
  
    # 治疗建议流程
    workflow.add_conditional_edges(
        "symptom_detector",
        lambda state: state["intent"],
        {
            "appointment": "department_recommender",
            "treatment": "medication_advisor"
        }
    )
    workflow.add_edge("medication_advisor", "safety_checker")
  
    # 医学知识流程
    workflow.add_edge("medical_knowledge", "safety_checker")
  
    # 通用对话直接结束
    workflow.add_edge("general_chat", END)
  
    # 答案融合后结束
    workflow.add_edge("answer_fusion", END)
  
    # 编译状态图
    app = workflow.compile()
    return app

def run(query: str, history: list | None = None) -> dict:
    """调用 LangGraph AI 系统（与当前 src/agents/graph.py 的 run 一致）。

    多轮上下文 history 由 API 层从历史存储注入（已含归属校验与 token 预算），
    图本身无状态：每次调用新建 initial_state，不依赖外部 user_id/session_id 参数。
    """
    app = create_medical_qa_graph()
    initial_state = create_initial_state(query, history=history)
    final_state = app.invoke(initial_state)
    return final_state  # 完整 AgentState；API 层从中取 final_answer/intent/... 组装响应
```

---

#### 模块 7：API 服务模块 (`src/api/`)

**职责**：提供 REST API 接口

**核心代码**：

```python
# api/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.api.routes.consultation import router as consultation_router
from src.api.routes.health import router as health_router

app = FastAPI(
    title="智慧问诊Agent系统",
    version="1.0.0",
    description="基于知识图谱的医疗咨询系统"
)

# 添加 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(consultation_router, prefix="/api", tags=["问诊"])
app.include_router(health_router, tags=["健康检查"])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

```python
# api/routes/consultation.py
from fastapi import APIRouter
from src.api.schemas.request import ConsultationRequest
from src.api.schemas.response import ConsultationResponse
from src.agents.graph import call_langgraph_ai

router = APIRouter()

@router.post("/consult", response_model=ConsultationResponse)
def consult(req: ConsultationRequest, current_user: User = Depends(get_current_user)):
    """处理用户问诊请求（V2：登录必需；历史按归属注入上下文）"""
    history = history_store.load_context(req.session_id, current_user.id)  # 归属裁决 + token 预算
    result = run(req.query, history)
    # 本轮双写历史（先 MySQL 再 Redis），assistant 落库后触发后台增量摘要
    history_store.persist_turn(req.session_id, "user", req.query, user_id=current_user.id)
    history_store.persist_turn(
        req.session_id, "assistant", result["final_answer"],
        intent=result["intent"], user_id=current_user.id
    )
    # 其余字段（symptoms/departments/medications/disclaimers/warnings/duration_ms）
    # 同样从 result 中取出组装，见 src/api/routes.py 的 _build_response_from_state
    return ConsultationResponse(answer=result["final_answer"], intent=result["intent"])
```

```python
# api/schemas/request.py
from pydantic import BaseModel
from typing import Dict, Any

class ConsultationRequest(BaseModel):
    """V2 当前契约（src/api/models.py）：身份走令牌，请求体只带业务字段"""
    query: str          # 本轮用户问题（1-1000 字）
    session_id: str     # 前端生成的会话 ID（= conversations 主键 = Redis 热缓存 key）
```

```python
# api/schemas/response.py
from pydantic import BaseModel
from typing import Dict, Any

class ConsultationResponse(BaseModel):
    result: Dict[str, Any]
```

---

#### 模块 8：前端模块 (`frontend/`)

**职责**：提供用户友好的聊天界面（Vue 3 + Element Plus）

**技术栈**：Vue 3（Composition API + `<script setup>`）+ Element Plus + Vite + axios

**目录结构**：

```
frontend/
├── index.html              # 入口 HTML
├── package.json            # 依赖与脚本
├── vite.config.js          # Vite 配置（含 /api 代理）
├── src/
│   ├── main.js             # 应用入口（挂载 Element Plus）
│   ├── App.vue             # 根组件
│   ├── api/index.js        # axios 封装（调用 FastAPI）
│   ├── views/ChatView.vue  # 聊天主界面
│   └── components/         # 消息气泡、免责声明横幅等组件
└── dist/                   # npm run build 产物（由 FastAPI 托管）
```

**核心代码**：

```javascript
// frontend/src/api/index.js —— axios 封装
import axios from 'axios'

const http = axios.create({ baseURL: '/api', timeout: 30000 })

export function consult(query, sessionId) {
  return http.post('/consult', { query, session_id: sessionId })
}
```

```vue
<!-- frontend/src/views/ChatView.vue —— 聊天主界面（节选） -->
<template>
  <div class="chat-app">
    <!-- 免责声明横幅（常驻顶部） -->
    <el-alert type="warning" :closable="false"
      title="本系统仅供健康咨询和就医指导，不能替代专业医疗诊断。身体不适请及时就医，切勿自行用药！" />

    <!-- 消息列表 -->
    <div class="messages" ref="listRef">
      <div v-for="(m, i) in messages" :key="i" :class="['bubble', m.role]">
        {{ m.content }}
        <!-- 推荐科室标签 -->
        <el-tag v-for="d in m.departments" :key="d" type="success">{{ d }}</el-tag>
        <!-- 急症警告 -->
        <el-alert v-for="w in m.warnings" :key="w" type="error" :title="w" />
      </div>
    </div>

    <!-- 输入框 -->
    <el-input v-model="input" :disabled="loading" @keyup.enter="send"
      placeholder="请描述您的症状或健康问题..." />
    <el-button type="primary" :loading="loading" @click="send">发送</el-button>
  </div>
</template>

<script setup>
import { ref, nextTick } from 'vue'
import { consult } from '../api'

const messages = ref([])       // 聊天历史（响应式）
const input = ref('')
const loading = ref(false)
const listRef = ref(null)

async function send() {
  if (!input.value || loading.value) return
  const query = input.value
  messages.value.push({ role: 'user', content: query })
  input.value = ''
  loading.value = true
  try {
    const { data } = await consult(query, crypto.randomUUID())
    messages.value.push({
      role: 'assistant',
      content: data.answer,
      departments: data.departments || [],
      warnings: data.warnings || []
    })
  } finally {
    loading.value = false
    nextTick(() => listRef.value?.scrollTo(0, listRef.value.scrollHeight))
  }
}
</script>
```

**部署方式**：

```bash
cd frontend && npm run build        # 产出 dist/ 静态文件
# FastAPI 中挂载（API 路由注册之后）：
# app.mount("/", StaticFiles(directory="frontend/dist", html=True))
# 单进程 :8000 同时服务 API 与前端页面
```

---

## 3. 数据流

### 3.1 整体数据流

```
医疗网站数据
    │ requests + BeautifulSoup（爬虫）
    ▼
Excel + JSON 文件（原始数据）
    │ LangChain + LLM（实体抽取）
    ▼
JSON 文件（结构化数据）
    │ Neo4j + Cypher（图谱构建）
    ▼
Neo4j 知识图谱（8 种节点，12 种关系）
    │ LangGraph + FAISS（智能问答）
    ▼
FastAPI（REST API）
    │ HTTP 请求
    ▼
Vue + Element Plus（聊天界面）→ 用户
```

### 3.2 详细数据流

#### 流程 1：数据采集与处理

```
1. 爬虫采集
   输入：医疗网站 URL
   处理：requests + BeautifulSoup 解析 HTML
   输出：原始数据（Excel + JSON）
   存储：data/raw/

2. 实体抽取
   输入：原始数据（txt/json）
   处理：LangChain + LLM 提取结构化信息
   输出：结构化数据（JSON）
   存储：data/processed/

3. 图谱构建
   输入：结构化数据（JSON）
   处理：Cypher MERGE 语句创建节点和关系
   输出：Neo4j 知识图谱
   存储：Neo4j 数据库

4. 向量索引构建
   输入：Neo4j 中的实体数据
   处理：BGE-M3 生成向量，FAISS 构建索引
   输出：FAISS 索引文件
   存储：data/indexes/
```

#### 流程 2：智能问答

```
用户输入
    │
    ▼
[Vue + Element Plus 前端]
    │ HTTP POST /api/consult
    ▼
[FastAPI 后端]
    │ 调用 call_langgraph_ai()
    ▼
[LangGraph Agent 系统]
    │
    ├─→ [意图识别 Agent]
    │     输入：用户问题
    │     输出：意图类型（appointment/treatment/knowledge/other）
    │
    ├─→ [症状检测 Agent]
    │     输入：用户问题
    │     输出：症状列表、严重程度、是否急症
    │
    ├─→ [科室推荐 Agent]
    │     输入：症状列表
    │     处理：FAISS 向量搜索 + Neo4j 图谱查询
    │     输出：推荐科室列表
    │
    ├─→ [用药建议 Agent]
    │     输入：疾病名称
    │     处理：Neo4j 图谱查询
    │     输出：药物信息、副作用、注意事项
    │
    ├─→ [医学知识 Agent]
    │     输入：用户问题
    │     处理：Neo4j 图谱查询
    │     输出：医学知识回答
    │
    ├─→ [就医指导 Agent]
    │     输入：症状、疾病
    │     输出：就医前注意事项
    │
    ├─→ [安全检查 Agent]
    │     输入：所有建议
    │     输出：免责声明、警告信息
    │
    └─→ [答案融合 Agent]
          输入：所有 Agent 的输出
          处理：整合信息，格式化输出
          输出：最终回答
    │
    ▼
[FastAPI 返回响应]
    │ HTTP Response
    ▼
[Vue 前端渲染回答]
    │
    ▼
用户看到回答
```

### 3.3 状态流转图

```
初始状态
    │
    ▼
user_input: "我最近总是头疼"
intent: null
symptoms: null
...
    │
    ▼ [意图识别 Agent]
    │
intent: "appointment"
messages: ["意图识别：appointment"]
    │
    ▼ [症状检测 Agent]
    │
symptoms: ["头疼"]
severity: "medium"
is_emergency: false
messages: [..., "症状检测：['头疼']"]
    │
    ▼ [科室推荐 Agent]
    │
recommended_departments: [
    {"name": "神经内科", "priority": "首选", "reason": "..."},
    {"name": "眼科", "priority": "备选", "reason": "..."}
]
messages: [..., "科室推荐：神经内科、眼科"]
    │
    ▼ [就医指导 Agent]
    │
pre_visit_advice: [
    "记录头痛的时间、频率",
    "就医前不要自行服用止痛药"
]
messages: [..., "就医指导：已生成"]
    │
    ▼ [安全检查 Agent]
    │
disclaimers: ["本建议仅供参考，不能替代专业医疗诊断..."]
warnings: []
messages: [..., "安全检查：已添加免责声明"]
    │
    ▼ [答案融合 Agent]
    │
final_answer: "根据您的症状描述，建议您就诊以下科室：..."
messages: [..., "答案融合：完成"]
    │
    ▼
返回最终回答
```

---

## 4. 技术选型

### 4.1 技术栈总览

| 层级       | 技术                              | 版本     | 用途            | 选型理由                                    |
| ---------- | --------------------------------- | -------- | --------------- | ------------------------------------------- |
| 网页爬虫   | requests + BeautifulSoup          | 最新     | 数据采集        | 简单易用，适合小规模爬取                    |
| 数据处理   | pandas + openpyxl                 | 最新     | Excel 读写      | 功能强大，API 友好                          |
| 大语言模型 | langchain + ChatOpenAI            | 最新     | LLM 调用        | 统一接口，支持多种 LLM                      |
| 结构化输出 | pydantic                          | v2       | 数据校验        | 类型安全，自动生成文档                      |
| 图数据库   | Neo4j                             | 5.26 LTS | 知识图谱存储    | 图数据库领导者，Cypher 语法简洁             |
| 向量检索   | sentence-transformers + FAISS     | 最新     | 语义搜索        | BGE-M3 多语言支持，FAISS 高效               |
| Agent 编排 | LangGraph                         | 最新     | 多 Agent 状态图 | LangChain 生态，易于扩展                    |
| Web API    | FastAPI + Uvicorn                 | 最新     | REST 接口       | 高性能，自动生成文档                        |
| 前端界面   | Vue 3 + Element Plus              | 最新     | 聊天界面        | 主流工程实现，构建为静态文件由 FastAPI 托管 |
| 环境配置   | python-dotenv + pydantic-settings | 最新     | 环境变量        | 类型安全，配置验证                          |

### 4.2 技术选型详细说明

#### 大语言模型：阿里云 DashScope API (qwen-plus)

**选型理由**：

- 中文能力强，适合医疗领域
- 成本相对较低
- API 兼容 OpenAI 格式，易于切换

**替代方案**：

- OpenAI GPT-4：性能更好，但成本高
- 本地部署（如 LLaMA）：隐私性好，但需要 GPU

#### 图数据库：Neo4j 5.26 LTS

**选型理由**：

- 图数据库领导者，市场占有率最高
- Cypher 查询语言简洁易懂
- 社区版免费，适合教学项目
- Docker 部署简单

**替代方案**：

- JanusGraph：开源，但配置复杂
- Amazon Neptune：云服务，但成本高

#### 向量检索：FAISS + BGE-M3

**选型理由**：

- FAISS：Facebook 开发，高效稳定
- BGE-M3：北京智源研究院开发，多语言支持，中文效果好
- 本地部署，无需网络请求

**替代方案**：

- Milvus：分布式向量数据库，适合大规模
- Pinecone：云服务，但需要网络

#### Agent 编排：LangGraph

**选型理由**：

- LangChain 生态，与现有工具无缝集成
- 状态图设计，逻辑清晰
- 支持条件边，灵活路由

**替代方案**：

- AutoGen：微软开发，多 Agent 对话
- CrewAI：角色扮演，但不够灵活

#### Web API：FastAPI

**选型理由**：

- 高性能（基于 Starlette）
- 自动生成 OpenAPI 文档
- 类型安全（基于 Pydantic）

**替代方案**：

- Flask：简单易用，但性能较低
- Django：功能全面，但较重

#### 前端界面：Vue 3 + Element Plus

**选型理由**：

- 国内 AI 产品（Dify、Coze 等）的主流前端方案，工程化程度高
- 编译型 SPA，虚拟 DOM 局部更新，性能优于 Streamlit 的全量重跑
- 构建为纯静态文件（dist/），由 FastAPI 单进程同源托管，部署极简
- Element Plus 组件库丰富，聊天界面、标签、警告框开箱即用
- 真正的前后端分离，前端可独立部署到 Nginx/CDN

**替代方案**：

- Streamlit：纯 Python 开发，但非生产级主流，性能较差
- React + Next.js：功能强大，国际主流，但学习成本较高

---

## 5. 接口设计

### 5.1 模块间接口

#### 接口 1：爬虫 → 实体抽取

**输入**：

```python
# 输入：Excel 文件路径
input_file = "data/raw/diseases.xlsx"
```

**输出**：

```python
# 输出：JSON 文件
output_file = "data/processed/disease_entities.json"

# 输出格式
[
    {
        "name": "高血压",
        "symptoms": ["头痛", "头晕", "心悸"],
        "causes": ["遗传因素", "不良生活习惯"],
        "treatments": ["药物治疗", "生活方式调整"],
        "departments": ["心血管内科"]
    }
]
```

#### 接口 2：实体抽取 → 图谱构建

**输入**：

```python
# 输入：JSON 文件
input_file = "data/processed/disease_entities.json"
```

**输出**：

```cypher
// 输出：Neo4j 数据库
MERGE (d:Disease {name: "高血压"})
MERGE (s:Symptom {name: "头痛"})
MERGE (d)-[:HAS_SYMPTOM]->(s)
```

#### 接口 3：图谱构建 → 向量检索

**输入**：

> ⚠ **设计期草图，非现状实现**：下面这段代码里的模块/类名在当前仓库中并不存在，照抄会 `ImportError`。真实现与对应关系见 §2.1 末尾的换算表；本节只保留其**设计意图**，不作为可执行参考。

```python
# 从 Neo4j 读取实体
entities = neo4j_client.run("""
    MATCH (d:Disease)
    RETURN d.name AS name, d.description AS description
""")

# entities: [{"name": "高血压", "description": "..."}]
```

**输出**：

```python
# 构建 FAISS 索引
vector_store.build_index(entities)
vector_store.save_index("data/indexes/")
```

#### 接口 4：向量检索 → Agent 系统

**输入**：

```python
# Agent 调用向量搜索
query = "头疼"
results = vector_store.search(query, top_k=5)
# results: [{"name": "头痛", "score": 0.95}, ...]
```

**输出**：

```python
# 返回搜索结果
return results
```

### 5.2 API 接口

#### 接口 1：POST /api/consult（V2：需登录，Bearer Token）

**请求**（V2 移除 `data` 包装与 `user_id`：身份由令牌携带，会话归属由服务端裁决）：

```json
{
  "query": "我最近总是头疼，应该挂什么科？",
  "session_id": "session_01"
}
```

**响应**：

```json
{
  "result": {
    "answer": "根据您的症状描述，建议您就诊以下科室：...",
    "timestamp": "2026-07-24T10:30:00"
  }
}
```

**错误响应**：

```json
{
  "error": {
    "code": 400,
    "message": "用户输入不能为空"
  }
}
```

#### 接口 2：GET /health

**请求**：

```
GET /health
```

**响应**：

```json
{
  "status": "ok",
  "version": "1.0.0"
}
```

### 5.3 Agent 间接口

#### Agent 状态传递

**输入状态**：

```python
state = {
    "query": "我最近总是头疼",
    "history": [],
    "intent": None,
    "symptoms": [],
    "messages": []
}
```

**输出状态**（经过意图识别 Agent）：

```python
state = {
    "query": "我最近总是头疼",
    "history": [],
    "intent": "appointment",
    "symptoms": [],
    "messages": ["意图识别：appointment"]
}
```

**状态流转**：

```python
# 每个 Agent 接收 state，修改 state，返回 state
def __call__(self, state: dict) -> dict:
    # 处理逻辑
    state["intent"] = "appointment"
    state["messages"].append("意图识别：appointment")
    return state
```

---

## 6. 安全设计

### 6.1 免责声明

**强制添加**：所有医疗建议都必须包含免责声明

```python
# agents/safety/safety_checker.py
DISCLAIMER = """
🔴 **重要声明**：
本建议仅供参考，不能替代专业医疗诊断。身体不适一定要去正规医院找医生看病，
千万不要自行用药或治疗！如有紧急情况，请立即就医或拨打急救电话（120）。
"""

class SafetyCheckerAgent:
    def __call__(self, state: dict) -> dict:
        state["disclaimers"] = [DISCLAIMER]
        return state
```

### 6.2 急症识别

**急症症状列表**：

```python
EMERGENCY_SYMPTOMS = [
    "胸痛", "呼吸困难", "意识丧失", "严重出血",
    "剧烈头痛", "突发视力丧失", "癫痫发作"
]

def check_emergency(symptoms: List[str]) -> bool:
    """检查是否包含急症症状"""
    return any(s in EMERGENCY_SYMPTOMS for s in symptoms)
```

**急症响应**：

```markdown
🚨 **紧急提醒**：

您描述的症状可能是急症表现，请立即就医或拨打急救电话（120）！
```

### 6.3 药物相互作用检查

```python
# 草图版（已废弃，别抄）：label 写成 :Drug（契约里是 Medication）、
# RETURN 里引用了未绑定的 r（MATCH 没写 -[r:...]->，Cypher 直接报错）、且走不存在的 neo4j_client。
# 下面是真实现：src/knowledge_graph/graph_queries.py 的 GraphQueries.get_medication_interactions()

def get_medication_interactions(self, medication_name: str) -> list[dict]:
    """查某药与其他药的相互作用（单药查询，不是批量 IN $drugs）"""
    driver = self.get_driver()
    with driver.session() as session:
        result = session.run("""
            MATCH (m1:Medication {name: $name})-[r:INTERACTS_WITH]->(m2:Medication)
            RETURN m2.name AS drug,
                   r.severity AS severity,
                   r.description AS description
        """, name=medication_name)
        return [{"drug": r["drug"], "severity": r["severity"], "description": r["description"]}
                for r in result]

# ⚠ 医疗红线：图谱里查不到相互作用 ≠ 没有相互作用。tools.py 的 get_medication_info 在
#   返回空时**不编造**，而是诚实声明「本系统未内置完整药物相互作用库，联用风险请以药师/权威数据库为准」。
#   这条兜底必须保留——宁可说不知道，不可给患者一个虚假的安全感。
```

### 6.4 用户认证与数据隔离（V2）

**威胁模型**：V1 匿名设计下任何人可读写全部对话（三个安全缺口见 day05 讲义 §7.1）；
医疗对话属敏感个人信息，是上线红线。方案 = **JWT 无状态认证 + 数据层归属裁决**。

| 契约         | 设计                                                                                                                    | 当前代码                                    |
| ------------ | ----------------------------------------------------------------------------------------------------------------------- | ------------------------------------------- |
| 密码存储     | SHA-256 预哈希 + bcrypt 加盐（规避 bcrypt 72 字节对中文密码的截断）；全系统不存明文                                     | `src/auth/security.py`                    |
| 令牌         | JWT HS256，payload`sub`(用户ID)/`role`/`exp`；`JWT_SECRET` 经 `.env` 注入，留空则进程级随机                   | `src/auth/security.py`                    |
| 认证注入     | `Depends(get_current_user)` 统一挂载受保护路由；token 缺失/无效/过期/用户停用 → 401/403                              | `src/auth/deps.py`                        |
| 归属绑定     | 首条消息落库惰性创建会话并绑定`user_id`；读写删他人会话 → `OwnershipError` → 路由转 **404**（不泄露存在性） | `src/history/store.py::_assert_owner`     |
| 无主会话认领 | 认证前遗留的`user_id=''` 会话，由持有其 session_id 的登录用户原子认领（`UPDATE ... WHERE user_id=''`）              | `src/history/store.py::_claim_orphan`     |
| 列表隔离     | `WHERE user_id = 当前用户`                                                                                            | `list_conversations`                      |
| 防枚举       | 登录失败统一"用户名或密码错误"；他人会话统一 404                                                                        | `src/api/auth_routes.py` / `routes.py`  |
| 切账号自愈   | 主动退出清 localStorage`session_id`；发送链路遇 404 自动轮换新会话重试一次                                            | `frontend/src/App.vue` / `ChatView.vue` |

**users 表契约**：`id VARCHAR(64) PK`、`username VARCHAR(32) UNIQUE`、`password_hash VARCHAR(128)`、
`role VARCHAR(16) DEFAULT 'user'`、`status VARCHAR(16) DEFAULT 'active'`、`created_at`、`last_login_at`。

---

## 7. 测试设计

> **现状对账（2026-10-11）**：本仓库**没有 `tests/` 目录**，也没装 pytest。下面的 pytest 片段是
> 「如果要做成标准套件该长这样」的样板，作迁移起点用；**课堂上真正跑的验收是下面三层**，
> 全部靠模块自测 + 退出码，零测试框架依赖：
>
> | 层 | 命令 | 覆盖 | 失败行为 |
> |---|---|---|---|
> | ① 模块自测 | `python src/agents/state.py`、`python src/knowledge_graph/schema.py`、`python src/agents/checkpointer.py` … | 每个模块的关键契约与边界（`if __name__ == "__main__"` 段） | 退出码非 0 |
> | ② 契约 + 集成 | `python scripts/check_contracts.py`（R1/R3/R4/R5）→ `python scripts/integration_test.py`（端到端 API 15 项，含认证与越权隔离） | 名字/字段/维度/数据一致性 + 四端点串通 | 退出码非 0 |
> | ③ 评测门禁 | `python evals/runner.py`（53 条金标，`make eval-full`） | 答案质量：意图、症状 F1、科室 hit@3、急症召回、免责覆盖、RAGAS、LLM 裁判 | 硬规则不达标 → 退出码非 0，可进 CI |
>
> R7/R8 的"全绿才算完成"指的就是 ②③，不是 ①之外的 pytest。（§0 R8 黄金冒烟三条命令与此一致。）

### 7.1 单元测试（样板，未落地）

```python
# tests/unit/test_extractor.py
import pytest
from src.extraction.disease_extractor import DiseaseExtractor

def test_disease_extractor():
    extractor = DiseaseExtractor()
    text = "高血压是一种常见的慢性病，症状包括头痛、头晕..."
    result = extractor.extract(text)
    assert result.name == "高血压"
    assert "头痛" in result.symptoms
```

### 7.2 集成测试

```python
# tests/integration/test_agents.py（V2 当前写法：run(query, history)）
import pytest
from src.agents.graph import run

def test_medical_qa_system():
    state = run("我最近总是头疼，应该挂什么科？", history=[])
    answer = state["final_answer"]
    assert "神经内科" in answer or "科室" in answer
    assert "免责声明" in answer or "就医" in answer
```

### 7.3 端到端测试

```python
# tests/e2e/test_api.py（V2 当前契约：先登录拿令牌，再带 Bearer 调 /api/consult）
import pytest
import requests

BASE = "http://127.0.0.1:8000/api"

def _login(username, password):
    r = requests.post(f"{BASE}/auth/login", data={"username": username, "password": password})
    r.raise_for_status()
    return r.json()["access_token"]

def test_api_endpoint(token):
    response = requests.post(
        f"{BASE}/consult",
        json={"query": "高血压吃什么药？", "session_id": "e2e-test"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["answer"]

def test_consult_requires_login():
    # 未登录一律 401（认证门禁，§6.4）
    r = requests.post(f"{BASE}/consult", json={"query": "头疼", "session_id": "x"})
    assert r.status_code == 401
```

---

## 8. 部署方案

### 8.1 开发环境

```bash
# 1. 启动 Neo4j（推荐 docker-compose）
docker-compose up -d
# 或：docker start neo4j（若容器已存在）

# 2. 构建前端（首次或前端改动后）
cd frontend && npm run build

# 3. 启动 FastAPI（同时托管 API + 前端静态文件，单进程）
python scripts/start_api.py
# 访问 http://localhost:8000 即是完整应用

# 开发模式（可选）：另开终端运行 cd frontend && npm run dev，访问 http://localhost:5173
```

### 8.2 生产环境

**Docker Compose**（项目根目录的 `docker-compose.yml`，唯一容器化基础设施为 Neo4j）：

```yaml
services:
  neo4j:
    image: neo4j:5.26
    container_name: neo4j
    ports:
      - "7474:7474"
      - "7687:7687"
    environment:
      - NEO4J_AUTH=neo4j/12345678
    volumes:
      - neo4j_data:/data
    restart: unless-stopped

volumes:
  neo4j_data:
    external: true       # 复用已存在的同名卷，保留知识图谱数据
    name: neo4j_data
```

> **应用部署**：FastAPI（含前端静态文件）可用 Gunicorn + Uvicorn Worker 直接部署在宿主机或独立容器中，连接 `bolt://neo4j:7687`。前端构建产物 `frontend/dist` 由 FastAPI 同源托管，无需独立前端容器。

**启动命令**：

```bash
docker-compose up -d
```

---

## 9. 性能优化

### 9.1 向量检索优化

**批量构建索引**：

```python
# 批量处理，减少内存占用
batch_size = 1000
for i in range(0, len(entities), batch_size):
    batch = entities[i:i+batch_size]
    vector_store.build_index(batch)
```

**缓存搜索结果**：

```python
from functools import lru_cache

@lru_cache(maxsize=1000)
def search_cached(query: str, top_k: int = 5):
    return vector_store.search(query, top_k)
```

### 9.2 Neo4j 查询优化

**创建索引**：

```cypher
CREATE INDEX disease_name FOR (d:Disease) ON (d.name);
CREATE INDEX symptom_name FOR (s:Symptom) ON (s.name);
```

**使用 EXPLAIN 分析查询**：

```cypher
EXPLAIN MATCH (d:Disease {name: "高血压"})-[:HAS_SYMPTOM]->(s:Symptom)
RETURN s.name
```

---

---

## 10. 对话历史与上下文管理设计（V2/V3）

### 10.1 存储契约（冷热分层 + 归属 + 摘要水位）

| 存储                | 结构                                                                                                                 | 说明                                                                                    |
| ------------------- | -------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `conversations`   | `id PK`、`user_id`(归属)、`title`、`summary TEXT`、`summary_msg_id BIGINT`、`created_at`、`updated_at` | `summary`=滚动摘要；`summary_msg_id`=**摘要水位**（该 id 之前的消息已摘要化） |
| `messages`        | `id PK`、`conversation_id`(索引)、`role`、`content MEDIUMTEXT`、`intent`、`created_at`                   | 全量只追加；**摘要不删原文**（历史回看 + 医疗审计价值）                           |
| Redis`sess:{sid}` | List，LPUSH+LTRIM 保留`HISTORY_MAX_TURNS` 轮，EXPIRE 24h                                                           | 热缓存；水位推进后`DEL` 并从 MySQL 回源重建，缓存永不越过水位                         |

### 10.2 写路径（双写容错 + 后台增量摘要）

1. `persist_turn`：先写 MySQL（事实源）再写 Redis；MySQL 失败本轮不落 Redis（防幻影数据）。
2. assistant 轮落库后 → `summarizer.schedule_summarization`（每会话一把锁、daemon 线程，不阻塞问诊）。
3. 触发条件：未摘要原文轮次 > `HISTORY_SUMMARY_MIN_KEEP` 且总 token > `HISTORY_MAX_TOKENS`。
4. 切分：最近 `MIN_KEEP` 轮留原文，更早的卷入摘要（LLM 增量合并旧摘要+溢出轮次）。
5. 摘要提示词**强制保留**过敏史/慢病史/正在用药/急症信号/用户声明禁忌（医疗安全信息不丢）。
6. `save_summary`：回写 `summary` + 推进 `summary_msg_id` + `DEL` Redis 热缓存。

### 10.3 读路径（`load_context` = 本项目的 Context Manager）

归属裁决 → 读水位 → 取水位后原文（Redis 三级命中 / MySQL 回源）→ `turns[-HISTORY_MAX_TURNS:]` 轮次上限
→ `trim_turns_to_budget` **token 级滑窗**（从最新往前装，装满预算即止，保底最近 2 轮）
→ 首部注入 `{role:"summary"}` → `format_history` 渲染为【更早对话摘要】段 → 注入各 Agent 提示词。

计数策略：tiktoken cl100k_base（对 Qwen 偏高估=保守方向，宁可少装不超窗口），离线降级启发式（CJK≈1 token/字）。

### 10.4 配置契约（`config/settings.py`）

| 配置                          | 默认 | 说明                             |
| ----------------------------- | ---- | -------------------------------- |
| `HISTORY_MAX_TURNS`         | 12   | 热缓存/上下文轮次硬上限          |
| `HISTORY_MAX_TOKENS`        | 3000 | 注入提示词的历史段 token 预算    |
| `HISTORY_SUMMARY_MIN_KEEP`  | 4    | 增量摘要时至少原文保留的最近轮次 |
| `HISTORY_SUMMARY_MAX_CHARS` | 600  | 滚动摘要硬截断保险丝             |

## 11. 可观测与 Tracing 设计（2026-09 新增）

**目标**：生产路径与离线评估（evals）共用同一套可观测钩子；请求级 span 树可查、可追溯、可排障。

**实现**（`src/common/observe.py`，生产与评估共用）：

| 组件                         | 契约                                                                                                                   |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `trace_span(name, **meta)` | 请求/用例级业务 span 上下文管理器；元数据携带 request_id / session_id / user_id / query → intent / duration_ms / ok   |
| `get_callbacks()`          | Agent 层 LangChain callbacks：Langfuse 模式返回 CallbackHandler（补内层 LLM 调用树）；LangSmith / no-op 模式返回空列表 |
| 后端选择                     | `LANGFUSE_*` → Langfuse；否则 `LANGSMITH_TRACING` → LangSmith；否则 **no-op（零开销）**                    |

**接线点**：

- `routes.py` 两个问诊端点包 `trace_span` 请求级 span（异常穿越 with 时同样 flush）；
- `graph.run / run_stream` 增加 `callbacks` 参数透传（`app.invoke/stream(config={"callbacks": ...})`）；
- LangSmith 依赖 langchain 的 env 自动 trace，原生产出完整调用树（节点 → LLM → tool_calls），无需额外代码。

**配置契约**（`.env`）：

| 配置                                          | 说明                                                                   |
| --------------------------------------------- | ---------------------------------------------------------------------- |
| `LANGSMITH_TRACING`                         | 开关（true 启用）                                                      |
| `LANGSMITH_ENDPOINT`                        | 美云默认`https://api.smith.langchain.com`；EU/自托管才需改           |
| `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT` | 密钥 / 控制台项目名                                                    |
| `LANGFUSE_*`（三行）                        | 自托管备选，**配置后优先于 LangSmith**（医疗合规数据不出域推荐） |

**工程红线**：

1. 所有后端调用 try/except 包裹——**绝不因可观测失败打断请求/评估**（降级记 `logs/observe.log`）；
2. `.env` 必须 `load_dotenv` 进 `os.environ`（pydantic-settings 只注入 Settings 对象；observe/SDK 经 os.getenv 读 env，不补这步配置不生效）；
3. 可观测与主链路无事务关系，上报失败静默丢弃。

**验收**：发一条问诊后 LangSmith 控制台 Traces 视图可见 `consult` 请求级 span + `LangGraph` 自动树（含 `tools → search_knowledge_graph` 工具轨迹）；env 未配置时行为完全不变（no-op）。

## 12. 二级答案缓存设计（2026-09 新增）

**目标**：命中即直返**完整响应**（含免责声明），跳过 LLM 与检索；未命中走 Agent 全链路并异步写回。**医疗数据强制用户隔离**，绝不跨用户、不跨上下文串答。

**核心思想**：缓存前先由 **Cacheability Rule** 判定（纯正则、微秒级、零 LLM/嵌入调用），把问题路由到正确的缓存域；误判方向刻意保守——宁可漏命中（走全链路），绝不跨上下文串答。

```
                 Query
                   ↓
          Cacheability Rule
                   ↓
     ┌─────────────┴─────────────┐
     ↓                           ↓
上下文/强个性化                通用稳定
（指代/接续/省略）          （自包含问题）
     ↓                           ↓
L1 Session 缓存            L1 User 精确
（仅本会话）                     ↓ miss
     ↓ miss                   L2 User 语义
   Agent                        ↓ miss
                              Agent
```

**实现**（`src/common/answer_cache.py`，对外只暴露三个接口）：

| 组件                                                 | 契约                                                                                                                                                                                                                                                              |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `classify(query)`                                  | Cacheability Rule → 返回`"session"` / `"user"`。指代词（这个/那个/它的/上述/你说的…）、接续词开头（那…/还有…/另外…，「那」后须跟典型接续字避免误伤"那格列奈"药名）、省略式短问（怎么办/严重吗/吃什么…）→ `session` 域；其余自包含问题 → `user` 域 |
| `lookup(query, user_id, session_id)`               | 返回`(层级, 完整响应)` 或 `None`。session 域只查 L1 会话键（**不做语义兜底**，防跨上下文串答）；user 域查 L1 用户键 → miss 再查 L2 语义                                                                                                                |
| `store_async(query, payload, user_id, session_id)` | 后台守护线程异步写对应域（按`classify` 结果）；失败仅记日志，不打断主链路                                                                                                                                                                                       |

**三把钥匙（全部含 `user_id`，绝不跨用户）**：

| 缓存         | Key / 匹配依据                            | Scope          | 适合场景                                 |
| ------------ | ----------------------------------------- | -------------- | ---------------------------------------- |
| L1 Session   | `ans:{版本}:su:{user}:{session}:{sha1}` | 仅本会话       | "那平时要注意什么""这个药呢"等上下文追问 |
| L1 User 精确 | `ans:{版本}:u:{user}:{sha1}`            | 跨会话、仅本人 | 用户重复问相同问题                       |
| L2 User 语义 | BGE-M3 余弦 ≥0.92，只在本用户条目内匹配  | 跨会话、仅本人 | 同一用户相似问法                         |

**阈值为何 0.92 从严（实测）**：真改写如"失眠脱发挂什么科"与"挂什么号"相似度仅 0.88；而危险对"孕妇感冒了能吃什么药"与"感冒了能吃什么药"高达 **0.868**——阈值降到 0.87 以下会跨人群串答。故 L2 只收近同义问法，用改写召回率换医疗安全。

**接线点**：

- `routes.py` 两个问诊端点：Agent 执行前调 `lookup`，命中直接构建响应（`cached=true`、重算 `duration_ms`），**照常落对话历史**（否则下一轮上下文断裂）；生成成功后调 `store_async`。
- stream 端点命中时发一帧 `progress {node:"answer_cache"}` + `answer` 帧（不新增帧类型，前端渲染零改动）。
- `models.py` `ConsultationResponse` 增加 `cached: bool`，前端显示 ⚡ 缓存命中徽章。
- docker-compose 的 `redis` 服务加 `command: redis-server --appendonly yes` + 数据卷——缓存键须扛机器重启。

**配置契约**（`.env` / `settings.py`，默认开启）：

| 配置                           | 说明                                               |
| ------------------------------ | -------------------------------------------------- |
| `CACHE_ENABLED`              | 总开关（跑评估基线对比可设 false）                 |
| `CACHE_VERSION`              | 命名空间版本号，知识库/图谱重建后 +1 即整体失效    |
| `CACHE_L1_TTL_SECONDS`       | L1 TTL（默认 24h），仅兜底，版本更新才是主失效手段 |
| `CACHE_SIMILARITY_THRESHOLD` | L2 余弦阈值（默认 0.92，医疗从严）                 |
| `CACHE_L2_MAX_ENTRIES`       | L2 容量上限（默认 256，超出最旧淘汰）              |

**工程红线**：

1. 缓存的是**完整响应**（含免责声明）——命中即合规，医疗红线不丢；
2. 请求路径上**不做同步网络/LLM 调用**（可观测曾因同步上报阻塞响应 20+s，见 §11 教训），写入一律后台线程异步；
3. 命中也要落对话历史，否则下一轮上下文断裂；
4. 缓存可缺不可幻——Redis/嵌入/磁盘任一故障均降级直通，绝不打断问诊主链路。

**边界（诚实声明）**：本项目**不做 L3（RAG/Tool 结果缓存）**——问诊链路检索开销可控，收益抵不上复杂度。L2 对真改写（换词不换义）召回有限，是刻意用召回率换医疗安全；如需提升改写命中，演进方向是问法归一化而非调低阈值。

**验收**：新会话首问 miss 并写回；跨会话重复同问命中 L1（<5ms）；近同义改写命中 L2（sim≥0.92）；"那平时要注意什么"类追问仅本会话命中、不跨会话；不同用户问同一问题必须 miss（用户隔离）；`docker restart redis` 后仍命中（AOF）；`CACHE_ENABLED=false` 行为与无缓存完全一致。

## 13. 图执行态持久化设计（checkpointer，2026-10 新增）

实现：`src/agents/checkpointer.py`；挂载点：`src/agents/graph.py` 的 `app = graph.compile(checkpointer=...)`。

### 13.1 定位边界（先划清，否则一定讲混）

| | 事实源 | 是否可丢弃 | 承载内容 |
|---|---|---|---|
| MySQL `messages`/`conversations`（§10） | ✅ **是** | ❌ 不可 | 对话转写、滚动摘要、摘要水位 |
| Redis `sess:{sid}`（§10） | ❌ 派生缓存 | ✅ 可 | 最近 N 轮热缓存 |
| **checkpoint SQLite（本节）** | ❌ **不是事实源** | ✅ **可丢弃、可重建** | 单次请求内的 **AgentState 执行态快照**（意图/症状/科室/免责…） |

**三条硬边界**（与讲义 day05 §6.7「存储 ≠ 记忆」「三层记忆」同源，互为佐证）：

1. **checkpoint 不承担跨轮记忆**。跨轮连续性只来自短期记忆：`load_context` → `state["history"]` → 注入提示词。
2. **工作记忆的定义没变**，仍是 `AgentState`；本节的增量是它**变得可持久化、可中断恢复、可回溯**。
   不许表述成"加了 checkpoint 才有工作记忆"。
3. 删除整个 checkpoint 库，系统行为**完全不变**（只是失去中断恢复与回溯）——这是"非事实源"的可验证判据。

### 13.2 thread_id 契约（R1：唯一主人 = `build_thread_id()`）

```python
# src/agents/checkpointer.py —— 全项目唯一构造点，任何模块不得自行拼接
thread_id = f"user_{user_id}__session_{session_id}"
```

| 规则 | 约束 | 违反后果 |
|---|---|---|
| 必须含 `user_id` | 取自 `src/auth/deps.py` 的 `User.id`（JWT 解析，前端不可伪造） | 用户 B 猜到 B 的 session_id 即可 resume 到 A 中断的图、读到 A 的医疗中间态 |
| API 层唯一入口 | `run(query, history, callbacks, thread_id)` / `run_stream(...)` | 裸调 `app.invoke` 不带 thread_id → LangGraph 直接 `ValueError` |
| 无用户上下文的调用 | 传 `None`，由 `run()` 生成 `anon:<uuid12>` 并在 `end_turn` 删除 | 评测 harness / 模块自测零改动；不清则 checkpoint 库无界增长 |
| 归属裁决 | 仍走 §10 的三道防线（`load_context` 先裁决） | 越权统一 404，不泄露会话存在性 |

### 13.3 轮次生命周期：一轮一线程 + 轮首清空（关键工程决策）

**为什么必须清空**：`AgentState` 有 10 个 `Annotated[list, operator.add]` 累加字段（symptoms / departments / medications / advice / disclaimers / warnings / retrieved_entities / retrieved_contexts / linked_entities / messages——以 `src/agents/state.py` 为准，数一下即可复现）。同一 thread 上第二次 `invoke`，LangGraph 会把新结果**追加**到旧值——实测 `symptoms` 由 `['头痛']` 变 `['头痛','头痛']`，免责声明同样翻倍。这既是数据错误也是医疗风险（上一轮建议混进本轮答案）。自测 `[3]` 就是在复现这个事故并证明 `begin_turn` 修掉了它。

| 动作 | 调用 | 语义 |
|---|---|---|
| 新提问 | `begin_turn(thread_id)` → `delete_thread` | 清掉上一轮执行态，本轮从干净状态起跑 |
| 评测/脚本跑完 | `end_turn(thread_id)` | 仅删 `anon:` 线程，用户线程保留（供排障/回溯） |
| **HITL 恢复（演进钩子）** | **不调 `begin_turn`**，直接 `Command(resume=...)` | resume 必须接着跑中断时那份快照；误调 begin_turn 会把待审状态删掉 |

**刻意接受的代价**：不做跨轮 time-travel（每轮独立快照），保留轮内逐步检查点（`get_thread_history`）。与"工作记忆只活在单次请求内"的既有口径一致；将来要跨轮回溯，把 thread_id 升级为带轮次序号即可，属兼容性演进。

### 13.4 配置契约（`config/settings.py`）

| 配置项 | 默认 | 说明 |
|---|---|---|
| `checkpoint_enabled` | `true` | 置 `false` → `get_checkpointer()` 返回 `None` → `compile()` 回退无持久化老路径（可对照实验，拓扑完全一致） |
| `checkpoint_db_path` | `data/checkpoints/agent_checkpoints.sqlite` | 相对路径按项目根解析；**运行时产物 + 含用户医疗问答内容 → 已进 `.gitignore`，永不提交** |

### 13.5 实现约束（踩过的坑，写进契约防回归）

1. **线程池共享连接**：API 经 `run_in_executor` / `asyncio.to_thread` 调用，SQLite 连接默认禁止跨线程 → 必须 `sqlite3.connect(..., check_same_thread=False)`，全进程共用一个 `SqliteSaver`（双检锁单例）。`SqliteSaver` 内部自带 `threading.Lock` 串行化游标、`setup()` 已开 `PRAGMA journal_mode=WAL`。
2. **只调公开 API**：清理用 `saver.list()` + `saver.delete_thread()`，**不写死 `checkpoints`/`writes` 表名与列**——直接 SQL 会随 `langgraph-checkpoint` 升级静默失效（首版就踩过：绕过 `setup()` 导致 `no such table`）。
3. **降级不阻断主链路**：`begin_turn` / `end_turn` / `prune_threads` / 两个检视函数全部 `try/except` + `logger.warning`。执行态清理失败绝不能让问诊报错（"缓存可缺不可幻"的同一原则）。
4. **依赖显式声明**：`requirements.txt` 加 `langgraph-checkpoint-sqlite==3.1.1`（此前 `langgraph-checkpoint` 只是传递依赖、装了也没用）。

### 13.6 可观测面

`get_thread_state(thread_id)` → 回读持久化快照；`get_thread_history(thread_id, limit)` → 轮内检查点列表（已完成节点 / intent / symptoms / 消息步数）；`describe_thread(thread_id)` → `hit|empty @ 路径`，进日志。这三个函数是 HITL 待审面板与课堂"看 Agent 到底算了什么"的共用底座。

### 13.7 验收（R7 模块自检）

```bash
# 零 LLM、零中间件，可在无 API Key 环境跑；失败退出码非 0
python src/agents/checkpointer.py
# 真实图挂载 + 执行态回读断言（需 MODEL_API_KEY + Neo4j）
python src/agents/graph.py
python scripts/check_contracts.py --no-db    # R1/R3 不受影响（未新增响应字段、未新增枚举）
```

自测覆盖五项：thread_id 契约与用户隔离、落盘后重开连接仍可读（=「重启不丢」）、累加事故复现与 `begin_turn` 修复、单例与配置路径契约、真实图 `app.checkpointer is not None`。

---

**文档版本**：v3.3
**创建日期**：2026-07-24
**最后更新**：2026-10-05
**状态**：已对齐当前代码 ✅（V2 认证 + V3 上下文预算 + 可观测 tracing + 二级答案缓存 + §13 执行态持久化）
