import hashlib
import json
import os
import re
import threading
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urljoin
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

# Webhook path is a token-derived hash so random scanners can't hit the
# endpoint. The secret_token header (separate hash) is the real auth — paths
# can leak into access logs, headers usually don't.
WEBHOOK_PATH = (
    "/tg/" + hashlib.sha256(TELEGRAM_BOT_TOKEN.encode()).hexdigest()[:32]
    if TELEGRAM_BOT_TOKEN else ""
)
WEBHOOK_SECRET = (
    hashlib.sha256(b"secret:" + TELEGRAM_BOT_TOKEN.encode()).hexdigest()[:48]
    if TELEGRAM_BOT_TOKEN else ""
)
USE_WEBHOOK = bool(TELEGRAM_BOT_TOKEN and RENDER_EXTERNAL_URL)

SOLD_OUT_KEYWORDS = ("已售完", "完售", "售完")
ON_SALE_KEYWORDS = ("熱賣中", "立即購票", "購票")
HEADER_KEYWORDS = ("票區", "空位", "區域", "座位", "票種", "類型")

# Seat-selection deep links live in the seat-map fragment, not the main table.
# Each <area> carries Send(page, performance_id, area_id, group_id, remaining);
# the matching table row exposes the same id via its `rel` attribute.
# Both single- and double-quoted JS args are accepted.
_SEND_RE = re.compile(
    r"""Send\(\s*['"]([^'"]*)['"]\s*,\s*['"]([^'"]*)['"]\s*,\s*['"]([^'"]*)['"]\s*,\s*['"]([^'"]*)['"]\s*,\s*['"]([^'"]*)['"]\s*\)"""
)
# Allow optional whitespace inside $() and support both quote styles.
_MAP_URL_RE = re.compile(r"""\$\(\s*['"]#mapdata['"]\s*\)\.load\(\s*['"]([^'"]+)['"]""")
_seat_link_cache: dict[str, dict[str, str]] = {}
_seat_link_cache_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Runtime config — list of monitors, mutated by Telegram commands
# ---------------------------------------------------------------------------

def _new_monitor(url: str = "", event_name: str = "", watch_zones: list[str] | None = None) -> dict:
    return {
        "target_url": url,
        "event_name": event_name,
        "watch_zones": watch_zones or [],
        "paused": False,
        "zone_status": {},   # tracks per-zone availability for change detection
    }


_monitors: list[dict] = []
_monitors_lock = threading.RLock()
_status: dict = {"last_check": "尚未執行", "zones": 0}

_env_url = os.getenv("TARGET_URL", "").strip()
if _env_url:
    _monitors.append(_new_monitor(
        url=_env_url,
        event_name=os.getenv("EVENT_NAME", "").strip(),
        watch_zones=[z.strip() for z in os.getenv("WATCH_ZONES", "").split(",") if z.strip()],
    ))

