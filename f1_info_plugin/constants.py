from __future__ import annotations

from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# 插件仓库根目录，用于定位 data、配置和本地资源文件。
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
# 新闻摘要等运行时缓存的默认写入位置。
CACHE_PATH = PLUGIN_ROOT / "data" / "cache.json"
# 用户可见时间统一转换到北京时间。
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
# 外部接口返回时间和内部超时计算使用 UTC 作为基准。
UTC = timezone.utc
# LLM 摘要失败时展示原始 RSS 内容前的提示文案。
NEWS_FALLBACK_NOTICE = "中文摘要生成失败或超时，以下显示 RSS 原始标题/导语和来源 URL："
# LLM 生成完成后额外等待结果落地的宽限秒数。
LLM_GENERATE_WAIT_GRACE_SECONDS = 5
# 显式新闻命令整体执行超时外层保留的宽限秒数。
NEWS_COMMAND_OUTER_GRACE_SECONDS = 5
# 用户可见输出模式的合法取值集合。
OUTPUT_MODE_VALUES = {"text", "image", "both"}
# HTML 转图片渲染的超时时间，单位为秒。
OUTPUT_IMAGE_RENDER_TIMEOUT_SECONDS = 10
# 旧版纯文本图片卡片的渲染视口尺寸。
OUTPUT_IMAGE_VIEWPORT = {"width": 760, "height": 1200}
# 旧版纯文本图片卡片的正文字号。
OUTPUT_CARD_BODY_FONT_SIZE = "20px"
# 旧版纯文本图片卡片的正文行高。
OUTPUT_CARD_BODY_LINE_HEIGHT = "1.65"
# 旧版紧凑纯文本图片卡片的正文字号。
OUTPUT_CARD_COMPACT_BODY_FONT_SIZE = "0.9rem"
# 旧版紧凑纯文本图片卡片的正文行高。
OUTPUT_CARD_COMPACT_BODY_LINE_HEIGHT = "1.5"
# 赛历感知缓存从当前或下一站起最多保留的分站数。
SCHEDULE_CONTEXT_RACE_LIMIT = 5
# 常规赛历刷新间隔允许的最小小时数，避免误配置为高频轮询。
SCHEDULE_CONTEXT_REFRESH_MIN_HOURS = 6
# 常规赛历刷新间隔允许的最大小时数，避免缓存长期不更新。
SCHEDULE_CONTEXT_REFRESH_MAX_HOURS = 168
# 每个 session 开始前固定触发一次赛历刷新。
SCHEDULE_CONTEXT_PRE_SESSION_REFRESH_MINUTES = 60
# 后台赛历刷新失败后的退避时间，避免异常状态下快速重试。
SCHEDULE_CONTEXT_FAILURE_BACKOFF_MINUTES = 30
# 不同刷新触发重叠时允许再次发起外部请求的最小间隔。
SCHEDULE_CONTEXT_MIN_REQUEST_GAP_SECONDS = 300
# Jolpica 只提供开始时间时，用于判断正赛是否仍在进行的宽限时间。
SCHEDULE_CONTEXT_RACE_RETENTION_HOURS = 6
# OpenF1 可作为结果页展示的 session 大类。
OPENF1_RESULT_SESSION_TYPES = {"Practice", "Qualifying", "Sprint", "Race"}
# 用户命令中的结果类型到 OpenF1 session 名称的映射。
OPENF1_RESULTS_SESSION_NAME_BY_SESSION = {"race": "Race", "qualifying": "Qualifying", "sprint": "Sprint"}
# 查询最近 OpenF1 结果时最多尝试的已完成 session 数量。
OPENF1_LATEST_RESULT_PROBE_LIMIT = 12
# 查询最近 OpenF1 结果时的总时间预算，单位为秒。
OPENF1_LATEST_RESULT_TIMEOUT_SECONDS = 45.0
# 用户命令中的结果类型到 Jolpica/Ergast session 名称的映射。
JOLPICA_RESULT_SESSION_BY_KEY = {"race": "Race", "sprint": "Sprint", "qualifying": "Qualifying"}
# Jolpica/Ergast 结果类型的中文展示标签。
JOLPICA_RESULT_SESSION_LABEL = {"race": "正赛", "sprint": "冲刺赛", "qualifying": "排位赛"}
# 外部数据源标识到用户可见名称的映射。
EXTERNAL_SOURCE_LABELS = {
    "jolpica": "Jolpica",
    "openf1": "OpenF1",
    "rss": "RSS 新闻源",
    "unknown": "外部数据源",
}
# 查询上下文标识到用户可见业务名称的映射。
EXTERNAL_CONTEXT_LABELS = {
    "schedule": "赛历",
    "results": "赛果",
    "latest_results": "最新结果",
    "news": "新闻",
    "scheduled_news": "定时新闻",
}
# 外部接口错误分类到用户可见失败描述的映射。
EXTERNAL_CATEGORY_PHRASES = {
    "rate_limited": "请求过于频繁",
    "upstream_unavailable": "当前不可用或源站响应超时",
    "timeout": "响应超时",
    "network": "网络连接失败",
    "invalid_response": "返回内容异常",
    "http_error": "请求失败",
    "unknown": "请求失败",
}
