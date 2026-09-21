"""Browser tests for the client-side engine (Playwright + pytest-playwright).

Fixtures are served from conftest's local HTTP server — no external network.
"""
import base64
import json


def hash_for(feeds):
    payload = json.dumps(feeds).encode()
    return "#feeds=" + base64.urlsafe_b64encode(payload).decode().rstrip("=")


class TestSharedLinks:
    def test_hash_edition_previews_without_saving(self, page, base_url):
        page.goto(base_url + "/" + hash_for(
            [{"n": "Fixtures", "u": base_url + "/fixture-a.xml"}]))
        page.wait_for_selector("#shared-banner")
        assert page.evaluate("localStorage.getItem('gazette-feeds')") is None
        assert "shared preview" in page.inner_text("#edition-label").lower()
        page.wait_for_selector("#sections article", timeout=15000)

    def test_use_this_edition_saves(self, page, base_url):
        page.goto(base_url + "/" + hash_for(
            [{"n": "Fixtures", "u": base_url + "/fixture-a.xml"}]))
        page.wait_for_selector("#shared-banner")
        page.click("#shared-use")
        saved = page.evaluate("localStorage.getItem('gazette-feeds')")
        assert saved and "fixture-a" in saved
        assert page.query_selector("#shared-banner") is None

    def test_dismiss_reverts_to_default(self, page, base_url):
        page.goto(base_url + "/" + hash_for(
            [{"n": "Fixtures", "u": base_url + "/fixture-a.xml"}]))
        page.wait_for_selector("#shared-banner")
        page.click("#shared-dismiss")
        page.wait_for_load_state("load")
        assert "compiled from rss feeds" in page.inner_text("#edition-label").lower()
        assert page.query_selector("#shared-banner") is None


class TestSearch:
    def test_filters_and_shows_count(self, page, base_url):
        page.goto(base_url + "/")
        page.fill("#search-box", "venus")
        visible = page.eval_on_selector_all(
            "#sections article", "els => els.filter(e => e.style.display !== 'none').length")
        assert visible == 1
        assert "1 result" in page.inner_text("#search-status")

    def test_esc_restores_full_edition(self, page, base_url):
        page.goto(base_url + "/")
        page.fill("#search-box", "venus")
        page.press("#search-box", "Escape")
        visible = page.eval_on_selector_all(
            "#sections article", "els => els.filter(e => e.style.display !== 'none').length")
        assert visible == 3
        assert page.inner_text("#search-status") == ""


class TestSettings:
    def test_add_save_remove_feed(self, page, base_url):
        page.goto(base_url + "/")
        page.click("#settings-btn")
        page.fill("#feed-name", "Fixtures")
        page.fill("#feed-url", base_url + "/fixture-a.xml")
        page.click("#feed-add")
        assert page.eval_on_selector_all("#feed-list .feed-row", "els => els.length") == 1
        page.click("#feed-save")
        page.wait_for_selector("#sections article", timeout=15000)
        assert "custom edition" in page.inner_text("#edition-label").lower()
        # remove it again and confirm localStorage is cleared
        page.click("#settings-btn")
        page.click(".feed-del")
        page.click("#feed-save")
        page.wait_for_load_state("load")
        assert page.evaluate("localStorage.getItem('gazette-feeds')") is None

    def test_feed_url_cap_rejects_overlong_url(self, page, base_url):
        page.goto(base_url + "/")
        page.click("#settings-btn")
        page.fill("#feed-url", "https://example.org/" + "a" * 3000)
        page.click("#feed-add")
        assert page.eval_on_selector_all("#feed-list .feed-row", "els => els.length") == 2  # defaults only


class TestOpml:
    def test_export_downloads_opml(self, page, base_url):
        page.goto(base_url + "/")
        page.click("#settings-btn")
        with page.expect_download() as dl:
            page.click("#opml-export")
        content = dl.value.path().read_text()
        assert content.startswith('<?xml version="1.0"')
        assert 'xmlUrl="https://bbc-feeds.danq.dev' in content

    def test_import_loads_feeds_into_settings(self, page, base_url, tmp_path):
        opml = tmp_path / "import.opml"
        opml.write_text(
            '<opml version="2.0"><body>'
            f'<outline type="rss" text="Fixtures" xmlUrl="{base_url}/fixture-a.xml"/>'
            '</body></opml>')
        page.goto(base_url + "/")
        page.click("#settings-btn")
        page.set_input_files("#opml-import", str(opml))
        assert page.eval_on_selector_all("#feed-list .feed-row", "els => els.length") == 1
        assert "Fixtures" in page.inner_text("#feed-list")


