"""F1 Meme 配置与输入规范化。"""

from __future__ import annotations

from typing import Literal, Self

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator, model_validator

import re


_SEND_ORDER_DESCRIPTION = (
    "按原帖发布时间排序。newest_first（由新到旧）优先发新图，旧图可能还没发就过期。"
    "oldest_first（由旧到新）优先清理积压，待发图较多时可能连续发送几天前的旧图。"
)


def normalize_times(values: list[str]) -> list[str]:
    result: set[str] = set()
    for value in values:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
        if not match or int(match[1]) > 23 or int(match[2]) > 59:
            raise ValueError("时间必须是北京时间 HH:MM，例如 08:00")
        result.add(f"{int(match[1]):02d}:{int(match[2]):02d}")
    return sorted(result)


class MemeSendJobConfig(PluginConfigBase):
    __ui_label__ = "Meme 发送任务"

    enabled: bool = Field(default=True, description="是否启用这个群的发送任务", json_schema_extra={"label": "启用"})
    platform: str = Field(default="qq", description="群所在平台", json_schema_extra={"label": "平台"})
    group_id: str = Field(default="", description="目标群号或群 ID", json_schema_extra={"label": "目标群", "hint": "填写群号，机器人必须已有该群的会话"})
    # 旧配置没有模式字段，默认沿用原来的手动时间列表。
    schedule_mode: Literal["manual", "interval"] = Field(default="manual", description="发送时间 B 的配置方式", json_schema_extra={"label": "发送模式（manual 手动 / interval 时间段）", "hint": "只有选中的模式参与调度，另一种模式的配置会保留"})
    times: list[str] = Field(default_factory=lambda: ["12:00"], description="手动模式的每天发送时间 B（北京时间）", json_schema_extra={"label": "手动模式：发送时间点", "hint": "仅 manual 模式生效；每项 HH:MM，每个时间只发一张"})
    start_time: str = Field(default="11:00", description="时间段模式的开始时间（北京时间）", json_schema_extra={"label": "时间段模式：开始时间", "placeholder": "HH:MM（北京时间）", "hint": "仅 interval 模式生效；从这个时间开始发送"})
    end_time: str = Field(default="22:00", description="时间段模式的结束时间（北京时间）", json_schema_extra={"label": "时间段模式：结束时间", "placeholder": "HH:MM（北京时间）", "hint": "仅 interval 模式生效；早于开始时间表示次日，落在间隔上的结束时间也发送"})
    interval_hours: int = Field(default=1, ge=1, le=24, strict=True, description="时间段内每隔多少小时发送一张", json_schema_extra={"label": "时间段模式：间隔小时（1～24）", "hint": "仅 interval 模式生效，填写整数；每天从开始时间重新起算"})

    @field_validator("platform", "group_id")
    @classmethod
    def trim_target(cls, value: str) -> str:
        return value.strip()

    @field_validator("times")
    @classmethod
    def validate_times(cls, values: list[str]) -> list[str]:
        return normalize_times(values)

    @field_validator("start_time", "end_time")
    @classmethod
    def validate_range_time(cls, value: str) -> str:
        return normalize_times([value])[0]

    @field_validator("interval_hours", mode="before")
    @classmethod
    def normalize_interval_hours(cls, value: object) -> object:
        # WebUI 的列表表单可能提交数字字符串；仅转换整数字面量，继续拒绝浮点数和布尔值。
        if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
            return int(value)
        return value

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.schedule_mode == "interval" and self.start_time == self.end_time:
            raise ValueError("时间段的开始和结束时间不能相同；全天发送可填写 00:00～23:59")
        return self

    @property
    def effective_times(self) -> list[str]:
        if self.schedule_mode == "manual":
            return list(self.times)
        start_hour, start_minute = map(int, self.start_time.split(":"))
        end_hour, end_minute = map(int, self.end_time.split(":"))
        start = start_hour * 60 + start_minute
        end = end_hour * 60 + end_minute
        if end < start:
            end += 24 * 60
        # 跨日区间先按连续分钟生成，再映射为每天的时间点；不额外补发未对齐的终点。
        return sorted(f"{minute // 60 % 24:02d}:{minute % 60:02d}" for minute in range(start, end + 1, self.interval_hours * 60))

    @property
    def schedule_description(self) -> str:
        if self.schedule_mode == "manual":
            return "手动时间点：" + "、".join(self.times)
        end = f"次日{self.end_time}" if self.end_time < self.start_time else self.end_time
        return f"时间段：{self.start_time}～{end}，每 {self.interval_hours} 小时一次"

    @property
    def target_key(self) -> str:
        # 使用长度前缀，避免平台或群 ID 内的分隔符造成标识碰撞。
        return f"{len(self.platform)}:{self.platform}:{self.group_id}"


