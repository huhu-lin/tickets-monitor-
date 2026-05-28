import os
import re
import threading
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# ---------------------------------------------------------------------------
# Runtime config — mutated by Telegram commands
# ---------------------------------------------------------------------------
_config: dict = {
    "target_url": os.getenv(
        "TARGET_URL",
        "https://guardians.fami.life/UTK0204_?PERFORMANCE_ID=P19LRRQA&PRODUCT_ID=P15UU08Q",
    ),
    "event_name": os.getenv("EVENT_NAME", "Guardians UTK0204"),
    "watch_zones": [z.strip() for z in os.getenv("WATCH_ZONES", "").split(",") if z.strip()],
    "paused": False,
    "url_changed": False,
}

_status: dict = {"last_check": "尚未執行", "zones": 0}

_session = requests.Session()
_session.headers.update(_HEADERS)

# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        zones_label = "、".join(_config["watch_zones"]) if _config["watch_zones"] else "全部"
        paused_label = "⏸ 已暫停" if _config["paused"] else "▶ 監控中"
        body = (
            f"{paused_label}\n"
            f"場次：{_config['event_name']}\n"
            f"篩選票區：{zones_label}\n"
            f"最後檢查：{_status['last_check']}\n"
            f"追蹤票區：{_status['zones']}"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
# Telegram command handling
# ---------------------------------------------------------------------------

_HELP = (
    "📋 <b>可用指令</b>\n\n"
    "/seturl 網址 — 設定監控網址\n"
    "/setzones 關鍵字,關鍵字 — 設定票區篩選（留空=全部）\n"
    "/status — 顯示目前設定與狀態\n"
    "/pause — 暫停監控\n"
    "/resume — 繼續監控\n"
    "/help — 顯示此說明\n\n"
    "範例：\n"
    "<code>/setzones B1層,外野</code>\n"
    "<code>/setzones</code>（清除篩選，監控全部）"
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

    if cmd == "/help":
        send_telegram(_HELP)

    elif cmd == "/seturl":
        if not arg:
            send_telegram("用法：/seturl 網址\n例如：\n<code>/seturl https://guardians.fami.life/...</code>")
        else:
            _config["target_url"] = arg
            _config["event_name"] = "自訂場次"
            _config["watch_zones"] = []
            _config["url_changed"] = True
            send_telegram(
                f"✅ <b>已更新監控網址</b>\n\n{arg}\n\n"
                f"票區篩選已重設為「全部」，下一輪開始監控新場次。"
            )

    elif cmd == "/setzones":
        zones = [z.strip() for z in arg.split(",") if z.strip()]
        _config["watch_zones"] = zones
        _config["url_changed"] = True
        if zones:
            send_telegram(f"✅ 已設定篩選票區：{'、'.join(zones)}")
        else:
            send_telegram("✅ 已清除篩選，下一輪監控全部票區")

    elif cmd == "/status":
        zones_label = "、".join(_config["watch_zones"]) if _config["watch_zones"] else "全部"
        paused_label = "⏸ 已暫停" if _config["paused"] else "▶️ 監控中"
        send_telegram(
            f"📊 <b>目前狀態</b>\n\n"
            f"狀態：{paused_label}\n"
            f"場次：{_config['event_name']}\n"
            f"篩選票區：{zones_label}\n"
            f"最後檢查：{_status['last_check']}\n"
            f"追蹤票區數：{_status['zones']}\n\n"
            f"🔗 {_config['target_url']}"
        )

    elif cmd == "/pause":
        _config["paused"] = True
        send_telegram("⏸ 監控已暫停，發送 /resume 繼續")

    elif cmd == "/resume":
        _config["paused"] = False
        send_telegram("▶️ 監控已繼續")

    else:
        send_telegram(f"未知指令：{cmd}\n\n{_HELP}")


def telegram_command_thread() -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    offset = 0
    print("[BOT] 開始接收 Telegram 指令")
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
                    try:
                        _handle_update(update)
                    except Exception as e:
                        print(f"[BOT] 指令處理錯誤：{e}")
        except Exception as e:
            print(f"[BOT] 輪詢錯誤：{e}")
            time.sleep(5)


# ---------------------------------------------------------------------------
# Page scraping (requests + BeautifulSoup, no browser required)
# ---------------------------------------------------------------------------

def _parse_html_table(soup: BeautifulSoup) -> list[dict]:
    """Extract zones from a standard HTML table (票區 / 票價 / 空位)."""
    zones = []
    for row in soup.select("table tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        name = cells[0].get_text(strip=True)
        price = re.sub(r"[^\d]", "", cells[1].get_text(strip=True))
        status = cells[2].get_text(strip=True)
        if name and price and (status == "售完" or status.isdigit()):
            zones.append({"name": name, "price": price, "status": status})
    return zones


def _parse_embedded_json(soup: BeautifulSoup) -> list[dict]:
    """
    Look for zone data embedded in <script> tags.
    Many SSR React/Vue apps serialize initial state as JSON in the page.
    """
    zones = []
    for script in soup.find_all("script"):
        content = script.string or ""
        if "售完" not in content:
            continue
        # Look for JSON arrays/objects containing 售完
        # Extract all quoted strings near 售完 to find zone names and prices
        # Pattern: "name":"...","price":NNNN,"status":"售完"
        for m in re.finditer(
            r'"(?:name|zoneName|areaName|seatName)"\s*:\s*"([^"]+)"'
            r'(?:.*?)"(?:price|ticketPrice|amount)"\s*:\s*(\d+)'
            r'(?:.*?)"(?:status|availability|remain|空位)"\s*:\s*"?(\d+|售完)"?',
            content,
            re.DOTALL,
        ):
            zones.append({"name": m.group(1), "price": m.group(2), "status": m.group(3)})

        if not zones:
            # Looser: find any occurrence of 售完 near a zone-like name
            for m in re.finditer(r'"([A-Z0-9_一-鿿]{3,30})"\s*,\s*(\d{3,5})\s*,\s*"?(售完|\d+)"?', content):
                zones.append({"name": m.group(1), "price": m.group(2), "status": m.group(3)})

        if zones:
            break
    return zones


def check_page(url: str) -> list[dict]:
    """
    Fetch the ticket page and extract per-zone availability.
    Uses requests + BeautifulSoup; no browser required.
    Returns list of {name, price, status, available}.
    """
    resp = _session.get(url, timeout=30)

    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}，網站可能需要瀏覽器才能存取")

    soup = BeautifulSoup(resp.text, "html.parser")

    zones = _parse_html_table(soup)
    if not zones:
        zones = _parse_embedded_json(soup)

    return [
        {**z, "available": z["status"] != "售完"}
        for z in zones
        if z.get("name") and len(z["name"]) >= 2
    ]


# ---------------------------------------------------------------------------
# Main monitor loop (synchronous — no asyncio needed without Playwright)
# ---------------------------------------------------------------------------

def main() -> None:
    zones_label = "、".join(_config["watch_zones"]) if _config["watch_zones"] else "全部"
    print(f"[監控啟動] {_config['event_name']}")
    print(f"[目標 URL] {_config['target_url']}")
    print(f"[篩選票區] {zones_label}")
    print(f"[檢查間隔] 每 {CHECK_INTERVAL} 秒")
    print("-" * 60)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[警告] 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，不會發送通知")
        print("-" * 60)

    send_telegram(
        f"✅ <b>票券監控已啟動</b>\n\n"
        f"場次：{_config['event_name']}\n"
        f"篩選票區：{zones_label}\n"
        f"檢查間隔：每 {CHECK_INTERVAL} 秒\n\n"
        f"🔗 {_config['target_url']}\n\n"
        f"發送 /help 查看可用指令"
    )

    zone_status: dict[str, bool | None] = {}
    last_url = _config["target_url"]
    last_ping = 0.0

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if _config["url_changed"] or _config["target_url"] != last_url:
            zone_status.clear()
            last_url = _config["target_url"]
            _config["url_changed"] = False
            print(f"[{now}] 設定已更新，重設票區追蹤狀態")

        if _config["paused"]:
            time.sleep(CHECK_INTERVAL)
            continue

        if time.time() - last_ping >= 600:
            self_ping()
            last_ping = time.time()

        try:
            zones = check_page(_config["target_url"])

            if not zones:
                print(
                    f"[{now}] [WARN] 未解析到票區資料。"
                    f"若持續出現，此網站可能需要瀏覽器才能載入（JavaScript 渲染）。"
                )
                time.sleep(CHECK_INTERVAL)
                continue

            _status["last_check"] = now
            _status["zones"] = len(zones)

            current_zones = _config["watch_zones"]
            event_name = _config["event_name"]
            current_url = _config["target_url"]

            newly_available: list[dict] = []
            already_available: list[dict] = []

            for zone in zones:
                name = zone["name"]
                if current_zones and not any(w in name for w in current_zones):
                    continue

                avail = zone["available"]
                prev = zone_status.get(name)
                status_label = f"✅ 有票（剩 {zone['status']}）" if avail else "❌ 售完"
                print(f"[{now}] {name} ｜ NT${zone['price']} ｜ {status_label}")

                if prev is None and avail:
                    already_available.append(zone)
                elif prev is not None and not prev and avail:
                    newly_available.append(zone)

                zone_status[name] = avail

            if already_available:
                lines = "\n".join(
                    f"• {z['name']}（NT${z['price']}，剩餘：{z['status']}）"
                    for z in already_available
                )
                send_telegram(
                    f"ℹ️ <b>啟動時即有票的票區</b>\n\n"
                    f"<b>{event_name}</b>\n\n{lines}\n\n"
                    f"🔗 <a href='{current_url}'>立即前往購票</a>"
                )

            if newly_available:
                lines = "\n".join(
                    f"• {z['name']}（NT${z['price']}，剩餘：{z['status']}）"
                    for z in newly_available
                )
                send_telegram(
                    f"🎫 <b>票券釋出通知！</b>\n\n"
                    f"<b>{event_name}</b>\n\n"
                    f"以下票區有票可購買：\n{lines}\n\n"
                    f"🔗 <a href='{current_url}'>立即前往購票</a>\n\n"
                    f"⏰ 偵測時間：{now}"
                )
                print(f"[{now}] Telegram 通知已發送（{len(newly_available)} 個票區）")

        except Exception as e:
            print(f"[{now}] [ERROR] {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    threading.Thread(target=start_web_server, daemon=True).start()
    threading.Thread(target=telegram_command_thread, daemon=True).start()
    main()
