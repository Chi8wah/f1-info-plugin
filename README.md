# F1 资讯插件

MaiBot SDK v2 插件，用于查询 F1 赛历、赛果，并聚合多家 RSS 来源生成每日重要 F1 新闻中文摘要。

> 本插件使用 `PluginConfigBase` 强类型配置模型。首次启动后，MaiBot Runner 会根据 `config_model` 自动生成 `config.toml`，并支持通过 Web UI 修改配置。

## 功能特性

- 查询下一站大奖赛时间表，并按北京时间展示各 session。
- 查询下一站或相对分站赛历。
- 查询最近已完成的正赛、排位赛或冲刺赛结果，也可用相对分站指定某一站。
- 查询最近一个已结束 session 的 OpenF1 结果，包含练习、排位、冲刺和正赛；OpenF1 不可用时回退到 Jolpica 最近可用的正式 session 结果。
- 聚合 Formula1、Autosport、Motorsport、The Race、PlanetF1、BBC、Guardian RSS 新闻。
- 使用 MaiBot `llm.generate` 能力生成一句话中文新闻摘要。
- 支持按平台、聊天流 ID 和聊天类型配置多个定时新闻发布目标。
- 新闻摘要默认缓存 1 天，按北京时间日期和条数分开复用；摘要失败时只缓存 RSS 新闻候选，下次查询会基于同批新闻重试生成中文摘要。
- 显式命令直接发送结果并拦截后续聊天链路；Tool 仍可供 planner/replyer 等大模型节点调用。

## 快速开始

### 1. 安装

- 插件市场安装：可通过 Web UI 插件市场下载安装。
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
cache_ttl_minutes = 1440 # 单位：分钟
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
| `plugin.config_version` | `1.0.0` | 配置版本，通常不需要手动修改 |
| `api.jolpica_base_url` | `https://api.jolpi.ca/ergast/f1` | Jolpica F1 API 基础地址 |
| `api.openf1_base_url` | `https://api.openf1.org/v1` | OpenF1 API 基础地址，用作赛历 session 补充和最新 session 结果查询 |
| `api.request_timeout_seconds` | `20` | HTTP 请求超时时间 |
| `api.retry_count` | `2` | HTTP 请求失败重试次数 |
| `news.feeds` | 内置 RSS 源列表 | 新闻源，格式为 `来源名|RSS URL|权重` |
| `news.lookback_hours` | `48` | 新闻候选时间窗口，单位：小时 |
| `news.max_candidates_per_feed` | `30` | 每个 RSS 源最多读取多少条候选新闻 |
| `news.daily_limit` | `10` | 默认每日新闻条数 |
| `news.include_urls_in_command` | `true` | 显式 `/f1 新闻` 命令是否显示来源 URL；Tool 输出始终保留 URL |
| `news.cache_ttl_minutes` | `1440 分钟（1 天）` | 新闻摘要缓存时间，单位：分钟 |
| `news.scheduled_jobs` | `[]` | 定时发布任务列表；Web UI 添加后会显示 `平台`、`聊天流 ID`、`聊天类型`、`发布时间`、`新闻条数`、`是否显示来源 URL` |
| `model.model_name` | `utils` | 用于生成中文摘要的模型任务名 |
| `model.temperature` | `1.0` | 摘要生成温度 |
| `model.max_tokens` | `28000` | 摘要生成最大 token；`0` 表示使用任务默认值 |
| `model.llm_timeout_seconds` | `60` | LLM 摘要生成超时时间，单位：秒 |

运行缓存写入 `data/cache.json`。新闻缓存会保存输出文本和已展示 URL，缓存 key 包含北京时间日期和新闻条数，因此默认 1440 分钟缓存不会让第二天复用前一天摘要；缓存过期后重新抓取时会按 URL 去重，避免重复输出旧缓存中已经出现过的来源。`scheduled_jobs` 建议通过 Web UI 添加，添加后逐项填写平台、聊天流 ID、聊天类型、发布时间、条数和 URL 显示开关。`rule_type = "group"` 时 `item_id` 填群号或群聊 ID，`rule_type = "private"` 时 `item_id` 填用户 ID；插件会解析到已存在的 MaiBot 会话后发送。旧版直接填写 `stream_id` 的配置仅作为兼容保留，不建议新配置使用。`config.toml` 和 `data/cache.json` 都是本地运行文件，不应提交到公开仓库。

### OpenF1 回退说明

OpenF1 在部分 session 进行期间可能对免费访问返回 401/403/429 或临时 5xx。插件会在这类不可用场景下继续使用 Jolpica/Ergast：`f1_schedule` 仍可查询赛历，`f1_results` 仍可查询正赛、排位赛和冲刺赛，`f1_latest_results` 会回退到 Jolpica 最近可用的正式 session 结果。Jolpica 不提供练习赛结果；如果 OpenF1 不可用且 Jolpica 也没有可用的正式 session 结果，插件会直接提示无法恢复练习赛或最新结果尚未发布。

## 使用示例

- `/f1_schedule`、`/f1 赛历`、`/f1 下一站`：查询下一站及各 session 北京时间。
- `/f1 赛历 [下一站|0|-1|8]`：查询下一站、相对分站或官方轮次赛历；`0` 表示当前/最近分站，`-1` 表示上一站，负数不限，正数仍兼容官方轮次。
- `/f1 赛果 [正赛|排位|冲刺] [0|-1|8]`、`/f1 排位`：查询最近已完成的同类型结果；追加相对分站可指定某一站，负数不限，正数仍兼容官方轮次。
- `/f1_latest_results`、`/f1 最新结果`、`/f1 最近赛果`：查询最近一个已结束 session 的结果，包含练习、排位、冲刺和正赛；OpenF1 不可用时会回退到 Jolpica 最近可用的正式 session 结果，练习赛无法通过 Jolpica 恢复。
- `/f1_news [条数]`、`/f1 新闻 [条数]`、`/f1 资讯 [条数]`：输出每日重要新闻中文摘要；是否显示来源 URL 由 `news.include_urls_in_command` 控制。若 LLM 摘要生成失败，会降级显示 RSS 原始标题/导语与来源 URL，此时 URL 始终保留。
- `/f1_clear_cache`、`/f1 清缓存`、`/f1 刷新缓存`：清除插件缓存，下次查询新闻会重新抓取。
- `/f1`、`/f1_help`、`/f1 帮助`：显示命令帮助。

## Tool

插件暴露以下 Tool 供 MaiBot planner/replyer 等大模型流程调用：

- `f1_schedule`：查询下一站或相对分站赛历，返回各 session 的北京时间安排。
- `f1_results`：查询最近已完成的正赛、排位赛或冲刺赛结果；也可用相对分站指定某一站结果，不包含练习赛。
- `f1_latest_results`：查询最近一个已结束 session 的结果，包含练习、排位、冲刺和正赛；OpenF1 不可用时回退到 Jolpica 最近可用的正式 session 结果。
- `f1_daily_news`：查询近期重要 F1 新闻中文摘要，Tool 输出始终保留来源 URL。

## 许可证

MIT License。详见 `LICENSE`。
