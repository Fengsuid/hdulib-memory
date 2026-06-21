import asyncio
import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import secrets
import sys
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib import error as urllib_error
from urllib import request as urllib_request

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from utils.api_client import LibraryAPIClient
from utils.browser_auth import HDU_BASE_URL, MY_APPOINT_PATH, parse_cookie_header
from utils.config import ConfigManager
from utils.memory import (
    HTML_TEMPLATE_FILE,
    MemoryFilters,
    _find_matching_brace,
    build_report_data,
    compute_stats,
    normalize_remote_records,
)


def load_local_env() -> None:
    env_path = ROOT_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_local_env()

APP_HOST = os.environ.get("HDULIB_MEMORY_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("HDULIB_MEMORY_PORT", "8018"))
DETAIL_LIMIT = os.environ.get("HDULIB_DETAIL_LIMIT", "300")
DETAIL_LIMIT_VALUE = None if DETAIL_LIMIT.lower() == "none" else int(DETAIL_LIMIT)
INCLUDE_NO_SEAT = os.environ.get("HDULIB_INCLUDE_NO_SEAT", "1") != "0"
OFFICIAL_LIBRARY_URL = f"{HDU_BASE_URL}/#!{MY_APPOINT_PATH}"
EXPORT_HELPER_VERSION = "20260620-v3"
PROXY_LOGIN_URL = "/hdu/User/Index/hduCASLogin?forward=%2FUser%2FCenter%2FmyAppoint"
STORAGE_DIR = Path(os.environ.get("HDULIB_STORAGE_DIR", ROOT_DIR / "storage"))
USER_DATA_DIR = STORAGE_DIR / "users"
PUBLIC_REPORT_DIR = STORAGE_DIR / "public_reports"
PUBLIC_BASE_URL = os.environ.get("HDULIB_PUBLIC_BASE_URL", f"http://{APP_HOST}:{APP_PORT}").rstrip("/")
AI_BASE_URL = os.environ.get("HDULIB_AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
AI_MODEL = os.environ.get("HDULIB_AI_MODEL", "gpt-4o-mini")
AI_API_KEY = os.environ.get("HDULIB_AI_API_KEY", "")
APP_SIGNING_SECRET = os.environ.get("HDULIB_SIGNING_SECRET") or hashlib.sha256(
    (AI_API_KEY or "hdulib-memory-dev-secret").encode("utf-8")
).hexdigest()
POST_BODY_LIMITS = {
    "/hakimi-review": 500_000,
    "/share-report": 5_000_000,
    "/generate": 80_000_000,
}
RATE_LIMITS = {
    "/hakimi-review": (12, 60),
    "/share-report": (20, 300),
    "/generate": (8, 300),
}
RATE_BUCKETS: dict[tuple[str, str], deque[float]] = defaultdict(deque)
ALLOWED_POST_HOSTS = {
    urlparse(PUBLIC_BASE_URL).netloc.lower(),
    f"{APP_HOST}:{APP_PORT}",
    "127.0.0.1:8018",
    "localhost:8018",
}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "media-src 'self'; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "upgrade-insecure-requests"
    ),
}
# Paths that should redirect to their /hdu/ equivalent (safety net for relative redirects from library)
HDU_PATH_REDIRECTS = {
    "/User/Center/myAppoint": "/hdu/User/Center/myAppoint",
    "/User/Index/hduCASLogin": "/hdu/User/Index/hduCASLogin",
    "/Seat/Index/myBookingList": "/hdu/Seat/Index/myBookingList",
    "/Seat/Index/myNoSeatBookingList": "/hdu/Seat/Index/myNoSeatBookingList",
}


def render_page(title: str, body: str, status: int = 200) -> bytes:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --paper: #f3e4c6;
      --ink: #211a14;
      --muted: #6f604e;
      --red: #b2211d;
      --line: rgba(33, 26, 20, 0.16);
      --dark: #171310;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100svh;
      color: var(--ink);
      background:
        linear-gradient(90deg, rgba(33, 26, 20, 0.04) 1px, transparent 1px),
        linear-gradient(180deg, rgba(33, 26, 20, 0.035) 1px, transparent 1px),
        #211914;
      background-size: 18px 18px, 24px 24px, auto;
      font-family: "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif;
    }}
    main {{
      width: min(920px, calc(100% - 28px));
      margin: 0 auto;
      padding: 26px 0 42px;
    }}
    .hero, .panel {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background:
        linear-gradient(180deg, rgba(255, 249, 233, 0.86), rgba(222, 199, 158, 0.94)),
        var(--paper);
    }}
    .hero {{ padding: 22px; }}
    .panel {{ margin-top: 14px; padding: 18px; }}
    .kicker {{
      margin: 0 0 10px;
      color: var(--red);
      font-size: 0.78rem;
      font-weight: 900;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(1.65rem, 4vw, 2.55rem);
      line-height: 1.14;
    }}
    h2 {{ margin: 0 0 10px; font-size: 1.15rem; }}
    p {{ color: var(--muted); line-height: 1.72; }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 16px;
    }}
    a.button, button {{
      min-height: 44px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0 16px;
      border: 2px solid var(--red);
      border-radius: 8px;
      color: #fff7e8;
      background: var(--red);
      font-weight: 900;
      text-decoration: none;
      cursor: pointer;
      box-shadow: 4px 4px 0 rgba(33, 26, 20, 0.18);
    }}
    a.button.secondary {{
      color: var(--red);
      background: rgba(255, 255, 255, 0.38);
    }}
    textarea, input {{
      width: 100%;
      padding: 11px 12px;
      border: 1px solid rgba(33, 26, 20, 0.22);
      border-radius: 8px;
      color: var(--ink);
      background: rgba(255, 255, 255, 0.42);
      font: inherit;
    }}
    textarea {{ min-height: 120px; resize: vertical; font-family: Consolas, monospace; }}
    label {{ display: block; margin-top: 12px; color: var(--muted); font-weight: 800; }}
    .notice {{
      padding: 12px;
      border-left: 4px solid var(--red);
      background: rgba(255,255,255,0.32);
    }}
    .hint {{
      color: var(--muted);
      font-size: 0.82rem;
      margin: 0 0 8px;
    }}
    .steps {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }}
    .step {{
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,0.3);
    }}
    .step strong {{ display: block; margin-bottom: 6px; }}
    .step span {{ color: var(--muted); font-size: 0.86rem; line-height: 1.55; }}
    /* New homepage step cards */
    .steps-v2 {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}
    .step-card {{
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(255,255,255,0.35);
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .step-num {{
      width: 32px;
      height: 32px;
      display: grid;
      place-items: center;
      border-radius: 50%;
      background: var(--red);
      color: #fff;
      font-weight: 900;
      font-size: 1.1rem;
      flex-shrink: 0;
    }}
    .drag-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 48px;
      padding: 0 22px;
      border: 2px dashed var(--red);
      border-radius: 10px;
      color: var(--red);
      background: rgba(178, 33, 29, 0.06);
      font-weight: 900;
      font-size: 1.05rem;
      text-decoration: none;
      cursor: grab;
      user-select: none;
      transition: all 0.15s;
    }}
    .drag-btn:hover {{
      background: rgba(178, 33, 29, 0.12);
      border-style: solid;
    }}
    details summary {{
      padding: 8px 0;
      color: var(--red);
    }}
    details .notice {{
      font-size: 0.85rem;
      margin-top: 6px;
    }}
    .code-box {{
      transition: border-color 0.15s;
    }}
    .code-box:focus {{
      outline: 2px solid var(--red);
      outline-offset: 2px;
    }}
    @media (max-width: 760px) {{
      .steps-v2 {{ grid-template-columns: 1fr; }}
      .steps {{ grid-template-columns: 1fr; }}
      .actions {{ display: grid; }}
      a.button, button {{ width: 100%; }}
    }}
  </style>
</head>
<body><main>{body}</main></body>
</html>""".encode("utf-8")


def home_page() -> bytes:
    console_code = (
        "var s=document.createElement('script');"
        f"s.src='{PUBLIC_BASE_URL}/export-helper.js?v={EXPORT_HELPER_VERSION}';"
        "document.body.appendChild(s)"
    )
    body = f"""
<section class="hero">
  <p class="kicker">HDU LIBRARY MEMORY</p>
  <h1>生成你的大学四年图书馆诊断报告</h1>
  <p>基于你的图书馆预约记录，分析入馆习惯、座位偏好、学期节奏，生成专属标签和锐评。</p>
</section>

<section class="panel">
  <h2>导出数据（电脑操作，约30秒）</h2>
  <div class="steps-v2">
    <div class="step-card">
      <div class="step-num">1</div>
      <strong>打开图书馆并登录</strong>
      <span>打开 <code>hdu.huitu.zhishulib.com</code>，用统一认证登录，停留在任意页面。</span>
      <a class="button secondary" href="{OFFICIAL_LIBRARY_URL}" target="_blank" rel="noopener" style="margin-top:6px">打开图书馆页面</a>
    </div>
    <div class="step-card">
      <div class="step-num">2</div>
      <strong>按F12打开控制台</strong>
      <span>在图书馆页面按 <kbd>F12</kbd>（Mac：<kbd>Cmd+Option+J</kbd>），点 <strong>Console</strong> 标签。</span>
    </div>
    <div class="step-card">
      <div class="step-num">3</div>
      <strong>粘贴代码回车</strong>
      <span>点击下方代码框自动全选，<strong>Ctrl+C</strong>，在 Console <strong>Ctrl+V</strong>，<strong>Enter</strong>。</span>
    </div>
  </div>
  <div style="margin-top:14px">
    <p style="font-weight:800;margin-bottom:6px">要复制的代码（点击自动全选，Ctrl+C 复制）：</p>
    <textarea readonly class="code-box" onclick="this.select()" rows="2" style="width:100%;font:13px ui-monospace,Consolas,monospace;padding:10px;background:#1e1e1e;color:#d4d4d4;border:none;border-radius:6px;resize:none;cursor:pointer">{console_code}</textarea>
  </div>
  <p class="notice" style="margin-top:10px">
    粘贴后如果提示 <code>allow pasting</code>，先在 Console 输入 <code>allow pasting</code> 回车，再粘贴。
  </p>
</section>

