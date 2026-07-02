from __future__ import annotations

from base64 import b64encode
from functools import lru_cache
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"


@lru_cache(maxsize=None)
def _font_data_url(file_name: str) -> str:
    encoded = b64encode((FONT_DIR / file_name).read_bytes()).decode("ascii")
    return f"data:font/woff2;base64,{encoded}"


def bundled_font_face_css() -> str:
    titillium_regular = _font_data_url("TitilliumWeb-Regular.woff2")
    titillium_semibold = _font_data_url("TitilliumWeb-SemiBold.woff2")
    titillium_bold = _font_data_url("TitilliumWeb-Bold.woff2")
    titillium_black = _font_data_url("TitilliumWeb-Black.woff2")
    source_han_sans_sc = _font_data_url("SourceHanSansSC-GB2312.woff2")
    return f"""@font-face {{
      font-family: "F1 Titillium Web";
      src: url("{titillium_regular}") format("woff2");
      font-style: normal;
      font-weight: 400;
      font-display: block;
    }}
    @font-face {{
      font-family: "F1 Titillium Web";
      src: url("{titillium_semibold}") format("woff2");
      font-style: normal;
      font-weight: 600;
      font-display: block;
    }}
    @font-face {{
      font-family: "F1 Titillium Web";
      src: url("{titillium_bold}") format("woff2");
      font-style: normal;
      font-weight: 700;
      font-display: block;
    }}
    @font-face {{
      font-family: "F1 Titillium Web";
      src: url("{titillium_black}") format("woff2");
      font-style: normal;
      font-weight: 900;
      font-display: block;
    }}
    @font-face {{
      font-family: "F1 Hei";
      src: url("{source_han_sans_sc}") format("woff2");
      font-style: normal;
      font-weight: 100 900;
      font-display: block;
    }}"""
