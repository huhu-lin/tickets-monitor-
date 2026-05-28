import asyncio
import os
import requests
from datetime import datetime
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

TARGET_URL = "https://guardians.fami.life/UTK0204_?PERFORMANCE_ID=P19LRRQA&PRODUCT_ID=P15UU08Q"
EVENT_NAME = "Guardians UTK0204"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

SOLD_OUT_KEYWORDS = ["售完", "已售完", "完售", "缺貨", "SOLD OUT", "Sold Out", "sold out"]
BUY_BUTTON_TEXTS = ["立即購票", "加入購物車", "購買", "購票", "Buy", "Add to Cart"]


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram 未設定，略過通知")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            print(f"[WARN] Telegram API 錯誤：{resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[WARN] 發送 Telegram 通知失敗：{e}")


async def check_availability() -> tuple[bool, list[str]]:
    """
    Returns (is_available, intercepted_api_urls).
    is_available = True means tickets are on sale.
    """
    intercepted_urls: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        async def on_response(response):
            ct = response.headers.get("content-type", "")
            if "json" in ct and any(
                k in response.url for k in ["ticket", "product", "seat", "stock", "avail", "remain"]
            ):
                intercepted_urls.append(response.url)

        page.on("response", on_response)

        try:
            await page.goto(TARGET_URL, wait_until="networkidle", timeout=30000)
        except Exception:
            # Fallback: wait for domcontentloaded if networkidle times out
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

        content = await page.content()

        # Detect sold-out
        has_sold_out_text = any(kw in content for kw in SOLD_OUT_KEYWORDS)

        # Detect active buy button
        active_buy_button = False
        for text in BUY_BUTTON_TEXTS:
            btn = await page.query_selector(f'button:has-text("{text}")')
            if btn:
                disabled = await btn.get_attribute("disabled")
                if disabled is None:
                    active_buy_button = True
                    break

        await browser.close()

    # Available = no sold-out text AND there's an active buy button
    is_available = (not has_sold_out_text) and active_buy_button
    return is_available, intercepted_urls


async def main() -> None:
    print(f"[監控啟動] {EVENT_NAME}")
    print(f"[目標 URL] {TARGET_URL}")
    print(f"[檢查間隔] 每 {CHECK_INTERVAL} 秒")
    print("-" * 60)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[警告] 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，不會發送通知")
        print("       請複製 .env.example 為 .env 並填入對應值")
        print("-" * 60)

    # Send a startup notification
    send_telegram(
        f"✅ <b>票券監控已啟動</b>\n\n"
        f"場次：{EVENT_NAME}\n"
        f"檢查間隔：每 {CHECK_INTERVAL} 秒\n\n"
        f"🔗 {TARGET_URL}"
    )

    last_available: bool | None = None
    api_urls_logged = False

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            is_available, api_urls = await check_availability()

            # Log intercepted API URLs once for debugging
            if api_urls and not api_urls_logged:
                print(f"[{now}] [DEBUG] 偵測到相關 API endpoints：")
                for u in api_urls:
                    print(f"         {u}")
                api_urls_logged = True

            status_label = "✅ 有票" if is_available else "❌ 售完"
            print(f"[{now}] 狀態：{status_label}")

            if last_available is not None and not last_available and is_available:
                # Sold-out → available transition
                msg = (
                    f"🎫 <b>票券釋出通知！</b>\n\n"
                    f"<b>{EVENT_NAME}</b> 場次有票可以購買了！\n\n"
                    f"🔗 <a href='{TARGET_URL}'>立即前往購票</a>\n\n"
                    f"⏰ 偵測時間：{now}"
                )
                send_telegram(msg)
                print(f"[{now}] Telegram 通知已發送！")

            last_available = is_available

        except Exception as e:
            print(f"[{now}] [ERROR] {e}")

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
