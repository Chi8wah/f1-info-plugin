from __future__ import annotations

from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = PLUGIN_ROOT / "data" / "cache.json"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
NEWS_FALLBACK_NOTICE = "中文摘要生成失败或超时，以下显示 RSS 原始标题/导语和来源 URL："
LLM_GENERATE_WAIT_GRACE_SECONDS = 5
NEWS_COMMAND_OUTER_GRACE_SECONDS = 5
OUTPUT_MODE_VALUES = {"text", "image", "both"}
OUTPUT_IMAGE_RENDER_TIMEOUT_SECONDS = 10
OUTPUT_IMAGE_VIEWPORT = {"width": 760, "height": 1200}
OUTPUT_CARD_BODY_FONT_SIZE = "20px"
OUTPUT_CARD_BODY_LINE_HEIGHT = "1.65"
OUTPUT_CARD_COMPACT_BODY_FONT_SIZE = "0.9rem"
OUTPUT_CARD_COMPACT_BODY_LINE_HEIGHT = "1.5"
OPENF1_RESULT_SESSION_TYPES = {"Practice", "Qualifying", "Sprint", "Race"}
OPENF1_RESULTS_SESSION_NAME_BY_SESSION = {"race": "Race", "qualifying": "Qualifying", "sprint": "Sprint"}
OPENF1_LATEST_RESULT_PROBE_LIMIT = 12
OPENF1_LATEST_RESULT_TIMEOUT_SECONDS = 45.0
JOLPICA_RESULT_SESSION_BY_KEY = {"race": "Race", "sprint": "Sprint", "qualifying": "Qualifying"}
JOLPICA_RESULT_SESSION_LABEL = {"race": "正赛", "sprint": "冲刺赛", "qualifying": "排位赛"}
EXTERNAL_SOURCE_LABELS = {
    "jolpica": "Jolpica",
    "openf1": "OpenF1",
    "rss": "RSS 新闻源",
    "unknown": "外部数据源",
}
EXTERNAL_CONTEXT_LABELS = {
    "schedule": "赛历",
    "results": "赛果",
    "latest_results": "最新结果",
    "news": "新闻",
    "scheduled_news": "定时新闻",
}
EXTERNAL_CATEGORY_PHRASES = {
    "rate_limited": "请求过于频繁",
    "upstream_unavailable": "当前不可用或源站响应超时",
    "timeout": "响应超时",
    "network": "网络连接失败",
    "invalid_response": "返回内容异常",
    "http_error": "请求失败",
    "unknown": "请求失败",
}
