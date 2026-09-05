"""新闻查询各阶段的超时预算。"""

from math import ceil
from typing import Dict

from .constants import (
    HTTP_RETRY_BACKOFF_SECONDS,
    LLM_GENERATE_WAIT_GRACE_SECONDS,
    NEWS_QUERY_FINISH_GRACE_SECONDS,
    OUTPUT_IMAGE_RENDER_TIMEOUT_SECONDS,
    OUTPUT_SEND_TIMEOUT_SECONDS,
)


def rss_request_budget_seconds(request_timeout_seconds: int, retry_count: int) -> float:
    """并发 RSS 源共享一条请求重试链的预算，不按源数量累加。"""

    backoff_seconds = HTTP_RETRY_BACKOFF_SECONDS * retry_count * (retry_count + 1) / 2
    return request_timeout_seconds * (retry_count + 1) + backoff_seconds


def news_component_timeouts_ms(
    request_timeout_seconds: int,
    retry_count: int,
    llm_timeout_seconds: int,
    output_mode: str,
) -> Dict[str, int]:
    """Tool 只查询；命令还需覆盖发送，以及图片失败后改发文字的完整路径。"""

    query_seconds = (
        rss_request_budget_seconds(request_timeout_seconds, retry_count)
        + llm_timeout_seconds
        + LLM_GENERATE_WAIT_GRACE_SECONDS
        + NEWS_QUERY_FINISH_GRACE_SECONDS
    )
    output_seconds = OUTPUT_SEND_TIMEOUT_SECONDS
    if output_mode in {"image", "both"}:
        # both 最多发送文字和图片；image 发图失败后最多再发送一次文字。
        output_seconds = OUTPUT_IMAGE_RENDER_TIMEOUT_SECONDS + 2 * OUTPUT_SEND_TIMEOUT_SECONDS
    return {
        "f1_daily_news": ceil(query_seconds * 1000),
        "f1_news_command": ceil((query_seconds + output_seconds) * 1000),
    }
