# F1 资讯插件

MaiBot SDK v2 插件，用于查询 F1 赛历、赛果，并聚合多家 RSS 来源生成每日重要 F1 新闻中文摘要。

> 本插件使用 `PluginConfigBase` 强类型配置模型。首次启动后，MaiBot Runner 会根据 `config_model` 自动生成 `config.toml`，并支持通过 Web UI 修改配置。

## 功能特性

- 查询下一站大奖赛时间表，并按北京时间展示各 session。
- 查询指定轮次赛历。
- 查询上一站正赛、排位赛或冲刺赛结果。
- 聚合 Formula1、Autosport、Motorsport、The Race、PlanetF1、BBC、Guardian RSS 新闻。
- 使用 MaiBot `llm.generate` 能力生成一句话中文新闻摘要。
- 支持为多个目标会话配置独立的定时新闻发布任务。
- 显式命令直接发送结果并拦截后续聊天链路；Tool 仍可供 planner/replyer 等大模型节点调用。

## 快速开始

### 1. 安装

- 插件市场安装：发布到插件中心后，可通过 Web UI 插件市场下载安装。
- 手动安装：将本插件目录复制到 MaiBot 的 `plugins/chi8wah_f1-info-plugin`，然后在插件管理中加载或重载插件。

插件仓库根目录应包含：`_manifest.json`、`plugin.py`、`README.md`、`LICENSE`。

### 2. 环境要求

- MaiBot 主程序：`1.0.0+`
- MaiBot SDK：`2.5.2+`

### 3. 配置

首次启动后会自动生成 `config.toml`。推荐通过 Web UI 修改配置；下面的字段仅供直接编辑配置文件时参考。

```toml
[plugin]
enabled = true
config_version = "1.0.0"

[api]
jolpica_base_url = "https://api.jolpi.ca/ergast/f1"
openf1_base_url = "https://api.openf1.org/v1"
request_timeout_seconds = 20
retry_count = 2

[news]
feeds = [
    "Formula1|https://www.formula1.com/en/latest/all.xml|1.35",
    "Autosport|https://www.autosport.com/rss/f1/news/|1.10",
    "Motorsport|https://www.motorsport.com/rss/f1/news/|1.05",
    "The Race|https://www.the-race.com/rss/|1.10",
    "PlanetF1|https://www.planetf1.com/rss/|0.95",
    "BBC|https://feeds.bbci.co.uk/sport/formula1/rss.xml|1.05",
    "Guardian|https://www.theguardian.com/sport/formulaone/rss|1.00",
]
lookback_hours = 48
max_candidates_per_feed = 30
daily_limit = 10
include_urls_in_command = true
cache_ttl_minutes = 45
scheduled_jobs = [
    { platform = "qq", item_id = "群号或用户ID", rule_type = "group", time = "09:00", limit = 5, include_urls = false },
    { platform = "qq", item_id = "群号或用户ID", rule_type = "group", time = "18:00", limit = 10, include_urls = true },
]

[model]
model_name = "utils"
temperature = 1.0
max_tokens = 28000
llm_timeout_seconds = 60
```

### 配置项说明

| 配置项 | 默认值 | 说明 |
| ------ | ------ | ---- |
| `plugin.enabled` | `true` | 是否启用插件 |
| `api.jolpica_base_url` | `https://api.jolpi.ca/ergast/f1` | Jolpica F1 API 基础地址 |
| `api.openf1_base_url` | `https://api.openf1.org/v1` | OpenF1 API 基础地址，用作赛历 session 补充 |
| `api.request_timeout_seconds` | `20` | HTTP 请求超时时间 |
| `api.retry_count` | `2` | HTTP 请求失败重试次数 |
| `news.feeds` | 内置 RSS 源列表 | 新闻源，格式为 `来源名|RSS URL|权重` |
| `news.lookback_hours` | `48` | 新闻候选时间窗口 |
| `news.daily_limit` | `10` | 默认每日新闻条数 |
| `news.include_urls_in_command` | `true` | 显式 `/f1 新闻` 命令是否显示来源 URL；Tool 输出始终保留 URL |
| `news.cache_ttl_minutes` | `45` | 新闻摘要缓存时间 |
| `news.scheduled_jobs` | `[]` | 定时发布任务列表；Web UI 添加后会显示 `平台`、`聊天流 ID`、`聊天类型`、`发布时间`、`新闻条数`、`是否显示来源 URL` |
| `model.model_name` | `utils` | 用于生成中文摘要的模型任务名 |
| `model.max_tokens` | `28000` | 摘要生成最大 token；`0` 表示使用任务默认值 |

运行缓存写入 `data/cache.json`。新闻缓存会保存输出文本和已展示 URL；缓存过期后重新抓取时会按 URL 去重，避免重复输出旧缓存中已经出现过的来源。`scheduled_jobs` 建议通过 Web UI 添加，添加后逐项填写平台、聊天流 ID、聊天类型、发布时间、条数和 URL 显示开关。`rule_type = "group"` 时 `item_id` 填群号或群聊 ID，`rule_type = "private"` 时 `item_id` 填用户 ID。`config.toml` 和 `data/cache.json` 都是本地运行文件，不应提交到公开仓库。

## 使用示例

- `/f1_schedule`、`/f1 赛历`、`/f1 下一站`：查询下一站及各 session 北京时间。
- `/f1_schedule 8`、`/f1 schedule 8`：查询指定轮次赛历。
- `/f1_results [race|qualifying|sprint]`、`/f1 赛果 [正赛|排位|冲刺]`、`/f1 排位`：查询上一站比赛结果。
- `/f1_news [条数]`、`/f1 新闻 [条数]`、`/f1 资讯 [条数]`：输出每日重要新闻中文摘要；是否显示来源 URL 由 `news.include_urls_in_command` 控制。
- `/f1_clear_cache`、`/f1 清缓存`、`/f1 刷新缓存`：清除插件缓存，下次查询新闻会重新抓取。

## Tool

插件保留以下 Tool 供 MaiBot 大模型流程调用：

- `f1_schedule`
- `f1_results`
- `f1_daily_news`

## 许可证

MIT License。详见 `LICENSE`。
