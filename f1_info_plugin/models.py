from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


class OpenF1UnavailableError(RuntimeError):
    """Raised when OpenF1 is temporarily inaccessible for the current caller."""


class F1ExternalApiError(RuntimeError):
    """External data source failure with safe user-facing metadata."""

    source: str
    category: str
    redacted_url: str
    status_code: int | None

    def __init__(
        self,
        message: str,
        *,
        source: str,
        category: str,
        redacted_url: str = "",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.source: str = source
        self.category: str = category
        self.redacted_url: str = redacted_url
        self.status_code: int | None = status_code


@dataclass
class NewsItem:
    source: str
    title: str
    url: str
    description: str
    published_at: datetime | None
    weight: float


@dataclass
class ScheduleSessionData:
    name: str
    start_text: str
    kind: str


@dataclass
class SchedulePageData:
    title: str
    place: str
    circuit: str
    sessions: list[ScheduleSessionData]


@dataclass
class ResultRowData:
    position: str
    driver: str
    constructor: str
    primary: str = ""
    meta: str = ""
    status: str = ""
    driver_full_name: str = ""


@dataclass
class ResultsPageData:
    title: str
    session: str
    rows: list[ResultRowData]
    end_time_text: str = ""
    notices: list[str] = field(default_factory=list)


@dataclass
class NewsSummaryData:
    summary: str
    url: str


@dataclass
class NewsPageData:
    title: str
    items: list[NewsSummaryData]
    beijing_date: str = ""
    notice: str = ""
    using_raw_fallback: bool = False