<section class="panel">
  <h2>安全与隐私声明</h2>
  <p class="notice">
    本工具不是学校官方系统，只用于根据你主动导入的图书馆预约记录生成纪念报告。推荐使用导出助手生成 JSON 后导入；Cookie 方式仅作为备用，不建议长期使用，也不要把统一认证密码填写到本站。
  </p>
  <div class="steps">
    <div class="step">
      <strong>数据使用</strong>
      <span>报告只基于你提交的预约记录生成。选择“保存”时，服务器会保存预约 JSON 和生成后的报告数据。</span>
    </div>
    <div class="step">
      <strong>公开分享</strong>
      <span>点击分享或保存公开报告后，对应链接可被访问。请确认你愿意公开学号、统计数据和生成评价。</span>
    </div>
    <div class="step">
      <strong>口令提醒</strong>
      <span>保存口令只用于本站读取已保存档案，请单独设置，不要使用统一认证密码、邮箱密码或常用密码。</span>
    </div>
    <div class="step">
      <strong>风险边界</strong>
      <span>导入前请确认数据来源可信。若不希望服务器保存数据，请不要勾选保存，也不要点击分享报告。</span>
    </div>
  </div>
</section>

<section class="panel">
  <h2>手动导入数据</h2>
  <form method="post" action="/generate">
    <label>学号或昵称</label>
    <input name="student_id" placeholder="例如 202201010001。需要保存时必填。" style="margin-bottom:12px">

    <label>从文件导入（支持 .json / .txt）</label>
    <input type="file" id="file-input" accept=".json,.txt" style="margin-bottom:6px">
    <p class="hint">选择之前导出的 JSON 文件，内容会自动填入下方文本框。</p>

    <label>预约记录 JSON</label>
    <textarea name="history_json" id="json-textarea" placeholder="从上方选择文件导入，或直接粘贴 JSON 数据"></textarea>

    <label>Cookie 方式（备用）</label>
    <textarea name="cookie_header" placeholder="粘贴 hdu.huitu.zhishulib.com 的 Cookie"></textarea>

    <label><input type="checkbox" name="save_data" value="1" style="width:auto;margin-right:6px">保存这次预约数据和报告</label>
    <p class="hint">保存后可用“学号 + 保存口令”重新生成报告。不要使用统一认证密码作为保存口令。</p>
    <input name="storage_code" type="password" placeholder="保存口令，至少4位；保存或读取时使用" style="margin-top:6px">

    <div class="actions">
      <button type="submit">生成报告</button>
    </div>
  </form>
</section>

<section class="panel">
  <h2>读取已保存档案</h2>
  <p class="notice">输入保存时填写的学号和保存口令，直接用服务器保存的预约 JSON 重新生成报告。</p>
  <form method="post" action="/generate">
    <input type="hidden" name="load_saved" value="1">
    <label>学号</label>
    <input name="student_id" placeholder="例如 202201010001" required>
    <label>保存口令</label>
    <input name="storage_code" type="password" placeholder="不是统一认证密码" required>
    <div class="actions">
      <button type="submit">读取并生成报告</button>
    </div>
  </form>
</section>

<section class="panel">
  <h2>查看用户类型样例</h2>
  <p>看看不同类型的人会得到什么样的标签和评价。</p>
  <a class="button secondary" href="/examples/">查看所有样例</a>
</section>

<script>
(function() {{
  var fi = document.getElementById('file-input');
  var ta = document.getElementById('json-textarea');
  if (fi && ta) {{
    fi.addEventListener('change', function() {{
      var f = fi.files[0];
      if (!f) return;
      var r = new FileReader();
      r.onload = function(e) {{ ta.value = e.target.result; }};
      r.readAsText(f);
    }});
  }}
}})();
</script>
"""
    return render_page("HDU 图书馆记忆", body)
def one_click_page(error_msg: str = "") -> bytes:
    console_code = (
        "var s=document.createElement('script');"
        f"s.src='{PUBLIC_BASE_URL}/export-helper.js?v={EXPORT_HELPER_VERSION}';"
        "document.body.appendChild(s)"
    )
    error_html = ""
    if error_msg:
        error_html = f'<p class="notice" style="background:#fff3cd;border-left-color:#e6a817">{html.escape(error_msg)}</p>'
    body = f"""
<section class="hero">
  <p class="kicker">自动生成报告</p>
  <h1>图书馆记忆报告 - 导出助手</h1>
  <p>此页面会先尝试自动读取。如果中转失败（CAS 限制），请使用下方手动方式。</p>
  {error_html}
  <div class="actions">
    <a class="button secondary" href="/">返回首页</a>
  </div>
</section>

<section class="panel">
  <h2>控制台导出（推荐）</h2>
  <p>在图书馆页面按 <kbd>F12</kbd> → <strong>Console</strong>，粘贴下方代码回车。</p>
  <textarea readonly class="code-box" onclick="this.select()" rows="2" style="width:100%;font:13px ui-monospace,Consolas,monospace;padding:10px;background:#1e1e1e;color:#d4d4d4;border:none;border-radius:6px;resize:none;cursor:pointer">{console_code}</textarea>
</section>

<section class="panel">
  <h2>手动导入数据</h2>
  <form method="post" action="/generate">
    <label>学号或昵称</label>
    <input name="student_id" placeholder="例如 202201010001。需要保存时必填。" style="margin-bottom:12px">

    <label>从文件导入（支持 .json / .txt）</label>
    <input type="file" id="file-input" accept=".json,.txt" style="margin-bottom:6px">
    <p class="hint">选择导出的 JSON 文件，内容会自动填入下方文本框。</p>

    <label>预约记录 JSON</label>
    <textarea name="history_json" id="json-textarea" placeholder="从上方选择文件导入，或直接粘贴 JSON 数据"></textarea>

    <label>Cookie 方式（备用）</label>
    <textarea name="cookie_header" placeholder="粘贴 hdu.huitu.zhishulib.com 的 Cookie"></textarea>

    <label><input type="checkbox" name="save_data" value="1" style="width:auto;margin-right:6px">保存这次预约数据和报告</label>
    <p class="hint">保存后可用“学号 + 保存口令”重新生成报告。不要使用统一认证密码作为保存口令。</p>
    <input name="storage_code" type="password" placeholder="保存口令，至少4位；保存或读取时使用" style="margin-top:6px">

    <div class="actions">
      <button type="submit">生成报告</button>
    </div>
  </form>
</section>

<script>
(function() {{
  var hasError = {json.dumps(bool(error_msg))};
  if (!hasError) {{
    var s = document.createElement('script');
    s.src = '/export-helper.js?v={EXPORT_HELPER_VERSION}&mode=proxy';
    document.body.appendChild(s);
  }}
  var fi = document.getElementById('file-input');
  var ta = document.getElementById('json-textarea');
  if (fi && ta) {{
    fi.addEventListener('change', function() {{
      var f = fi.files[0];
      if (!f) return;
      var r = new FileReader();
      r.onload = function(e) {{ ta.value = e.target.result; }};
      r.readAsText(f);
    }});
  }}
}})();
</script>
"""
    return render_page("自动生成报告", body)
def error_page(message: str, detail: str = "", status: int = 400) -> bytes:
    body = f"""
<section class="hero">
  <p class="kicker">REPORT FAILED</p>
  <h1>暂时没能生成报告</h1>
  <p>{html.escape(message)}</p>
  {f'<p class="notice">{html.escape(detail)}</p>' if detail else ''}
  <div class="actions">
    <a class="button" href="/">返回首页</a>
    <a class="button secondary" href="/login" target="_blank" rel="noopener">打开官方图书馆</a>
  </div>