class MemeConfig(PluginConfigBase):
    __ui_label__ = "F1 Meme"
    __ui_icon__ = "image"
    __ui_order__ = 7

    enabled: bool = Field(default=False, description="定时采集和发送 F1 Meme", json_schema_extra={"label": "启用 F1 Meme", "hint": "仅使用第三方 FxEmbed，可能间歇返回 404、限流或超时；可重试请求最多尝试 3 次（含首次），仍可能采集失败"})
    accounts: list[str] = Field(default_factory=lambda: ["@f1trollofficial"], description="X 图片来源账号", json_schema_extra={"label": "采集账号", "hint": "支持多个用户名，可带 @；无需 Cookie 或 API Key"})
    fetch_times: list[str] = Field(default_factory=lambda: ["08:00"], description="每天采集时间 A（北京时间）", json_schema_extra={"label": "采集时间 A", "hint": "每项 HH:MM；建议每天 1～2 次，避免短间隔重复采集增加上游和本地负荷；启动不补抓错过的时间点"})
    fetch_image_limit: int = Field(default=30, ge=1, le=1000, description="X：每账号每轮新增图片的目标与上限", json_schema_extra={"label": "每轮图片数量 X", "hint": "成功下载、校验并去重后计数；X 不是保底数量，遇历史帖子、源耗尽或请求失败时可少于 X"})
    max_age_days: int = Field(
        default=7, ge=1, le=90, description="图片、帖子和任务明细的保留天数",
        json_schema_extra={"label": "图片有效期（天）", "hint": "图片按原帖发布时间计算，任务明细按结束时间计算。已尝试发送的图片哈希长期用于同群去重"},
    )
    max_pending_per_group: int = Field(default=200, ge=1, le=10000, description="每群待发送图片上限，溢出时跳过最旧图片", json_schema_extra={"label": "每群队列上限"})
    max_storage_mb: int = Field(default=512, ge=1, le=102400, description="本地图片存储上限（MiB）", json_schema_extra={"label": "图片存储上限（MiB）"})
    max_image_mb: int = Field(default=10, ge=1, le=50, description="单张图片体积上限（MiB）", json_schema_extra={"label": "单图体积上限（MiB）"})
    send_order: Literal["oldest_first", "newest_first"] = Field(
        default="oldest_first",
        description=_SEND_ORDER_DESCRIPTION,
        json_schema_extra={"label": "发送顺序", "hint": _SEND_ORDER_DESCRIPTION},
    )
    # SDK 不保留发送任务子项的 hint/description，频率建议集中在 send_jobs 的提示中。
    send_jobs: list[MemeSendJobConfig] = Field(default_factory=list, description="按群配置的定时发送任务", json_schema_extra={"label": "发送任务", "hint": "添加平台、群号并选择手动时间点或时间段模式；建议在活跃时段每 1～2 小时或更久发送一张，减少机器人负荷；每群独立记录进度，按实际新增量和库存控制每日发送次数，空队列时跳过发送"})

    @field_validator("accounts")
    @classmethod
    def validate_accounts(cls, values: list[str]) -> list[str]:
        accounts: list[str] = []
        for value in values:
            name = value.strip().removeprefix("@").lower()
            if not re.fullmatch(r"[a-z0-9_]{1,15}", name):
                raise ValueError("采集账号应为 X 用户名（可带 @），不能填写链接或空项")
            if f"@{name}" not in accounts:
                accounts.append(f"@{name}")
        return accounts

    @field_validator("fetch_times")
    @classmethod
    def validate_times(cls, values: list[str]) -> list[str]:
        return normalize_times(values)

    @model_validator(mode="after")
    def validate_jobs(self) -> Self:
        seen: set[str] = set()
        if self.enabled and (not self.accounts or not self.fetch_times):
            raise ValueError("启用 Meme 时，采集账号和采集时间不能为空")
        for job in self.send_jobs:
            if self.enabled and job.enabled and (not job.platform or not job.group_id or not job.effective_times):
                raise ValueError("启用的 Meme 发送任务必须填写平台、群号和发送时间")
            if job.group_id:
                if job.target_key in seen:
                    raise ValueError("同平台同群只配置一个 Meme 任务，在该任务中选择发送模式并填写时间")
                seen.add(job.target_key)
        return self

    @property
    def source_names(self) -> list[str]:
        return [name.removeprefix("@").lower() for name in self.accounts]

    @property
    def targets(self) -> list[str]:
        return [job.target_key for job in self.send_jobs if job.enabled and job.group_id]
