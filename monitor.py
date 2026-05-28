import asyncio
import os
import threading
import time
import requests
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

TARGET_URL = os.getenv(
    "TARGET_URL",
    "https://guardians.fami.life/UTK0204_?PERFORMANCE_ID=P19LRRQA&PRODUCT_ID=P15UU08Q"
)
EVENT_NAME = os.getenv("EVENT_NAME", "Guardians UTK0204")
# Comma-separated keywords to filter zones (empty = watch all)
# e.g. "B1層,外野" watches only zones whose names contain "B1層" or "外野"
WATCH_ZONES = [z.strip() for z in os.getenv("WATCH_ZONES", "").split(",") if z.strip()]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")  # auto-set by Render

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# JavaScript injected into the page to extract zone rows.
# Tries a standard <table> first, then falls back to finding leaf nodes
# with text "售完" and walking up the DOM to find the row context.
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


# Shared status dict updated by the monitor loop, read by the HTTP handler
_status: dict = {"last_check": "尚未執行", "zones": 0}


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            f"OK\n"
            f"最後檢查：{_status['last_check']}\n"
            f"追蹤票區：{_status['zones']}"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # Suppress per-request logs


def start_web_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), _HealthHandler).serve_forever()


def self_ping() -> None:
    """Ping own health endpoint so Render free tier doesn't spin down."""
    if not RENDER_EXTERNAL_URL:
        return
    try:
        requests.get(f"{RENDER_EXTERNAL_URL}/", timeout=5)
    except Exception:
        pass


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram 未設定，略過通知")
        return
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            api_url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            print(f"[WARN] Telegram API 錯誤：{resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[WARN] 發送 Telegram 通知失敗：{e}")


async def check_page(browser) -> tuple[list[dict], list[str]]:
    """
    Load the ticket page and extract per-zone availability.
    Reuses the provided browser instance (cheaper than launching a new one each round).
    Returns (zones, debug_api_urls).
    Each zone: {name, price, status, available}
    """
    api_urls: list[str] = []

    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
    )
    page = await context.new_page()

    async def on_response(response):
        ct = response.headers.get("content-type", "")
        if "json" in ct:
            try:
                body = await response.text()
                if "售完" in body:
                    api_urls.append(response.url)
            except Exception:
                pass

    page.on("response", on_response)

    try:
        await page.goto(TARGET_URL, wait_until="networkidle", timeout=30000)
    except Exception:
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(4)

    raw = await page.evaluate(_JS_EXTRACT)
    await context.close()

    zones = [
        {
            "name": z["name"],
            "price": z["price"],
            "status": z["status"],
            "available": z["status"] != "售完",
        }
        for z in raw
        if z.get("name") and len(z["name"]) >= 2
    ]
    return zones, api_urls


async def main() -> None:
    filter_label = "、".join(WATCH_ZONES) if WATCH_ZONES else "全部"
    print(f"[監控啟動] {EVENT_NAME}")
    print(f"[目標 URL] {TARGET_URL}")
    print(f"[篩選票區] {filter_label}")
    print(f"[檢查間隔] 每 {CHECK_INTERVAL} 秒")
    print("-" * 60)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[警告] 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，不會發送通知")
        print("-" * 60)

    send_telegram(
        f"✅ <b>票券監控已啟動</b>\n\n"
        f"場次：{EVENT_NAME}\n"
        f"篩選票區：{filter_label}\n"
        f"檢查間隔：每 {CHECK_INTERVAL} 秒\n\n"
        f"🔗 {TARGET_URL}"
    )

    zone_status: dict[str, bool | None] = {}
    api_logged = False
    last_ping_time = 0.0
    PING_INTERVAL = 600  # self-ping every 10 minutes to prevent Render sleep

    launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=launch_args)

        while True:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                if not browser.is_connected():
                    browser = await p.chromium.launch(headless=True, args=launch_args)

                # Self-ping to keep Render free tier awake
                if time.time() - last_ping_time >= PING_INTERVAL:
                    self_ping()
                    last_ping_time = time.time()

                zones, api_urls = await check_page(browser)

                if api_urls and not api_logged:
                    api_logged = True
                    for u in api_urls:
                        print(f"[{now}] [DEBUG] 票券 API：{u}")

                if not zones:
                    print(f"[{now}] [WARN] 未能解析票區資料，頁面可能尚未載入完成")
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                _status["last_check"] = now
                _status["zones"] = len(zones)

                newly_available: list[dict] = []
                already_available: list[dict] = []

                for zone in zones:
                    name = zone["name"]
                    if WATCH_ZONES and not any(w in name for w in WATCH_ZONES):
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
                        f"<b>{EVENT_NAME}</b>\n\n"
                        f"{lines}\n\n"
                        f"🔗 <a href='{TARGET_URL}'>立即前往購票</a>"
                    )

                if newly_available:
                    lines = "\n".join(
                        f"• {z['name']}（NT${z['price']}，剩餘：{z['status']}）"
                        for z in newly_available
                    )
                    send_telegram(
                        f"🎫 <b>票券釋出通知！</b>\n\n"
                        f"<b>{EVENT_NAME}</b>\n\n"
                        f"以下票區有票可購買：\n{lines}\n\n"
                        f"🔗 <a href='{TARGET_URL}'>立即前往購票</a>\n\n"
                        f"⏰ 偵測時間：{now}"
                    )
                    print(f"[{now}] Telegram 通知已發送（{len(newly_available)} 個票區）")

            except Exception as e:
                print(f"[{now}] [ERROR] {e}")

            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    # Start health endpoint in background thread (required by Render web service)
    threading.Thread(target=start_web_server, daemon=True).start()
    asyncio.run(main())
