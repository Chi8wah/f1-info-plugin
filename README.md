# F1 资讯插件

MaiBot SDK v2 插件，用于查询 F1 赛历、赛果，聚合每日重要新闻，并向模型注入可编辑的车手群聊上下文。

> 本插件使用 `PluginConfigBase` 强类型配置模型。首次启动后，MaiBot Runner 会根据 `config_model` 自动生成 `config.toml`，并支持通过 Web UI 修改配置。

## 功能特性

- 查询下一站大奖赛时间表，并按北京时间展示各 session。
- 查询下一站或相对分站赛历。
- 可选向 MaiBot planner/replyer 注入当前比赛周或下一站赛历，使模型在未主动调用 Tool 时也能感知 F1 赛程。
- 可选识别近期用户消息中的 F1 车手姓名、缩写和社区外号，并向 planner/replyer 注入用户维护的车手资料与群聊梗上下文。
- 查询最近已完成的正赛、排位赛或冲刺赛结果，也可用相对分站指定某一站。
- 查询最近一个已结束 session 的 OpenF1 结果，包含练习、排位、冲刺和正赛；OpenF1 不可用时回退到 Jolpica 最近可用的正式 session 结果。
- 聚合 Formula1、Autosport、Motorsport、The Race、PlanetF1、BBC、Guardian RSS 新闻。
- 使用 MaiBot `llm.generate` 能力生成一句话中文新闻摘要。
- 支持按平台、聊天流 ID 和聊天类型配置多个定时新闻发布目标。
- 新闻摘要默认缓存 1 天，按北京时间日期和条数分开复用；摘要失败时仅当次降级显示 RSS 原始标题/导语，不写入缓存，下次查询会重新抓取并重试生成中文摘要。
- 显式命令和定时新闻可配置为文字、图片卡片或两者同时输出；Tool 仍只返回文本供 planner/replyer 等大模型节点调用。

## 快速开始

### 1. 安装

- 插件市场安装：可通过 Web UI 插件市场下载安装。
- 手动安装：将本插件目录复制到 MaiBot 的 `plugins/chi8wah_f1-info-plugin`，然后在插件管理中加载或重载插件。

插件仓库根目录应包含：`_manifest.json`、`plugin.py`、`f1_info_plugin/`、`README.md`、`LICENSE`。

### 2. 环境要求

- MaiBot 主程序：`1.0.12+`
- MaiBot SDK：`2.7.0+`

### 3. 配置

首次启动后会自动生成 `config.toml`。推荐通过 Web UI 修改配置；下面的字段仅供直接编辑配置文件时参考。

```toml
[plugin]
enabled = true
config_version = "1.2.0"

[api]
jolpica_base_url = "https://api.jolpi.ca/ergast/f1"
openf1_base_url = "https://api.openf1.org/v1"
request_timeout_seconds = 20
retry_count = 2

[schedule_context]
enabled = false
refresh_interval_hours = 24 # 允许 6-168 小时

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
output_mode = "text" # 可选：text、image、both
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

[driver_context]
enabled = false
max_matched_drivers = 2
recent_user_message_limit = 4
reset_profiles_on_next_start = false # 设为 true 后，在下次插件加载时恢复作者默认资料并自动改回 false

[[driver_context.profiles]]
driver_id = "charles_leclerc"
enabled = true
name = "Charles Leclerc"
number = 16
aliases = ["夏尔·勒克莱尔", "勒克莱尔", "Leclerc", "乐扣", "LEC"]
team = "Ferrari"
info = "2026 赛季法拉利正式车手。中文社区常用“乐扣”称呼他。"

# 首次生成的实际配置包含作者维护的全部默认车手资料；此处只展示一项。
```

### 配置项说明