_session = requests.Session()
_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
})


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _monitors_lock:
            monitors = list(_monitors)
        if not monitors:
            summary = "🟡 待機（未設定任何監控網址）"
        else:
            lines = []
            for i, m in enumerate(monitors, 1):
                state = "⏸" if m["paused"] else "▶"
                name = m["event_name"] or m["target_url"] or "未設定"
                zones = "、".join(m["watch_zones"]) if m["watch_zones"] else "全部"
                lines.append(f"{state} [{i}] {name}（票區：{zones}）")
            summary = "\n".join(lines)
        body = (
            f"{summary}\n"
            f"最後檢查：{_status['last_check']}\n"
            f"追蹤票區：{_status['zones']}"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Only the Telegram webhook is allowed to POST. Anything else 404.
        if not WEBHOOK_PATH or self.path != WEBHOOK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        # Validate Telegram's secret_token header — forged requests with the
        # right path but no matching header are dropped.
        if self.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
            self.send_response(403)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            raw = self.rfile.read(length).decode("utf-8")
            update = json.loads(raw) if raw else {}
        except Exception as e:
            print(f"[BOT] webhook 解析失敗：{e}")
            self.send_response(400)
            self.end_headers()
            return
        # ACK immediately so Telegram does not retry; do the actual work
        # in the background since /check can take 5–10 seconds.
        self.send_response(200)
        self.end_headers()
        threading.Thread(
            target=_handle_update_safe, args=(update,), daemon=True
        ).start()

    def log_message(self, *args):
        pass


def start_web_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), _HealthHandler).serve_forever()


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram 未設定，略過通知")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            print(f"[WARN] Telegram API 錯誤：{resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[WARN] 發送 Telegram 通知失敗：{e}")


def self_ping() -> None:
    if not RENDER_EXTERNAL_URL:
        return
    try:
        requests.get(f"{RENDER_EXTERNAL_URL}/", timeout=5)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Page scraping — static HTML for fami.life-style (utiki) ticket pages.
# Table row layout: ['', '票區名稱', '票價', '空位 (數字 / 售完 / 熱賣中)']
# The empty first cell is the gotcha that broke the previous attempt.
# Logic ported from /Users/linzhanhu/清票監控/scrapers/utiki.py.
# ---------------------------------------------------------------------------

def _build_seat_links(page_html: str, base_url: str) -> dict[str, str]:
    """Map a table row's `rel` id (e.g. 'a288') to its direct seat-selection URL.

    Source is the seat-map fragment referenced by $('#mapdata').load(...). Each
    <area> there carries a Send(...) call with the parameters the site itself
    uses to navigate. These params are static per zone, so results are cached
    by the (version-stamped) fragment URL.
    """
    m = _MAP_URL_RE.search(page_html)
    if not m:
        return {}
    # Resolve relative URLs against the page URL so requests.get gets a full URL.
    map_url = urljoin(base_url, m.group(1))

    with _seat_link_cache_lock:
        cached = _seat_link_cache.get(map_url)
    if cached is not None:
        return cached

    try:
        resp = _session.get(map_url, timeout=20)
        if resp.status_code != 200:
            return {}
        map_html = resp.text
    except Exception:
        return {}

    origin = "/".join(base_url.split("/")[:3]) if "://" in base_url else ""
    links: dict[str, str] = {}
    soup = BeautifulSoup(map_html, "lxml")
    for area in soup.find_all("area"):
        area_id = area.get("id") or ""
        sm = _SEND_RE.search(area.get("href") or "")
        if not area_id or not sm:
            continue
        page, perf, area_param, group, _remaining = sm.groups()
        links[area_id] = (
            f"{origin}/UTK{page}_?PERFORMANCE_ID={perf}"
            f"&GROUP_ID={group}&PERFORMANCE_PRICE_AREA_ID={area_param}"
        )

    with _seat_link_cache_lock:
        _seat_link_cache[map_url] = links
    return links


def _parse_zones(html: str, base_url: str = "", seat_links: dict[str, str] | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    zones: list[dict] = []
    seat_links = seat_links or {}
    origin = "/".join(base_url.split("/")[:3]) if "://" in base_url else ""

    for row in soup.select("table tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        cell_texts = [c.get_text(strip=True) for c in cells]
        # Skip header rows
        if cells[0].name == "th" or any(kw in " ".join(cell_texts) for kw in HEADER_KEYWORDS):
            continue

        # Name is in cell[1] when first column is empty (fami.life layout),
        # otherwise in cell[0].
        name = cell_texts[1] if len(cell_texts) >= 2 and cell_texts[0] == "" else cell_texts[0]
        if not name or len(name) < 2:
            continue

        last_cell = cell_texts[-1]

        # Find price (>= 100 to avoid picking up tiny counts)
        price = ""
        price_idx = 2 if len(cell_texts) >= 4 and cell_texts[0] == "" else 1
        if price_idx < len(cell_texts):
            price_text = cell_texts[price_idx].replace(",", "")
            if re.fullmatch(r"\d+", price_text) and int(price_text) >= 100:
                price = price_text

        count_match = re.fullmatch(r"(\d+)", last_cell)
        if count_match:
            remaining = int(count_match.group(1))
            available = remaining > 0
            status = str(remaining) if available else "售完"
        elif any(kw in last_cell for kw in SOLD_OUT_KEYWORDS):
            available = False
            status = "售完"
        elif "熱賣中" in last_cell or any(kw in last_cell for kw in ON_SALE_KEYWORDS):
            available = True
            status = "熱賣中"
        else:
            # Unknown status text — skip rather than misreport
            continue

        # Primary: seat map matched by table row's `rel` id.
        # Fallback: Send() call in the row's own onclick attribute.
        rel = row.get("rel") or ""
        if isinstance(rel, list):
            rel = rel[0] if rel else ""
        zone_url = seat_links.get(rel)
        if not zone_url and origin:
            sm2 = _SEND_RE.search(row.get("onclick") or "")
            if sm2:
                pg, pf, ap, gp, _ = sm2.groups()
                zone_url = (
                    f"{origin}/UTK{pg}_?PERFORMANCE_ID={pf}"
                    f"&GROUP_ID={gp}&PERFORMANCE_PRICE_AREA_ID={ap}"
                )

        zones.append({
            "name": name,
            "price": price or "—",
            "status": status,
            "available": available,
            "url": zone_url,
        })

    return zones


def check_page(url: str) -> list[dict]:
    """Fetch the ticket page and return per-zone availability list."""
    headers = {"Referer": f"https://{url.split('/')[2]}/" if "://" in url else ""}
    resp = _session.get(url, headers={k: v for k, v in headers.items() if v}, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    seat_links = _build_seat_links(resp.text, url)
    return _parse_zones(resp.text, url, seat_links)


def _zone_link(zone: dict, fallback_url: str) -> str:
    return zone.get("url") or fallback_url


# ---------------------------------------------------------------------------
# Telegram command handling
# ---------------------------------------------------------------------------

_HELP = (
    "📋 <b>可用指令</b>\n\n"
    "<b>── 監控管理 ──</b>\n"
    "/addurl 網址 — 新增一場監控\n"
    "/list — 列出所有監控場次（含編號）\n"
    "/remove 編號 — 移除指定場次\n\n"
    "<b>── 場次設定（編號可省略，僅一場時）──</b>\n"
    "/seturl [編號] 網址 — 更新指定場次的網址\n"
    "/setevent [編號] 名稱 — 設定場次顯示名稱\n"
    "/setzones [編號] 關鍵字,關鍵字 — 設定票區篩選（留空=全部）\n\n"
    "<b>── 操作 ──</b>\n"
    "/check [編號] — 立即檢查（不填=全部）\n"
    "/status — 顯示所有場次狀態\n"
    "/pause [編號] — 暫停（不填=全部）\n"
    "/resume [編號] — 繼續（不填=全部）\n"
    "/help — 顯示此說明\n\n"
    "<b>範例（多場）：</b>\n"
    "<code>/addurl https://guardians.fami.life/...</code>\n"
    "<code>/setevent 2 週六場</code>\n"
    "<code>/setzones 2 B區,C區</code>"
)


def _parse_idx_arg(arg: str) -> tuple[int | None, str]:
    """Split optional 1-based index prefix from arg string.
    '/setevent 2 週六場' → (1, '週六場');  '/setevent 週六場' → (None, '週六場')
    Returns (0-based index or None, remaining text).
    """
    parts = arg.split(None, 1)
    if parts and parts[0].isdigit():
        return int(parts[0]) - 1, (parts[1] if len(parts) > 1 else "")
    return None, arg


def _resolve_monitor(idx: int | None) -> tuple[dict | None, str]:
    """Return (monitor, error_message). idx is 0-based or None (auto-select if single)."""
    with _monitors_lock:
        if not _monitors:
            return None, "⚠️ 尚未設定任何監控，請先發送 <code>/addurl 網址</code>"
        if idx is None:
            if len(_monitors) == 1:
                return _monitors[0], ""
            return None, "⚠️ 有多個場次，請加上編號，例如 <code>/check 1</code>\n\n發送 /list 查看編號"
        if idx < 0 or idx >= len(_monitors):
            return None, f"⚠️ 編號 {idx + 1} 不存在，發送 /list 查看目前場次"
        return _monitors[idx], ""


def _check_one(monitor: dict) -> None:
    """Fetch and report ticket availability for a single monitor."""
    url = monitor["target_url"]
    event_name = monitor["event_name"] or url
    watch_zones = monitor["watch_zones"]

    send_telegram(f"🔍 正在檢查 <b>{event_name}</b>，請稍候...")
    try:
        zones = check_page(url)
    except Exception as e:
        send_telegram(f"❌ <b>檢查失敗</b>（{event_name}）\n\n{e}")
        return

    if not zones:
        send_telegram(
            f"⚠️ <b>未能解析到任何票區資料</b>（{event_name}）\n\n"
            "頁面已載入但找不到票區。請確認網址正確，且為 fami.life 等"
            "靜態 HTML 票券頁面（不支援 tsghawks 等 SPA 平台）。"
        )
        return

    filtered = [z for z in zones if not watch_zones or any(w in z["name"] for w in watch_zones)]
    available_count = sum(1 for z in filtered if z["available"])
    sold_out_count = sum(1 for z in filtered if not z["available"])

    lines = []
    for z in filtered[:30]:
        if z["available"]:
            lines.append(f"✅ <a href='{_zone_link(z, url)}'>{z['name']}</a>（NT${z['price']}）")
        else:
            lines.append(f"❌ {z['name']}（NT${z['price']}）")
    if len(filtered) > 30:
        lines.append(f"⋯ 共 {len(filtered)} 個票區（只顯示前 30）")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    purchase_hint = "\n👆 點有票票區名稱直達選位頁" if available_count > 0 else ""
    send_telegram(
        f"📋 <b>即時票況｜{event_name}</b>\n\n"
        + "\n".join(lines)
        + f"\n\n✅ 有票：{available_count}　❌ 售完：{sold_out_count}\n"
        f"⏰ {now}"
        + purchase_hint
    )


def _handle_update(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message") or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))

    if chat_id != str(TELEGRAM_CHAT_ID):
        return

    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return

    parts = text.split(None, 1)
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/help", "/start"):
        send_telegram(_HELP)

    elif cmd == "/addurl":
        if not arg:
            send_telegram("用法：<code>/addurl 網址</code>")
            return
        with _monitors_lock:
            _monitors.append(_new_monitor(url=arg))
            n = len(_monitors)
        send_telegram(
            f"✅ <b>已新增監控 [{n}]</b>\n\n{arg}\n\n"
            f"發送 <code>/setevent {n} 場次名稱</code> 設定顯示名稱（選填）\n"
            f"發送 <code>/setzones {n} 票區關鍵字</code> 設定篩選（選填）"
        )

    elif cmd == "/list":
        with _monitors_lock:
            monitors = list(_monitors)
        if not monitors:
            send_telegram("目前沒有任何監控場次。\n\n發送 <code>/addurl 網址</code> 新增。")
            return
        lines = []
        for i, m in enumerate(monitors, 1):
            state = "⏸" if m["paused"] else "▶️"
            name = m["event_name"] or "（未命名）"
            zones = "、".join(m["watch_zones"]) if m["watch_zones"] else "全部"
            lines.append(f"{state} <b>[{i}]</b> {name}\n     票區：{zones}\n     {m['target_url']}")
        send_telegram("📋 <b>監控場次列表</b>\n\n" + "\n\n".join(lines))

    elif cmd == "/remove":
        if not arg or not arg.isdigit():
            send_telegram("用法：<code>/remove 編號</code>（發送 /list 查看編號）")
            return
        idx = int(arg) - 1
        with _monitors_lock:
            if idx < 0 or idx >= len(_monitors):
                send_telegram(f"⚠️ 編號 {idx + 1} 不存在")
                return
            removed = _monitors.pop(idx)
        name = removed["event_name"] or removed["target_url"]
        send_telegram(f"🗑 已移除監控：{name}")

    elif cmd == "/check":
        idx, _ = _parse_idx_arg(arg)
        with _monitors_lock:
            if not _monitors:
                send_telegram("⚠️ 尚未設定任何監控，請先發送 <code>/addurl 網址</code>")
                return
            if idx is not None:
                monitor, err = _resolve_monitor(idx)
                if err:
                    send_telegram(err)
                    return
                targets = [monitor]
            else:
                targets = list(_monitors)
        for m in targets:
            _check_one(m)

    elif cmd == "/seturl":
        idx, url = _parse_idx_arg(arg)
        if not url:
            send_telegram("用法：<code>/seturl [編號] 網址</code>")
            return
        with _monitors_lock:
            if not _monitors:
                # Create first monitor automatically
                _monitors.append(_new_monitor(url=url))
                n = len(_monitors)
                send_telegram(
                    f"✅ <b>已新增監控 [{n}]</b>\n\n{url}\n\n"
                    f"票區篩選已設為「全部」。"
                )
                return
            monitor, err = _resolve_monitor(idx)
        if err:
            send_telegram(err)
            return
        with _monitors_lock:
            monitor["target_url"] = url
            monitor["watch_zones"] = []
            monitor["zone_status"] = {}
        name = monitor["event_name"] or f"監控 {_monitors.index(monitor) + 1}"
        send_telegram(
            f"✅ <b>已更新網址</b>（{name}）\n\n{url}\n\n"
            f"票區篩選已重設為「全部」。"
        )

    elif cmd == "/setevent":
        idx, name = _parse_idx_arg(arg)
        if not name:
            send_telegram("用法：<code>/setevent [編號] 場次名稱</code>")
            return
        monitor, err = _resolve_monitor(idx)
        if err:
            send_telegram(err)
            return
        with _monitors_lock:
            monitor["event_name"] = name
        send_telegram(f"✅ 場次名稱已設為：{name}")

    elif cmd == "/setzones":
        idx, zones_str = _parse_idx_arg(arg)
        monitor, err = _resolve_monitor(idx)
        if err:
            send_telegram(err)
            return
        if not monitor["target_url"]:
            send_telegram("⚠️ 請先用 <code>/seturl</code> 設定監控網址")
            return
        zones = [z.strip() for z in zones_str.split(",") if z.strip()]
        with _monitors_lock:
            monitor["watch_zones"] = zones
            monitor["zone_status"] = {}
        if zones:
            send_telegram(f"✅ 已設定篩選票區：{'、'.join(zones)}")
        else:
            send_telegram("✅ 已清除篩選，下一輪監控全部票區")

    elif cmd == "/status":
        with _monitors_lock:
            monitors = list(_monitors)
        if not monitors:
            send_telegram("目前沒有任何監控場次。\n\n發送 <code>/addurl 網址</code> 新增。")
            return
        lines = []
        for i, m in enumerate(monitors, 1):
            if not m["target_url"]:
                state = "🟡 待機"
            elif m["paused"]:
                state = "⏸ 已暫停"
            else:
                state = "▶️ 監控中"
            name = m["event_name"] or "（未命名）"
            zones = "、".join(m["watch_zones"]) if m["watch_zones"] else "全部"
            lines.append(
                f"<b>[{i}] {name}</b> — {state}\n"
                f"     票區：{zones}\n"
                f"     {m['target_url'] or '未設定'}"
            )
        send_telegram(
            f"📊 <b>監控狀態</b>\n\n"
            + "\n\n".join(lines)
            + f"\n\n⏰ 最後檢查：{_status['last_check']}"
        )

    elif cmd == "/pause":
        idx, _ = _parse_idx_arg(arg)
        with _monitors_lock:
            if not _monitors:
                send_telegram("目前沒有任何監控場次。")
                return
            if idx is not None:
                monitor, err = _resolve_monitor(idx)
                if err:
                    send_telegram(err)
                    return
                monitor["paused"] = True
                targets = [monitor]
            else:
                for m in _monitors:
                    m["paused"] = True
                targets = list(_monitors)
        names = "、".join(m["event_name"] or f"場次{i+1}" for i, m in enumerate(_monitors) if m in targets)
        msg_body = f"⏸ <b>已暫停：{names}</b>"
        if USE_WEBHOOK and idx is None:
            msg_body += (
                "\n\n已停止 self-ping，約 15 分鐘後 Render 會自動休眠。\n"
                "下次發任何指令會自動喚醒（cold start 約 30–60 秒）。\n\n"
                "⚠️ 喚醒後設定會重設，需重新 /addurl。"
            )
        send_telegram(msg_body)

    elif cmd == "/resume":
        idx, _ = _parse_idx_arg(arg)
        with _monitors_lock:
            if not _monitors:
                send_telegram("目前沒有任何監控場次。")
                return
            if idx is not None:
                monitor, err = _resolve_monitor(idx)
                if err:
                    send_telegram(err)
                    return
                monitor["paused"] = False
            else:
                for m in _monitors:
                    m["paused"] = False
        send_telegram("▶️ 監控已繼續")

    else:
        send_telegram(f"未知指令：{cmd}\n\n{_HELP}")


def _handle_update_safe(update: dict) -> None:
    try:
        _handle_update(update)
    except Exception as e:
        print(f"[BOT] 指令處理錯誤：{e}")


def register_bot_commands() -> None:
    """Register bot commands so Telegram shows the shortcut menu."""
    if not TELEGRAM_BOT_TOKEN:
        return
    commands = [
        {"command": "addurl",   "description": "新增一場監控"},
        {"command": "list",     "description": "列出所有監控場次"},
        {"command": "check",    "description": "立即檢查票況（可加編號）"},
        {"command": "status",   "description": "顯示所有場次狀態"},
        {"command": "seturl",   "description": "更新指定場次網址"},
        {"command": "setevent", "description": "設定場次顯示名稱"},
        {"command": "setzones", "description": "設定票區篩選（逗號分隔）"},
        {"command": "remove",   "description": "移除指定場次"},
        {"command": "pause",    "description": "暫停監控（可加編號）"},
        {"command": "resume",   "description": "繼續監控（可加編號）"},
        {"command": "help",     "description": "顯示說明"},
    ]
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands",
            json={"commands": commands},
            timeout=10,
        )
        if resp.ok and resp.json().get("ok"):
            print("[BOT] 指令選單已註冊")
        else:
            print(f"[BOT] 指令選單註冊失敗：{resp.text}")
    except Exception as e:
        print(f"[BOT] 指令選單註冊錯誤：{e}")


def register_telegram_webhook() -> bool:
    """Switch the bot to webhook mode so cold-started Render instances
    can wake up on incoming Telegram messages. Returns True on success."""
    if not USE_WEBHOOK:
        return False
    webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
            json={
                "url": webhook_url,
                # Keep pending updates: the message that woke us up during a
                # cold start is queued by Telegram and must be delivered.
                "drop_pending_updates": False,
                "allowed_updates": ["message"],
                "secret_token": WEBHOOK_SECRET,
            },
            timeout=10,
        )
        if resp.ok and resp.json().get("ok"):
            print(f"[BOT] Webhook 註冊成功：{webhook_url}")
            return True
        print(f"[BOT] Webhook 註冊失敗：{resp.text}")
    except Exception as e:
        print(f"[BOT] Webhook 註冊錯誤：{e}")
    return False


def telegram_polling_thread() -> None:
    """Long-poll fallback for local development (no RENDER_EXTERNAL_URL)."""
    if not TELEGRAM_BOT_TOKEN:
        return
    # Clear any webhook so getUpdates is allowed.
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook",
            json={"drop_pending_updates": True},
            timeout=10,
        )
    except Exception:
        pass
    offset = 0
    print("[BOT] 改用 long-polling 接收 Telegram 指令")
    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"timeout": 30, "offset": offset, "allowed_updates": ["message"]},
                timeout=35,
            )
            if resp.ok:
                for update in resp.json().get("result", []):
                    offset = update["update_id"] + 1
                    threading.Thread(
                        target=_handle_update_safe, args=(update,), daemon=True
                    ).start()
        except Exception as e:
            print(f"[BOT] 輪詢錯誤：{e}")
            time.sleep(5)