</section>
"""
    return render_page("生成失败", body, status=status)


def render_report_html(report_data: dict) -> bytes:
    template = HTML_TEMPLATE_FILE.read_text(encoding="utf-8")
    marker = "const memory = {"
    start = template.find(marker)
    if start == -1:
        raise RuntimeError("Report template marker not found")
    brace_start = template.find("{", start)
    brace_end = _find_matching_brace(template, brace_start)
    if brace_start == -1 or brace_end == -1:
        raise RuntimeError("Report template payload not found")
    signed_data = attach_report_signature(report_data)
    payload = json.dumps(signed_data, ensure_ascii=False, indent=6)
    payload = (
        payload.replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return (template[:brace_start] + payload + template[brace_end + 1 :]).encode("utf-8")


def canonical_report_data(report_data: dict) -> str:
    data = json.loads(json.dumps(report_data, ensure_ascii=False))
    data.pop("_server_signature", None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sign_report_data(report_data: dict, student_id: str) -> str:
    safe_id = normalize_storage_id(student_id)
    message = f"{safe_id}\n{canonical_report_data(report_data)}".encode("utf-8")
    return hmac.new(APP_SIGNING_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()


def attach_report_signature(report_data: dict) -> dict:
    data = json.loads(json.dumps(report_data, ensure_ascii=False))
    profile = data.get("user_profile") if isinstance(data.get("user_profile"), dict) else {}
    student_id = profile.get("student_id") or data.get("user")
    if student_id:
        data["_server_signature"] = sign_report_data(data, student_id)
    return data


def verify_report_signature(report_data: dict, student_id: str) -> bool:
    if not isinstance(report_data, dict):
        return False
    supplied = str(report_data.get("_server_signature") or "")
    if not supplied or not re.fullmatch(r"[0-9a-f]{64}", supplied):
        return False
    expected = sign_report_data(report_data, student_id)
    return hmac.compare_digest(supplied, expected)


def compact_report_for_ai(report_data: dict) -> dict:
    summary = report_data.get("summary") or {}
    meme = report_data.get("meme_stats") or {}
    global_stats = report_data.get("global_stats") or {}
    spatial = report_data.get("spatial_stats") or {}
    temporal = report_data.get("temporal_stats") or {}
    semesters = report_data.get("semester_stats") or {}
    return {
        "student_id": (report_data.get("user_profile") or {}).get("student_id") or report_data.get("user"),
        "main_type": {
            "title": meme.get("title"),
            "title_tag": meme.get("title_tag"),
            "sharp_evaluation": meme.get("sharp_evaluation"),
            "extra_tags": meme.get("extra_tags") or [],
            "habit_signals": meme.get("habit_signals") or {},
            "defeat_percentage": meme.get("defeat_percentage"),
        },
        "totals": {
            "sessions": global_stats.get("total_sessions") or summary.get("sessions"),
            "days": global_stats.get("total_days"),
            "hours": global_stats.get("total_hours") or summary.get("hours"),
            "favorite_period": summary.get("favoritePeriod"),
            "most_active_month": summary.get("mostActiveMonth"),
            "max_streak": summary.get("streak"),
        },
        "space": {
            "favorite_seat": spatial.get("most_loved_seat") or summary.get("favoriteSeat"),
            "coverage_percent": spatial.get("coverage_percent"),
            "visited_floor_count": spatial.get("visited_floor_count"),
            "favorite_floor": spatial.get("favorite_floor") or summary.get("favoriteFloor"),
            "favorite_seat_count": spatial.get("favorite_seat_count"),
            "favorite_floor_ratio": spatial.get("favorite_floor_ratio"),
            "floor_distribution": (spatial.get("floor_distribution") or [])[:8],
            "territory_roast": spatial.get("territory_roast"),
        },
        "time": {
            "earliest_time": temporal.get("earliest_time"),
            "latest_time": temporal.get("latest_time"),
            "peak_time": temporal.get("peak_time"),
            "top_months": (temporal.get("top_months") or [])[:5],
            "year_labels": temporal.get("year_labels") or [],
            "yearly_comparison": temporal.get("yearly_comparison") or [],
            "yearly_observations": (temporal.get("yearly_observations") or [])[:4],
            "exam_pulse": temporal.get("exam_pulse") or {},
        },
        "semesters": (semesters.get("semesters") or [])[:8],
        "strongest_semester": semesters.get("strongest_semester") or {},
        "crossroads": (semesters.get("crossroads") or {}),
    }


def fallback_hakimi_review(report_data: dict) -> str:
    compact = compact_report_for_ai(report_data)
    totals = compact["totals"]
    main_type = compact["main_type"]
    space = compact["space"]
    hours = float(totals.get("hours") or 0)
    sessions = int(totals.get("sessions") or 0)
    title = main_type.get("title") or "图书馆记忆样本"
    favorite = space.get("favorite_floor") or space.get("favorite_seat") or "图书馆"
    if hours >= 1800 or sessions >= 350:
        tone = "这份记录不是临时热血，是长期反复坐下来的稳定输出。你把图书馆过成了自己的第二个作息表。"
    elif hours >= 700 or sessions >= 150:
        tone = "数据不靠夸张取胜，但能看出你确实在关键阶段认真出现过。努力不是摆拍，记录会替你作证。"
    elif sessions < 20:
        tone = "你来得不多，但大学不只一种打开方式。也许教室、宿舍、实习和快乐生活，才是你的主战场。"
    else:
        tone = "你不是最高频的那批人，但每一次到场都算数。大学四年的节奏，贵在真实，不必装成别人。"
    return (
        '<div class="hakimi-review">'
        "<h3>哈基米评价</h3>"
        f"<p>{html.escape(tone)} 主标签是<strong>【{html.escape(title)}】</strong>，"
        f"主战场偏向<strong>【{html.escape(str(favorite))}】</strong>，这份报告至少说明你有自己的节奏。</p>"
        "<ul>"
        f"<li>累计预约：<strong>{sessions} 次</strong></li>"
        f"<li>累计时长：<strong>{hours:.1f} 小时</strong></li>"
        "</ul>"
        '<p class="hakimi-blessing">无论这些时间多还是少，它们都已经成为你大学四年青春的一部分。'
        "如今毕业不是结束，而是下一段主线任务开启。毕业快乐，愿你带着这份认真和底气奔向更辽阔的未来。</p>"
        f'<p class="hakimi-hit"><strong>一针见血：</strong>你一共留下 {sessions} 次、{hours:.1f} 小时，'
        "别解释，数据已经把你的大学图书馆人格写明白了。</p>"
        "</div>"
    )


class HakimiHTMLSanitizer(HTMLParser):
    allowed_tags = {"div", "p", "h3", "strong", "ul", "li", "span"}
    allowed_classes = {"hakimi-review", "hakimi-hit", "hakimi-blessing"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "section":
            tag = "div"
        if tag not in self.allowed_tags:
            return
        class_value = ""
        for name, value in attrs:
            if name.lower() != "class" or not value:
                continue
            classes = [
                item
                for item in re.split(r"\s+", value.strip())
                if item in self.allowed_classes
            ]
            if classes:
                class_value = " class=\"" + html.escape(" ".join(classes), quote=True) + "\""
            break
        self.parts.append(f"<{tag}{class_value}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "section":
            tag = "div"
        if tag in self.allowed_tags:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(html.escape(data))

    def get_html(self) -> str:
        return "".join(self.parts).strip()


def normalize_hakimi_review_html(content: str, report_data: dict) -> str:
    source = str(content or "")
    source = re.sub(r"```(?:html)?", "", source, flags=re.IGNORECASE).replace("```", "")
    source = re.sub(r"</?(?:html|body)[^>]*>", "", source, flags=re.IGNORECASE)
    source = re.sub(r"<\s*(div|p|span|section|h3|strong|ul|li)(class|id|style|data-[\w-]+|aria-[\w-]+|role|title)=", r"<\1 \2=", source, flags=re.IGNORECASE)
    source = re.sub(r"<\s*/\s*(div|p|span|section|h3|strong|ul|li)\s*>", r"</\1>", source, flags=re.IGNORECASE)
    source = re.sub(r"<\s*/?\s*(script|style|iframe|object|embed|link|meta)[^>]*>", "", source, flags=re.IGNORECASE)

    sanitizer = HakimiHTMLSanitizer()
    try:
        sanitizer.feed(source)
        cleaned = sanitizer.get_html()
    except Exception:
        cleaned = ""

    if not cleaned or len(re.sub(r"<[^>]+>", "", cleaned).strip()) < 40:
        cleaned = fallback_hakimi_review(report_data)

    if 'class="hakimi-review"' not in cleaned:
        cleaned = f'<div class="hakimi-review">{cleaned}</div>'

    if "<h3>" not in cleaned:
        cleaned = cleaned.replace('<div class="hakimi-review">', '<div class="hakimi-review"><h3>哈基米评价</h3>', 1)

    if 'class="hakimi-hit"' not in cleaned:
        compact = compact_report_for_ai(report_data)
        totals = compact["totals"]
        sessions = int(totals.get("sessions") or 0)
        hours = float(totals.get("hours") or 0)
        hit = (
            f'<p class="hakimi-hit"><strong>一针见血：</strong>'
            f"你一共留下 {sessions} 次、{hours:.1f} 小时，数据已经把这段青春写得很清楚。</p>"
        )
        cleaned = cleaned.replace("</div>", hit + "</div>", 1)

    return cleaned


def iter_text_chunks(text: str, chunk_size: int = 12):
    for index in range(0, len(text), chunk_size):
        yield text[index : index + chunk_size]


def build_hakimi_messages(report_data: dict) -> list[dict]:
    compact = compact_report_for_ai(report_data)
    main_type = compact["main_type"]
    totals = compact["totals"]
    space = compact["space"]
    time_stats = compact["time"]
    semesters = compact.get("semesters") or []
    strongest_semester = compact.get("strongest_semester") or {}
    crossroads = compact.get("crossroads") or {}
    tags = "、".join(
        str(item.get("name"))
        for item in (main_type.get("extra_tags") or [])[:5]
        if isinstance(item, dict) and item.get("name")
    ) or "暂无明显额外标签"
    top_months = "、".join(
        f"{item.get('year')}.{item.get('month')}({item.get('sessions')}次)"
        for item in (time_stats.get("top_months") or [])[:4]
        if isinstance(item, dict)
    ) or "月份样本不足"
    exam = time_stats.get("exam_pulse") or {}
    floors = "、".join(
        f"{item.get('name')}({item.get('sessions')}次/{item.get('hours')}小时/{item.get('ratio')}%)"
        for item in (space.get("floor_distribution") or [])[:6]
        if isinstance(item, dict)
    ) or "楼层样本不足"
    years = "、".join(
        f"{label}:{value}次"
        for label, value in zip(time_stats.get("year_labels") or [], time_stats.get("yearly_comparison") or [])
    ) or "年度样本不足"
    year_peaks = "；".join(
        f"{item.get('year')}年峰值{item.get('peak_month_label')}，{item.get('peak_month_sessions')}次，{item.get('peak_month_hours')}小时"
        for item in (time_stats.get("yearly_observations") or [])[:4]
        if isinstance(item, dict)
    ) or "年度峰值不足"
    semester_line = "；".join(
        f"{item.get('label')}:{item.get('sessions')}次/{item.get('hours')}小时/{item.get('phase_tag')}"
        for item in semesters[:8]
        if isinstance(item, dict)
    ) or "学期样本不足"
    signals = "、".join(
        f"{item.get('label')}={item.get('value')}({item.get('detail')})"
        for item in (crossroads.get("signal_cards") or [])[:4]
        if isinstance(item, dict)
    ) or "关键阶段信号不足"
    user_prompt = (
        f"主标签：{main_type.get('title')} / {main_type.get('title_tag')}\n"
        f"额外标签：{tags}\n"
        f"标签触发信号：{main_type.get('habit_signals')}\n"
        f"累计：{totals.get('sessions')}次，{totals.get('hours')}小时，"
        f"{totals.get('days')}天，最长连续{totals.get('max_streak')}天\n"
        f"时段：最早{time_stats.get('earliest_time')}，最晚{time_stats.get('latest_time')}，"
        f"高峰{time_stats.get('peak_time')}，偏好{totals.get('favorite_period')}\n"
        f"空间：主战场{space.get('favorite_floor')}，最常座位{space.get('favorite_seat')}，"
        f"同座{space.get('favorite_seat_count')}次，主楼层占比{space.get('favorite_floor_ratio')}%，"
        f"覆盖{space.get('coverage_percent')}%，去过{space.get('visited_floor_count')}个楼层/空间\n"
        f"楼层分布：{floors}\n"
        f"月份：最活跃{totals.get('most_active_month')}；重点月份{top_months}\n"
        f"年度曲线：{years}\n"
        f"年度峰值：{year_peaks}\n"
        f"学期数据：{semester_line}\n"
        f"最重学期：{strongest_semester.get('label')}，{strongest_semester.get('sessions')}次，"
        f"{strongest_semester.get('hours')}小时，峰值{strongest_semester.get('peak_month_label')}\n"
        f"关键阶段：{crossroads.get('stage_copy')}；{crossroads.get('comparison_copy')}；信号：{signals}\n"
        f"期末：{exam.get('alert_level')}，期末{exam.get('finals_sessions')}次，"
        f"{exam.get('finals_hours')}小时，高频启动{exam.get('peak_hour')}，高频签退{exam.get('checkout_peak_hour')}，"
        f"高压月份{exam.get('stress_months')}"
    )
    system_prompt = (
        "你是图书馆记忆报告里的“哈基米评价”模块。"
        "请基于用户图书馆预约数据，输出积极、有梗、简体中文、不贬低人的毕业回望评价。"
        "这份数据覆盖用户大学四年，现在用户已经毕业；无论TA把多少时间花在图书馆，这都是TA精彩青春的一部分。"
        "整体基调必须是毕业快乐、祝福未来可期，在活泼锐评之外保留温柔和期许。"
        "你必须只返回一个可嵌入页面的 HTML 片段，不要 Markdown，不要代码块，不要 body/html 标签。"
        "只允许使用这些标签：div、h3、p、strong、ul、li、span。"
        "根节点必须是 <div class=\"hakimi-review\">。"
        "如果使用 class 属性，标签名和 class 之间必须有空格，例如 <p class=\"hakimi-hit\">，不要写成 <pclass=\"...\">。"
        "结构要求：一个 <h3>哈基米评价</h3>，2-3 个 <p> 自然段，"
        "一个 <ul> 列出 3 条最关键数据洞察，"
        "一个 <p class=\"hakimi-blessing\">...</p> 写给毕业后的未来期许，"
        "最后一个 <p class=\"hakimi-hit\"><strong>一针见血：</strong>...</p>，这一句会被前端提取到评价框外单独展示。"
        "评价要多一点，控制在 420-650 个汉字，必须引用至少 6 个具体数据或标签。"
        "句式要有变化，适度活泼，可以用游戏化表达，例如通关、地图全开、战力峰值、成就解锁、主线任务等。"
        "不要反复使用“你简直是”，整段最多出现一次，优先换用“数据判定”“这波属于”“系统盖章”“成就解锁”“画像很明显”等相近表达。"
        "未来期许段必须明确表达：四年图书馆数据是大学生涯的一部分，无论多少都是精彩青春，毕业快乐，未来可期。"
        "最后的一针见血要直接、不绕弯，但不能破坏毕业祝福基调。"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def iter_ai_review_chunks(report_data: dict):
    if not AI_API_KEY:
        yield from iter_text_chunks(normalize_hakimi_review_html(fallback_hakimi_review(report_data), report_data))
        return

    payload = {
        "model": AI_MODEL,
        "stream": True,
        "temperature": 0.72,
        "max_tokens": 900,
        "messages": build_hakimi_messages(report_data),
    }
    request = urllib_request.Request(
        f"{AI_BASE_URL}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {AI_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "HDULibMemory/1.0",
        },
        method="POST",
    )
    try:
        parts: list[str] = []
        with urllib_request.urlopen(request, timeout=60) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if content:
                            parts.append(content)
                except json.JSONDecodeError:
                    continue
        normalized = normalize_hakimi_review_html("".join(parts), report_data)
        yield from iter_text_chunks(normalized)
    except (urllib_error.URLError, TimeoutError, OSError, RuntimeError):
        non_streamed = fetch_ai_review_once(report_data)
        normalized = normalize_hakimi_review_html(
            non_streamed or fallback_hakimi_review(report_data),
            report_data,
        )
        yield from iter_text_chunks(normalized)


def fetch_ai_review_once(report_data: dict) -> str:
    if not AI_API_KEY:
        return ""
    payload = {
        "model": AI_MODEL,
        "stream": False,
        "temperature": 0.72,
        "max_tokens": 900,
        "messages": build_hakimi_messages(report_data),
    }
    request = urllib_request.Request(
        f"{AI_BASE_URL}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {AI_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "HDULibMemory/1.0",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=45) as response:
            event = json.loads(response.read().decode("utf-8", errors="replace"))
            choices = event.get("choices") or []
            if not choices:
                return ""
            message = choices[0].get("message") or {}
            return str(message.get("content") or "").strip()
    except (urllib_error.URLError, TimeoutError, OSError, RuntimeError, json.JSONDecodeError):
        traceback.print_exc()
        return ""


def public_report_url(student_id: str) -> str:
    safe_id = normalize_storage_id(student_id)
    return f"{PUBLIC_BASE_URL}/{safe_id}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_storage_id(student_id: str | None) -> str:
    value = (student_id or "").strip()
    if not value:
        raise RuntimeError("保存或读取档案时必须填写学号。")
    value = re.sub(r"\s+", "", value)
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,40}", value):
        raise RuntimeError("学号只能包含数字、字母、下划线或短横线，长度 4-40。")
    return value


def user_archive_path(student_id: str) -> Path:
    safe_id = normalize_storage_id(student_id)
    return USER_DATA_DIR / f"{safe_id}.json"


def public_report_path(student_id: str) -> Path:
    safe_id = normalize_storage_id(student_id)
    return PUBLIC_REPORT_DIR / f"{safe_id}.json"


def hash_storage_code(code: str, salt: bytes | None = None) -> dict:
    value = (code or "").strip()
    if len(value) < 4:
        raise RuntimeError("保存口令至少需要 4 位。")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt, 180_000)
    return {
        "algorithm": "pbkdf2_sha256",
        "iterations": 180_000,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
    }


def verify_storage_code(code: str, auth: dict) -> bool:
    try:
        salt = base64.b64decode(str(auth.get("salt", "")))
        expected = base64.b64decode(str(auth.get("hash", "")))
        iterations = int(auth.get("iterations") or 180_000)
        digest = hashlib.pbkdf2_hmac("sha256", (code or "").strip().encode("utf-8"), salt, iterations)
        return secrets.compare_digest(digest, expected)
    except Exception:
        return False


def build_report_artifacts(
    remote_items: list[dict],
    student_id: str | None = None,
) -> tuple[bytes, dict, list[dict], str]:
    records = normalize_remote_records(remote_items, expected_user=student_id or None)
    if not records and not student_id:
        records = normalize_remote_records(remote_items, expected_user="HDU同学")
    if not records:
        raise RuntimeError("导入数据里没有识别到有效预约记录。请确认粘贴的是导出助手生成的 JSON。")

    report_user = student_id or records[0].user or "HDU同学"
    stats = compute_stats(records)
    if not stats:
        raise RuntimeError("预约记录为空，暂时无法生成报告。")

    report_data = build_report_data(records, stats, MemoryFilters(user=report_user))
    return render_report_html(report_data), report_data, remote_items, report_user


def save_user_archive(
    student_id: str,
    storage_code: str,
    remote_items: list[dict],
    report_data: dict,
    source: str,
) -> None:
    safe_id = normalize_storage_id(student_id)
    path = user_archive_path(safe_id)
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        auth = existing.get("auth") if isinstance(existing.get("auth"), dict) else {}
        if auth and not verify_storage_code(storage_code, auth):
            raise RuntimeError("这个学号已经保存过档案，但保存口令不正确。")
        created_at = existing.get("created_at") or now_iso()
        auth_payload = auth or hash_storage_code(storage_code)
    else:
        created_at = now_iso()
        auth_payload = hash_storage_code(storage_code)

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    archive = {
        "format": "hdulib-memory-saved",
        "version": 1,
        "student_id": safe_id,
        "created_at": created_at,
        "updated_at": now_iso(),
        "source": source,
        "auth": auth_payload,
        "history_items": remote_items,
        "report_data": report_data,
        "summary": {
            "records": len(remote_items),
            "title": ((report_data.get("meme_stats") or {}).get("title")),
            "total_days": ((report_data.get("global_stats") or {}).get("total_days")),
            "total_hours": ((report_data.get("global_stats") or {}).get("total_hours")),
        },
    }
    path.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    save_public_report(safe_id, report_data, source=source)


def save_public_report(student_id: str, report_data: dict, source: str = "manual-share") -> str:
    safe_id = normalize_storage_id(student_id)
    PUBLIC_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "hdulib-memory-public-report",
        "version": 1,
        "student_id": safe_id,
        "updated_at": now_iso(),
        "source": source,
        "report_data": with_public_metadata(report_data, safe_id),
    }
    public_report_path(safe_id).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return public_report_url(safe_id)


def with_public_metadata(report_data: dict, student_id: str) -> dict:
    safe_id = normalize_storage_id(student_id)
    data = json.loads(json.dumps(report_data, ensure_ascii=False))
    data["user"] = safe_id
    profile = data.setdefault("user_profile", {})
    profile["student_id"] = safe_id
    profile["student_id_mask"] = safe_id
    data["public_share_url"] = public_report_url(safe_id)
    return data


def load_public_report(student_id: str) -> dict:
    path = public_report_path(student_id)
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        report_data = payload.get("report_data") if isinstance(payload, dict) else None
        if isinstance(report_data, dict):
            return with_public_metadata(report_data, student_id)

    archive_path = user_archive_path(student_id)
    if archive_path.exists():
        archive = json.loads(archive_path.read_text(encoding="utf-8"))
        report_data = archive.get("report_data") if isinstance(archive, dict) else None
        if isinstance(report_data, dict):
            return with_public_metadata(report_data, student_id)

    raise RuntimeError("没有找到这个学号的公开报告。")


def iter_report_summaries() -> list[dict]:
    reports: dict[str, dict] = {}
    for directory in (USER_DATA_DIR, PUBLIC_REPORT_DIR):
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                report_data = payload.get("report_data") if isinstance(payload, dict) else None
                if not isinstance(report_data, dict):
                    continue
                student_id = normalize_storage_id(payload.get("student_id") or path.stem)
                profile = report_data.get("user_profile") or {}
                global_stats = report_data.get("global_stats") or {}
                meme = report_data.get("meme_stats") or {}
                reports[student_id] = {
                    "student_id": student_id,
                    "display_id": profile.get("student_id") or student_id,
                    "title": meme.get("title") or "图书馆记忆样本",
                    "total_hours": float(global_stats.get("total_hours") or 0),
                    "total_sessions": int(global_stats.get("total_sessions") or 0),
                    "share_url": public_report_url(student_id),
                    "updated_at": payload.get("updated_at") or payload.get("created_at") or "",
                }
            except Exception:
                continue
    return sorted(reports.values(), key=lambda item: (-item["total_hours"], item["student_id"]))


def leaderboard_payload(limit: int = 100) -> dict:
    rows = iter_report_summaries()[:limit]
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return {
        "updated_at": now_iso(),
        "metric": "total_hours",
        "rows": rows,
    }


def leaderboard_page() -> bytes:
    payload = leaderboard_payload(100)
    rows = payload["rows"]
    body = """
