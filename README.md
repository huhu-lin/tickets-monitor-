# tickets-monitor-

監控 fami.life 票券是否從售完狀態釋出（退票/清票），有票時立即透過 Telegram 發送通知。

## 功能

- 每 1 分鐘自動檢查所有票區狀態
- 偵測「售完 → 有票」時立即發送 Telegram 通知，包含票區名稱與剩餘張數
- 使用 Playwright 無頭瀏覽器，應對 JS 渲染頁面與防爬機制
- 支援 `WATCH_ZONES` 篩選特定票區（如只看「B1層」或「外野」）

---

## 部署到 Render（推薦，電腦可關機）

### 1. 在 Render 建立 Background Worker

1. 前往 [render.com](https://render.com) → **New → Background Worker**
2. 連結此 GitHub 倉庫
3. Runtime 選 **Docker**（Render 會自動偵測 `Dockerfile`）
4. Plan 選 **Starter**（$7 美元/月，Background Worker 需要付費方案）

### 2. 設定環境變數

在 Render 的 **Environment** 頁面新增以下變數：

| 變數 | 值 |
|------|-----|
| `TELEGRAM_BOT_TOKEN` | 你的 Bot Token |
| `TELEGRAM_CHAT_ID` | 你的 Chat ID |
| `TARGET_URL` | `https://guardians.fami.life/UTK0204_?PERFORMANCE_ID=P19LRRQA&PRODUCT_ID=P15UU08Q` |
| `EVENT_NAME` | `Guardians UTK0204` |
| `WATCH_ZONES` | 留空（全部）或填入如 `B1層,外野` |
| `CHECK_INTERVAL` | `60` |

### 3. 部署

點 **Deploy** 即可，之後每次 push 到 GitHub 會自動重新部署。

---

## 本機執行

```bash
# 建立虛擬環境
python3 -m venv venv
source venv/bin/activate

# 安裝依賴
pip install -r requirements.txt
playwright install chromium

# 設定環境變數
cp .env.example .env
# 編輯 .env 填入 Token 和 Chat ID

# 執行
python monitor.py
```

---

## WATCH_ZONES 篩選說明

填入逗號分隔的關鍵字，對應頁面上的分類標籤，不填則監控全部票區：

```
# 只監控 B1層 和 外野
WATCH_ZONES=B1層,外野

# 只監控搖滾熱力區
WATCH_ZONES=搖滾

# 監控全部（預設）
WATCH_ZONES=
```

可用關鍵字：**內野、B1層、熱力區、熱區、一般區、L2層、L4層、L5層、外野、輪椅席**

---

## 如何取得 Telegram Bot Token 和 Chat ID

### Bot Token
1. Telegram 搜尋 `@BotFather` → 發送 `/newbot` → 依指示建立
2. 取得格式如 `123456789:ABCdefGhIjKlMnOpQrStUvWxYz` 的 Token

### Chat ID
1. 對你的 bot 發送任意訊息
2. 開啟 `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. 找 `"chat"` → `"id"` 的數值

---

## Console 輸出範例

```
[監控啟動] Guardians UTK0204
[目標 URL] https://guardians.fami.life/UTK0204_?PERFORMANCE_ID=P19LRRQA&PRODUCT_ID=P15UU08Q
[篩選票區] 全部
[檢查間隔] 每 60 秒
------------------------------------------------------------
[2026-05-28 12:00:00] B1_108搖滾熱力區 ｜ NT$2500 ｜ ❌ 售完
[2026-05-28 12:00:00] B1_108應援熱力區 ｜ NT$2200 ｜ ❌ 售完
...
[2026-05-28 12:30:00] B1_108搖滾熱力區 ｜ NT$2500 ｜ ✅ 有票（剩 3）  ← 發送通知
```
