# 旅伴分帳

以 [旅伴分帳-獨立版計劃書.md](旅伴分帳-獨立版計劃書.md) 為實作規格，Phase 7–9 依 [旅伴分帳-功能擴充計劃書.md](旅伴分帳-功能擴充計劃書.md) 與使用者確認的規格實作。部署者自己的網址、LIFF ID 等資訊放在不進版控的 `CLAUDE.local.md`。本文件以 `<PUBLIC_BASE_URL>` 代表對外網址，例如 `https://trip-split.<tailnet>.ts.net`。自行架設的步驟見 [README.md](README.md)。

## Phase 0

目前提供 `GET /healthz`，回傳 `{"ok": true}`。使用 Python 3.12、FastAPI 和單一 Uvicorn worker。Compose 專案名為 `trip-split`，採計劃書方案 A。應用程式共用 Tailscale sidecar 的網路，主機連接埠為 `127.0.0.1:8082`，公開 Funnel 走 sidecar 的 HTTPS 443。

1. 從 `.env.example` 建立 `.env`。可在 Tailscale 管理介面產生一把無 tag、非 reusable、非 ephemeral 的 auth key，填入 `TS_AUTHKEY`；也可留空改用互動登入。Funnel 需要 tailnet 啟用 MagicDNS、HTTPS，以及授予該節點 Funnel 權限的政策。
2. 執行 `docker compose up -d --build`，再執行 `curl -fsS http://127.0.0.1:8082/healthz`。
3. 用 `docker compose exec ts tailscale funnel status` 確認公開網址，填入 `.env` 的 `PUBLIC_BASE_URL`。
4. 用手機關閉 Wi-Fi 和 Tailscale，透過行動網路開啟公開網址的 `/healthz`，確認看到 `{"ok":true}`。

Tailscale 登入狀態保存在 `tailscale-state` volume。首次登入成功後可清空 `.env` 的 `TS_AUTHKEY`。保留該 volume 讓重啟後沿用同一個節點。

### 注意事項

- 不填 `TS_AUTHKEY` 時，`docker compose logs ts` 會印出登入網址。containerboot 只等 60 秒，逾時會重啟並換新網址，要用最新那個。
- 應用程式共用 `ts` 的網路命名空間。若只有 `ts` 被 Docker 重啟，應用程式會留在舊的命名空間，Funnel 回 502。`ts` 在共用網路的 `127.0.0.1:9002/healthz` 提供健康檢查（`TS_ENABLE_HEALTH_CHECK`），`app/services/watchdog.py` 每 15 秒檢查一次，連續 4 次連線被拒就結束程序，由 `unless-stopped` 重啟並加入新的命名空間。`ts` 有回應但狀態不佳時不會觸發。

## Phase 1