# ---------------------------------------------------------------------------
# Main monitor loop
# ---------------------------------------------------------------------------

def _check_monitor_once(monitor: dict, now: str) -> None:
    """Run one monitoring cycle for a single monitor (called from main loop)."""
    target_url = monitor["target_url"]
    current_zones = monitor["watch_zones"]
    event_name = monitor["event_name"] or target_url
    zone_status = monitor["zone_status"]

    try:
        zones = check_page(target_url)

        if not zones:
            print(f"[{now}] [{event_name}] [WARN] 未解析到票區資料")
            return

        newly_available: list[dict] = []
        already_available: list[dict] = []

        for zone in zones:
            name = zone["name"]
            if current_zones and not any(w in name for w in current_zones):
                continue

            avail = zone["available"]
            prev = zone_status.get(name)
            status_label = f"✅ 有票（剩 {zone['status']}）" if avail else "❌ 售完"
            print(f"[{now}] [{event_name}] {name} ｜ NT${zone['price']} ｜ {status_label}")

            if prev is None and avail:
                already_available.append(zone)
            elif prev is not None and not prev and avail:
                newly_available.append(zone)

            zone_status[name] = avail

        if already_available:
            lines = "\n".join(
                f"• <a href='{_zone_link(z, target_url)}'>{z['name']}</a>"
                f"（NT${z['price']}，剩餘：{z['status']}）"
                for z in already_available
            )
            send_telegram(
                f"ℹ️ <b>啟動時即有票的票區</b>\n\n"
                f"<b>{event_name}</b>\n\n{lines}\n\n"
                f"👆 點票區名稱直達選位頁"
            )

        if newly_available:
            lines = "\n".join(
                f"• <a href='{_zone_link(z, target_url)}'>{z['name']}</a>"
                f"（NT${z['price']}，剩餘：{z['status']}）"
                for z in newly_available
            )
            send_telegram(
                f"🎫 <b>票券釋出通知！</b>\n\n"
                f"<b>{event_name}</b>\n\n"
                f"以下票區有票可購買：\n{lines}\n\n"
                f"👆 點票區名稱直達選位頁\n\n"
                f"⏰ 偵測時間：{now}"
            )
            print(f"[{now}] [{event_name}] Telegram 通知已發送（{len(newly_available)} 個票區）")

    except Exception as e:
        print(f"[{now}] [{event_name}] [ERROR] {e}")


