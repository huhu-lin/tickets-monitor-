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

SOLD_OUT_KEYWORDS = ("已售完", "完售", "售完")
ON_SALE_KEYWORDS = ("熱賣中", "立即購票", "購票")
HEADER_KEYWORDS = ("票區", "空位", "區域", "座位", "票種", "類型")

# ---------------------------------------------------------------------------
# Runtime config — mutated by Telegram commands
# ---------------------------------------------------------------------------
_config: dict = {
    "target_url": os.getenv("TARGET_URL", "").strip(),
    "event_name": os.getenv("EVENT_NAME", "").strip(),
    "watch_zones": [z.strip() for z in os.getenv("WATCH_ZONES", "").split(",") if z.strip()],
    "paused": False,
    "url_changed": False,
}

_status: dict = {"last_check": "尚未執行", "zones": 0}
_config_lock = threading.RLock()

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

def _event_label() -> str:
    return _config["event_name"] or "未設定"


def _url_label() -> str:
    return _config["target_url"] or "未設定"


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        zones_label = "、".join(_config["watch_zones"]) if _config["watch_zones"] else "全部"
        if not _config["target_url"]:
            paused_label = "🟡 待機（未設定網址）"
        elif _config["paused"]:
            paused_label = "⏸ 已暫停"
        else:
            paused_label = "▶ 監控中"
        body = (
            f"{paused_label}\n"
            f"場次：{_event_label()}\n"
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
# Page scraping — static HTML for fami.life-style (utiki) ticket pages.
# Table row layout: ['', '票區名稱', '票價', '空位 (數字 / 售完 / 熱賣中)']
# The empty first cell is the gotcha that broke the previous attempt.
# Logic ported from /Users/linzhanhu/清票監控/scrapers/utiki.py.
# ---------------------------------------------------------------------------

def _parse_zones(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    zones: list[dict] = []

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

        zones.append({
            "name": name,
            "price": price or "—",
            "status": status,
            "available": available,
        })

    return zones


def check_page(url: str) -> list[dict]:
    """Fetch the ticket page and return per-zone availability list."""
    headers = {"Referer": f"https://{url.split('/')[2]}/" if "://" in url else ""}
    resp = _session.get(url, headers={k: v for k, v in headers.items() if v}, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return _parse_zones(resp.text)


# ---------------------------------------------------------------------------
# Telegram command handling
# ---------------------------------------------------------------------------

_HELP = (
    "📋 <b>可用指令</b>\n\n"
    "/check — 立即檢查並回傳當下票況\n"
    "/seturl 網址 — 設定監控網址\n"
    "/setevent 場次名稱 — 設定場次顯示名稱\n"
    "/setzones 關鍵字,關鍵字 — 設定票區篩選（留空=全部）\n"
    "/status — 顯示目前設定與狀態\n"
    "/pause — 暫停監控\n"
    "/resume — 繼續監控\n"
    "/help — 顯示此說明\n\n"
    "<b>第一次使用流程：</b>\n"
    "<code>/seturl https://guardians.fami.life/...</code>\n"
    "<code>/setevent 場次顯示名稱</code>（選填）\n"
    "<code>/setzones B1層,外野</code>（選填）"
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

    elif cmd == "/check":
        if not _config["target_url"]:
            send_telegram("⚠️ 尚未設定監控網址，請先發送：\n<code>/seturl 網址</code>")
            return
        send_telegram("🔍 正在檢查，請稍候...")
        try:
            zones = check_page(_config["target_url"])
        except Exception as e:
            send_telegram(f"❌ <b>檢查失敗</b>\n\n{e}")
            return

        if not zones:
            send_telegram(
                "⚠️ <b>未能解析到任何票區資料</b>\n\n"
                "頁面已載入但找不到票區。請確認網址正確，且為 fami.life 等"
                "靜態 HTML 票券頁面（不支援 tsghawks 等 SPA 平台）。"
            )
            return

        current_zones = _config["watch_zones"]
        filtered = [
            z for z in zones
            if not current_zones or any(w in z["name"] for w in current_zones)
        ]

        available_count = sum(1 for z in filtered if z["available"])
        sold_out_count = sum(1 for z in filtered if not z["available"])

        lines = []
        for z in filtered[:30]:
            icon = "✅" if z["available"] else "❌"
            lines.append(f"{icon} {z['name']}（NT${z['price']}）")
        if len(filtered) > 30:
            lines.append(f"⋯ 共 {len(filtered)} 個票區（只顯示前 30）")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        purchase_link = (
            f"\n🔗 <a href='{_config['target_url']}'>立即前往購票</a>"
            if available_count > 0 else ""
        )
        send_telegram(
            f"📋 <b>即時票況</b>\n\n"
            + "\n".join(lines)
            + f"\n\n✅ 有票：{available_count}　❌ 售完：{sold_out_count}\n"
            f"⏰ {now}"
            + purchase_link
        )

    elif cmd == "/seturl":
        if not arg:
            send_telegram("用法：<code>/seturl 網址</code>")
        else:
            with _config_lock:
                _config["target_url"] = arg
                _config["watch_zones"] = []
                _config["url_changed"] = True
            send_telegram(
                f"✅ <b>已更新監控網址</b>\n\n{arg}\n\n"
                f"票區篩選已重設為「全部」。\n"
                f"如需自訂場次顯示名稱，發送 <code>/setevent 名稱</code>。"
            )

    elif cmd == "/setevent":
        if not arg:
            send_telegram("用法：<code>/setevent 場次名稱</code>")
        else:
            with _config_lock:
                _config["event_name"] = arg
            send_telegram(f"✅ 場次名稱已更新為：{arg}")

    elif cmd == "/setzones":
        if not _config["target_url"]:
            send_telegram("⚠️ 請先用 <code>/seturl</code> 設定監控網址")
            return
        zones = [z.strip() for z in arg.split(",") if z.strip()]
        with _config_lock:
            _config["watch_zones"] = zones
            _config["url_changed"] = True
        if zones:
            send_telegram(f"✅ 已設定篩選票區：{'、'.join(zones)}")
        else:
            send_telegram("✅ 已清除篩選，下一輪監控全部票區")

    elif cmd == "/status":
        zones_label = "、".join(_config["watch_zones"]) if _config["watch_zones"] else "全部"
        if not _config["target_url"]:
            state_label = "🟡 待機（未設定網址）"
        elif _config["paused"]:
            state_label = "⏸ 已暫停"
        else:
            state_label = "▶️ 監控中"
        send_telegram(
            f"📊 <b>目前狀態</b>\n\n"
            f"狀態：{state_label}\n"
            f"場次：{_event_label()}\n"
            f"篩選票區：{zones_label}\n"
            f"最後檢查：{_status['last_check']}\n"
            f"追蹤票區數：{_status['zones']}\n\n"
            f"🔗 {_url_label()}"
        )

    elif cmd == "/pause":
        _config["paused"] = True
        send_telegram("⏸ 監控已暫停，發送 /resume 繼續")

    elif cmd == "/resume":
        _config["paused"] = False
        send_telegram("▶️ 監控已繼續")

    else:
        send_telegram(f"未知指令：{cmd}\n\n{_HELP}")


def _handle_update_safe(update: dict) -> None:
    try:
        _handle_update(update)
    except Exception as e:
        print(f"[BOT] 指令處理錯誤：{e}")


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
                    threading.Thread(
                        target=_handle_update_safe, args=(update,), daemon=True
                    ).start()
        except Exception as e:
            print(f"[BOT] 輪詢錯誤：{e}")
            time.sleep(5)


# ---------------------------------------------------------------------------
# Main monitor loop
# ---------------------------------------------------------------------------

def main() -> None:
    zones_label = "、".join(_config["watch_zones"]) if _config["watch_zones"] else "全部"
    print(f"[監控啟動] {_event_label()}")
    print(f"[目標 URL] {_url_label()}")
    print(f"[篩選票區] {zones_label}")
    print(f"[檢查間隔] 每 {CHECK_INTERVAL} 秒")
    print("-" * 60)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[警告] 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，不會發送通知")
        print("-" * 60)

    if _config["target_url"]:
        send_telegram(
            f"✅ <b>票券監控已啟動</b>\n\n"
            f"場次：{_event_label()}\n"
            f"篩選票區：{zones_label}\n"
            f"檢查間隔：每 {CHECK_INTERVAL} 秒\n\n"
            f"🔗 {_config['target_url']}\n\n"
            f"發送 /help 查看可用指令"
        )
    else:
        send_telegram(
            "🟡 <b>票券監控待機中</b>\n\n"
            "尚未設定監控網址，請先發送：\n"
            "<code>/seturl 網址</code>\n\n"
            "發送 /help 查看所有指令"
        )

    zone_status: dict[str, bool | None] = {}
    last_url = _config["target_url"]
    last_ping = 0.0
    notified_idle = False

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with _config_lock:
            target_url = _config["target_url"]
            current_zones = list(_config["watch_zones"])
            paused = _config["paused"]
            url_changed = _config["url_changed"]
            event_name = _config["event_name"] or "未設定"
            if url_changed:
                _config["url_changed"] = False

        if url_changed or target_url != last_url:
            zone_status.clear()
            last_url = target_url
            notified_idle = False
            print(f"[{now}] 設定已更新，重設票區追蹤狀態")

        if not target_url:
            if not notified_idle:
                print(f"[{now}] 待機中：尚未設定監控網址")
                notified_idle = True
            time.sleep(CHECK_INTERVAL)
            continue

        if paused:
            time.sleep(CHECK_INTERVAL)
            continue

        if time.time() - last_ping >= 600:
            self_ping()
            last_ping = time.time()

        try:
            zones = check_page(target_url)

            if not zones:
                print(f"[{now}] [WARN] 未解析到票區資料。網址可能不是支援的票券頁面。")
                time.sleep(CHECK_INTERVAL)
                continue

            _status["last_check"] = now
            _status["zones"] = len(zones)

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
                    f"🔗 <a href='{target_url}'>立即前往購票</a>"
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
                    f"🔗 <a href='{target_url}'>立即前往購票</a>\n\n"
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
