import asyncio
import os
import requests
from datetime import datetime
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

SOLD_OUT_KEYWORDS = ["售完", "已售完", "完售", "缺貨", "SOLD OUT", "Sold Out", "sold out"]
BUY_BUTTON_TEXTS = ["立即購票", "加入購物車", "購買", "購票", "Buy", "Add to Cart"]


def load_targets() -> list[dict]:
    """
    Load monitoring targets from env vars.
    Reads TARGET_1_NAME / TARGET_1_URL, TARGET_2_NAME / TARGET_2_URL, ...
    Falls back to the legacy single-target TARGET_URL / EVENT_NAME if present.
    """
    targets = []
    i = 1
    while True:
        name = os.getenv(f"TARGET_{i}_NAME")
        url = os.getenv(f"TARGET_{i}_URL")
        if not name or not url:
            break
        targets.append({"name": name, "url": url})
        i += 1

    # Legacy fallback
    if not targets:
        url = os.getenv("TARGET_URL", "https://guardians.fami.life/UTK0204_?PERFORMANCE_ID=P19LRRQA&PRODUCT_ID=P15UU08Q")
        name = os.getenv("EVENT_NAME", "Guardians UTK0204")
        targets.append({"name": name, "url": url})

    return targets


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


async def check_one(browser: Browser, target: dict, api_urls_logged: set) -> bool:
    """Check a single target URL. Returns True if tickets are available."""
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )
    page = await context.new_page()
    intercepted: list[str] = []

    async def on_response(response):
        ct = response.headers.get("content-type", "")
        if "json" in ct and any(
            k in response.url for k in ["ticket", "product", "seat", "stock", "avail", "remain"]
        ):
            intercepted.append(response.url)

    page.on("response", on_response)

    try:
        await page.goto(target["url"], wait_until="networkidle", timeout=30000)
    except Exception:
        await page.goto(target["url"], wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

    content = await page.content()

    has_sold_out_text = any(kw in content for kw in SOLD_OUT_KEYWORDS)

    active_buy_button = False
    for text in BUY_BUTTON_TEXTS:
        btn = await page.query_selector(f'button:has-text("{text}")')
        if btn and await btn.get_attribute("disabled") is None:
            active_buy_button = True
            break

    await context.close()

    # Log intercepted API URLs once per target
    key = target["name"]
    if intercepted and key not in api_urls_logged:
        api_urls_logged.add(key)
        print(f"  [DEBUG] {key} — 偵測到 API endpoints：")
        for u in intercepted:
            print(f"          {u}")

    return (not has_sold_out_text) and active_buy_button


async def main() -> None:
    targets = load_targets()

    print(f"[監控啟動] 共 {len(targets)} 個監控目標，每 {CHECK_INTERVAL} 秒檢查一次")
    for t in targets:
        print(f"  • {t['name']}")
        print(f"    {t['url']}")
    print("-" * 60)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[警告] 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，不會發送通知")
        print("       請複製 .env.example 為 .env 並填入對應值")
        print("-" * 60)

    target_lines = "\n".join(f"• {t['name']}" for t in targets)
    send_telegram(
        f"✅ <b>票券監控已啟動</b>\n\n"
        f"監控目標：\n{target_lines}\n\n"
        f"檢查間隔：每 {CHECK_INTERVAL} 秒"
    )

    last_available: dict[str, bool | None] = {t["name"]: None for t in targets}
    api_urls_logged: set[str] = set()

    async with async_playwright() as p:
        while True:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            browser = await p.chromium.launch(headless=True)

            try:
                for target in targets:
                    name = target["name"]
                    try:
                        is_available = await check_one(browser, target, api_urls_logged)
                        status_label = "✅ 有票" if is_available else "❌ 售完"
                        print(f"[{now}] {name}：{status_label}")

                        prev = last_available[name]
                        if prev is not None and not prev and is_available:
                            msg = (
                                f"🎫 <b>票券釋出通知！</b>\n\n"
                                f"<b>{name}</b> 有票可以購買了！\n\n"
                                f"🔗 <a href='{target['url']}'>立即前往購票</a>\n\n"
                                f"⏰ 偵測時間：{now}"
                            )
                            send_telegram(msg)
                            print(f"[{now}] Telegram 通知已發送（{name}）")

                        last_available[name] = is_available

                    except Exception as e:
                        print(f"[{now}] [ERROR] {name}：{e}")

            finally:
                await browser.close()

            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