def main() -> None:
    print(f"[監控啟動] 共 {len(_monitors)} 個場次")
    print(f"[檢查間隔] 每 {CHECK_INTERVAL} 秒")
    print("-" * 60)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[警告] 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，不會發送通知")
        print("-" * 60)

    with _monitors_lock:
        initial_monitors = list(_monitors)

    if initial_monitors:
        lines = "\n".join(
            f"• {m['event_name'] or m['target_url']}" for m in initial_monitors
        )
        send_telegram(
            f"✅ <b>票券監控已啟動</b>\n\n"
            f"共 {len(initial_monitors)} 個監控場次：\n{lines}\n\n"
            f"檢查間隔：每 {CHECK_INTERVAL} 秒\n\n"
            f"發送 /help 查看可用指令"
        )
    else:
        send_telegram(
            "🟡 <b>票券監控待機中</b>\n\n"
            "尚未設定監控網址，請先發送：\n"
            "<code>/addurl 網址</code>\n\n"
            "發送 /help 查看所有指令"
        )

    last_ping = 0.0
    notified_idle = False

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with _monitors_lock:
            active = [m for m in _monitors if m["target_url"] and not m["paused"]]

        if not active:
            if not notified_idle:
                print(f"[{now}] 待機中：無啟用中的監控（允許 Render 自然 spin down）")
                notified_idle = True
            time.sleep(CHECK_INTERVAL)
            continue

        notified_idle = False

        # Self-ping only while actively monitoring. In standby / paused state
        # we deliberately let the Render free instance spin down to save
        # account-wide free hours. The webhook (POST from Telegram) will cold-
        # start the service back up when the user sends /resume or /addurl.
        if time.time() - last_ping >= 600:
            self_ping()
            last_ping = time.time()

        total_zones = 0
        for monitor in active:
            _check_monitor_once(monitor, now)
            total_zones += len(monitor["zone_status"])

        _status["last_check"] = now
        _status["zones"] = total_zones

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    # HTTPServer() binds the port synchronously inside __init__ which happens
    # before serve_forever returns control, so by the time the next statement
    # runs the socket is already accepting connections.
    threading.Thread(target=start_web_server, daemon=True).start()
    register_bot_commands()
    if not register_telegram_webhook():
        # No RENDER_EXTERNAL_URL (likely local dev) or webhook setup failed:
        # fall back to long-polling so the bot still works.
        threading.Thread(target=telegram_polling_thread, daemon=True).start()
    main()
