from __future__ import annotations

from maibot_sdk import Field, PluginConfigBase

from .constants import SCHEDULE_CONTEXT_REFRESH_MAX_HOURS, SCHEDULE_CONTEXT_REFRESH_MIN_HOURS


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "flag"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用 F1 资讯插件",
        json_schema_extra={"label": "启用插件", "hint": "关闭后命令、Tool 和定时发布都会返回未启用"},
    )
    config_version: str = Field(
        default="1.1.0",
        description="配置版本",
        json_schema_extra={"label": "配置版本", "hint": "用于未来配置迁移，通常不需要手动修改"},
    )


class ApiConfig(PluginConfigBase):
    """结构化数据源配置。"""

    __ui_label__ = "赛历与赛果"
    __ui_icon__ = "calendar"
    __ui_order__ = 1

    jolpica_base_url: str = Field(
        default="https://api.jolpi.ca/ergast/f1",
        description="Jolpica F1 API 基础地址",
        json_schema_extra={"label": "Jolpica API 地址", "hint": "用于查询 F1 赛历和赛果", "placeholder": "https://api.jolpi.ca/ergast/f1"},
    )
    openf1_base_url: str = Field(
        default="https://api.openf1.org/v1",
        description="OpenF1 API 基础地址",
        json_schema_extra={"label": "OpenF1 API 地址", "hint": "用于补充分站 session 时间和最近 session 结果", "placeholder": "https://api.openf1.org/v1"},
    )
    request_timeout_seconds: int = Field(
        default=20,
        description="HTTP 请求超时时间",
        ge=3,
        le=120,
        json_schema_extra={"label": "HTTP 超时时间", "hint": "抓取赛历、赛果和 RSS 时的单次请求超时秒数"},
    )
    retry_count: int = Field(
        default=2,
        description="HTTP 请求失败重试次数",
        ge=0,
        le=5,
        json_schema_extra={"label": "HTTP 重试次数", "hint": "网络请求失败后的额外重试次数"},
    )


class ScheduledNewsJobConfig(PluginConfigBase):
    """定时新闻发布任务配置。"""

    __ui_label__ = "定时新闻任务"
    __ui_icon__ = "clock"
    __ui_order__ = 0

    platform: str = Field(default="", description="平台")
    item_id: str = Field(default="", description="聊天流 ID（群号或用户 ID）")
    rule_type: str = Field(default="group", description="聊天类型（group/private）")
    time: str = Field(default="09:00", description="发布时间（北京时间 HH:MM）")
    limit: int = Field(default=10, description="新闻条数", ge=1, le=20)
    include_urls: bool = Field(default=True, description="是否显示来源 URL")


class ScheduleContextConfig(PluginConfigBase):
    """Planner/Replyer 赛历感知配置。"""

    __ui_label__ = "赛历感知"
    __ui_icon__ = "calendar"
    __ui_order__ = 2

    enabled: bool = Field(
        default=False,
        description="是否向 Planner 和 Replyer 注入当前赛历信息",
        json_schema_extra={
            "label": "启用赛历感知",
            "hint": "启用后，非比赛周注入下一站的非练习赛赛程，比赛周注入该站完整赛程；会少量增加模型输入 Token。",
        },
    )
    refresh_interval_hours: int = Field(
        default=24,
        description="赛历缓存的常规定时刷新间隔，单位为小时",
        ge=SCHEDULE_CONTEXT_REFRESH_MIN_HOURS,
        le=SCHEDULE_CONTEXT_REFRESH_MAX_HOURS,
        json_schema_extra={
            "label": "定时刷新间隔（小时）",
            "hint": (
                f"允许 {SCHEDULE_CONTEXT_REFRESH_MIN_HOURS}-{SCHEDULE_CONTEXT_REFRESH_MAX_HOURS} 小时；"
                "仅控制常规定时刷新，每个 session 开始前一小时仍会刷新一次。"
            ),
            "step": 1,
        },
    )


