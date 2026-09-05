from __future__ import annotations
# pyright: reportAttributeAccessIssue=false

from html import escape as html_escape
from typing import Any

import asyncio

from .constants import (
    EXTERNAL_CATEGORY_PHRASES,
    EXTERNAL_CONTEXT_LABELS,
    EXTERNAL_SOURCE_LABELS,
    OUTPUT_CARD_BODY_FONT_SIZE,
    OUTPUT_CARD_BODY_LINE_HEIGHT,
    OUTPUT_CARD_COMPACT_BODY_FONT_SIZE,
    OUTPUT_CARD_COMPACT_BODY_LINE_HEIGHT,
    OUTPUT_IMAGE_RENDER_TIMEOUT_SECONDS,
    OUTPUT_IMAGE_VIEWPORT,
    OUTPUT_MODE_VALUES,
    OUTPUT_SEND_TIMEOUT_SECONDS,
)
from .font_assets import bundled_font_face_css
from .models import F1ExternalApiError, NewsPageData, ResultsPageData, SchedulePageData


class OutputMixin:

    def _context_error_message(self, context: str, exc: BaseException) -> str:
        context_label = EXTERNAL_CONTEXT_LABELS.get(context, "查询")
        if isinstance(exc, F1ExternalApiError):
            source_label = EXTERNAL_SOURCE_LABELS.get(exc.source, EXTERNAL_SOURCE_LABELS["unknown"])
            phrase = EXTERNAL_CATEGORY_PHRASES.get(exc.category, EXTERNAL_CATEGORY_PHRASES["unknown"])
            return f"F1 {context_label}数据源 {source_label} {phrase}，请稍后重试。"
        return f"F1 {context_label}查询执行异常，请稍后重试。"

    def _log_external_exception(self, context: str, exc: BaseException) -> None:
        if isinstance(exc, F1ExternalApiError):
            self._log_warning(
                "F1 %s 外部接口失败: source=%s category=%s status=%s url=%s error=%s",
                context,
                exc.source,
                exc.category,
                exc.status_code if exc.status_code is not None else "-",
                exc.redacted_url or "-",
                exc.__cause__ or exc,
            )
            return
        logger_obj = getattr(getattr(self, "ctx", None), "logger", None)
        if logger_obj is not None:
            logger_obj.exception("F1 %s 执行异常: %s", context, exc)

    def _tool_error_result(self, name: str, context: str, exc: BaseException) -> dict[str, str]:
        self._log_external_exception(context, exc)
        return {"name": name, "content": self._context_error_message(context, exc)}

    async def _send_command_error(self, stream_id: str, context: str, exc: BaseException) -> tuple[bool, str, bool]:
        self._log_external_exception(context, exc)
        message = self._context_error_message(context, exc)
        if not stream_id:
            return False, message, True
        try:
            await self.ctx.send.text(
                message, stream_id, rpc_timeout_ms=OUTPUT_SEND_TIMEOUT_SECONDS * 1000
            )
        except Exception as send_exc:
            self._log_warning("发送 F1 %s 错误提示失败: %s", context, send_exc)
            return False, message, True
        return True, message, True

    async def _send_scheduled_news_error(self, batch: list[dict[str, Any]], date_key: str, exc: BaseException) -> None:
        self._log_external_exception("scheduled_news", exc)
        message = f"定时 F1 新闻发布失败：{self._context_error_message('news', exc)}"
        for job in batch:
            for stream_id in job["stream_ids"]:
                publish_key = f"{date_key}:{job['time']}:{stream_id}"
                try:
                    await self.ctx.send.text(
                        message, stream_id, rpc_timeout_ms=OUTPUT_SEND_TIMEOUT_SECONDS * 1000
                    )
                    self._published_schedule_keys.add(publish_key)
                except Exception as send_exc:
                    self._log_warning("发送定时 F1 新闻错误提示失败: %s", send_exc)

    @staticmethod
    def _normalize_output_mode(value: object) -> str:
        mode = str(value or "text").strip().lower()
        return mode if mode in OUTPUT_MODE_VALUES else "text"

    def _output_mode(self) -> str:
        return self._normalize_output_mode(self.config.news.output_mode)

    @staticmethod
    def _output_card_html(
        title: str,
        text: str,
        *,
        body_font_size: str = OUTPUT_CARD_BODY_FONT_SIZE,
        body_line_height: str = OUTPUT_CARD_BODY_LINE_HEIGHT,
    ) -> str:
        safe_title = html_escape(title, quote=True)
        safe_text = html_escape(text, quote=True)
        safe_body_font_size = html_escape(body_font_size, quote=True)
        safe_body_line_height = html_escape(body_line_height, quote=True)
        font_face_css = bundled_font_face_css()
        return f"""<!doctype html>
    <html lang="zh-CN">
    <head>
    <meta charset="utf-8">
    <style>
    {font_face_css}
    :root {{
      --f1-bg: #0b0f14;
      --f1-card: #f8fafc;
      --f1-text: #111827;
      --f1-muted: #64748b;
      --f1-accent: #e10600;
      --f1-border: #e2e8f0;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 24px;
      background: var(--f1-bg);
      color: var(--f1-text);
      font-family: "F1 Titillium Web", "F1 Hei", sans-serif;
    }}
    .f1-card {{
      width: 712px;
      padding: 28px;
      border: 1px solid var(--f1-border);
      border-radius: 24px;
      background: var(--f1-card);
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.28);
    }}
    .f1-kicker {{
      margin: 0 0 10px;
      color: var(--f1-accent);
      font-size: 13px;
      font-weight: 800;
      letter-spacing: 0.16em;
      text-transform: uppercase;
    }}
    .f1-title {{
      margin: 0 0 20px;
      color: var(--f1-text);
      font-size: 28px;
      line-height: 1.2;
      font-weight: 800;
    }}
    .f1-body {{
      margin: 0;
      color: var(--f1-text);
      font-family: inherit;
      font-size: {safe_body_font_size};
      line-height: {safe_body_line_height};
      white-space: pre-wrap;
    }}
    .f1-footer {{
      margin-top: 22px;
      padding-top: 14px;
      border-top: 1px solid var(--f1-border);
      color: var(--f1-muted);
      font-size: 13px;
    }}
    </style>
    </head>
    <body>
    <article class="f1-card">
      <p class="f1-kicker">Formula 1</p>
      <h1 class="f1-title">{safe_title}</h1>
      <pre class="f1-body">{safe_text}</pre>
      <div class="f1-footer">F1 资讯插件</div>
    </article>
    </body>
    </html>"""

    def _rendered_image_base64(self, result: Any) -> str:
        payload = self._peel_envelope(result)
        image_base64 = payload.get("image_base64") if isinstance(payload, dict) else getattr(payload, "image_base64", "")
        if not isinstance(image_base64, str) or not image_base64:
            raise RuntimeError("html2png 未返回 image_base64")
        return image_base64

    async def _render_output_image(
        self,
        title: str,
        text: str,
        *,
        body_font_size: str = OUTPUT_CARD_BODY_FONT_SIZE,
        body_line_height: str = OUTPUT_CARD_BODY_LINE_HEIGHT,
    ) -> str:
        html = self._output_card_html(
            title,
            text,
            body_font_size=body_font_size,
            body_line_height=body_line_height,
        )
        result = await asyncio.wait_for(
            self.ctx.render.html2png(
                html,
                selector=".f1-card",
                viewport=OUTPUT_IMAGE_VIEWPORT,
                device_scale_factor=2,
                allow_network=False,
                wait_for_selector=".f1-card",
                wait_for_timeout_ms=100,
                render_timeout_ms=OUTPUT_IMAGE_RENDER_TIMEOUT_SECONDS * 1000,
            ),
            timeout=OUTPUT_IMAGE_RENDER_TIMEOUT_SECONDS,
        )
        return self._rendered_image_base64(result)

    async def _render_html_image(self, html: str, selector: str = ".page") -> str:
        result = await asyncio.wait_for(
            self.ctx.render.html2png(
                html,
                selector=selector,
                viewport={"width": 430, "height": 1400},
                device_scale_factor=2,
                allow_network=False,
                wait_for_selector=selector,
                wait_for_timeout_ms=100,
                render_timeout_ms=OUTPUT_IMAGE_RENDER_TIMEOUT_SECONDS * 1000,
            ),
            timeout=OUTPUT_IMAGE_RENDER_TIMEOUT_SECONDS,
        )
        return self._rendered_image_base64(result)

    async def _send_page_output(
        self,
        stream_id: str,
        title: str,
        page: SchedulePageData | ResultsPageData | NewsPageData | str,
        render_text_fn: Any,
        render_html_fn: Any,
        *,
        mode: str | None = None,
        image_base64: str | None = None,
        render_on_missing: bool = True,
        body_font_size: str = OUTPUT_CARD_COMPACT_BODY_FONT_SIZE,
        body_line_height: str = OUTPUT_CARD_COMPACT_BODY_LINE_HEIGHT,
    ) -> None:
        if isinstance(page, str):
            await self._send_user_output(
                stream_id,
                title,
                page,
                mode=mode,
                image_base64=image_base64,
                render_on_missing=render_on_missing,
                body_font_size=body_font_size,
                body_line_height=body_line_height,
            )
            return

        output_mode = self._normalize_output_mode(mode or self._output_mode())
        text = render_text_fn(page)
        sent_text = False
        if output_mode in {"text", "both"}:
            await self.ctx.send.text(
                text, stream_id, rpc_timeout_ms=OUTPUT_SEND_TIMEOUT_SECONDS * 1000
            )
            sent_text = True
        if output_mode not in {"image", "both"}:
            return
        try:
            image_payload = image_base64
            if image_payload is None:
                if not render_on_missing:
                    raise RuntimeError("图片渲染不可用")
                image_payload = await self._render_html_image(render_html_fn(page))
            await self.ctx.send.image(
                image_payload, stream_id, rpc_timeout_ms=OUTPUT_SEND_TIMEOUT_SECONDS * 1000
            )
        except Exception as exc:
            self._log_warning("发送 F1 结构化图片输出失败，降级为文本: %s", exc)
            if not sent_text:
                await self.ctx.send.text(
                    text, stream_id, rpc_timeout_ms=OUTPUT_SEND_TIMEOUT_SECONDS * 1000
                )

    async def _send_user_output(
        self,
        stream_id: str,
        title: str,
        text: str,
        *,
        mode: str | None = None,
        image_base64: str | None = None,
        render_on_missing: bool = True,
        body_font_size: str = OUTPUT_CARD_BODY_FONT_SIZE,
        body_line_height: str = OUTPUT_CARD_BODY_LINE_HEIGHT,
    ) -> None:
        output_mode = self._normalize_output_mode(mode or self._output_mode())
        sent_text = False
        if output_mode in {"text", "both"}:
            await self.ctx.send.text(
                text, stream_id, rpc_timeout_ms=OUTPUT_SEND_TIMEOUT_SECONDS * 1000
            )
            sent_text = True
        if output_mode not in {"image", "both"}:
            return
        try:
            image_payload = image_base64
            if image_payload is None:
                if not render_on_missing:
                    raise RuntimeError("图片渲染不可用")
                image_payload = await self._render_output_image(
                    title,
                    text,
                    body_font_size=body_font_size,
                    body_line_height=body_line_height,
                )
            await self.ctx.send.image(
                image_payload, stream_id, rpc_timeout_ms=OUTPUT_SEND_TIMEOUT_SECONDS * 1000
            )
        except Exception as exc:
            self._log_warning("发送 F1 图片输出失败，降级为文本: %s", exc)
            if not sent_text:
                await self.ctx.send.text(
                    text, stream_id, rpc_timeout_ms=OUTPUT_SEND_TIMEOUT_SECONDS * 1000
                )
