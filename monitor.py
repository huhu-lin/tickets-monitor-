import os
import queue
import threading
import time
import requests
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

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
# Page scraping — single Playwright worker thread (sync_playwright is NOT
# thread-safe; all browser interactions must happen inside one OS thread).
# ---------------------------------------------------------------------------

_JS_EXTRACT = r"""
() => {
    const zones = [];

    // Strategy 1: standard <table>
    const rows = document.querySelectorAll('table tr');
    for (const row of rows) {
        const cells = row.querySelectorAll('td');
        if (cells.length < 3) continue;
        const name   = (cells[0].innerText || '').trim();
        const price  = (cells[1].innerText || '').trim().replace(/[^\d]/g, '');
        const status = (cells[2].innerText || '').trim();
        if (name.length > 1 && price && (status === '售完' || /^\d+$/.test(status))) {
            zones.push({ name, price, status });
        }
    }
    if (zones.length > 0) return zones;

    // Strategy 2: div-based layout — find leaf "售完" nodes, walk up
    const leaves = Array.from(document.querySelectorAll('*'))
        .filter(el => !el.children.length && (el.innerText || '').trim() === '售完');
    for (const el of leaves) {
        let p = el.parentElement;
        for (let d = 0; d < 6 && p; d++, p = p.parentElement) {
            const kids = Array.from(p.children)
                .map(c => (c.innerText || '').trim())
                .filter(Boolean);
            if (kids.length >= 3) {
                const name   = kids[0];
                const status = kids[kids.length - 1];
                const price  = kids.find(t => /^\d{3,5}$/.test(t)) || '';
                if (name.length > 2 && price && (status === '售完' || /^\d+$/.test(status))) {
                    zones.push({ name, price, status });
                    break;
                }
            }
        }
    }

    return zones;
}
"""

_scraper_request_q: "queue.Queue[tuple[str, queue.Queue]]" = queue.Queue()
_scraper_ready = threading.Event()
_scraper_dead = threading.Event()
_scraper_error: list[str] = []


def _scraper_worker() -> None:
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            _scraper_ready.set()
            print("[SCRAPER] Playwright worker 就緒")
            while True:
                url, reply_q = _scraper_request_q.get()
                try:
                    context = browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        locale="zh-TW",
                    )
                    page = context.new_page()
                    try:
                        page.goto(url, wait_until="networkidle", timeout=30000)
                        raw = page.evaluate(_JS_EXTRACT)
                        zones = [
                            {**z, "available": z["status"] != "售完"}
                            for z in raw
                            if z.get("name") and len(z["name"]) >= 2
                        ]
                        reply_q.put(("ok", zones))
                    finally:
                        try:
                            page.close()
                        except Exception:
                            pass
                        context.close()
                except Exception as e:
                    reply_q.put(("err", e))
    except Exception as e:
        _scraper_error.append(str(e))
        _scraper_dead.set()
        _scraper_ready.set()
        print(f"[SCRAPER] worker 異常結束：{e}")
        # Drain pending requests so callers don't hang forever
        while True:
            try:
                _, reply_q = _scraper_request_q.get_nowait()
                reply_q.put(("err", RuntimeError(f"Playwright worker 已停止：{e}")))
            except queue.Empty:
                break


def check_page(url: str) -> list[dict]:
    if _scraper_dead.is_set():
        msg = _scraper_error[0] if _scraper_error else "未知原因"
        raise RuntimeError(f"Playwright worker 已停止運作：{msg}")
    if not _scraper_ready.wait(timeout=60):
        raise RuntimeError("Playwright worker 尚未就緒")
    if _scraper_dead.is_set():
        msg = _scraper_error[0] if _scraper_error else "未知原因"
        raise RuntimeError(f"Playwright worker 已停止運作：{msg}")
    reply_q: queue.Queue = queue.Queue(maxsize=1)
    _scraper_request_q.put((url, reply_q))
    try:
        kind, payload = reply_q.get(timeout=90)
    except queue.Empty:
        raise RuntimeError("檢查逾時（90 秒）")
    if kind == "err":
        raise payload
    return payload


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
    "<code>/seturl https://...</code>\n"
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

    if cmd == "/help" or cmd == "/start":
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
                "頁面已載入但找不到票區，請確認網址是否正確。"
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
        send_telegram(
            f"📋 <b>即時票況</b>\n\n"
            + "\n".join(lines)
            + f"\n\n✅ 有票：{available_count}　❌ 售完：{sold_out_count}\n"
            f"⏰ {now}"
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
                    # Run each update in its own thread so /check (which can
                    # block 30–90s on Playwright) does not stall the long-poll.
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

        # Snapshot config under lock so all reads within this iteration agree.
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
                print(f"[{now}] [WARN] 未解析到票區資料，請確認網址是否正確。")
                time.sleep(CHECK_INTERVAL)
                continue

            _status["last_check"] = now
            _status["zones"] = len(zones)

            current_url = target_url

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
    threading.Thread(target=_scraper_worker, daemon=True).start()
    threading.Thread(target=telegram_command_thread, daemon=True).start()
    main()