| 配置项 | 默认值 | 说明 |
| ------ | ------ | ---- |
| `plugin.enabled` | `true` | 是否启用插件 |
| `plugin.config_version` | `1.2.0` | 配置版本，通常不需要手动修改 |
| `api.jolpica_base_url` | `https://api.jolpi.ca/ergast/f1` | Jolpica F1 API 基础地址 |
| `api.openf1_base_url` | `https://api.openf1.org/v1` | OpenF1 API 基础地址，用作赛历 session 补充和最新 session 结果查询 |
| `api.request_timeout_seconds` | `20` | HTTP 请求超时时间 |
| `api.retry_count` | `2` | HTTP 请求失败重试次数 |
| `schedule_context.enabled` | `false` | 是否向 planner/replyer 注入赛历感知上下文；默认关闭 |
| `schedule_context.refresh_interval_hours` | `24` | 赛历缓存常规刷新间隔，限制为 6-168 小时；每个 session 开始前一小时仍会刷新 |
| `driver_context.enabled` | `false` | 是否识别近期消息中的车手并注入用户维护的群聊上下文；默认关闭 |
| `driver_context.max_matched_drivers` | `2` | 单次最多注入的车手数，限制为 1-5 |
| `driver_context.recent_user_message_limit` | `4` | 从最新消息向前检查的 user 消息数，限制为 1-20 |
| `driver_context.reset_profiles_on_next_start` | `false` | 设为 `true` 后，下次插件加载时以作者默认资料覆盖完整列表，并自动改回 `false`；会丢弃所有车手资料修改 |
| `driver_context.profiles` | 内置 2026 车手资料 | 22 位正赛车手及周冠宇的可编辑资料，包含 ID、开关、姓名、号码、别名、车队和自由文本信息 |
| `news.feeds` | 内置 RSS 源列表 | 新闻源，格式为 `来源名|RSS URL|权重` |
| `news.lookback_hours` | `48` | 新闻候选时间窗口，单位：小时 |
| `news.max_candidates_per_feed` | `30` | 每个 RSS 源最多读取多少条候选新闻 |
| `news.daily_limit` | `10` | 默认每日新闻条数 |
| `news.include_urls_in_command` | `true` | 显式 `/f1 新闻` 命令是否显示来源 URL；Tool 输出始终保留 URL |
| `news.output_mode` | `text` | 命令和定时新闻的用户可见输出模式：`text` 纯文字、`image` 图片卡片、`both` 文字和图片；Tool 输出始终为文本 |
| `news.cache_ttl_minutes` | `1440 分钟（1 天）` | 新闻摘要缓存时间，单位：分钟 |
| `news.scheduled_jobs` | `[]` | 定时发布任务列表；Web UI 添加后会显示 `平台`、`聊天流 ID`、`聊天类型`、`发布时间`、`新闻条数`、`是否显示来源 URL` |
| `model.model_name` | `utils` | 用于生成中文摘要的模型任务名 |
| `model.temperature` | `1.0` | 摘要生成温度 |
| `model.max_tokens` | `28000` | 摘要生成最大 token；`0` 表示使用任务默认值 |
| `model.llm_timeout_seconds` | `60` | LLM 摘要生成超时时间，单位：秒 |

运行缓存写入 `data/cache.json`。只有成功生成的中文摘要会写入新闻缓存并保存已展示 URL；摘要失败的 RSS 降级结果不会写入缓存。缓存 key 包含北京时间日期和新闻条数，因此默认 1440 分钟缓存不会让第二天复用前一天摘要；缓存过期后重新抓取时会按 URL 去重，避免重复输出旧缓存中已经出现过的来源。`output_mode` 只影响命令和定时新闻发送：`image` 会把赛历、赛果或新闻结构化数据渲染成移动端 F1 图片卡片，渲染或发图失败时降级为文字。`scheduled_jobs` 建议通过 Web UI 添加，添加后逐项填写平台、聊天流 ID、聊天类型、发布时间、条数和 URL 显示开关。`rule_type = "group"` 时 `item_id` 填群号或群聊 ID，`rule_type = "private"` 时 `item_id` 填用户 ID；插件会解析到已存在的 MaiBot 会话后发送。旧版直接填写 `stream_id` 的配置仅作为兼容保留，不建议新配置使用。`config.toml` 和 `data/cache.json` 都是本地运行文件，不应提交到公开仓库。

启用 `schedule_context.enabled` 后，插件会在 MaiBot 授予的数据目录中缓存当前或下一站起的五站赛历。常规刷新由 `refresh_interval_hours` 控制，同时在每个 session 开始前一小时刷新一次；重叠触发会合并，并在请求失败时退避。非比赛周只向 planner/replyer 注入下一站的非练习赛赛程，比赛周注入该站完整赛程。replyer 只接收赛历事实，planner 还会获得按需调用 F1 Tool 的提示。Planner 上下文仅注入具备内置 `reply` 工具的主规划请求，并合并到开头唯一的 system 消息；表达选择、行为分析和表情选择等复用同名 Hook 的辅助任务不会收到这些上下文。

启用 `driver_context.enabled` 后，插件只在 Hook 内读取配置并检查近期外部用户消息，不会联网或定时刷新。MaiBot 上下文中存在 `<message>` 片段时，插件只扫描这些真实聊天正文：会跳过带 `is_self_message="true"` 的机器人旧消息，也会跳过时间、上下文恢复、工具列表和人物画像等无标签内部信息，避免其中的姓名或数字误触发车手匹配。作者维护的 `f1_info_plugin/resources/default_driver_profiles.json` 会作为 `profiles` 的默认值，在首次加载时写入 `config.toml`，因此可在 MaiBot Web UI 中查看和修改。配置生成后以用户保存的完整列表为准，插件升级不会合并新版作者资料。如需主动采用新版作者资料，可将 `driver_context.reset_profiles_on_next_start` 设为 `true` 并保存：下一次插件加载时，它会以默认资料覆盖整个 `profiles` 列表，再自动写回 `false`；所有增删改的车手资料都会丢失。`name`、`number` 和 `aliases` 都参与匹配；其中 `number` 只做独立数字词匹配，并会随命中的车手资料注入模型，便于明确号码与车手的对应关系。英文缩写（如 `HAM`、`VER`、`GAS`）匹配时忽略大小写，但仍需满足英文词边界。重复 `driver_id` 只使用第一项；同一号码或别名如果属于多位已启用车手，会命中所有拥有者，最终仍受单次车手上限约束。

