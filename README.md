# tickets-monitor-

監控 fami.life 票券是否從售完狀態釋出（退票/清票），有票時立即透過 Telegram 發送通知。

## 功能

- 每 1 分鐘自動檢查票券狀態
- 偵測「售完 → 有票」時立即發送 Telegram 通知
- 使用 Playwright 無頭瀏覽器，應對 JS 渲染頁面與防爬機制
- 啟動時發送確認通知，確保 Telegram 設定正確

---

## 安裝

### 1. 安裝 Python 依賴

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. 設定環境變數

```bash
cp .env.example .env
```

編輯 `.env`，填入 Telegram 資訊，並設定要監控的票種：

```
TELEGRAM_BOT_TOKEN=你的 Bot Token
TELEGRAM_CHAT_ID=你的 Chat ID
CHECK_INTERVAL=60

TARGET_1_NAME=一樓內野區
TARGET_1_URL=https://guardians.fami.life/UTK0204_?PERFORMANCE_ID=P19LRRQA&PRODUCT_ID=P15UU08Q

TARGET_2_NAME=三樓外野區
TARGET_2_URL=https://guardians.fami.life/UTK0204_?PERFORMANCE_ID=P19LRRQA&PRODUCT_ID=xxxxxxxx
```

每個票種或區域用一組 `TARGET_N_NAME` + `TARGET_N_URL` 定義，數字從 1 開始連續編號，可新增任意數量。

---

## 如何取得 Telegram Bot Token 和 Chat ID

### 取得 Bot Token

1. 在 Telegram 搜尋 `@BotFather`
2. 發送 `/newbot`，依指示設定 bot 名稱
3. BotFather 會回傳一組 Token，格式如：`123456789:ABCdefGhIjKlMnOpQrStUvWxYz`

### 取得 Chat ID

1. 先對你的 bot 發送任意訊息（例如「hi」）
2. 在瀏覽器開啟以下網址（替換 `<TOKEN>`）：
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
3. 在回傳的 JSON 中找到 `"chat"` → `"id"` 的數值，即為 Chat ID

---

## 執行

```bash
python monitor.py
```

啟動後 console 會持續顯示每輪的檢查狀態：

```
[監控啟動] Guardians UTK0204
[目標 URL] https://guardians.fami.life/UTK0204_?PERFORMANCE_ID=P19LRRQA&PRODUCT_ID=P15UU08Q
[檢查間隔] 每 60 秒
------------------------------------------------------------
[2026-05-28 12:00:00] 狀態：❌ 售完
[2026-05-28 12:01:00] 狀態：❌ 售完
...
[2026-05-28 12:30:00] 狀態：✅ 有票   ← 此時發送 Telegram 通知
```

---

## 注意事項

- 請確保執行環境保持開機且網路連線正常
- 建議在背景執行：`nohup python monitor.py > monitor.log 2>&1 &`
- 調整 `.env` 中的 `CHECK_INTERVAL` 可改變檢查頻率（單位：秒）
