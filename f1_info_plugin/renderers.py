from __future__ import annotations

from html import escape as html_escape

from .font_assets import bundled_font_face_css
from .models import NewsPageData, NewsSummaryData, ResultRowData, ResultsPageData, SchedulePageData


class RendererMixin:

    @staticmethod
    def _render_schedule_text(page: SchedulePageData) -> str:
        lines = [page.title, f"举办地：{page.place or '未知'}", f"赛道：{page.circuit or '未知赛道'}", "时间安排（北京时间）："]
        for session in page.sessions:
            lines.append(f"- {session.name}：{session.start_text}")
        return "\n".join(lines)

    @staticmethod
    def _render_results_text(page: ResultsPageData) -> str:
        lines = [*page.notices, page.title]
        if page.end_time_text:
            lines.append(f"结束时间：{page.end_time_text}")
        for row in page.rows:
            detail = f" {row.primary}" if row.primary else ""
            if row.meta:
                detail = f"{detail}，{row.meta}" if detail else f" {row.meta}"
            lines.append(f"{row.position}. {row.driver} ({row.constructor}){detail}")
        return "\n".join(lines)

    @staticmethod
    def _render_news_text(page: NewsPageData, include_urls: bool = True) -> str:
        lines = [page.title]
        if page.beijing_date:
            lines.append(page.beijing_date)
        if page.notice:
            lines.append(page.notice)
        for idx, item in enumerate(page.items, 1):
            if page.using_raw_fallback:
                lines.append(f"{idx}. {item.summary}")
                if include_urls and item.url:
                    lines.append(f"   URL：{item.url}")
            else:
                suffix = f" {item.url}" if include_urls and item.url else ""
                lines.append(f"{idx}. {item.summary}{suffix}")
        return "\n".join(lines)

    @staticmethod
    def _page_shell_html(title: str, body: str, accent_at: str = "18% 0%") -> str:
        safe_title = html_escape(title, quote=True)
        safe_accent_at = html_escape(accent_at, quote=True)
        font_face_css = bundled_font_face_css()
        return f'''<!doctype html>
    <html lang="zh-CN">
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{safe_title}</title>
    <style>
    {font_face_css}
    :root {{
      --bg: #06080b;
      --surface: #111820;
      --card: #151d28;
      --card-soft: #1a2430;
      --line: #2b3747;
      --text: #f7f9fc;
      --muted: #aab4c2;
      --red: #e10600;
      --green: #22c55e;
      --gold: #f6c744;
      --silver: #cbd5e1;
      --bronze: #d9995c;
      --carbon: rgba(255, 255, 255, 0.045);
      --shadow: 0 18px 48px rgba(0, 0, 0, 0.35);
      --font: "F1 Titillium Web", "F1 Hei", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    html {{ background: var(--bg); }}
    body {{
      margin: 0;
      min-height: 100vh;
      overflow-x: hidden;
      color: var(--text);
      font-family: var(--font);
      font-size: 16px;
      line-height: 1.62;
      background:
    radial-gradient(circle at {safe_accent_at}, rgba(225, 6, 0, 0.24), transparent 34%),
    linear-gradient(135deg, rgba(255, 255, 255, 0.05) 0 10%, transparent 10% 20%, rgba(255, 255, 255, 0.035) 20% 30%, transparent 30% 100%),
    var(--bg);
    }}
    .page {{ width: 100%; max-width: 390px; margin: 0 auto; padding: 16px 12px 22px; }}
    .hero, .event-card {{ overflow: hidden; border: 1px solid var(--line); border-radius: 22px; box-shadow: var(--shadow); }}
    .hero, .event-head {{
      position: relative;
      overflow: hidden;
      padding: 20px;
      background:
    linear-gradient(145deg, rgba(225, 6, 0, 0.2), transparent 45%),
    repeating-linear-gradient(90deg, transparent 0 14px, var(--carbon) 14px 15px),
    var(--surface);
    }}
    .hero::before, .event-head::before {{ content: ""; display: block; width: 40px; height: 4px; margin-bottom: 14px; background: var(--red); transform: skewX(-24deg); }}
    .hero::after, .event-head::after {{ content: ""; position: absolute; right: 18px; bottom: 18px; width: 82px; height: 5px; background: var(--red); box-shadow: -24px 13px 0 rgba(225, 6, 0, 0.42), -50px 26px 0 rgba(225, 6, 0, 0.18); transform: skewX(-28deg); }}
    h1 {{ position: relative; z-index: 1; margin: 0; font-size: clamp(27px, 7.2vw, 32px); line-height: 1.1; word-break: keep-all; overflow-wrap: break-word; }}
    .event-card, .result-row, .news-card {{ background: linear-gradient(180deg, var(--card-soft), var(--card)); }}
    .event-head {{ border-bottom: 1px solid var(--line); }}
    .event-info, .sessions, .rows, .news-list {{ display: grid; gap: 10px; }}
    .event-info {{ padding: 14px; border-bottom: 1px solid var(--line); }}
    .info-row, .session-row {{ border: 1px solid var(--line); border-radius: 16px; background: rgba(255, 255, 255, 0.035); }}
    .info-row {{ margin: 0; padding: 13px 14px; color: var(--muted); font-size: 15px; line-height: 1.45; font-weight: 600; }}
    .info-row strong {{ color: var(--text); font-weight: 600; }}
    .schedule-title {{ margin: 0; padding: 16px 16px 0; color: var(--text); font-size: 17px; font-weight: 700; }}
    .sessions {{ padding: 10px 14px 14px; }}
    .session-row {{ position: relative; display: grid; grid-template-columns: minmax(84px, auto) 1fr; gap: 12px; align-items: center; padding: 13px 14px; overflow: hidden; }}
    .session-row::before, .result-row::before, .news-card::before {{ content: ""; position: absolute; inset: 0 auto 0 0; width: 4px; background: var(--red); }}
    .session-row.practice::before {{ background: var(--green); }}
    .session-row.sprint::before {{ background: var(--gold); }}
    .session-name {{ color: var(--text); font-size: 16px; font-weight: 600; line-height: 1.3; }}
    .session-time {{ color: var(--muted); font-size: 15px; font-weight: 700; line-height: 1.35; text-align: right; }}
    .end-time, .notice, .news-date {{ margin: 12px 0 0; color: var(--muted); font-size: 16px; font-weight: 700; }}
    .rows, .news-list {{ margin-top: 14px; }}
    .result-row, .news-card {{ position: relative; overflow: hidden; padding: 12px; border: 1px solid var(--line); border-radius: 16px; }}
    .row-main {{ display: flex; align-items: center; gap: 10px; }}
    .pos, .rank {{ display: grid; place-items: center; width: 34px; height: 34px; border-radius: 10px; color: var(--text); background: var(--rank-color, #445062); font-size: 22px; font-weight: 700; line-height: 1; transform: skewX(-10deg); }}
    .rank {{ color: var(--text); background: var(--red); }}
    .gold {{ --rank-color: var(--gold); color: var(--bg); }} .silver {{ --rank-color: var(--silver); color: var(--bg); }} .bronze {{ --rank-color: var(--bronze); color: var(--bg); }}
    .driver {{ min-width: 0; flex: 1; display: flex; gap: 10px; align-items: baseline; }}
    .code {{ display: block; color: var(--text); font-size: 26px; font-weight: 900; line-height: 1.08; letter-spacing: 0.04em; }}
    .team {{ display: block; margin-top: 4px; color: var(--muted); font-size: 15px; font-weight: 600; line-height: 1.3; }}
    .detail {{ display: grid; gap: 5px; margin-top: 6px; padding-left: 44px; }}
    .time {{ color: var(--text); font-size: 15px; font-weight: 700; line-height: 1.32; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; word-break: break-word; text-wrap: pretty; }}
    .meta {{ color: var(--muted); font-size: 14px; font-weight: 750; line-height: 1.35; }}
    .news-card {{ display: grid; grid-template-columns: auto 1fr; gap: 12px; padding: 16px; }}
    .news-card p {{ margin: 0; color: var(--text); font-size: 15px; line-height: 1.68; }}
    .news-card a {{ color: var(--muted); overflow-wrap: anywhere; text-decoration: none; }}
    </style>
    </head>
    <body>
    {body}
    </body>
    </html>'''

    def _render_schedule_html(self, page: SchedulePageData) -> str:
        title = html_escape(page.title, quote=True)
        place = html_escape(page.place or "未知", quote=True)
        circuit = html_escape(page.circuit or "未知赛道", quote=True)
        sessions = "".join(
            f'<div class="session-row {html_escape(session.kind, quote=True)}"><span class="session-name">{html_escape(session.name, quote=True)}</span><span class="session-time">{html_escape(session.start_text, quote=True)}</span></div>'
            for session in page.sessions
        )
        body = f'''  <main class="page">
    <article class="event-card">
      <header class="event-head"><h1>{title}</h1></header>
      <div class="event-info">
        <p class="info-row"><strong>举办地：</strong>{place}</p>
        <p class="info-row"><strong>赛道：</strong>{circuit}</p>
      </div>
      <p class="schedule-title">时间安排（北京时间）：</p>
      <div class="sessions">{sessions}</div>
    </article>
      </main>'''
        return self._page_shell_html(page.title, body, "80% 0%")

    def _render_results_html(self, page: ResultsPageData) -> str:
        title = html_escape(page.title, quote=True)
        end_time = f'<p class="end-time">结束时间：{html_escape(page.end_time_text, quote=True)}</p>' if page.end_time_text else ""
        notices = "".join(f'<p class="notice">{html_escape(notice, quote=True)}</p>' for notice in page.notices)
        rows = "".join(self._render_result_row_html(row) for row in page.rows)
        body = f'''  <main class="page">
    <header class="hero"><h1>{title}</h1>{end_time}{notices}</header>
    <section class="rows" data-session="{html_escape(page.session, quote=True)}">{rows}</section>
      </main>'''
        return self._page_shell_html(page.title, body, "18% 0%")

    def _render_result_row_html(self, row: ResultRowData) -> str:
        rank_class = self._result_rank_class(row.position)
        primary = html_escape(row.primary, quote=True)
        meta = html_escape(row.meta or row.status, quote=True)
        primary_html = f'<span class="time">{primary}</span>' if primary else ""
        meta_html = f'<span class="meta">{meta}</span>' if meta else ""
        detail_html = f'<div class="detail">{primary_html}{meta_html}</div>' if primary_html or meta_html else ""
        return (
            f'<article class="result-row"><div class="row-main"><span class="pos {rank_class}">{html_escape(row.position, quote=True)}</span>'
            f'<span class="driver"><span class="code">{html_escape(row.driver, quote=True)}</span>'
            f'<span class="team">{html_escape(row.constructor, quote=True)}</span></span></div>{detail_html}</article>'
        )

    @staticmethod
    def _result_rank_class(position: str) -> str:
        return {"1": "gold", "2": "silver", "3": "bronze"}.get(str(position).strip(), "")

    def _render_news_html(self, page: NewsPageData) -> str:
        title = html_escape(page.title, quote=True)
        date = f'<p class="news-date">{html_escape(page.beijing_date, quote=True)}</p>' if page.beijing_date else ""
        notice = f'<p class="notice">{html_escape(page.notice, quote=True)}</p>' if page.notice else ""
        cards = "".join(
            self._render_news_card_html(idx, item)
            for idx, item in enumerate(page.items, 1)
        )
        body = f'''  <main class="page">
    <header class="hero"><h1>{title}</h1>{date}{notice}</header>
    <section class="news-list">{cards}</section>
      </main>'''
        return self._page_shell_html(page.title, body, "15% 0%")

    @staticmethod
    def _render_news_card_html(idx: int, item: NewsSummaryData) -> str:
        summary = html_escape(item.summary, quote=True).replace("\n", "<br>")
        return f'<article class="news-card"><span class="rank">{idx}</span><p>{summary}</p></article>'
