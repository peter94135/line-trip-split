"""以暫存帳本和模擬 LIFF 操作手機版五分頁，所有 HTTP 請求留在測試內。"""

from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def test_five_tabs(web):
    client, login, (trip_id, alice, bob) = web
    base = f"/api/trips/{trip_id}"
    headers = login("Bob")
    assert client.put(f"{base}/members/{bob}/payment-note", headers=headers,
                      json={"payment_note": "街口末碼 1234"}).status_code == 204
    sdk = """
      window.liff = {init: async () => {}, isLoggedIn: () => true,
        getDecodedIDToken: () => ({sub:'U-Alice'}), getIDToken: () => 'Alice',
        isApiAvailable: name => name === 'shareTargetPicker',
        shareTargetPicker: async messages => { window.shared = messages; return {status:'success'}; }};
    """
    def serve(route):
        url = urlsplit(route.request.url)
        if url.hostname == "static.line-scdn.net":
            route.fulfill(body=sdk, content_type="text/javascript")
        elif url.hostname == "trip.test":
            request = route.request
            response = client.request(request.method, url.path + ("?" + url.query if url.query else ""),
                                      headers=request.headers, content=request.post_data_buffer)
            route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
        else:
            route.abort()

    with sync_playwright() as pw:
        # CI 使用 playwright install chromium；開發機也可直接用既有 Chrome。
        channel = None if Path(pw.chromium.executable_path).exists() else "chrome"
        browser = pw.chromium.launch(channel=channel)
        page = browser.new_page(viewport={"width": 375, "height": 812}, service_workers="block")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", serve)
        page.goto(f"https://trip.test/liff/?trip={trip_id}")
        def tab(key):
            page.locator(f'#tabs [data-tab="{key}"]').click()
        expect(page.locator("#ai-text")).to_be_visible()
        page.locator("#ai-text").fill("早餐 100")
        page.get_by_role("button", name="解析").click()
        expect(page.get_by_text("待確認 1 筆")).to_be_visible()
        tab("manual")
        form = page.locator("#manual-form")
        form.locator('[name="amount"]').fill("300")
        form.locator('[name="item"]').fill("高鐵")
        expect(form.locator('[name="category"]')).to_have_value("交通")
        mode = form.locator('[name="split_mode"]')
        expect(mode).to_be_hidden()
        form.get_by_text("進階", exact=True).click()
        mode.select_option("weighted")
        weight = form.locator(f'[data-share="{alice}"] input')
        weight.fill("2")
        mode.select_option("exact")
        expect(weight).to_have_value("")
        mode.select_option("weighted")
        expect(weight).to_have_value("2")
        mode.select_option("even")
        form.get_by_text("進階", exact=True).click()
        form.locator('[name="payer_id"]').select_option(str(bob))
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(form.locator('[name="amount"]')).to_have_value("300")
        form.get_by_role("button", name="儲存").click()
        expect(form.locator('[name="amount"]')).to_have_value("")
        tab("expenses")
        expect(page.locator(".date-heading")).to_contain_text("第 1 天")
        expect(page.locator(".date-heading")).to_contain_text("當日小計 TWD 300")
        page.get_by_role("button", name="高鐵", exact=False).click()
        expect(page.locator("#dialog")).to_contain_text("Alice")
        expect(page.locator("#dialog")).to_contain_text("網頁")
        page.locator("#dialog [data-action=dismiss]").click()
        tab("settle")
        expect(page.get_by_text("你要付Bob")).to_be_visible()
        expect(page.get_by_text("收款方式 街口末碼 1234")).to_be_visible()
        page.get_by_role("tab", name="統計").click()
        expect(page.get_by_text("平均每天")).to_be_visible()
        tab("trip")
        expect(page.locator("select[name=timezone]")).to_have_value("Asia/Taipei")
        expect(page.locator("[data-action=delete-member]")).to_have_count(0)
        page.get_by_role("button", name="用 LINE 分享邀請").click()
        assert "join=AbCd1234" in page.evaluate("window.shared[0].text")
        page.get_by_role("button", name="新增匯率").click()
        row = page.locator(".rate-row").last
        row.locator("[data-code]").fill("USD")
        row.locator("[data-rate]").fill("31.5")
        page.get_by_role("button", name="儲存設定").click()
        expect(page.get_by_text("設定已儲存")).to_be_visible()
        assert client.get(base, headers=headers).json()["fixed_rates"] == {"JPY": .2, "USD": 31.5}
        tab("expenses")
        body = dict(kind="refund", item="車票退款", spent_on=client.get(base, headers=headers).json()["today"],
                    amount=100, currency="TWD", payer_id=bob, split_mode="equal",
                    shares=[{"member_id": alice}, {"member_id": bob}])
        assert client.post(base + "/expenses", headers=headers, json=body).status_code == 201
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(page.locator(".date-heading")).to_contain_text("當日小計 TWD 200")
        tab("settle")
        page.get_by_role("tab", name="結算", exact=True).click()
        expect(page.get_by_text("1 筆支出 · 1 筆退款")).to_be_visible()
        page.get_by_role("button", name="標記已付").click()
        page.get_by_role("button", name="確認已付").click()
        expect(page.get_by_text("你已結清。")).to_be_visible()
        assert not errors
        browser.close()
