from __future__ import annotations

from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"


def _font_url(file_name: str) -> str:
    return (FONT_DIR / file_name).as_uri()


def bundled_font_face_css() -> str:
    titillium_regular = _font_url("TitilliumWeb-Regular.ttf")
    titillium_semibold = _font_url("TitilliumWeb-SemiBold.ttf")
    titillium_bold = _font_url("TitilliumWeb-Bold.ttf")
    titillium_black = _font_url("TitilliumWeb-Black.ttf")
    noto_sans_sc = _font_url("NotoSansSC-VF.ttf")
    return f"""@font-face {{
      font-family: "F1 Titillium Web";
      src: url("{titillium_regular}") format("truetype");
      font-style: normal;
      font-weight: 400;
      font-display: block;
    }}
    @font-face {{
      font-family: "F1 Titillium Web";
      src: url("{titillium_semibold}") format("truetype");
      font-style: normal;
      font-weight: 600;
      font-display: block;
    }}
    @font-face {{
      font-family: "F1 Titillium Web";
      src: url("{titillium_bold}") format("truetype");
      font-style: normal;
      font-weight: 700;
      font-display: block;
    }}
    @font-face {{
      font-family: "F1 Titillium Web";
      src: url("{titillium_black}") format("truetype");
      font-style: normal;
      font-weight: 900;
      font-display: block;
    }}
    @font-face {{
      font-family: "F1 Hei";
      src: url("{noto_sans_sc}") format("truetype");
      font-style: normal;
      font-weight: 100 900;
      font-display: block;
    }}"""