<section class="hero">
  <p class="kicker">HDU LIBRARY RANKING</p>
  <h1>总时长排行榜</h1>
  <p>按已保存/已分享报告的总预约时长排序。只有用户主动保存或分享后才会进入榜单。</p>
  <a class="button" href="/">返回生成报告</a>
</section>
<section class="panel">
  <h2>榜单</h2>
  <div class="rank-list">
"""
    if not rows:
        body += "<p>暂时还没有公开报告。</p>"
    for row in rows:
        body += (
            f"<a class=\"rank-row\" href=\"/{html.escape(row['student_id'])}\">"
            f"<strong>#{row['rank']} {html.escape(row['display_id'])}</strong>"
            f"<span>{row['total_hours']:.1f}h · {row['total_sessions']} 次 · {html.escape(row['title'])}</span>"
            "</a>"
        )
    body += """
  </div>
</section>
<style>
  .rank-list{display:grid;gap:8px}
  .rank-row{display:flex;justify-content:space-between;gap:12px;padding:12px;border:1px solid var(--line);border-radius:8px;background:rgba(255,255,255,.4);color:inherit;text-decoration:none}
  .rank-row span{color:var(--muted)}
  @media (max-width:640px){.rank-row{display:grid}}
</style>
"""
    return render_page("HDU 图书馆预约总时长排行榜", body)


def load_user_archive(student_id: str, storage_code: str) -> dict:
    path = user_archive_path(student_id)
    if not path.exists():
        raise RuntimeError("没有找到这个学号的已保存档案。")
    archive = json.loads(path.read_text(encoding="utf-8"))
    auth = archive.get("auth") if isinstance(archive.get("auth"), dict) else {}
    if not auth or not verify_storage_code(storage_code, auth):
        raise RuntimeError("保存口令不正确。")
    return archive


def render_saved_notice(report: bytes, student_id: str, saved: bool) -> bytes:
    if not saved:
        return report
    notice = (
        f"<!-- saved archive for {html.escape(student_id)} -->\n"
        "<script>console.info('HDU Library Memory archive saved.');</script>\n"
    ).encode("utf-8")
    return report.replace(b"</body>", notice + b"</body>")


def export_helper_js() -> bytes:
    script = r"""
