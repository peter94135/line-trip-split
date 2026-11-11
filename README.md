# 旅伴分帳

在 LINE 群組裡記帳、拍收據、看結算的旅遊分帳機器人，搭配從 LINE 開啟的網頁（LIFF）查明細、改帳和匯出。資料存在你自己的主機上。

## 功能

- **LINE 群組記帳**：傳「記 晚餐 3000」就記一筆，也能指定付款人和分攤對象、記外幣、一次記多筆、記退款、還款與公費。
- **AI 記帳**：用 Gemini 讀收據照片和複雜句子，先回確認卡片，按「好」才入帳。
- **結算**：算出最少的轉帳筆數，結算卡片可按「我已付」或「已收到」，直接記成還款。
- **網頁（LIFF）**：AI 記帳、手動記帳、明細搜尋與篩選、統計、結算、成員與設定、CSV 匯出。旅伴從 LINE 點邀請連結就能加入，不需要密碼。
- **分攤方式**：可平分、依權重分（例如小孩算 0.5 份），或指定每人金額。也支援固定匯率和每日牌告匯率。
- **管理頁**：查看所有帳本、操作紀錄、LINE 事件、推播額度與 Gemini 用量，也能還原已刪除的帳本。

群組裡可用的指令：

| 說法 | 結果 |
|---|---|
| `開帳本 京都 11/20-11/25 日幣` | 建立帳本、綁定群組，回覆邀請連結 |
| `加入` | 加入這個群組的帳本 |
| `記 晚餐 3000` | 傳訊息的人付款，全員平分 |
| `記 小明付 計程車 800 我和小華` | 指定付款人和分攤對象 |
| `記` 接著傳照片，或傳照片後說 `記 這張` | 讀收據（需要 Gemini） |
| `已還 小明 1200`、`交公費 小明 5000` | 記還款或公費 |
| `記 押金退回 5000 小明收` | 記退款 |
| `帳本`、`成員`、`明細`、`結算`、`我欠多少`、`統計`、`我的統計` | 查詢 |
| `結束帳本` | 結束並回覆結算 |
| `說明` | 顯示用法 |

## 架構

```
LINE 群組 ──Webhook──▶ https://<你的網址>/line/webhook ─┐
LINE 內開網頁 ────────▶ https://<你的網址>/liff/        ├─▶ trip-split 容器（FastAPI + SQLite）
                                                        │     ├─▶ LINE Messaging API
                                                        │     ├─▶ Gemini API（選用）
                                                        │     └─▶ open.er-api.com（匯率）
                         Tailscale Funnel（ts 容器）◀───┘
```

- Python 3.12、FastAPI、SQLModel（SQLite WAL）、APScheduler，前端是原生 HTML/CSS/JS，不需要建置步驟。
- 只能跑**單一** uvicorn worker，因為排程和處理順序的鎖都放在記憶體。
- 預設用 Tailscale Funnel 取得公開 HTTPS 網址，不必開放路由器的連接埠，也不必申請網域。

## 自行架設

### 需要準備