- 應用程式啟動時初始化 `data/trip.db`，開啟 SQLite WAL 和外鍵檢查。Compose 把 `./data` 掛載到 `/app/data`。時間戳記讀出時保留 UTC 時區。SQLite 的部分唯一索引限制每個 LINE 群組同時只有一本進行中的帳本。
- `app/services/split.py` 的 `build_shares()` 保存每筆支出自己的分攤設定，成員權重或狀態變動時，舊帳仍用原來的分攤。`convert()` 優先使用固定匯率，再以 TWD 為基準交叉換算。支出或還款缺匯率時，`settlement()` 另列待補紀錄。
- 結算會四捨五入到結算幣別的最小單位，捨入差額優先歸給有參與分攤的付款人。付款人未參與分攤，或修正後會出現負分攤時，將差額分配給其他分攤者，保持合計一致且沒有負分攤。計入已還款後，用最多個可各自結清的成員組合，求得全帳本最少的轉帳筆數。
- `app/services/fx.py` 每天快取牌告匯率，刷新失敗時沿用上次成功的結果。使用匯率的介面要顯示來源。[Rates By Exchange Rate API](https://www.exchangerate-api.com)

## Phase 2

- LINE Webhook 為 `/line/webhook`。原始請求通過 LINE 簽章驗證後，按 `webhookEventId` 去重並存入 SQLite，再回 HTTP 200。後台依同一群組或私訊的入庫順序處理；重啟時補處理待辦，停機多久都照樣補做，回覆權杖過期就改用推播；處理中斷的事件標為待核對。成功事件的原始內容會清掉。
- 群組支援開帳本、加入、文字記帳、指定付款人、分攤對象或金額、外幣、多筆、還款、成員、明細及結算。建立者或管理員可結束帳本，群組內的編輯按鈕僅接受該帳本成員操作。`我的ID` 可私訊查詢。快速編輯支援改分攤、付款人、類別及刪除／復原。「已記」和每次修改後都回同一張 Flex 卡片（`line_edit.expense_card()`），顯示最新內容，並固定附上改分攤、改付款人、改類別、刪除四個按鈕。刪除後的「復原」也是固定按鈕。成員和類別的選單仍用 Quick Reply。面向使用者一律稱「類別」，避免和「分攤」混淆；資料值「待分類」不變。收據照片與複雜句子由 Phase 3 擴充。
- 服務定時補匯率、掃描 LINE 收件匣並重試待補送的回覆。回覆優先使用 reply token，確定未受理時才排補送。所有推播重試共用原本的 retry key。平常的群組聊天不進收件匣。
- 已綁定 LINE 群組的帳本會在網頁新增、編輯或刪除支出與退款，以及新增或刪除還款時，推播卡片到該群組。支出與退款沿用 LINE 記帳卡片，刪除後可按「復原」。批次存檔一次推播多張卡片。通知和帳目一起提交，由既有佇列負責送出與補送。推播會依群組人數用掉每月 200 則的額度，所以帳本設定有「網頁記帳或修改時通知 LINE 群組」開關（`Trip.notify_web_changes`，預設開啟），由建立者或管理員切換並記入操作紀錄。
- 將 LINE 官方帳號的 `LINE_CHANNEL_SECRET` 和 `LINE_CHANNEL_ACCESS_TOKEN` 放在本機 `.env`。Messaging API 的 Webhook URL 為 `<PUBLIC_BASE_URL>/line/webhook`，需開啟 Use webhook、Webhook redelivery、允許加入群組，並關閉內建自動回覆。
- 容器測試包含簽章、群組分帳手算、重送去重、群組權限、編輯、還款以及重啟補處理。群組實測從加入官方帳號、建立測試帳本和逐位傳「加入」開始。

## Phase 3

- 單筆快速格式直接記帳。複雜句子、多筆文字與收據由 Gemini 回傳 `TripParse`，再以帳本的成員、分攤規則和日期驗證。Gemini 暫時無法使用時，能確定解析的多筆文字仍可在本機產生草稿。
- 確認卡片是一般文字訊息，列出日期、原幣金額、類別、付款人與分攤對象，不附操作說明。下方的 Quick Reply 有「好」「取消」「改成」。「改成」會打開鍵盤並預填「改成 」，需要 LINE 12.6 以上。發起者回「好」才會將整批支出寫入同一筆交易。「取消」清除草稿，「改成 …」會產生新卡片。草稿以群組、使用者和帳本隔離，15 分鐘後失效。新版卡片也會讓舊按鈕失效。
- 群組請先傳「記」，2 分鐘內傳收據，或在傳照片後 5 分鐘內說「記 這張」。只有符合時間窗的本人照片會下載。收據上限 10 MB，單張預設取實付總額；每批最多五筆。
- Gemini 共用一個 client，先試主要模型，遇到忙碌、逾時、模型不可用或 429 時再試備援。429 冷卻存在 SQLite，重啟後仍有效。模型不支援指定 thinking level 時，會移除該設定重試。`GeminiCall` 紀錄 token 與牌價估算。Compose 從本機 `.env` 讀取 Gemini 設定。
- 驗證過計劃書的兩則文字範例與三張合成圖（日文收據、台灣發票和手寫風格帳單）。LINE 草稿與確認流程透過簽章 webhook 模擬驗證。

## Phase 4

- 網頁在 `/liff/`，管理頁在 `/admin`，API 在 `/api/`。LIFF 取得 ID token 後，後端向 LINE 驗證並發 12 小時的 JWT。管理員用 `ADMIN_PASSWORD` 登入，取得 1 小時的 JWT，每分鐘最多 8 次失敗。`APP_SECRET` 留空時，簽章金鑰自動產生在 `data/.app_secret`。
- 只有帳本成員能讀寫，其他人呼叫 API 會收到 403。邀請連結為 `https://liff.line.me/<LIFF_ID>?join=<invite_code>`。網頁寫入用 `BEGIN IMMEDIATE`，和 LINE 共用 SQLite 寫入鎖，所以網頁改的帳，LINE「結算」會立刻反映。
- 頁面包括帳本列表、總覽與結算、還款、明細與編輯表單（權重或指定金額）、成員、設定和 CSV 匯出（UTF-8 BOM，防公式注入）。設定、結束帳本、新增或停用成員限建立者或 `LINE_ADMIN_USER_ID`。已有紀錄時，結算幣別會鎖定。
- 外部瀏覽器的 ID token 過期時，網頁會先登出 LIFF 再請使用者重新登入，避免一直帶著舊 token 失敗。
- LINE Developers 需設定：LINE Login channel 的 LIFF endpoint 為 `<PUBLIC_BASE_URL>/liff/`，scope 包含 `openid` 與 `profile`。channel 要設為 Published，否則只有 channel 管理員和測試者能登入。Published 之後無法改回 Developing。
- 修改 `web/` 時，要一起遞增 `sw.js` 的 `CACHE`，以及 HTML 和 import 的 `?v=`。
- 配色使用 Claude 主題色，都是 `style.css` 的 `:root` 變數。淺色底 `#faf9f5`，按鈕 `#b85a3b` 配白字；深色底 `#141413`，主色 `#d97757` 配深色字。HTML 的 `theme-color` 要跟著改。大頭貼與網頁 logo 的原始檔是 `assets/line-oa-avatar.svg`（`#d97757` 底），用 `rsvg-convert` 輸出 PNG，再複製成 `web/assets/logo.svg` 與 180px 的 `apple-touch-icon.png`。
- 用模擬 LIFF 在本機瀏覽器操作過以下流程：新增指定金額支出、編輯、還款、固定匯率設定、非成員被擋、邀請連結加入及管理頁。

## Phase 5

- 正式服務的 LINE Webhook 使用 `/line/webhook`，LIFF Endpoint 使用 `/liff/`，管理頁位於 `/admin`，三者都掛在 `<PUBLIC_BASE_URL>` 下。
- 主機的容器執行環境（例如 OrbStack 或 Docker Desktop）要設為登入時啟動，兩個容器的重啟政策是 `unless-stopped`。Tailscale 使用 `trip-split_tailscale-state` volume，容器停啟後維持同一節點。實測只重啟 `ts` 容器時，約 60 秒後應用程式自行重啟，公開網址隨即恢復，不需手動操作。
- 停止此 Compose 專案後備份 `data/`，備份完成後再啟動。備份檔放在 `backups/`（不進版控），還原前可在隔離容器解開並跑 SQLite 完整性和外鍵檢查。

## Phase 6

待實際旅行使用。若 LINE 對某句指令的理解與預期不同，記下原句、機器人回覆和預期結果，之後以該句補進解析器或提示詞的回歸測試。

- 帳本建立者或有加入帳本的管理員，可在已結束帳本的「設定」刪除帳本。刪除後，LINE、LIFF、邀請與匯出連結及管理頁不再顯示帳本；SQLite 仍保留帳目。
- 已結束的帳本可由相同權限的成員在「設定」重新開啟。原明細、成員與邀請連結保留。同一個 LINE 群組已有進行中帳本時，須先結束該帳本。
- 可在「成員」刪除未曾付款、分攤或還款的成員；建立者和有歷史紀錄的成員會保留。進行中的帳本可從成員編輯取消「參與新支出的分攤」。
- 實戰時曾有群組成員按到舊卡片的「刪除」，因為事件內容已清除、操作紀錄沒輸出，查不出是誰。現在 LINE 刪除要先按「確定刪除」，`Expense.deleted_by` 記錄 LINE 或網頁的刪除者，回覆顯示「某某 已刪除」，`app.*` 的 INFO 紀錄會輸出到 `docker compose logs`。

## Phase 7

- 統計、明細與結餘共用結算已捨入的分攤及還款金額。統計依類別、日期、成員及登入者分攤呈現。缺匯率的支出另列筆數。天數以設定的旅程起訖日為準，缺少的日期以可換算支出的最早或最晚日期補齊。
- LINE「統計」列出最多五個類別，「我的統計」只列傳訊者的分擔。「我欠多少」附總覽連結，「結束帳本」在原回覆附統計連結。
- 用測試帳本核對結算、統計與 LINE 金額，並在 375px 的 Chrome 淺色與深色預覽驗證統計、類別跳轉及結餘明細。

## Phase 8

- LINE 結算 Flex 卡片列出結餘與最多六筆轉帳建議。付款人可按「我已付」，收款人可按「已收到」。LIFF 的「標記已付」限付款人或帳本建立者。已結束帳本仍可確認當前建議。SQLite 在同一筆交易驗證版本、建議與操作者，並存入還款與 `created_by`。
- 成員可編輯自己的收款平台與最多四位末碼。收款備註顯示於 LIFF 和 LINE 群組結算卡片，CSV 不匯出。明細支援品項或商家、類別、付款人、與我有關四種篩選，條件保存在 URL hash，每列顯示自己的分擔。
- 簽章 webhook 模擬通過權限、版本更新和重送測試。合成卡片通過 LINE 官方格式驗證。

## Phase 9

- 退款金額以正數入庫，結算及統計扣除退款。收款人記在 `payer_id`，CSV 以「退款」類型及負金額輸出。現有紀錄升級時預設為「支出」。LINE 快速格式和 Gemini 草稿均支援退款。
- 「交公費 小明 5000」使用還款資料表，LIFF 可從「還款／交公費」記錄。總覽可切換轉帳建議的顯示幣別，優先使用固定匯率，否則顯示牌告匯率來源。標記已付仍以帳本結算幣別入帳。
- 測試包含 SQLite 升級、住宿退款與公費多退少補。Chrome 淺色與深色預覽均驗證了退款與幣別切換。Gemini 以合成句子實際驗證退款欄位。
- LIFF 以分色比例條及色標顯示類別占比，明細沿用相同類別色。進行中的帳本各分頁上方提供「手動記帳」。LINE 群組開帳本或查詢進行中的帳本時會回覆邀請連結，旅伴經 LINE 登入後自動加入。

## 版面改版

- LIFF 底部五個分頁：手動記帳｜明細｜AI 記帳（中間、開帳本的首頁）｜結算｜帳本。從 LINE 打開一律從 AI 記帳開始，重新整理則留在網址上的分頁；已結束的帳本停用手動與 AI 記帳，改從結算開始。舊網址的 `overview`、`stats`、`members`、`settings` 會對到新分頁。
- AI 記帳：`POST /api/trips/{id}/ai/parse`（文字）和 `/ai/photo`（照片原始位元組，前端先縮到長邊 1600px）只產生草稿，沿用 LINE 的 `parse_quick()`、Gemini 與 `line_draft.prepare()`；確認後由 `/expenses/bulk` 在同一筆交易入帳，來源記為 `web`、`web_ai` 或 `web_photo`。
- 手動記帳是整頁表單，可切換支出、退款、還款。明細把支出與還款依日期合併，篩選收在「搜尋與篩選」。結算分頁內可切換結算與統計。帳本分頁合併成員、邀請和設定。
- `AuditLog` 記錄修改與刪除支出、改分攤／付款人／類別、標記已付、刪除還款、停用或刪除成員、結束／重開／刪除／還原帳本。管理頁列出帳本摘要（含已刪除），點開可看成員、結算、類別、含已刪除的明細、還款與操作紀錄，並可還原已刪除的帳本；另有 LINE 近 24 小時事件、推播額度、Gemini 模型冷卻與每日次數、系統狀態。
- 部署時帶入版本，管理頁才看得到：`GIT_COMMIT=$(git describe --always --dirty) docker compose up -d --build trip-split`。

## 開發驗證

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
docker compose config --quiet
```

每個 Phase 完成後，跑測試、更新此文件，等使用者確認再 commit。使用繁體中文撰寫面向使用者的內容。公開文件不寫實際網址、LIFF ID、channel 名稱或備份檔名，這些放在 `CLAUDE.local.md`。