(() => {
  if (window.__HDULIB_MEMORY_EXPORTER__) return;
  window.__HDULIB_MEMORY_EXPORTER__ = true;

  const APP_ORIGIN = "__APP_ORIGIN__";
  const FORMAT = "hdulib-memory-export";
  const VERSION = 1;
  const PROXY_LOGIN_URL = "__PROXY_LOGIN_URL__";
  const IS_PROXY_MODE = location.origin === APP_ORIGIN
    && (location.pathname === "/one-click" || location.pathname === "/hdu/User/Center/myAppoint");
  const BASE_URL = IS_PROXY_MODE ? `${location.origin}/hdu` : location.origin;

  const state = { text: null, detail: null, actions: null };

  function mount() {
    const old = document.getElementById("hdulib-memory-exporter");
    if (old) old.remove();

    const root = document.createElement("div");
    root.id = "hdulib-memory-exporter";
    root.innerHTML = `
      <div class="hdm-card">
        <div class="hdm-title">HDU 图书馆记忆导出助手</div>
        <div class="hdm-text">正在准备读取你的预约记录...</div>
        <div class="hdm-detail"></div>
        <div class="hdm-actions"></div>
      </div>
    `;
    const style = document.createElement("style");
    style.textContent = `
      #hdulib-memory-exporter{position:fixed;inset:0;z-index:2147483647;display:grid;place-items:center;background:rgba(18,14,10,.58);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:#211a14}
      #hdulib-memory-exporter .hdm-card{width:min(520px,calc(100vw - 28px));max-height:calc(100vh - 28px);overflow:auto;padding:18px;border-radius:8px;background:#f3e4c6;border:1px solid rgba(33,26,20,.18);box-shadow:0 18px 55px rgba(0,0,0,.36)}
      #hdulib-memory-exporter .hdm-title{font-weight:900;font-size:18px;margin-bottom:8px;color:#b2211d}
      #hdulib-memory-exporter .hdm-text{font-weight:800;line-height:1.65}
      #hdulib-memory-exporter .hdm-detail{margin-top:8px;color:#6f604e;line-height:1.6;white-space:pre-wrap;font-size:13px}
      #hdulib-memory-exporter .hdm-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
      #hdulib-memory-exporter button{min-height:40px;border:2px solid #b2211d;border-radius:8px;padding:0 12px;background:#b2211d;color:#fff7e8;font-weight:900}
      #hdulib-memory-exporter button.secondary{background:rgba(255,255,255,.38);color:#b2211d}
      #hdulib-memory-exporter textarea{width:100%;min-height:180px;margin-top:10px;border:1px solid rgba(33,26,20,.22);border-radius:8px;padding:8px;background:rgba(255,255,255,.55);font:12px ui-monospace,Consolas,monospace}
    `;
    document.head.appendChild(style);
    document.body.appendChild(root);
    state.text = root.querySelector(".hdm-text");
    state.detail = root.querySelector(".hdm-detail");
    state.actions = root.querySelector(".hdm-actions");
  }

  function setStatus(text, detail = "") {
    state.text.textContent = text;
    state.detail.textContent = detail;
  }

  function setActions(buttons) {
    state.actions.innerHTML = "";
    for (const button of buttons) state.actions.appendChild(button);
  }

  function button(label, handler, secondary = false) {
    const el = document.createElement("button");
    el.type = "button";
    el.textContent = label;
    if (secondary) el.className = "secondary";
    el.addEventListener("click", handler);
    return el;
  }

  function withLabJson(url) {
    const next = new URL(url, location.origin);
    next.searchParams.set("LAB_JSON", "1");
    return next.toString();
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(withLabJson(url), {
      credentials: "include",
      headers: { "Accept": "application/json,text/plain,*/*" },
      ...options,
    });
    const text = await response.text();
    if (!response.ok) {
      throw new Error(`接口返回 ${response.status}：${text.slice(0, 200)}`);
    }
    if (/^\s*</.test(text)) {
      throw new Error(`接口返回了 HTML 页面而非 JSON（${response.status}），可能需要重新登录。`);
    }
    try {
      return JSON.parse(text);
    } catch (error) {
      throw new Error(`接口没有返回 JSON（${response.status}），内容：${text.slice(0, 120)}`);
    }
  }

  function isAuthRedirect(payload) {
    return payload && payload.ui_type === "com.Redirect" && String(payload.href || "").includes("hduCASLogin");
  }

  function looksLikeBookingItem(item) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return false;
    const keys = ["booking", "seat", "space", "begin_time", "beginTime", "duration", "id", "bookingId", "roomName", "seatNum", "orderTime"];
    return keys.filter((key) => Object.prototype.hasOwnProperty.call(item, key)).length >= 2;
  }

  function searchNamedItems(payload) {
    if (!payload) return null;
    if (Array.isArray(payload)) {
      for (const item of payload) {
        const found = searchNamedItems(item);
        if (found) return found;
      }
      return null;
    }
    if (typeof payload !== "object") return null;
    for (const key of ["items", "defaultItems", "list", "rows"]) {
      const value = payload[key];
      if (Array.isArray(value) && (!value.length || value.some(looksLikeBookingItem))) return value;
    }
    for (const value of Object.values(payload)) {
      const found = searchNamedItems(value);
      if (found) return found;
    }
    return null;
  }

  function searchItems(payload) {
    if (!payload) return null;
    if (Array.isArray(payload)) {
      if (payload.length && payload.every((item) => item && typeof item === "object") && payload.some(looksLikeBookingItem)) return payload;
      for (const item of payload) {
        const found = searchItems(item);
        if (found) return found;
      }
      return null;
    }
    if (typeof payload !== "object") return null;
    if (looksLikeBookingItem(payload)) return [payload];
    for (const value of Object.values(payload)) {
      const found = searchItems(value);
      if (found) return found;
    }
    return null;
  }

  function extractItems(payload) {
    return searchNamedItems(payload) || searchItems(payload) || [];
  }

  function extractNextUrl(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
    for (const key of ["nextUrl", "defaultNextUrl", "next_url", "default_next_url", "next"]) {
      if (typeof payload[key] === "string" && payload[key].trim()) return payload[key].trim();
    }
    for (const value of Object.values(payload)) {
      const found = extractNextUrl(value);
      if (found) return found;
    }
    return null;
  }

  function extractBookingId(item) {
    if (!item || typeof item !== "object") return "";
    for (const key of ["bookingId", "booking_id", "id"]) {
      if (item[key] !== undefined && String(item[key]).trim()) return String(item[key]).trim();
    }
    if (item.booking && typeof item.booking === "object") {
      for (const key of ["bookingId", "booking_id", "id"]) {
        if (item.booking[key] !== undefined && String(item.booking[key]).trim()) return String(item.booking[key]).trim();
      }
    }
    for (const value of Object.values(item)) {
      if (typeof value !== "string") continue;
      const match = value.match(/bookingId=(\d+)/);
      if (match) return match[1];
    }
    return "";
  }

  function searchBookingDetail(payload) {
    if (!payload) return {};
    if (Array.isArray(payload)) {
      for (const item of payload) {
        const found = searchBookingDetail(item);
        if (Object.keys(found).length) return found;
      }
      return {};
    }
    if (typeof payload !== "object") return {};
    if (payload.booking && typeof payload.booking === "object") return payload;
    for (const value of Object.values(payload)) {
      const found = searchBookingDetail(value);
      if (Object.keys(found).length) return found;
    }
    return {};
  }

  async function collectList(endpointName) {
    const endpoint = `${BASE_URL}/Seat/Index/${endpointName}`;
    const first = await requestJson(endpoint);
    if (isAuthRedirect(first)) {
      if (IS_PROXY_MODE) {
        setStatus("需要登录 HDU", "正在跳转到 HDU 登录页，登录成功后会自动继续生成。");
        location.href = PROXY_LOGIN_URL;
        return new Promise(() => {});
      }
      throw new Error("官方图书馆登录态已失效，请先在本页面完成登录。");
    }

    const items = [...extractItems(first)];
    let nextUrl = extractNextUrl(first);
    const seen = new Set();
    let pageCount = 1;

    while (nextUrl && pageCount < 200) {
      const absolute = new URL(nextUrl, location.origin).toString();
      if (seen.has(absolute)) break;
      seen.add(absolute);
      const payload = await requestJson(absolute);
      if (isAuthRedirect(payload)) {
        if (IS_PROXY_MODE) {
          setStatus("需要重新登录 HDU", "正在跳转到 HDU 登录页。");
          location.href = PROXY_LOGIN_URL;
          return new Promise(() => {});
        }
        throw new Error("官方图书馆登录态已失效，请重新登录。");
      }
      items.push(...extractItems(payload));
      nextUrl = extractNextUrl(payload);
      pageCount += 1;
      setStatus("正在读取预约分页...", `${endpointName}: ${items.length} 条`);
    }
    return dedupe(items);
  }

  function dedupe(items) {
    const seen = new Set();
    const result = [];
    for (const item of items) {
      const id = extractBookingId(item);
      const key = id ? `booking:${id}` : JSON.stringify(item);
      if (seen.has(key)) continue;
      seen.add(key);
      result.push(item);
    }
    return result;
  }

  function stripDetail(detail) {
    // Only keep booking sub-object to reduce payload size (full detail includes huge seat maps etc)
    if (!detail || typeof detail !== 'object') return detail;
    var booking = detail.booking;
    if (booking && typeof booking === 'object') {
      var stripped = { booking: {} };
      var keepKeys = ['begin_time','beginTime','duration','hours',
        'create_time','createTime','orderTime','time',
        'space','space_id','spaceId','space_name','spaceName',
        'seat','seatNum','seat_id','seatId','seat_name','seatName',
        'title','name','roomName','format',
        'user','student_number','cardno'];
      for (var i = 0; i < keepKeys.length; i++) {
        var k = keepKeys[i];
        if (booking[k] !== undefined) stripped.booking[k] = booking[k];
      }
      if (booking.space && typeof booking.space === 'object') {
        stripped.booking.space = {};
        var spaceKeys = ['space_id','spaceId','id','space','space_name','spaceName','name','title','format'];
        for (var j = 0; j < spaceKeys.length; j++) {
          var sk = spaceKeys[j];
          if (booking.space[sk] !== undefined) stripped.booking.space[sk] = booking.space[sk];
        }
      }
      if (booking.seat && typeof booking.seat === 'object') {
        stripped.booking.seat = {};
        var seatKeys = ['seat','seat_name','seatName','seatNum','name','title','num','number'];
        for (var m = 0; m < seatKeys.length; m++) {
          var stk = seatKeys[m];
          if (booking.seat[stk] !== undefined) stripped.booking.seat[stk] = booking.seat[stk];
        }
      }
      return stripped;
    }
    return { booking: booking || {} };
  }

  async function enrichDetails(items) {
    let done = 0;
    let cursor = 0;
    async function worker() {
      while (cursor < items.length) {
        const index = cursor++;
        const item = items[index];
        const bookingId = extractBookingId(item);
        if (bookingId) {
          try {
            const detailPayload = await requestJson(`${BASE_URL}/Seat/Index/bookingInfo?bookingId=${encodeURIComponent(bookingId)}`);
            const detail = searchBookingDetail(detailPayload);
            if (Object.keys(detail).length) item.detail = stripDetail(detail);
          } catch (_) {
            item.detail_error = "bookingInfo failed";
          }
        }
        done += 1;
        if (done % 8 === 0 || done === items.length) {
          setStatus("正在补全预约详情...", `${done}/${items.length}`);
        }
      }
    }
    await Promise.all(Array.from({ length: Math.min(4, Math.max(1, items.length)) }, worker));
  }

  function submitToReport(jsonText) {
    const form = document.createElement("form");
    form.method = "post";
    form.action = `${APP_ORIGIN}/generate`;
    form.target = "_blank";
    const input = document.createElement("textarea");
    input.name = "history_json";
    input.value = jsonText;
    form.appendChild(input);
    appendIdentityFields(form);
    form.style.display = "none";
    document.body.appendChild(form);
    form.submit();
    form.remove();
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      return false;
    }
  }

  function downloadJson(text) {
    const blob = new Blob([text], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `hdulib-memory-export-${Date.now()}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function preprocessRecords(rawItems) {
    // Convert raw library booking items into compact normalized format
    var records = [];
    var seen = {};
    for (var i = 0; i < rawItems.length; i++) {
      var item = rawItems[i];
      var bookingId = extractBookingId(item);
      if (!bookingId) continue;
      if (seen[bookingId]) continue;
      seen[bookingId] = true;

      var detail = (item.detail && typeof item.detail === 'object') ? item.detail : {};
      var booking = detail.booking || item.booking || item;
      if (typeof booking !== 'object') booking = {};

      var beginRaw = booking.begin_time || booking.beginTime || detail.begin_time || detail.beginTime || item.begin_time || item.beginTime || item.time || item.orderTime;
      var beginTs = 0;
      if (beginRaw) {
        var d = new Date(beginRaw);
        if (!isNaN(d.getTime())) beginTs = Math.floor(d.getTime() / 1000);
        else beginTs = parseInt(beginRaw, 10) || 0;
      }
      if (!beginTs) continue;

      var durRaw = booking.duration || detail.duration || item.duration || item.hours || item.use_hours || 0;
      var durHours = parseFloat(durRaw) || 0;
      if (durHours <= 0) durHours = 0.5;

      var createdRaw = booking.create_time || booking.createTime || detail.create_time || detail.createTime || item.create_time || item.createTime || item.orderTime || item.time || beginRaw;
      var createdTs = 0;
      if (createdRaw) {
        var cd = new Date(createdRaw);
        if (!isNaN(cd.getTime())) createdTs = Math.floor(cd.getTime() / 1000);
        else createdTs = parseInt(createdRaw, 10) || beginTs;
      }
      if (!createdTs) createdTs = beginTs;

      var user = '';
      var bkUser = booking.user;
      if (bkUser && typeof bkUser === 'object') {
        user = bkUser.student_number || bkUser.cardno || bkUser.name || bkUser.rid || '';
      }
      if (!user) user = booking.student_number || item.student_number || '';

      var space = booking.space || item.space || {};
      if (typeof space !== 'object') space = {};
      var floorId = space.space_id || space.spaceId || space.id || booking.space_id || booking.spaceId || item.space_id || item.spaceId || '';
      var floorName = space.space || space.space_name || space.spaceName || space.name || space.title || space.format || booking.space_name || booking.spaceName || item.roomName || item.format || '';

      var seat = booking.seat || item.seat || {};
      if (typeof seat !== 'object') seat = {};
      var seatNum = seat.seat || seat.seat_name || seat.seatName || seat.seatNum || seat.name || seat.title || seat.num || seat.number || booking.seatNum || item.seatNum || '';

      var roomName = item.roomName || item.room_name || booking.roomName || booking.room_name || space.roomName || space.name || space.title || '';

      records.push({
        id: bookingId,
        bt: beginTs,
        dh: durHours,
        ct: createdTs,
        u: String(user),
        fi: String(floorId),
        fn: String(floorName),
        sn: String(seatNum),
        rn: String(roomName)
      });
    }
    setStatus('预处理完成', '共 ' + records.length + ' 条有效记录（已去重压缩）');
    return records;
  }

  function submitCompactRecords(records) {
    var jsonText = JSON.stringify({
      format: 'hdulib-memory-compact',
      version: 2,
      exported_at: new Date().toISOString(),
      source: location.origin,
      total: records.length,
      records: records
    });
    var form = document.createElement('form');
    form.method = 'post';
    form.action = APP_ORIGIN + '/generate';
    form.target = '_blank';
    var input = document.createElement('textarea');
    input.name = 'history_json';
    input.value = jsonText;
    form.appendChild(input);
    appendIdentityFields(form);
    form.style.display = 'none';
    document.body.appendChild(form);
    form.submit();
    form.remove();
  }

  function appendHidden(form, name, value) {
    var input = document.createElement('input');
    input.type = 'hidden';
    input.name = name;
    input.value = value;
    form.appendChild(input);
  }

  function appendIdentityFields(form) {
    var studentId = window.prompt('输入学号或昵称，用于报告开头和保存档案。留空则只生成临时报告。', '');
    if (studentId === null) studentId = '';
    studentId = String(studentId || '').trim();
    if (!studentId) return;
    appendHidden(form, 'student_id', studentId);
    var code = window.prompt('如果要保存到服务器，请输入保存口令（至少4位）。留空则不保存。不要使用统一认证密码。', '');
    if (code === null) code = '';
    code = String(code || '').trim();
    if (code) {
      appendHidden(form, 'save_data', '1');
      appendHidden(form, 'storage_code', code);
    }
  }

  async function main() {
    mount();
    if (!IS_PROXY_MODE && location.hostname !== 'hdu.huitu.zhishulib.com') {
      throw new Error('请先打开 hdu.huitu.zhishulib.com 的图书馆页面，再运行导出助手。');
    }

    var REDIRECT_KEY = 'hdulib-memory-redirect-count';
    if (location.search.includes('ticket=')) {
      try { localStorage.removeItem(REDIRECT_KEY); } catch (_) {}
    }
    var redirectCount = 0;
    try { redirectCount = parseInt(localStorage.getItem(REDIRECT_KEY) || '0', 10); } catch (_) {}
    if (redirectCount >= 4) {
      try { localStorage.removeItem(REDIRECT_KEY); } catch (_) {}
      throw new Error('登录跳转次数过多，请检查 HDU 账号状态，或使用首页的备用流程手动导入。');
    }

    setStatus(
      '正在读取座位预约记录...',
      IS_PROXY_MODE ? '一键模式会通过本站 /hdu/ 中转访问图书馆接口。' : '这一步只在官方图书馆域名下访问官方接口。'
    );
    var normal;
    try {
      normal = await collectList('myBookingList');
    } catch (e) {
      if (IS_PROXY_MODE && e.message && e.message.indexOf('HTML') >= 0) {
        try { localStorage.setItem(REDIRECT_KEY, String(redirectCount + 1)); } catch (_) {}
        setStatus('会话可能已过期', '正在跳转到 HDU 登录页...');
        location.href = PROXY_LOGIN_URL;
        return new Promise(function() {});
      }
      throw e;
    }
    setStatus('正在读取无座预约记录...', '座位预约 ' + normal.length + ' 条');
    var noSeat = [];
    try { noSeat = await collectList('myNoSeatBookingList'); } catch (_) {}
    var items = dedupe(normal.concat(noSeat));
    if (!items.length) {
      if (IS_PROXY_MODE) {
        try { localStorage.setItem(REDIRECT_KEY, String(redirectCount + 1)); } catch (_) {}
        setStatus('未读取到记录', '可能登录态已过期，正在跳转到 HDU 登录页...');
        location.href = PROXY_LOGIN_URL;
        return new Promise(function() {});
      }
      throw new Error('没有读取到预约记录。请确认已经在官方图书馆页面登录。');
    }

    try { localStorage.removeItem(REDIRECT_KEY); } catch (_) {}

    setStatus('正在补全预约详情...', '0/' + items.length);
    await enrichDetails(items);

    setStatus('正在预处理数据...', '压缩中...');
    var records = preprocessRecords(items);

    if (!records.length) throw new Error('处理后没有有效记录。');

    var payload = {
      format: FORMAT,
      version: VERSION,
      exported_at: new Date().toISOString(),
      source: location.origin,
      lists: { myBookingList: normal, myNoSeatBookingList: noSeat },
      items: items,
      compact: records
    };
    var jsonText = JSON.stringify(payload);
    setStatus(
      '导出完成',
      '共读取 ' + items.length + ' 条原始记录，预处理后 ' + records.length + ' 条（压缩约 90%）。' + (IS_PROXY_MODE ? '正在生成报告。' : '点击生成报告提交数据。')
    );

    var textarea = document.createElement('textarea');
    textarea.readOnly = true;
    textarea.value = jsonText;
    state.detail.appendChild(textarea);
    setActions([
      button('生成报告', function() { submitToReport(jsonText); }),
      button('生成报告(压缩)', function() { submitCompactRecords(records); }, true),
      button('复制 JSON', async function() {
        var ok = await copyText(jsonText);
        setStatus(ok ? 'JSON 已复制' : '复制失败，请手动选中下方 JSON', '共 ' + items.length + ' 条记录');
      }, true),
      button('下载 JSON', function() { downloadJson(jsonText); }, true),
      button('关闭', function() { var el = document.getElementById('hdulib-memory-exporter'); if (el) el.remove(); }, true),
    ]);
    if (IS_PROXY_MODE) {
      window.setTimeout(function() { submitCompactRecords(records); }, 500);
    }
  }

  main().catch(function(error) {
    if (!state.text) mount();
    setStatus("导出失败", error && error.message ? error.message : String(error));
    setActions([
      button("重试", () => { document.getElementById("hdulib-memory-exporter")?.remove(); window.__HDULIB_MEMORY_EXPORTER__ = false; main(); }),
      button("返回首页", () => { location.href = "/"; }, true),
      button("打开预约页", () => { location.href = "https://hdu.huitu.zhishulib.com/#!/User/Center/myAppoint"; }, true),
      button("关闭", () => document.getElementById("hdulib-memory-exporter")?.remove(), true),
    ]);
  });
})();
"""
    return (
        script.replace("__APP_ORIGIN__", PUBLIC_BASE_URL)
        .replace("__PROXY_LOGIN_URL__", PROXY_LOGIN_URL)
        .encode("utf-8")
    )


def build_report_from_remote_items(
    remote_items: list[dict],
    student_id: str | None = None,
) -> bytes:
    report, _, _, _ = build_report_artifacts(remote_items, student_id=student_id)
    return report


def build_report_from_import_json(
    payload_text: str,
    student_id: str | None = None,
) -> bytes:
    remote_items = parse_import_json_items(payload_text)
    return build_report_from_remote_items(remote_items, student_id=student_id)


def parse_import_json_items(payload_text: str) -> list[dict]:
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"导入 JSON 格式不正确：{exc}") from exc

    remote_items = extract_import_items(payload)
    if not remote_items:
        raise RuntimeError("导入 JSON 里没有找到预约列表。")
    return remote_items


def expand_compact_records(compact_records: list[dict]) -> list[dict]:
    """Expand compact preprocessed records back to full MemoryRecord-compatible dicts."""
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    result = []
    for r in compact_records:
        if not isinstance(r, dict):
            continue
        booking_id = str(r.get("id", ""))
        if not booking_id:
            continue
        begin_ts = int(r.get("bt", 0))
        duration_hours = float(r.get("dh", 0))
        if not begin_ts or duration_hours <= 0:
            continue
        created_ts = int(r.get("ct", 0)) or begin_ts
        begin_dt = datetime.fromtimestamp(begin_ts, tz=tz)
        created_dt = datetime.fromtimestamp(created_ts, tz=tz)

        result.append({
            "id": booking_id,
            "bookingId": booking_id,
            "begin_time": begin_dt.isoformat(),
            "beginTime": begin_dt.isoformat(),
            "duration": duration_hours,
            "hours": duration_hours,
            "create_time": created_dt.isoformat(),
            "createTime": created_dt.isoformat(),
            "orderTime": created_dt.isoformat(),
            "user": str(r.get("u", "")),
            "space_id": str(r.get("fi", "")),
            "spaceId": str(r.get("fi", "")),
            "space_name": str(r.get("fn", "")),
            "spaceName": str(r.get("fn", "")),
            "roomName": str(r.get("rn", "")),
            "seatNum": str(r.get("sn", "")),
            "floor_name": str(r.get("fn", "")),
            "room_name": str(r.get("rn", "")),
        })
    return result

def extract_import_items(payload) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    if payload.get("format") == "hdulib-memory-saved":
        value = payload.get("history_items") or payload.get("items") or []
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return []

    # Compact format: preprocessed by export helper
    if payload.get("format") == "hdulib-memory-compact":
        compact_records = payload.get("records") or payload.get("compact") or []
        if isinstance(compact_records, list):
            return expand_compact_records(compact_records)
        return []

    # Also check nested compact field
    compact = payload.get("compact")
    if isinstance(compact, list) and compact:
        return expand_compact_records(compact)

    for key in ("items", "records", "history", "booking_history", "history_items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    lists = payload.get("lists")
    if isinstance(lists, dict):
        items: list[dict] = []
        for value in lists.values():
            if isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))
        if items:
            return items

    return search_import_items(payload)


def search_import_items(payload) -> list[dict]:
    if isinstance(payload, list):
        dict_items = [item for item in payload if isinstance(item, dict)]
        if dict_items and any(looks_like_booking_item(item) for item in dict_items):
            return dict_items
        for item in payload:
            found = search_import_items(item)
            if found:
                return found

    if isinstance(payload, dict):
        for key in ("defaultItems", "list", "rows", "data", "DATA", "content"):
            found = search_import_items(payload.get(key))
            if found:
                return found
        if looks_like_booking_item(payload):
            return [payload]
        for value in payload.values():
            found = search_import_items(value)
            if found:
                return found

    return []


def looks_like_booking_item(item: dict) -> bool:
    keys = {
        "booking",
        "seat",
        "space",
        "begin_time",
        "beginTime",
        "duration",
        "id",
        "bookingId",
        "roomName",
        "seatNum",
        "orderTime",
    }
    return len(keys & set(item.keys())) >= 2


async def build_report_from_cookies(
    cookies: dict[str, str],
    student_id: str | None = None,
) -> bytes:
    remote_items = await fetch_remote_items_from_cookies(cookies)
    return build_report_from_remote_items(remote_items, student_id=student_id)


async def fetch_remote_items_from_cookies(cookies: dict[str, str]) -> list[dict]:
    async with LibraryAPIClient(
        ConfigManager(),
        session_cookies=cookies,
    ) as client:
        return await client.get_booking_history(
            include_no_seat=INCLUDE_NO_SEAT,
            detail_limit=DETAIL_LIMIT_VALUE,
        )


def parse_request_cookies(header_value: str) -> dict[str, str]:
    return parse_cookie_header(header_value or "")


def cookie_helper_js() -> bytes:
    """Return JS that extracts cookies from the library page and shows them in a copyable popup."""
    return r"""
(() => {
  if (window.__HDULIB_COOKIE_HELPER__) return;
  window.__HDULIB_COOKIE_HELPER__ = true;

  var cookie = document.cookie;
  if (!cookie) {
    alert('没有检测到 Cookie。
请确认你已在 hdu.huitu.zhishulib.com 登录。');
    return;
  }

  var overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.75);z-index:2147483647;display:grid;place-items:center;font-family:-apple-system,BlinkMacSystemFont,sans-serif';
  overlay.innerHTML = '<div style="background:#fff;padding:18px;border-radius:10px;max-width:92vw;min-width:280px;box-shadow:0 12px 40px rgba(0,0,0,.4)">'
    + '<h3 style="margin:0 0 6px;color:#b2211d">图书馆登录 Cookie</h3>'
    + '<p style="color:#666;font-size:13px;margin:0 0 8px">点击下方框自动全选，然后复制</p>'
    + '<textarea readonly onclick="this.select()" style="width:100%;height:80px;font:11px monospace;padding:6px;border:1px solid #ddd;border-radius:6px;margin-bottom:8px;word-break:break-all">' + cookie + '</textarea>'
    + '<button id="hdm-copy-btn" style="width:100%;padding:12px;background:#b2211d;color:#fff;border:none;border-radius:8px;font-size:16px;font-weight:700">点此复制 Cookie</button>'
    + '<p style="color:#999;font-size:12px;margin:8px 0 0">复制后回到报告生成页面，粘贴到 Cookie 输入框</p>'
    + '<button onclick="this.parentElement.parentElement.remove()" style="width:100%;padding:8px;margin-top:6px;background:#f0f0f0;border:none;border-radius:6px;color:#666">关闭</button>'
    + '</div>';
  document.body.appendChild(overlay);

  document.getElementById('hdm-copy-btn').addEventListener('click', function() {
    var ta = overlay.querySelector('textarea');
    ta.select();
    try {
      navigator.clipboard.writeText(cookie).then(function() {
        var btn = document.getElementById('hdm-copy-btn');
        btn.textContent = '已复制！';
        btn.style.background = '#2d6a4f';
      }).catch(function() {
        document.execCommand('copy');
      });
    } catch (_) {
      document.execCommand('copy');
    }
  });
})();
""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "HDULibMemory/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def send_security_headers(self) -> None:
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)

    def client_ip(self) -> str:
        cf_ip = self.headers.get("CF-Connecting-IP")
        if cf_ip:
            return cf_ip.strip()
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
        return self.client_address[0]

    def same_origin_post_allowed(self) -> bool:
        origin = self.headers.get("Origin") or ""
        referer = self.headers.get("Referer") or ""
        value = origin or referer
        if not value:
            return True
        try:
            parsed = urlparse(value)
            host = parsed.netloc.lower()
            return parsed.scheme in {"http", "https"} and host in ALLOWED_POST_HOSTS
        except Exception:
            return False

    def rate_limit_allowed(self, path: str) -> bool:
        limit_window = RATE_LIMITS.get(path)
        if not limit_window:
            return True
        limit, window_seconds = limit_window
        key = (path, self.client_ip())
        now = time.monotonic()
        bucket = RATE_BUCKETS[key]
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    def send_bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def send_json(self, payload: dict, status: int = 200) -> None:
        self.send_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def send_asset(self, relative_path: str) -> None:
        safe_path = Path(relative_path.lstrip("/"))
        if safe_path.is_absolute() or ".." in safe_path.parts:
            self.send_bytes(b"", "text/plain; charset=utf-8", 404)
            return
        path = ROOT_DIR / "docs" / "assets" / safe_path
        if not path.exists() or not path.is_file():
            self.send_bytes(b"", "text/plain; charset=utf-8", 404)
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        payload = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_security_headers()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            self.send_response(302)
            self.send_header("Location", OFFICIAL_LIBRARY_URL)
            self.send_security_headers()
            self.end_headers()
            return
        if parsed.path in HDU_PATH_REDIRECTS:
            self.send_response(302)
            self.send_header("Location", HDU_PATH_REDIRECTS[parsed.path])
            self.send_security_headers()
            self.end_headers()
            return
        if parsed.path in {
            "/",
            "/one-click",
            "/hdu/User/Center/myAppoint",
            "/health",
            "/examples/",
            "/examples",
            "/leaderboard",
            "/leaderboard.json",
            "/export-helper.js",
        }:
            if parsed.path == "/health":
                content_type = "text/plain; charset=utf-8"
            elif parsed.path == "/leaderboard.json":
                content_type = "application/json; charset=utf-8"
            elif parsed.path == "/export-helper.js":
                content_type = "application/javascript; charset=utf-8"
            elif parsed.path == "/cookie-helper.js":
                content_type = "application/javascript; charset=utf-8"
            else:
                content_type = "text/html; charset=utf-8"
            self.send_bytes(b"", content_type)
            return
        if parsed.path.startswith("/assets/"):
            self.send_asset(parsed.path.removeprefix("/assets/"))
            return
        self.send_bytes(b"", "text/html; charset=utf-8", 404)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_bytes(home_page(), "text/html; charset=utf-8")
            return
        if parsed.path in {"/one-click", "/hdu/User/Center/myAppoint"}:
            qs = parse_qs(parsed.query)
            error_msg = ""
            if "error" in qs:
                error_map = {
                    "cas_mismatch": "CAS 登录验证失败（学校统一认证的安全策略限制）。请使用下方的备用方式。",
                }
                error_msg = error_map.get(qs["error"][0], qs["error"][0])
            self.send_bytes(one_click_page(error_msg=error_msg), "text/html; charset=utf-8")
            return
        if parsed.path == "/login":
            self.send_response(302)
            self.send_header("Location", OFFICIAL_LIBRARY_URL)
            self.send_security_headers()
            self.end_headers()
            return
        if parsed.path == "/export-helper.js":
            self.send_bytes(export_helper_js(), "application/javascript; charset=utf-8")
            return
        if parsed.path == "/cookie-helper.js":
            self.send_bytes(cookie_helper_js(), "application/javascript; charset=utf-8")
            return
        if parsed.path == "/health":
            self.send_bytes(b"ok", "text/plain; charset=utf-8")
            return
        if parsed.path == "/leaderboard.json":
            self.send_json(leaderboard_payload(100))
            return
        if parsed.path == "/leaderboard":
            self.send_bytes(leaderboard_page(), "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/assets/"):
            self.send_asset(parsed.path.removeprefix("/assets/"))
            return
        if parsed.path == "/examples/" or parsed.path == "/examples":
            path = ROOT_DIR / "docs" / "memory-persona-examples.html"
            self.send_bytes(path.read_bytes(), "text/html; charset=utf-8")
            return
        # Safety net: redirect common library paths missing /hdu/ prefix
        if parsed.path in HDU_PATH_REDIRECTS:
            self.send_response(302)
            self.send_header("Location", HDU_PATH_REDIRECTS[parsed.path])
            self.send_security_headers()
            self.end_headers()
            return
        match = re.fullmatch(r"/([A-Za-z0-9_-]{4,40})/?", parsed.path)
        if match:
            try:
                report_data = load_public_report(match.group(1))
                self.send_bytes(render_report_html(report_data), "text/html; charset=utf-8")
            except Exception:
                self.send_bytes(error_page("没有找到这个学号的公开报告。", status=404), "text/html; charset=utf-8", 404)
            return
        self.send_bytes(error_page("页面不存在。", status=404), "text/html; charset=utf-8", 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/generate", "/share-report", "/hakimi-review"}:
            self.send_bytes(error_page("接口不存在。", status=404), "text/html; charset=utf-8", 404)
            return

        if not self.same_origin_post_allowed():
            self.send_json({"ok": False, "error": "跨站请求已被拒绝。"}, 403)
            return

        if not self.rate_limit_allowed(parsed.path):
            self.send_json({"ok": False, "error": "请求过于频繁，请稍后再试。"}, 429)
            return

        length = int(self.headers.get("Content-Length") or "0")
        limit = POST_BODY_LIMITS.get(parsed.path, 1_000_000)
        if length > limit:
            self.send_json({"ok": False, "error": "请求体过大。"}, 413)
            return
        body = self.rfile.read(min(length, limit)).decode("utf-8", errors="replace")
        if parsed.path == "/hakimi-review":
            try:
                payload = json.loads(body or "{}")
                report_data = payload.get("report_data") if isinstance(payload, dict) else None
                if not isinstance(report_data, dict):
                    raise RuntimeError("缺少报告数据。")
                profile = report_data.get("user_profile") if isinstance(report_data.get("user_profile"), dict) else {}
                review_student_id = normalize_storage_id(profile.get("student_id") or report_data.get("user"))
                if not verify_report_signature(report_data, review_student_id):
                    self.send_json({"ok": False, "error": "报告签名无效。"}, 403)
                    return
            except Exception:
                self.send_json({"ok": False, "error": "报告数据无效。"}, 400)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_security_headers()
            self.end_headers()
            try:
                for chunk in iter_ai_review_chunks(report_data):
                    if not chunk:
                        continue
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            return

        if parsed.path == "/share-report":
            try:
                payload = json.loads(body or "{}")
                report_data = payload.get("report_data")
                student_id = normalize_storage_id(payload.get("student_id") or ((report_data or {}).get("user")))
                if not isinstance(report_data, dict):
                    raise RuntimeError("缺少报告数据。")
                if not verify_report_signature(report_data, student_id):
                    raise RuntimeError("报告签名无效，请从本站生成报告后再分享。")
                share_url = save_public_report(student_id, report_data, source="report-share-button")
                self.send_json({"ok": True, "student_id": student_id, "share_url": share_url})
            except Exception as exc:
                traceback.print_exc()
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return

        form = parse_qs(body, keep_blank_values=True)
        student_id = (form.get("student_id") or [""])[0].strip() or None
        storage_code = (form.get("storage_code") or [""])[0].strip()
        save_data = (form.get("save_data") or [""])[0] == "1"
        load_saved = (form.get("load_saved") or [""])[0] == "1"
        history_json = (form.get("history_json") or [""])[0].strip()
        cookie_header = (form.get("cookie_header") or [""])[0].strip()
        cookies = parse_cookie_header(cookie_header) if cookie_header else parse_request_cookies(self.headers.get("Cookie", ""))

        if load_saved:
            try:
                archive = load_user_archive(student_id or "", storage_code)
                remote_items = extract_import_items(archive)
                if not remote_items:
                    raise RuntimeError("已保存档案里没有可用预约记录。")
                report, report_data, _, _ = build_report_artifacts(remote_items, student_id=student_id)
                self.send_bytes(report, "text/html; charset=utf-8")
            except Exception as exc:
                traceback.print_exc()
                self.send_bytes(
                    error_page(
                        "读取已保存档案失败。",
                        str(exc),
                        400,
                    ),
                    "text/html; charset=utf-8",
                    400,
                )
            return

        if history_json:
            try:
                remote_items = parse_import_json_items(history_json)
                report, report_data, saved_items, report_user = build_report_artifacts(remote_items, student_id=student_id)
                if save_data:
                    safe_id = normalize_storage_id(student_id)
                    save_user_archive(
                        safe_id,
                        storage_code,
                        saved_items,
                        report_data,
                        source="json-import",
                    )
                    report = render_saved_notice(report, safe_id, True)
            except Exception as exc:
                traceback.print_exc()
                self.send_bytes(
                    error_page(
                        "导入预约 JSON 失败。",
                        str(exc),
                        400,
                    ),
                    "text/html; charset=utf-8",
                    400,
                )
                return
            self.send_bytes(report, "text/html; charset=utf-8")
            return

        if not cookies:
            self.send_bytes(
                error_page(
                    "没有检测到可生成报告的数据。",
                    "请优先使用官方页导出助手提交预约 JSON；只有助手不可用时，再粘贴 hdu.huitu.zhishulib.com 的 Cookie 作为兜底。",
                    401,
                ),
                "text/html; charset=utf-8",
                401,
            )
            return

        try:
            remote_items = asyncio.run(fetch_remote_items_from_cookies(cookies))
            report, report_data, saved_items, report_user = build_report_artifacts(remote_items, student_id=student_id)
            if save_data:
                safe_id = normalize_storage_id(student_id)
                save_user_archive(
                    safe_id,
                    storage_code,
                    saved_items,
                    report_data,
                    source="cookie-sync",
                )
                report = render_saved_notice(report, safe_id, True)
        except Exception as exc:
            traceback.print_exc()
            self.send_bytes(
                error_page(
                    "同步图书馆数据失败。",
                    str(exc),
                    502,
                ),
                "text/html; charset=utf-8",
                502,
            )
            return

        self.send_bytes(report, "text/html; charset=utf-8")


def main() -> None:
    server = ThreadingHTTPServer((APP_HOST, APP_PORT), Handler)
    print(f"HDULib Memory web app listening on http://{APP_HOST}:{APP_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
