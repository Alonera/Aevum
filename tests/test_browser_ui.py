import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright, expect
import ytdl_tray as app
from test_feedback import env, VALID


@pytest.fixture
def browser_ui(env, monkeypatch):
    client, store = env
    monkeypatch.setattr(app, "_IS_WINDOWS", False)
    # Never attempt to extract the user's real browser cookies in UI tests.
    def no_real_cookies(*args, **kwargs):
        raise app.CookieError('cookieAutoFailed')
    monkeypatch.setattr(app, '_run_cookie_export', no_real_cookies)
    monkeypatch.setattr(app, "_run_probe_json", lambda *a, **kw: {"title": "Local fixture", "formats": []})
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 1050})
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        def route_request(route):
            req = route.request
            parsed = urlsplit(req.url)
            if parsed.hostname != "localhost":
                route.abort()
                return
            if parsed.path == "/settings":
                route.fulfill(json={"canStartup": False, "canMenu": False, "version": "test", "pkgVersion": "test"})
                return
            if parsed.path.startswith(("/update", "/packages")):
                route.fulfill(json={})
                return
            reply = client.open(parsed.path + ("?" + parsed.query if parsed.query else ""),
                                method=req.method, data=req.post_data_buffer,
                                headers={k: v for k, v in req.headers.items() if k.lower() not in ("host", "content-length")},
                                base_url="http://localhost:5000")
            route.fulfill(status=reply.status_code, content_type=reply.content_type, body=reply.get_data())
        page.route("**/*", route_request)
        page.add_init_script("localStorage.setItem('aevum_onboarded','1')")
        page.goto("http://localhost:5000")
        page.wait_for_function("typeof cookieNative !== 'undefined'")
        yield page, store, errors
        browser.close()
        assert not errors


def upload(page, name="cookies.txt", content=VALID):
    page.locator('[data-g="cookies"][data-v="firefox"]').click()
    with page.expect_file_chooser() as event:
        page.locator("#cookieLoad").click()
    event.value.set_files({"name": name, "mimeType": "text/plain", "buffer": content})


def test_cookie_feedback_browser_switch_and_audio_ui(browser_ui):
    page, store, errors = browser_ui
    expect(page.locator("#cookieLoad")).to_be_disabled()
    upload(page)
    expect(page.locator("#cookieFeedback")).to_contain_text("Cookies loaded")
    token = page.evaluate("state.cookieToken")
    assert store.snapshot("firefox", token)
    page.locator('[data-g="cookies"][data-v="chrome"]').click()
    expect(page.locator("#cookieFeedback")).to_have_text("")
    assert page.evaluate("state.cookieToken") == ""
    upload(page)
    page.locator(".cookie-reset").click()
    expect(page.locator("#cookieFeedback")).to_have_text("")
    page.locator('[data-g="mode"][data-v="audio"]').click()
    page.locator('#arows [data-flag="thumb"]').click()
    assert page.evaluate("state.thumb") is True
    page.locator('[data-g="fmt"][data-v="original"]').click()
    expect(page.locator("#bitrateRow")).to_be_hidden()
    expect(page.locator("#audioHint")).to_contain_text("separate JPG")
    page.locator('[data-g="fmt"][data-v="mp3"]').click()
    expect(page.locator("#bitrateRow")).to_be_visible()
    expect(page.locator("#audioHint")).to_contain_text("encoding target")
    page.evaluate("applyLang('tr')")
    expect(page.locator("#cookieLoad")).to_have_attribute("title", "Tarayıcı çerezlerini al")
    upload(page)
    expect(page.locator("#cookieFeedback")).to_contain_text("Çerezler yüklendi")
    page.locator("#u").fill("https://example.test/song")
    Path("docs/qa").mkdir(parents=True, exist_ok=True)
    page.screenshot(path="docs/qa/audio-cookies.png")
    # The upload button stays after the four browser choices, within the row.
    chip = page.locator('[data-g="cookies"][data-v="brave"]').bounding_box()
    load = page.locator("#cookieLoad").bounding_box()
    assert abs(chip["y"] - load["y"]) < 2 and load["x"] > chip["x"]
    page.set_viewport_size({"width": 460, "height": 920})
    page.screenshot(path="docs/qa/audio-cookies-narrow.png")
    # The animated fixed background deliberately extends beyond the viewport.
    # Check the form/controls, not the background's scrollable paint area.
    assert page.evaluate("document.getElementById('root').getBoundingClientRect().right <= innerWidth")
    assert page.locator("#cookieLoad").bounding_box()["x"] < 460