| 項目 | 說明 |
|---|---|
| 一台常開的電腦 | 裝 Docker（macOS 可用 OrbStack 或 Docker Desktop）。機器關機時，機器人就不會回應。 |
| Tailscale 帳號 | 免費方案即可，用來開 Funnel。也可改用自己的 HTTPS 反向代理，見[不用 Tailscale](#不用-tailscale)。 |
| LINE 官方帳號 | 在 LINE Developers 建立 Messaging API channel。 |
| LINE Login channel | 和官方帳號放在**同一個 provider**，在裡面建立 LIFF app。 |
| Gemini API key（選用） | 到 [Google AI Studio](https://aistudio.google.com/apikey) 申請。沒有 key 時只能用快速格式記帳，無法讀收據。 |

### 1. 下載並建立設定檔

```sh
git clone https://github.com/peter94135/line-trip-split.git
cd line-trip-split
cp .env.example .env
```

在 `.env` 填入 `ADMIN_PASSWORD`。其餘欄位在後面的步驟陸續補上。

### 2. 開通 Tailscale Funnel

1. 在 [Tailscale 管理介面](https://login.tailscale.com/admin/dns)開啟 MagicDNS 和 HTTPS Certificates。
2. 在 Access controls 的政策加入 Funnel 權限：

   ```json
   "nodeAttrs": [
     { "target": ["autogroup:member"], "attr": ["funnel"] }
   ]
   ```

3. 啟動服務：

   ```sh
   docker compose up -d --build
   docker compose logs -f ts
   ```

   如果沒有填 `TS_AUTHKEY`，log 會印出一個登入網址，用瀏覽器打開並登入。這個網址 60 秒後就會換新，要用最新的那個。登入狀態存在 `tailscale-state` volume，之後重啟不用再登入。

4. 確認公開網址，並把它填進 `.env` 的 `PUBLIC_BASE_URL`：

   ```sh
   docker compose exec ts tailscale funnel status
   curl -fsS https://trip-split.<tailnet>.ts.net/healthz
   ```

   看到 `{"ok":true}` 就代表公開網址已經通了。主機名稱預設是 `trip-split`，要改的話，修改 `docker-compose.yml` 的 `TS_HOSTNAME`。名稱裡不能有 `--`，否則 LINE 會拒絕這個 Webhook URL。

### 3. 建立 LINE 官方帳號（Messaging API）

1. 到 [LINE Developers Console](https://developers.line.biz/console/) 建立 provider，然後建立 Messaging API channel。
2. 在 **Basic settings** 複製 Channel secret，填入 `LINE_CHANNEL_SECRET`。
3. 在 **Messaging API** 分頁做以下設定：
   - 產生 Channel access token（long-lived），填入 `LINE_CHANNEL_ACCESS_TOKEN`。
   - Webhook URL 填 `https://<你的網址>/line/webhook`，並開啟 **Use webhook** 和 **Webhook redelivery**。
   - 開啟 **Allow bot to join group chats**。
   - 關閉自動回應訊息。加入好友的歡迎訊息可依喜好決定要不要關。

### 4. 建立 LINE Login channel 與 LIFF

1. 在**同一個 provider** 底下建立 LINE Login channel。
2. 在 **Basic settings** 複製 Channel ID，填入 `LINE_LOGIN_CHANNEL_ID`。
3. 在 **LIFF** 分頁新增 LIFF app，然後把它的 LIFF ID（格式像 `1234567890-AbCdEfGh`）填入 `LIFF_ID`。設定如下：
   - Size：Full
   - Endpoint URL：`https://<你的網址>/liff/`
   - Scopes：`openid`、`profile`
4. 把 channel 狀態從 Developing 改成 **Published**，朋友才能登入。Developing 狀態下只有 channel 管理員和測試者能登入。注意：改成 Published 之後就無法改回。

### 5. 套用設定並驗證

```sh
docker compose up -d
```

1. 在 LINE Developers 的 Webhook 設定按 **Verify**，應該會回 Success。
2. 用手機加官方帳號好友，私訊「我的ID」，把回覆的 userId 填入 `LINE_ADMIN_USER_ID`，再執行一次 `docker compose up -d`。這個帳號可以管理所有帳本。
3. 把官方帳號拉進 LINE 群組，傳「開帳本 測試」，再點回覆裡的邀請連結開啟網頁。
4. 管理頁在 `https://<你的網址>/admin`，用 `ADMIN_PASSWORD` 登入。

想讓管理頁顯示版本號，部署時改用這個指令：

```sh
GIT_COMMIT=$(git describe --always --dirty) docker compose up -d --build trip-split
```

### 開機自動恢復

- 兩個容器的重啟政策都是 `unless-stopped`。把 Docker 或 OrbStack 設成登入時自動啟動，主機重開機後服務就會自己恢復。
- 如果只有 `ts` 容器重啟，應用程式會在約 60 秒內偵測到並自行重啟，重新接回網路（見 `app/services/watchdog.py`）。
- LINE 會重送停機期間的事件，應用程式也會補處理收件匣裡還沒處理的事件。

### 不用 Tailscale

如果你已經有自己的網域和 HTTPS 反向代理（例如 Caddy、nginx 或 Cloudflare Tunnel），可以這樣改：

1. 編輯 `docker-compose.yml`，刪除 `ts` 服務和 `tailscale-state` volume。
2. 在 `trip-split` 服務刪除 `network_mode` 和 `depends_on`，加上 `ports: ["127.0.0.1:8082:8000"]`，並把 `TS_HEALTH_URL` 設成空字串。沒有清空的話，watchdog 偵測不到 sidecar，會一直重啟應用程式。
3. 讓反向代理把 `https://<你的網域>` 轉到 `http://127.0.0.1:8082`。

## 費用與限制

- **LINE 推播額度**：免費方案每月 200 則。群組推播按群組人數計算，例如 5 人群組推一次就算 5 則。群組裡的對話回覆不計入額度。會用到推播額度的情況有兩種：
  - 回覆權杖過期後改用推播補送。
  - 在網頁改帳時通知群組。人多的群組可以在帳本設定關閉這個通知。
- **Gemini**：用量和估算費用會顯示在管理頁。模型忙碌或觸發 429 時，會改用 `GEMINI_FALLBACK_MODELS` 列出的備援模型。
- **一般帳號的 LINE 限制**：一般官方帳號拿不到群組的完整成員名單，所以每位旅伴要傳「加入」、被 @提及，或點邀請連結才會成為成員。
- **管理員的權限**：架設者用管理頁可以看到所有帳本的明細。讓朋友使用前，請先告訴他們這一點。

## 備份與更新

先停止服務再備份 `data/`，這樣 SQLite 的 WAL 才會寫回主檔：

```sh
docker compose stop trip-split
tar czf backups/trip-split_$(date +%F_%H%M%S).tar.gz data
docker compose start trip-split
```

更新到新版本：

```sh
git pull
docker compose up -d --build trip-split
```

資料庫結構會在啟動時自動升級。升級前建議先備份。

## 開發

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

- 修改 `web/` 時，要一起遞增 `web/sw.js` 的 `CACHE`，以及 HTML 和 import 網址上的 `?v=`，否則手機會繼續使用舊快取。
- 設計細節記錄在 [CLAUDE.md](CLAUDE.md) 和兩份計劃書裡。

## 致謝

匯率資料來自 [Rates By Exchange Rate API](https://www.exchangerate-api.com)。

## 授權

[MIT](LICENSE)