class TestRelayFallback:
    def test_second_relay_is_used_when_direct_and_first_fail(self, page, base_url):
        page.add_init_script("""
          window.__realFetch = window.fetch;
          window.fetch = function (url, opts) {
            url = String(url);
            if (url.indexOf('allorigins') >= 0 ||
                (url.indexOf('fixture-a.xml') >= 0 && url.indexOf('codetabs') < 0)) {
              return Promise.reject(new TypeError('Failed to fetch'));
            }
            if (url.indexOf('codetabs') >= 0) {
              return window.__realFetch('__BASE__/fixture-a.xml', opts);
            }
            return window.__realFetch(url, opts);
          };
        """.replace("__BASE__", base_url))
        page.goto(base_url + "/" + hash_for(
            [{"n": "RelayTest", "u": base_url + "/fixture-a.xml"}]))
        page.wait_for_selector("#sections article", timeout=20000)
        assert "relaytest" in page.inner_text("#sections").lower()

    def test_jina_is_used_for_article_text_when_all_relays_fail(self, page, base_url):
        page.add_init_script("""
          window.__realFetch = window.fetch;
          window.fetch = function (url, opts) {
            url = String(url);
            // feeds load fine; article HTML fetches all fail (CORS + relay outage).
            // jina URLs embed the article path, so check the relay hostname first.
            if (url.indexOf('r.jina.ai') >= 0) {
              return window.__realFetch('__BASE__/article-alpha.jina', opts);
            }
            if (url.indexOf('article-alpha.html') >= 0 || url.indexOf('allorigins') >= 0
                || url.indexOf('codetabs') >= 0) {
              return Promise.reject(new TypeError('Failed to fetch'));
            }
            return window.__realFetch(url, opts);
          };
        """.replace("__BASE__", base_url))
        page.goto(base_url + "/" + hash_for(
            [{"n": "JinaTest", "u": base_url + "/fixture-a.xml"}]))
        page.wait_for_selector("#sections article", timeout=15000)
        page.click(".art-link")
        page.wait_for_function(
            "document.getElementById('r-kicker').textContent === 'Full story'", timeout=15000)
        body = page.inner_text("#r-body")
        assert "jina one" in body.lower()
        assert "numbered jina three" in body.lower()


class TestReader:
    def test_plain_click_opens_popup(self, page, base_url):
        page.goto(base_url + "/")
        page.click(".art-link")
        page.wait_for_selector("#reader[open]")
        assert "Alpha story about mars rover" in page.inner_text("#r-title")
        assert "also in: tech, science" in page.inner_text("#r-body").lower()

    def test_modifier_click_is_not_intercepted(self, page, base_url):
        page.goto(base_url + "/")
        prevented = page.evaluate("""(() => {
          const l = document.querySelectorAll('.art-link')[1];
          const ev = new MouseEvent('click', {bubbles: true, cancelable: true, metaKey: true});
          return !l.dispatchEvent(ev);
        })()""")
        assert prevented is False
        assert page.locator("#reader[open]").count() == 0

    def test_partial_story_confidence_label(self, page, base_url):
        page.goto(base_url + "/")
        page.click(".art-link")  # alpha fixture has a single paragraph
        page.wait_for_selector("#reader[open]")
        assert "partial story" in page.inner_text("#r-kicker").lower()

    def test_client_boilerplate_filtering(self, page, base_url):
        page.goto(base_url + "/" + hash_for(
            [{"n": "Fixtures", "u": base_url + "/fixture-a.xml"}]))
        page.wait_for_selector("#sections article", timeout=15000)
        page.click(".art-link")
        page.wait_for_function(
            "document.getElementById('r-kicker').textContent === 'Full story'", timeout=15000)
        body = page.inner_text("#r-body")
        assert "video can not" not in body.lower()
        assert "image caption" not in body.lower()
        assert "numbered one" in body


class TestOffline:
    def test_service_worker_caches_the_edition(self, page, base_url):
        page.goto(base_url + "/")
        page.wait_for_function(
            "caches.keys().then(ks => ks.some(k => k.startsWith('gazette-')))",
            timeout=10000)