def test_invalid_cookie_and_download_error_preserve_input(browser_ui):
    page, store, errors = browser_ui
    upload(page, "Cookies", b"SQLite format 3\0")
    expect(page.locator("#cookieFeedback")).to_contain_text("browser database")
    assert page.evaluate("state.cookieToken") == ""
    page.locator("#u").fill("https://example.test/video")
    page.evaluate("state.cookieToken='expired-session-token'")
    page.locator("#gb").click()
    expect(page.locator("#pt")).to_contain_text("no longer available")
    assert page.evaluate("state.cookieToken") == ""
    expect(page.locator("#u")).to_have_value("https://example.test/video")


def test_zen_chip_order_manual_upload_and_browser_switch(browser_ui):
    page, store, errors = browser_ui
    order = page.locator('.cookie-chips [data-g="cookies"]').evaluate_all('(items)=>items.map(x=>x.dataset.v)')
    assert order == ['none', 'chrome', 'edge', 'firefox', 'brave', 'zen']
    zen = page.locator('[data-g="cookies"][data-v="zen"]')
    expect(zen).to_have_text('Zen')
    zen.click()
    assert page.evaluate('state.cookies') == 'zen'
    with page.expect_file_chooser() as selected:
        page.locator('#cookieLoad').click()
    selected.value.set_files({'name': 'cookies.txt', 'mimeType': 'text/plain', 'buffer': VALID})
    expect(page.locator('#cookieFeedback')).to_contain_text('Cookies loaded')
    token = page.evaluate('state.cookieToken')
    assert store.snapshot('zen', token)
    page.locator('[data-g="cookies"][data-v="brave"]').click()
    expect(page.locator('#cookieFeedback')).to_have_text('')
    assert page.evaluate('state.cookieToken') == ''
    page.wait_for_timeout(100)
    assert token not in store._imports
    # Zen precedes the upload control, including after a responsive wrap.
    assert page.locator('.cookie-chips').evaluate('(row)=>row.children[row.children.length-2].dataset.v') == 'zen'


def test_native_cancel_and_stale_selection(browser_ui, monkeypatch):
    page, store, errors = browser_ui
    monkeypatch.setattr(app, "_IS_WINDOWS", True)
    monkeypatch.setattr(app, "browser_directory", lambda b: None)
    monkeypatch.setattr(app, "select_cookie_file", lambda initial, language='en': None)
    page.locator('[data-g="cookies"][data-v="firefox"]').click()
    page.evaluate("cookieNative=true")
    page.locator("#cookieLoad").click()
    expect(page.locator("#cookieFeedback")).to_contain_text("Automatic collection failed")
    assert not store._imports
    old = store.load("firefox", VALID)
    page.evaluate("clearCookie(); state.cookies='chrome'")
    page.evaluate("([token])=>acceptCookie({token},'firefox',-1)", [old])
    page.wait_for_timeout(100)
    assert old not in store._imports


def test_automatic_cookie_success_and_selected_browser_help(browser_ui, monkeypatch):
    page, store, errors = browser_ui
    monkeypatch.setattr(app, '_run_cookie_export', lambda browser, path: Path(path).write_bytes(VALID))
    for browser, database in [('chrome', 'Cookies'), ('zen', 'cookies.sqlite')]:
        page.locator(f'[data-g="cookies"][data-v="{browser}"]').click()
        expect(page.locator('#cookiesHint')).to_contain_text(database)
        expect(page.locator('#cookiesHint')).to_contain_text('Netscape')
        expect(page.locator('#cookiesHint')).to_contain_text('not a built-in profile file')
        page.locator('#cookieLoad').click()
        expect(page.locator('#cookieFeedback')).to_contain_text('Browser cookies loaded')
        assert store.snapshot(browser, page.evaluate('state.cookieToken'))
        assert page.evaluate('cookieManualReady') is False
    page.locator('[data-g="cookies"][data-v="none"]').click()
    expect(page.locator('#cookiesHint')).to_contain_text('Select a browser')
    expect(page.locator('#cookieFeedback')).to_have_text('')