class NewsConfig(PluginConfigBase):
    """新闻聚合配置。"""

    __ui_label__ = "每日新闻"
    __ui_icon__ = "newspaper"
    __ui_order__ = 3

    feeds: list[str] = Field(
        default_factory=lambda: [
            "Formula1|https://www.formula1.com/en/latest/all.xml|1.35",
            "Autosport|https://www.autosport.com/rss/f1/news/|1.10",
            "Motorsport|https://www.motorsport.com/rss/f1/news/|1.05",
            "The Race|https://www.the-race.com/rss/|1.10",
            "PlanetF1|https://www.planetf1.com/rss/|0.95",
            "BBC|https://feeds.bbci.co.uk/sport/formula1/rss.xml|1.05",
            "Guardian|https://www.theguardian.com/sport/formulaone/rss|1.00",
        ],
        description="RSS 源，格式为 来源名|URL|权重",
        json_schema_extra={"label": "RSS 新闻源", "hint": "每行格式：来源名|RSS URL|权重；权重越高排序越靠前"},
    )
    lookback_hours: int = Field(
        default=48,
        description="新闻候选时间窗口",
        ge=6,
        le=168,
        json_schema_extra={"label": "新闻候选时间窗口", "hint": "只汇总最近多少小时内发布的 RSS 新闻"},
    )
    max_candidates_per_feed: int = Field(
        default=30,
        description="每个源最多读取多少条候选",
        ge=5,
        le=100,
        json_schema_extra={"label": "单源候选上限", "hint": "每个 RSS 源最多读取多少条新闻候选"},
    )
    daily_limit: int = Field(
        default=10,
        description="默认每日新闻条数",
        ge=1,
        le=20,
        json_schema_extra={"label": "默认新闻条数", "hint": "用户没有指定条数时默认输出多少条新闻"},
    )
    include_urls_in_command: bool = Field(
        default=True,
        description="显式 /f1 新闻命令是否显示来源 URL",
        json_schema_extra={"label": "命令显示来源 URL", "hint": "关闭后 /f1 新闻 输出不带 URL；Tool 和缓存仍保留 URL"},
    )
    output_mode: str = Field(
        default="text",
        description="命令和定时新闻的用户可见输出模式：text、image 或 both",
        json_schema_extra={"label": "用户输出模式", "hint": "text=纯文字，image=图片卡片，both=文字和图片；命令和定时新闻生效，Tool 输出始终为文本"},
    )
    scheduled_jobs: list[ScheduledNewsJobConfig] = Field(
        default_factory=list,
        description="定时发布任务列表",
        json_schema_extra={"label": "定时发布任务", "hint": "点击添加后逐项填写平台、聊天流 ID、聊天类型、发布时间、条数和 URL 显示开关"},
    )
    cache_ttl_minutes: int = Field(
        default=1440,
        description="新闻摘要缓存时间（分钟）",
        ge=5,
        le=1440,
        json_schema_extra={"label": "新闻缓存时间（分钟）", "hint": "单位：分钟；缓存未过期时复用摘要，过期后重新抓取并按 URL 去重"},
    )


class ModelConfig(PluginConfigBase):
    """LLM 摘要配置。"""

    __ui_label__ = "模型"
    __ui_icon__ = "brain"
    __ui_order__ = 4

    model_name: str = Field(
        default="utils",
        description="用于生成中文一句话摘要的模型任务名",
        json_schema_extra={"label": "摘要模型任务名", "hint": "对应 MaiBot 模型配置中的任务名"},
    )
    temperature: float = Field(
        default=1,
        description="摘要生成温度",
        ge=0.0,
        le=2.0,
        json_schema_extra={"label": "摘要生成温度", "hint": "越高越发散，越低越稳定"},
    )
    max_tokens: int = Field(
        default=28000,
        description="摘要生成最大 token；0 表示使用任务默认值",
        ge=0,
        le=1000000,
        json_schema_extra={"label": "摘要最大 token", "hint": "0 表示使用模型任务默认值；过小可能导致 JSON 摘要截断"},
    )
    llm_timeout_seconds: int = Field(
        default=60,
        description="LLM 摘要超时时间",
        ge=5,
        le=300,
        json_schema_extra={"label": "摘要超时时间", "hint": "LLM 生成新闻摘要的最长等待秒数"},
    )


class F1InfoPluginConfig(PluginConfigBase):
    """F1 资讯插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    schedule_context: ScheduleContextConfig = Field(default_factory=ScheduleContextConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
