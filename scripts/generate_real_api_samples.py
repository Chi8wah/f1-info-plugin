from __future__ import annotations
# pyright: reportAny=false, reportExplicitAny=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnusedCallResult=false

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import time
import types
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PACKAGE = ROOT / "f1_info_plugin"


def load_sdk_free_module(module_name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {module_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


package = types.ModuleType("f1_info_plugin")
package.__path__ = [str(PLUGIN_PACKAGE)]
_ = sys.modules.setdefault("f1_info_plugin", package)

constants_module = load_sdk_free_module("f1_info_plugin.constants", PLUGIN_PACKAGE / "constants.py")
models_module = load_sdk_free_module("f1_info_plugin.models", PLUGIN_PACKAGE / "models.py")
renderers_module = load_sdk_free_module("f1_info_plugin.renderers", PLUGIN_PACKAGE / "renderers.py")

BEIJING_TZ = constants_module.BEIJING_TZ
UTC = constants_module.UTC
NewsPageData = models_module.NewsPageData
NewsSummaryData = models_module.NewsSummaryData
ResultRowData = models_module.ResultRowData
ResultsPageData = models_module.ResultsPageData
SchedulePageData = models_module.SchedulePageData
ScheduleSessionData = models_module.ScheduleSessionData
RendererMixin = renderers_module.RendererMixin


OPENF1_BASE_URL = "https://api.openf1.org/v1"
JOLPICA_BASE_URL = "https://api.jolpi.ca/ergast/f1"
OUTPUT_DIR = ROOT / "data" / "previewimg" / "real-api-samples"
NEWS_SOURCE = ROOT / "data" / "mock-data" / "news.txt"
CHROME_APP = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
VIEWPORT_WIDTH = 430
CAPTURE_VIEWPORT_WIDTH = 500
DEVICE_SCALE_FACTOR = 2
MIN_SCREENSHOT_HEIGHT = 760
MAX_SCREENSHOT_HEIGHT = 3600

OPENF1_TARGETS = [
    ("race-result", "Race"),
    ("qualifying-result", "Qualifying"),
    ("sprint-result", "Sprint"),
    ("sprint-qualifying-result", "Sprint Qualifying"),
    ("practice-1-result", "Practice 1"),
    ("practice-2-result", "Practice 2"),
    ("practice-3-result", "Practice 3"),
]

SESSION_NAME_ZH = {
    "Practice 1": "一练",
    "Practice 2": "二练",
    "Practice 3": "三练",
    "Sprint Qualifying": "冲刺排位赛",
    "Sprint Shootout": "冲刺排位赛",
    "Sprint": "冲刺赛",
    "Qualifying": "排位赛",
    "Race": "正赛",
}


class SampleRenderer(RendererMixin):
    pass


def api_url(base_url: str, endpoint: str, params: dict[str, Any] | None = None) -> str:
    query = f"?{urlencode(params)}" if params else ""
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}{query}"


def fetch_json(url: str) -> Any:
    last_error: Exception | None = None
    for attempt in range(4):
        request = Request(url, headers={"User-Agent": "f1-info-plugin-real-api-samples/1.0"})
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
            return json.loads(payload)
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error


def parse_datetime(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def format_beijing(dt: datetime) -> str:
    return dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")


def zh_session_name(name: str) -> str:
    return SESSION_NAME_ZH.get(name, name or "未知 session")


def openf1_year_candidates() -> list[int]:
    current_year = datetime.now(UTC).year
    return [current_year, current_year - 1, current_year - 2]


def completed_openf1_sessions(session_name: str, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    completed: list[tuple[datetime, dict[str, Any]]] = []
    for session in sessions:
        if session.get("is_cancelled"):
            continue
        if str(session.get("session_name") or "") != session_name:
            continue
        ended_at = parse_datetime(session.get("date_end"))
        if ended_at is None or ended_at > now:
            continue
        completed.append((ended_at, session))
    return [session for _ended_at, session in sorted(completed, key=lambda item: item[0], reverse=True)]


def openf1_result_position(row: dict[str, Any]) -> int:
    try:
        return int(row.get("position") or 999)
    except (TypeError, ValueError):
        return 999


def format_seconds(seconds: float) -> str:
    if seconds >= 3600:
        hours = int(seconds // 3600)
        remainder = seconds - hours * 3600
        minutes = int(remainder // 60)
        secs = remainder - minutes * 60
        return f"{hours}:{minutes:02d}:{secs:06.3f}"
    minutes = int(seconds // 60)
    secs = seconds - minutes * 60
    return f"{minutes}:{secs:06.3f}"


def format_openf1_duration(value: Any) -> str:
    if isinstance(value, list):
        return " / ".join(part for part in (format_openf1_duration(item) for item in value) if part)
    if isinstance(value, (int, float)):
        return format_seconds(float(value))
    return str(value or "").strip()


def format_openf1_gap(value: Any) -> str:
    if isinstance(value, list):
        return " / ".join(part for part in (format_openf1_gap(item) for item in value) if part)
    if isinstance(value, (int, float)):
        if abs(float(value)) < 0.001:
            return ""
        return f"+{float(value):.3f}"
    text = str(value or "").strip()
    return text if text and text != "0" else ""


def format_openf1_result_detail(row: dict[str, Any]) -> str:
    parts = []
    duration = format_openf1_duration(row.get("duration"))
    gap = format_openf1_gap(row.get("gap_to_leader"))
    if duration:
        parts.append(duration)
    if gap:
        parts.append(gap)
    laps = row.get("number_of_laps")
    if laps not in (None, ""):
        parts.append(f"{laps}圈")
    points = row.get("points")
    if points not in (None, ""):
        points_text = f"{float(points):g}" if isinstance(points, (int, float)) else str(points)
        parts.append(f"积分 {points_text}")
    flags = [name.upper() for name in ("dnf", "dns", "dsq") if row.get(name)]
    if flags:
        parts.append("/".join(flags))
    return "，".join(parts)


def openf1_results_page(session: dict[str, Any], results: list[dict[str, Any]], drivers: dict[str, dict[str, Any]]) -> ResultsPageData:
    raw_session_name = str(session.get("session_name") or "")
    session_label = zh_session_name(raw_session_name)
    location = str(session.get("location") or session.get("circuit_short_name") or "未知分站")
    year = str(session.get("year") or "")
    title = " ".join(part for part in [year, "F1", location, f"{session_label}结果"] if part)
    ended_at = parse_datetime(session.get("date_end"))
    rows = []
    for row in sorted(results, key=openf1_result_position):
        driver_number = str(row.get("driver_number") or "")
        driver = drivers.get(driver_number, {})
        driver_label = str(driver.get("name_acronym") or driver.get("broadcast_name") or driver_number or "未知车手")
        team_name = str(driver.get("team_name") or "-")
        rows.append(
            ResultRowData(
                position=str(row.get("position") or "-"),
                driver=driver_label,
                constructor=team_name,
                primary=format_openf1_result_detail(row),
            )
        )
    return ResultsPageData(
        title=title,
        session=raw_session_name,
        rows=rows,
        end_time_text=format_beijing(ended_at) if ended_at else "",
    )


def fetch_openf1_sample(session_name: str) -> tuple[ResultsPageData, dict[str, Any]]:
    checked_urls = []
    for year in openf1_year_candidates():
        sessions_url = api_url(OPENF1_BASE_URL, "sessions", {"year": year, "session_name": session_name})
        checked_urls.append(sessions_url)
        sessions_data = fetch_json(sessions_url)
        sessions = [item for item in sessions_data if isinstance(item, dict)] if isinstance(sessions_data, list) else []
        for session in completed_openf1_sessions(session_name, sessions):
            session_key = str(session.get("session_key") or "").strip()
            if not session_key:
                continue
            results_url = api_url(OPENF1_BASE_URL, "session_result", {"session_key": session_key})
            results_data = fetch_json(results_url)
            results = [item for item in results_data if isinstance(item, dict)] if isinstance(results_data, list) else []
            if not results:
                continue
            drivers_url = api_url(OPENF1_BASE_URL, "drivers", {"session_key": session_key})
            drivers_data = fetch_json(drivers_url)
            drivers = {
                str(item.get("driver_number") or ""): item
                for item in drivers_data
                if isinstance(item, dict) and str(item.get("driver_number") or "")
            } if isinstance(drivers_data, list) else {}
            source = {
                "session_name": session_name,
                "session_key": session_key,
                "meeting_key": session.get("meeting_key"),
                "year": session.get("year"),
                "country_name": session.get("country_name"),
                "location": session.get("location"),
                "date_end": session.get("date_end"),
                "sessions_urls_checked": checked_urls,
                "session_result_url": results_url,
                "drivers_url": drivers_url,
            }
            return openf1_results_page(session, results, drivers), source
    raise RuntimeError(f"No completed OpenF1 session_result rows found for {session_name}")


def parse_jolpica_datetime(date_text: Any, time_text: Any) -> datetime | None:
    if not date_text:
        return None
    return parse_datetime(f"{date_text}T{time_text or '00:00:00Z'}")


def jolpica_round_sort_key(race: dict[str, Any]) -> tuple[int, str]:
    try:
        return int(race.get("round") or 0), str(race.get("date") or "")
    except (TypeError, ValueError):
        return 0, str(race.get("date") or "")


def schedule_session_kind(name: str) -> str:
    if "练" in name or "Practice" in name:
        return "practice"
    if "冲刺" in name or "Sprint" in name:
        return "sprint"
    return "other"


def schedule_page_from_jolpica_race(race: dict[str, Any]) -> SchedulePageData:
    mapping = [
        ("FirstPractice", "一练"),
        ("SecondPractice", "二练"),
        ("ThirdPractice", "三练"),
        ("SprintQualifying", "冲刺排位赛"),
        ("Sprint", "冲刺赛"),
        ("Qualifying", "排位赛"),
        ("Race", "正赛"),
    ]
    sessions = []
    for key, label in mapping:
        raw = {"date": race.get("date"), "time": race.get("time")} if key == "Race" else race.get(key)
        if not isinstance(raw, dict):
            continue
        start = parse_jolpica_datetime(raw.get("date"), raw.get("time"))
        if start:
            sessions.append(ScheduleSessionData(name=label, start_text=format_beijing(start), kind=schedule_session_kind(label)))
    circuit = race.get("Circuit") or {}
    location = circuit.get("Location") or {}
    place = "，".join(part for part in [location.get("locality"), location.get("country")] if part)
    return SchedulePageData(
        title=f"{race.get('season', '')} F1 {race.get('raceName', '未知分站')}",
        place=place or "未知",
        circuit=str(circuit.get("circuitName") or "未知赛道"),
        sessions=sessions,
    )


def fetch_jolpica_schedule_samples() -> dict[str, tuple[SchedulePageData, dict[str, Any]]]:
    checked_urls = []
    for year in openf1_year_candidates():
        season = "current" if year == datetime.now(UTC).year else str(year)
        url = api_url(JOLPICA_BASE_URL, f"{season}.json", {"limit": 100})
        checked_urls.append(url)
        data = fetch_json(url)
        races = (((data or {}).get("MRData") or {}).get("RaceTable") or {}).get("Races") or []
        races = sorted([race for race in races if isinstance(race, dict)], key=jolpica_round_sort_key)
        sprint_race = next((race for race in races if isinstance(race.get("Sprint"), dict)), None)
        no_sprint_race = next((race for race in races if not isinstance(race.get("Sprint"), dict)), None)
        if sprint_race and no_sprint_race:
            return {
                "schedule-without-sprint": (
                    schedule_page_from_jolpica_race(no_sprint_race),
                    {"jolpica_schedule_url": url, "round": no_sprint_race.get("round"), "raceName": no_sprint_race.get("raceName"), "urls_checked": checked_urls},
                ),
                "schedule-with-sprint": (
                    schedule_page_from_jolpica_race(sprint_race),
                    {"jolpica_schedule_url": url, "round": sprint_race.get("round"), "raceName": sprint_race.get("raceName"), "urls_checked": checked_urls},
                ),
            }
    raise RuntimeError("No Jolpica season schedule contained both sprint and non-sprint races")


def news_page_from_txt(path: Path) -> NewsPageData:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"News source is empty: {path}")
    title = lines[0]
    items = [NewsSummaryData(summary=re.sub(r"^\d+[.)、]\s*", "", line), url="") for line in lines[1:]]
    return NewsPageData(title=title, items=items, beijing_date="北京时间 2026-07-16")


def write_html(renderer: SampleRenderer, slug: str, page: ResultsPageData | SchedulePageData | NewsPageData) -> Path:
    if isinstance(page, ResultsPageData):
        html = renderer._render_results_html(page)
    elif isinstance(page, SchedulePageData):
        html = renderer._render_schedule_html(page)
    else:
        html = renderer._render_news_html(page)
    output_path = OUTPUT_DIR / f"{slug}.html"
    _ = output_path.write_text(html, encoding="utf-8")
    return output_path


def find_chrome() -> str | None:
    if CHROME_APP.exists():
        return str(CHROME_APP)
    for candidate in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def estimated_line_count(text: str, chars_per_line: int) -> int:
    if not text:
        return 0
    return max(1, (len(text) + chars_per_line - 1) // chars_per_line)


def clamp_screenshot_height(height: int) -> int:
    return min(MAX_SCREENSHOT_HEIGHT, max(MIN_SCREENSHOT_HEIGHT, height))


def estimate_screenshot_height(page: ResultsPageData | SchedulePageData | NewsPageData) -> int:
    if isinstance(page, ResultsPageData):
        title_lines = estimated_line_count(page.title, 15)
        height = 112 + title_lines * 36
        if page.end_time_text:
            height += 32
        for row in page.rows:
            detail_text = "，".join(part for part in (row.primary, row.meta) if part)
            detail_lines = estimated_line_count(detail_text, 32) if detail_text else 0
            height += 82 + detail_lines * 24
        return clamp_screenshot_height(height + 42)

    if isinstance(page, SchedulePageData):
        title_lines = estimated_line_count(page.title, 16)
        height = 208 + title_lines * 36 + len(page.sessions) * 58
        return clamp_screenshot_height(height)

    title_lines = estimated_line_count(page.title, 16)
    height = 104 + title_lines * 36
    if page.beijing_date:
        height += 32
    if page.notice:
        height += 32
    for item in page.items:
        height += 44 + estimated_line_count(item.summary, 19) * 27
    return clamp_screenshot_height(height + 40)


def screenshot_html(chrome_path: str, html_path: Path, png_path: Path, height: int) -> int:
    raw_path = png_path.with_name(f"{png_path.stem}.raw{png_path.suffix}")
    raw_path.unlink(missing_ok=True)
    command = [
        chrome_path,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        f"--force-device-scale-factor={DEVICE_SCALE_FACTOR}",
        f"--window-size={CAPTURE_VIEWPORT_WIDTH},{height}",
        f"--screenshot={raw_path}",
        html_path.resolve().as_uri(),
    ]
    try:
        try:
            _ = subprocess.run(command, check=True, capture_output=True, text=True, timeout=45)
        except subprocess.CalledProcessError:
            command[1] = "--headless"
            _ = subprocess.run(command, check=True, capture_output=True, text=True, timeout=45)

        sips_path = shutil.which("sips")
        output_width = VIEWPORT_WIDTH * DEVICE_SCALE_FACTOR
        output_height = height * DEVICE_SCALE_FACTOR
        if sips_path:
            crop_command = [sips_path, "-c", str(output_height), str(output_width), str(raw_path), "--out", str(png_path)]
            _ = subprocess.run(crop_command, check=True, capture_output=True, text=True, timeout=15)
            return output_width

        raw_path.replace(png_path)
        return CAPTURE_VIEWPORT_WIDTH * DEVICE_SCALE_FACTOR
    finally:
        raw_path.unlink(missing_ok=True)


def write_outputs() -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    renderer = SampleRenderer()
    manifest: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "viewport": {"width": VIEWPORT_WIDTH, "capture_width": CAPTURE_VIEWPORT_WIDTH, "device_scale_factor": DEVICE_SCALE_FACTOR, "min_height": MIN_SCREENSHOT_HEIGHT, "max_height": MAX_SCREENSHOT_HEIGHT},
        "samples": {},
        "screenshot_error": "",
    }
    pages: dict[str, tuple[ResultsPageData | SchedulePageData | NewsPageData, dict[str, Any]]] = {}

    for slug, session_name in OPENF1_TARGETS:
        pages[slug] = fetch_openf1_sample(session_name)
    pages.update(fetch_jolpica_schedule_samples())
    pages["news"] = (news_page_from_txt(NEWS_SOURCE), {"source_file": str(NEWS_SOURCE.relative_to(ROOT))})

    chrome_path = find_chrome()
    if not chrome_path:
        manifest["screenshot_error"] = f"Chrome not found. Checked {CHROME_APP} and PATH chromium/google-chrome candidates."

    for slug, (page, source) in pages.items():
        html_path = write_html(renderer, slug, page)
        png_path = OUTPUT_DIR / f"{slug}.png"
        png_path.unlink(missing_ok=True)
        screenshot_height = estimate_screenshot_height(page)
        screenshot_width = VIEWPORT_WIDTH * DEVICE_SCALE_FACTOR
        screenshot_status = "skipped"
        screenshot_error = ""
        if chrome_path:
            try:
                screenshot_width = screenshot_html(chrome_path, html_path, png_path, screenshot_height)
                screenshot_status = "ok"
            except Exception as exc:
                screenshot_status = "failed"
                screenshot_error = str(exc)
        manifest["samples"][slug] = {
            "title": page.title,
            "source": source,
            "html": str(html_path.relative_to(ROOT)),
            "png": str(png_path.relative_to(ROOT)) if screenshot_status == "ok" and png_path.exists() else "",
            "screenshot_viewport": {"width": screenshot_width, "height": screenshot_height * DEVICE_SCALE_FACTOR},
            "screenshot_status": screenshot_status,
            "screenshot_error": screenshot_error,
        }

    manifest_path = OUTPUT_DIR / "manifest.json"
    _ = manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    manifest = write_outputs()
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
