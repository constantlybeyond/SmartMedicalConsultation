"""
Global Configuration Module
Reads configuration from .env file using pydantic_settings
"""

from pathlib import Path
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env 锚定在项目根目录查找，不依赖运行时工作目录
# （从 scripts/、IDE 或其他 CWD 启动时都能读到同一份配置）
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables and .env file.

    All settings can be overridden via environment variables or .env file.
    优先级：系统环境变量 > .env 文件 > 字段默认值。
    生产部署建议用环境变量/云密钥服务注入，不在服务器上放置 .env 文件。
    """

    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ===== LLM Configuration =====
    model_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="LLM API base URL (DashScope compatible with OpenAI)"
    )
    model_api_key: str = Field(
        default="",
        description="LLM API key (DashScope API Key)"
    )
    model_name: str = Field(
        # default="qwen-plus",
        default="qwen3.7-flash",
        description="LLM model name"
    )
    llm_enable_thinking: bool = Field(
        default=False,
        description="是否启用 qwen3 系列混合思考模型的 reasoning(思考)模式。"
                    "本系统多数 LLM 调用为意图分类/症状抽取/科室与用药择优等结构化小任务，"
                    "默认关闭思考可大幅降低单次延迟(常见 5-20s → 1-3s)与 token 消耗；"
                    "如需复杂医学推理，可在 .env 设为 true。对非 qwen3 模型此参数被服务端忽略，无副作用。"
    )
    llm_max_retries: int = Field(
        default=1,
        description="LLM 调用失败重试次数。延迟敏感路径建议 ≤1，避免慢调用重试导致耗时翻倍。"
    )

    # ===== Extraction LLM Configuration（微调模型，仅用于实体/关系抽取）=====
    # base_url 留空则抽取任务回退到主 LLM，行为与改动前完全一致
    extraction_model_base_url: str = Field(
        default="",
        description="微调模型 vLLM 服务地址（如 http://192.168.122.3:8000/v1）。"
                    "留空=不启用，实体/关系抽取仍走主 LLM"
    )
    extraction_model_api_key: str = Field(
        default="EMPTY",
        description="vLLM 服务不校验密钥，固定填 EMPTY"
    )
    extraction_model_name: str = Field(
        default="qwen2.5-merged",
        description="vLLM --served-model-name 指定的模型名"
    )

    # ===== Neo4j Configuration =====
    neo4j_uri: str = Field(
        default="bolt://localhost:7687",
        description="Neo4j database connection URI"
    )
    neo4j_user: str = Field(
        default="neo4j",
        description="Neo4j username"
    )
    neo4j_password: str = Field(
        default="12345678",
        description="Neo4j password"
    )

    # ===== Embedding Model Configuration =====
    embedding_model_path: str = Field(
        default="models/bge-m3",
        description="Local path to BGE-M3 embedding model (relative to project root or absolute)"
    )

    # ===== Conversation History Storage（Redis 热缓存 + MySQL 冷存储）=====
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis URL for session hot cache"
    )
    mysql_host: str = Field(default="localhost", description="MySQL host")
    mysql_port: int = Field(default=3306, description="MySQL port")
    mysql_user: str = Field(default="root", description="MySQL user")
    mysql_password: str = Field(default="123456", description="MySQL password")
    mysql_database: str = Field(default="smc_history", description="MySQL database for conversation history")

    # ===== 多轮上下文 Token 预算（token 级控制：计数 + 滑窗 + 摘要，讲义 §6.6）=====
    history_max_turns: int = Field(
        default=12,
        description="热缓存/上下文窗口的轮次硬上限（原有 HOT_TURNS 配置化）"
    )
    history_max_tokens: int = Field(
        default=3000,
        description="注入提示词的历史段 token 预算（保守估算）。读路径按此从最旧裁剪，"
                    "写路径超出预算的旧轮次触发增量摘要，不再硬截断丢失"
    )
    history_summary_min_keep: int = Field(
        default=4,
        description="增量摘要时至少原文保留的最近轮次数（对话连贯性下限）"
    )
    history_summary_max_chars: int = Field(
        default=600,
        description="滚动摘要的最大字符数（提示词要求 ≤300 字，此为硬截断保险丝）"
    )

    # ===== 答案缓存（L1 精确 + L2 语义，实现见 src/common/answer_cache.py）=====
    cache_enabled: bool = Field(
        default=True,
        description="答案缓存总开关。关闭后行为与无缓存完全一致（跑评估基线对比时可关）"
    )
    cache_version: str = Field(
        default="v1",
        description="缓存命名空间版本号：知识库/图谱重建后改个新值即整体失效"
                    "（L1 key 前缀 + L2 文件版本同时作废）"
    )
    cache_l1_ttl_seconds: int = Field(
        default=86400,
        description="L1 精确缓存 TTL（秒）；版本更新才是主失效手段，TTL 只是兜底"
    )
    cache_similarity_threshold: float = Field(
        default=0.92,
        description="L2 语义缓存命中阈值（归一化余弦）。医疗场景从严：只命中近乎同义的问法，"
                    "调低会误命中不同病症的问题"
    )
    cache_l2_max_entries: int = Field(
        default=256,
        description="L2 语义缓存容量上限，超出按最旧淘汰（FIFO）"
    )

    # ===== User Authentication（JWT 认证，讲义 §7）=====
    jwt_secret: str = Field(
        default="",
        description="JWT 签名密钥（HS256）。强烈建议在 .env 配置固定随机串，例如执行 "
                    "python -c \"import secrets;print(secrets.token_urlsafe(48))\" 生成。"
                    "留空=每次启动随机生成，重启后所有登录态失效（仅适合开发环境）。"
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT 签名算法")
    access_token_expire_minutes: int = Field(
        default=120,
        description="访问令牌有效期（分钟）。过期后前端需重新登录"
    )

    # ===== CORS（开启认证后收紧，禁止 "*"）=====
    cors_allowed_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        description="CORS 允许来源白名单，逗号分隔。开发=Vite :5173；"
                    "生产单进程同源部署时前端不触发 CORS，无需添加"
    )

    @property
    def cors_origins(self) -> list:
        """把逗号分隔的白名单解析为列表（过滤空项）。"""
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    # ===== Logging Configuration =====
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"
    )

    # ===== Application Configuration =====
    app_name: str = Field(
        default="SmartMedicalConsultation",
        description="Application name"
    )
    app_version: str = Field(
        default="1.0.0",
        description="Application version"
    )
    api_port: int = Field(
        default=8000,
        description="FastAPI server port"
    )

    # ===== Agent Configuration =====
    enable_react_layer: bool = Field(
        default=True,
        description="医学知识分支是否在『强制检索』之上再启用可选 ReAct 多跳层（tool-calling）。"
                    "强制检索是 grounding 底线、必走；ReAct 仅作多跳补充与工具调用演示。关闭则纯强制检索，更快更省。"
    )

    # ===== Agent 执行态持久化（checkpointer，实现见 src/agents/checkpointer.py）=====
    checkpoint_enabled: bool = Field(
        default=True,
        description="是否为 LangGraph 挂 SqliteSaver checkpointer：图执行态（AgentState）按 "
                    "thread_id=user_{uid}__session_{sid} 落盘，支撑中断恢复 / 状态回溯 / 「重启不丢」。"
                    "注意它不是事实源——对话记录仍以 MySQL 为准，跨轮连续性靠短期记忆（history 注入）；"
                    "新一轮提问会清空上一轮执行态（AgentState 的 operator.add 累加字段不清会翻倍）。"
                    "设为 False 即回退到无 checkpointer 的老行为（thread_id 自动失效，便于对照实验）。"
    )
    checkpoint_db_path: str = Field(
        default="data/checkpoints/agent_checkpoints.sqlite",
        description="checkpoint SQLite 文件路径（相对路径按项目根解析）。运行时产物 + 含用户医疗问答内容，"
                    "已进 .gitignore，永不提交。"
    )

    # ===== Entity Linking Configuration =====
    entity_link_threshold: float = Field(
        default=0.85,
        description="实体链接相似度阈值（归一化余弦）。仅 ≥ 阈值 的图谱实体视为成功链接，"
                    "过高漏链、过低误链，可按 evals 结果调优。"
    )
    entity_link_top_k: int = Field(
        default=3,
        description="实体链接时每个输入词在 FAISS 中考察的候选数"
    )

    # ===== Text2Cypher Configuration =====
    text2cypher_enabled: bool = Field(
        default=True,
        description="是否为 ReAct 层提供 natural_language_graph_query 工具（LLM 生成只读 Cypher 查图谱）。"
                    "需同时开启 enable_react_layer 才生效。"
    )
    text2cypher_max_records: int = Field(
        default=50,
        description="Text2Cypher 单次查询返回的最大记录数（驱动层截断，防全表扫描）"
    )


@lru_cache()
def get_settings() -> Settings:
    """
    Get cached settings instance.
    Using lru_cache ensures we only load settings once.
    """
    return Settings()


# Global settings instance
settings = get_settings()