车手 `info` 是自由文本，可以包含基本资料、社区昵称、主观看法和群聊梗。注入提示会明确要求模型只将其用于理解称呼和聊天语境；遇到积分、排名、最新赛果、处罚、合同或转会等时效性问题时，planner 仍应调用合适的 F1 Tool，不能把静态资料当成实时结论。Planner 命中结果通过同一 `session_id` 短时传给 replyer，并在配置更新或插件卸载时清除。

### OpenF1 回退说明

OpenF1 在部分 session 进行期间可能对免费访问返回 401/403/429 或临时 5xx。插件会在这类不可用场景下继续使用 Jolpica/Ergast：`f1_schedule` 仍可查询赛历，`f1_results` 仍可查询正赛、排位赛和冲刺赛，`f1_latest_results` 会回退到 Jolpica 最近可用的正式 session 结果。Jolpica 不提供练习赛结果；如果 OpenF1 不可用且 Jolpica 也没有可用的正式 session 结果，插件会直接提示无法恢复练习赛或最新结果尚未发布。

## 使用示例

- `/f1_schedule`、`/f1 赛历`、`/f1 下一站`：查询相对偏移为 `0` 的分站及各 session 北京时间。
- `/f1 赛历 [0|1|-1]`：只使用相对分站，不再支持官方绝对轮次。留空或 `0` 在比赛周正赛开始时间戳之前表示本站，非比赛周或到达正赛开始时间戳后表示下一站；`1` 表示 `0` 的下一站，`-1` 表示 `0` 的上一站。
- `/f1 赛果 [正赛|排位|冲刺] [0|-1|8]`、`/f1 排位`：查询最近已完成的同类型结果；追加相对分站可指定某一站，负数不限，正数仍兼容官方轮次。
- `/f1_latest_results`、`/f1 最新结果`、`/f1 最近赛果`：查询最近一个已结束 session 的结果，包含练习、排位、冲刺和正赛；OpenF1 不可用时会回退到 Jolpica 最近可用的正式 session 结果，练习赛无法通过 Jolpica 恢复。
- `/f1_news [条数]`、`/f1 新闻 [条数]`、`/f1 资讯 [条数]`：输出每日重要新闻中文摘要；是否显示来源 URL 由 `news.include_urls_in_command` 控制。若 LLM 摘要生成失败，会降级显示 RSS 原始标题/导语与来源 URL，此时 URL 始终保留且不会缓存，下次查询会重试。
- 以上赛历、赛果、最新结果和新闻命令会按 `news.output_mode` 发送纯文字、图片卡片或两者；错误提示、帮助和清缓存结果始终使用文字。
- `/f1_clear_cache`、`/f1 清缓存`、`/f1 刷新缓存`：清除插件缓存，下次查询新闻会重新抓取。
- `/f1`、`/f1_help`、`/f1 帮助`：显示命令帮助。

## Tool

插件暴露以下 Tool 供 MaiBot planner/replyer 等大模型流程调用；Tool 返回形状和内容保持文本字典，不受 `news.output_mode` 影响：

- `f1_schedule`：查询下一站或相对分站赛历，返回各 session 的北京时间安排。
- `f1_results`：查询最近已完成的正赛、排位赛或冲刺赛结果；也可用相对分站指定某一站结果，不包含练习赛。
- `f1_latest_results`：查询最近一个已结束 session 的结果，包含练习、排位、冲刺和正赛；OpenF1 不可用时回退到 Jolpica 最近可用的正式 session 结果。
- `f1_daily_news`：查询近期重要 F1 新闻中文摘要，Tool 输出始终保留来源 URL。

### 新闻查询超时

新闻 Tool 和命令的总超时会在加载插件时根据配置计算，包含 RSS 抓取与重试、模型等待和结果收尾；命令还会计入发送时间，图片模式另包含渲染、发图以及发图失败后改发文字的时间。

RSS 源并发抓取，共享 `单次 HTTP 超时 × (重试次数 + 1) + 重试退避时间` 的阶段预算；重试退避依次为 0.5 秒、1 秒、1.5 秒等。单个源超过整体截止时间会记录超时，已成功源的新闻仍可继续参与摘要生成。SDK 发文字和图片的单次 RPC 等待上限均为 30 秒。

默认配置下，RSS 预算为 61.5 秒，模型等待预算为 65 秒，收尾预留 5 秒，因此 `f1_daily_news` 总超时为 131.5 秒；文字新闻命令为 161.5 秒，图片或混合输出命令为 201.5 秒。命中缓存或请求提前完成时会立即返回，无需等待预算耗尽。

**修改 `api.request_timeout_seconds`、`api.retry_count`、`model.llm_timeout_seconds` 或 `news.output_mode` 后，请保存并重载插件。** 当前 MaiBot 的配置热更新不会重新注册组件总超时，仅保存配置时 Host 仍沿用加载时的预算；插件会在预算发生变化时输出重载提醒。

## 许可证

MIT License。详见 `LICENSE`。