def test_auto_failure_cancel_and_explicit_manual_retry(browser_ui, monkeypatch):
    page, store, errors = browser_ui
    calls = []
    def fail(browser, path):
        calls.append(browser)
        raise app.CookieError('cookieLocked')
    monkeypatch.setattr(app, '_run_cookie_export', fail)
    page.locator('[data-g="cookies"][data-v="zen"]').click()
    with page.expect_file_chooser() as chooser:
        page.locator('#cookieLoad').click()
    chooser.value.set_files([])
    expect(page.locator('#cookieFeedback')).to_contain_text('cookies are locked')
    with page.expect_file_chooser() as chooser:
        page.get_by_role('button', name='Select cookies.txt', exact=True).click()
    chooser.value.set_files({'name': 'cookies.txt', 'mimeType': 'text/plain', 'buffer': VALID})
    expect(page.locator('#cookieFeedback')).to_contain_text('Cookies loaded')
    assert calls == ['zen']
    assert page.evaluate('cookieAutomatic') is False


def test_stale_auto_response_does_not_open_picker_or_change_browser(browser_ui):
    page, store, errors = browser_ui
    token = store.load('zen', VALID)
    page.locator('[data-g="cookies"][data-v="zen"]').click()
    page.evaluate('''() => {
        const original = window.fetch;
        window.fetch = (url, ...args) => url === '/cookies/auto' ? new Promise(resolve => {
            window.finishAuto = result => resolve({json: async () => result});
        }) : original(url, ...args);
    }''')
    page.locator('#cookieLoad').click()
    page.locator('[data-g="cookies"][data-v="brave"]').click()
    page.evaluate('(token) => finishAuto({token, automatic:true})', token)
    expect(page.locator('#cookieLoad')).to_be_enabled()
    assert page.evaluate('state.cookies') == 'brave'
    assert page.evaluate('state.cookieToken') == ''
    expect(page.locator('#cookieFeedback')).to_have_text('')
    page.wait_for_timeout(100)
    assert token not in store._imports


@pytest.mark.parametrize('lang', ['en', 'tr', 'es', 'de', 'fr', 'it', 'pt', 'ru'])
def test_all_languages_complete_and_render_without_fallback(browser_ui, lang):
    page, store, errors = browser_ui
    # Check before T() can conceal missing translations with English fallback.
    missing = page.evaluate('''lang => {
        const keys = Object.keys(EXTRA_TEXT.en);
        return keys.filter(k => typeof EXTRA_TEXT[lang][k] !== 'string' || !EXTRA_TEXT[lang][k].trim());
    }''', lang)
    assert missing == []
    missing_base = page.evaluate('lang => Object.keys(I18N.en).filter(k => !(k in I18N[lang]))', lang)
    assert missing_base == []
    page.evaluate('applyLang', lang)
    page.locator('[data-g="mode"][data-v="audio"]').click()
    page.locator('[data-g="fmt"][data-v="opus"]').click()
    expected_auto = page.evaluate('T("audioAuto")')
    expect(page.locator('[data-g="br"][data-v="best"]')).to_have_text(expected_auto)
    upload(page)
    expect(page.locator('#cookieFeedback')).to_contain_text(page.evaluate('T("cookieLoaded")'))
    page.evaluate("cookieMessage='cookieDatabase'; renderCookies()")
    expect(page.locator('#cookieFeedback')).to_contain_text(page.evaluate('T("cookieDatabase")'))
    page.set_viewport_size({'width': 460, 'height': 1050})
    for selector in ('#cookieLoad', '#arows', '#root'):
        box = page.locator(selector).bounding_box()
        assert box['x'] + box['width'] <= 461
