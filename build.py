#!/usr/bin/env python3
"""Fetch two BBC RSS feeds and render a newspaper-format static site with reader-mode popups."""
import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

FEEDS = [
    ("World & Canada", "https://bbc-feeds.danq.dev/us_and_canada-no-sports.xml"),
    ("Technology", "https://bbc-feeds.danq.dev/technology-no-sports.xml"),
]
# Optional override: a feeds.json next to this script replaces the defaults.
#   [{"name": "Section name", "url": "https://example.com/feed.xml"}, ...]
FEEDS_FILE = Path(__file__).parent / "feeds.json"
if FEEDS_FILE.exists():
    FEEDS = [(f["name"], f["url"]) for f in json.loads(FEEDS_FILE.read_text(encoding="utf-8"))]
# Or an OPML export from any RSS reader (feeds.json wins if both exist).
OPML_FILE = Path(__file__).parent / "feeds.opml"

TITLE = "The Feed Gazette"
TAGLINE = "All the news that fits, we syndicate."
OUT = Path(__file__).parent / "site"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
MAX_PARAS = 40
MAX_FEEDS = 20
MAX_URL_LEN = 2048

BOILERPLATE_PREFIXES = (
    "this video can", "image caption", "image source", "more on this story",
    "related topics", "follow us on", "watch: ", "listen: ", "copyright ",
)
MIN_PARA_LEN = 15


def _clean_paras(paras):
    """Drop boilerplate (video fallbacks, captions, promos), dupes, and stubs."""
    out, seen = [], set()
    for p in paras:
        norm = p.strip()
        if len(norm) < MIN_PARA_LEN:
            continue
        low = norm.lower()
        if any(low.startswith(bp) for bp in BOILERPLATE_PREFIXES):
            continue
        key = low[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
    return out

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def strip_html(s):
    if not s:
        return ""
    out, depth = [], 0
    for ch in s:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return " ".join("".join(out).split())

def parse_opml(text):
    """Extract (name, url) from OPML <outline xmlUrl=...> elements, any nesting depth."""
    root = ET.fromstring(text)
    out = []
    for o in root.iter("outline"):
        url = (o.get("xmlUrl") or "").strip()
        if not url:
            continue
        name = (o.get("text") or o.get("title") or "Feed").strip()
        out.append((name, url))
    return out

if not FEEDS_FILE.exists() and OPML_FILE.exists():
    FEEDS = parse_opml(OPML_FILE.read_text(encoding="utf-8"))

def cap_feeds(feeds):
    """Editions above MAX_FEEDS smell like a mistake, not a feed list."""
    return feeds[:MAX_FEEDS]

FEEDS = cap_feeds(FEEDS)

SW_JS = """/* Feed Gazette offline cache: stale-while-revalidate for the edition page,
   cache-first for fonts and images. The whole article corpus is embedded in
   the HTML, so the last edition you opened works fully offline. */
const CACHE = 'gazette-__VERSION__';
const CORE = ['./', './index.html'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(CORE)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  const cacheable = url.origin === location.origin
    || url.hostname.endsWith('googleapis.com')
    || url.hostname.endsWith('gstatic.com');
  if (!cacheable) return;
  e.respondWith(caches.open(CACHE).then(async (cache) => {
    const hit = await cache.match(e.request);
    const fetching = fetch(e.request).then((resp) => {
      if (resp.ok || resp.type === 'opaque') cache.put(e.request, resp.clone());
      return resp;
    }).catch(() => hit);
    return hit || fetching;
  }));
});
"""

ATOM_NS = "{http://www.w3.org/2005/Atom}"
MEDIA_NS = {"media": "http://search.yahoo.com/mrss/"}

def parse_feed(url):
    return parse_feed_text(fetch(url))

def parse_feed_text(raw):
    """Parse RSS 2.0 or Atom XML bytes/text into a list of item dicts."""
    root = ET.fromstring(raw)
    if root.tag == ATOM_NS + "feed" or root.tag == "feed":
        return _parse_atom(root)
    return _parse_rss(root)

def _thumb_from(el):
    media = el.find("media:thumbnail", MEDIA_NS)
    if media is not None and media.get("url"):
        return media.get("url")
    enc = el.find("media:content", MEDIA_NS)
    if enc is not None and enc.get("url"):
        return enc.get("url")
    return None

def _parse_rss(root):
    items = []
    for item in root.iter("item"):
        title = strip_html(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        desc = strip_html(item.findtext("description") or "")
        pub = item.findtext("pubDate") or ""
        items.append({"title": title, "link": link, "desc": desc,
                      "pub": pub, "thumb": _thumb_from(item)})
    return items

def _parse_atom(root):
    items = []
    for entry in root.findall(ATOM_NS + "entry"):
        title = strip_html(entry.findtext(ATOM_NS + "title") or "")
        link = ""
        for le in entry.findall(ATOM_NS + "link"):
            href = (le.get("href") or "").strip()
            rel = le.get("rel") or "alternate"
            if href and rel == "alternate":
                link = href
                break
            if href and not link:
                link = href  # keep first link as fallback
        desc = strip_html(entry.findtext(ATOM_NS + "summary")
                          or entry.findtext(ATOM_NS + "content") or "")
        pub = (entry.findtext(ATOM_NS + "published")
               or entry.findtext(ATOM_NS + "updated") or "")
        items.append({"title": title, "link": link, "desc": desc,
                      "pub": pub, "thumb": _thumb_from(entry)})
    return items


class _ParaExtractor(HTMLParser):
    """Collect text of <p> tags whose class contains 'Paragraph'."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras = []
        self._in_p = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "p" and "Paragraph" in dict(attrs).get("class", ""):
            self._in_p = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "p" and self._in_p:
            text = " ".join("".join(self._buf).split())
            if text:
                self.paras.append(text)
            self._in_p = False

    def handle_data(self, data):
        if self._in_p:
            self._buf.append(data)


def fetch_fulltext(link):
    """Return full article paragraphs, or [] on any failure."""
    try:
        raw = fetch(link).decode("utf-8", "replace")
    except Exception:
        return []
    # isolate <main> so nav/footer promos don't leak in
    m = re.search(r"<main\b.*?</main>", raw, re.S)
    scope = m.group(0) if m else raw
    p = _ParaExtractor()
    try:
        p.feed(scope)
    except Exception:
        return []
    return _clean_paras(p.paras[:MAX_PARAS])

def fmt_date(pub):
    if not pub:
        return ""
    # RFC 822 like "Fri, 19 Sep 2026 14:22:00 GMT"
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.strptime(pub.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%b %d, %Y · %H:%M UTC")
        except ValueError:
            continue
    # ISO 8601 (Atom) like "2026-09-19T14:22:00Z" or "2026-09-19T14:22:00+02:00"
    try:
        dt = datetime.fromisoformat(pub.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%b %d, %Y · %H:%M UTC")
    except ValueError:
        return ""

def esc(s):
    return html.escape(s or "", quote=True)

def safe_link(url):
    """Only http(s) and mailto links survive; anything else becomes inert."""
    u = (url or "").strip()
    if re.match(r"^https?://", u, re.I) or u.lower().startswith("mailto:"):
        return u
    return "#"

def article_html(a, lead=False, with_img=True):
    d = fmt_date(a["pub"])
    img = ""
    if with_img and a.get("thumb"):
        img = (f'<figure class="art-fig"><img src="{esc(a["thumb"])}" alt="" loading="lazy" '
               f'onerror="this.parentElement.remove()">'
               f'</figure>')
    cls = "article lead" if lead else "article"
    also = a.get("also_in") or []
    also_html = f'<p class="also-in">Also in: {esc(", ".join(also))}</p>' if also else ""
    return f'''<article class="{cls}">
{img}
<h2><a href="{esc(safe_link(a["link"]))}" data-idx="{a["idx"]}" class="art-link">{esc(a["title"])}</a></h2>
<p class="dateline">{esc(d)}</p>
{also_html}
<p class="excerpt" data-idx="{a["idx"]}">{esc(a["desc"])}</p>
</article>'''

SCRIPT_FEEDS = r'''
<script>
/* Feed Gazette client-side edition engine: per-visitor feed config in localStorage.
   Active only when custom feeds are saved; otherwise the server-built BBC page stands. */
(function () {
  var DEFAULTS = __DEFAULT_FEEDS__;
  var RELAY = 'https://api.allorigins.win/raw?url=';
  var RELAY2 = 'https://api.codetabs.com/v1/proxy?quest=';
  var RELAY3 = 'https://r.jina.ai/'; // markdown text extraction service, article URLs only
  var liveData = {};
  var liveIdx = 0;
  var active = null;        // saved edition (localStorage)
  var sharedPreview = null; // a #feeds link: rendered but NOT saved until accepted
  var MAX_FEEDS = 20;
  var MAX_URL_LEN = 2048;
  function b64uEncode(s) {
    return btoa(unescape(encodeURIComponent(s))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  function b64uDecode(s) {
    s = s.replace(/-/g, '+').replace(/_/g, '/');
    while (s.length % 4) s += '=';
    return decodeURIComponent(escape(atob(s)));
  }
  // a shared edition in the URL hash: preview only, never auto-saves
  var hashMatch = /[#&]feeds=([^&]+)/.exec(location.hash);
  if (hashMatch) {
    try {
      var arr = JSON.parse(b64uDecode(hashMatch[1]));
      if (Array.isArray(arr) && arr.length && arr.every(function (f) { return f && f.u && String(f.u).length <= MAX_URL_LEN; })) {
        sharedPreview = arr.slice(0, MAX_FEEDS);
      }
    } catch (e) {}
  }
  try {
    var saved = JSON.parse(localStorage.getItem('gazette-feeds') || 'null');
    if (Array.isArray(saved) && saved.length && saved.every(function (f) { return f && f.u; })) active = saved;
  } catch (e) {}
  var viewing = sharedPreview || active;

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }
  function stripHtml(s) {
    var d = document.createElement('div');
    d.innerHTML = s || '';
    return (d.textContent || '').replace(/\s+/g, ' ').trim();
  }
  function fmtDate(s) {
    if (!s) return '';
    var d = new Date(s);
    if (isNaN(d)) return '';
    return d.toLocaleString('en-US', { month: 'short', day: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }) + ' UTC';
  }
  function txt(el, tag) {
    var e = el.getElementsByTagName(tag)[0];
    return e ? e.textContent.trim() : '';
  }
  function safeLink(u) {
    u = (u || '').trim();
    return (/^https?:\/\//i.test(u) || u.toLowerCase().indexOf('mailto:') === 0) ? u : '#';
  }

  function tryFetchText(url) {
    // 15s cap so a slow or degraded relay settles the popup instead of hanging it
    var ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, 15000) : null;
    return fetch(url, { redirect: 'follow', signal: ctrl ? ctrl.signal : undefined })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      })
      .finally(function () { if (timer) clearTimeout(timer); });
  }
  function fetchFeedText(url) {
    // direct first (some feeds send CORS headers), then two public relays
    return tryFetchText(url).catch(function () {
      return tryFetchText(RELAY + encodeURIComponent(url)).catch(function () {
        return tryFetchText(RELAY2 + encodeURIComponent(url));
      });
    });
  }

  function parseFeedXml(text) {
    var doc = new DOMParser().parseFromString(text, 'text/xml');
    if (doc.querySelector('parsererror')) throw new Error('not a valid feed');
    var nodes = doc.querySelectorAll('item, entry');
    var out = [];
    for (var i = 0; i < nodes.length && i < 25; i++) {
      var it = nodes[i];
      var link = '';
      var links = it.getElementsByTagName('link');
      for (var j = 0; j < links.length; j++) {
        var rel = links[j].getAttribute('rel');
        var href = (links[j].getAttribute('href') || links[j].textContent || '').trim();
        if (href && (!rel || rel === 'alternate')) { link = href; break; }
      }
      var thumb = '';
      var m = it.getElementsByTagName('media:thumbnail')[0] || it.getElementsByTagName('media:content')[0];
      if (m && m.getAttribute('url')) thumb = m.getAttribute('url');
      if (!thumb) {
        var en = it.querySelector('enclosure[type^="image"]');
        if (en && en.getAttribute('url')) thumb = en.getAttribute('url');
      }
      var title = txt(it, 'title');
      if (!title) continue;
      out.push({
        t: title,
        u: link,
        d: fmtDate(txt(it, 'pubDate') || txt(it, 'published') || txt(it, 'updated')),
        s: stripHtml(txt(it, 'description') || txt(it, 'summary') || txt(it, 'content')).slice(0, 600),
        thumb: thumb,
        p: [],
        live: true
      });
    }
    return out;
  }

  var BOILERPLATE = ['this video can', 'image caption', 'image source', 'more on this story',
    'related topics', 'follow us on', 'watch: ', 'listen: ', 'copyright '];
  function extractParas(html) {
    var doc = new DOMParser().parseFromString(html, 'text/html');
    var root = doc.querySelector('main') || doc.querySelector('article') || doc.body;
    if (!root) return [];
    var out = [];
    var seen = {};
    var ps = root.querySelectorAll('p');
    for (var i = 0; i < ps.length && out.length < 40; i++) {
      var t = (ps[i].textContent || '').replace(/\s+/g, ' ').trim();
      if (t.length < 40) continue;
      var low = t.toLowerCase();
      var skip = false;
      for (var b = 0; b < BOILERPLATE.length; b++) { if (low.indexOf(BOILERPLATE[b]) === 0) { skip = true; break; } }
      if (skip || seen[low.slice(0, 120)]) continue;
      seen[low.slice(0, 120)] = true;
      out.push(t);
    }
    return out;
  }

  /* r.jina.ai returns pre-extracted markdown; turn it into clean paragraphs */
  function parseJinaMarkdown(text) {
    var out = [];
    var seen = {};
    var chunks = text.split(/\n{2,}/);
    for (var i = 0; i < chunks.length && out.length < 40; i++) {
      var t = chunks[i].trim();
      if (!t || t.length < 40) continue;
      if (/^(#|!|\[|Title:|URL Source:|Published Time:|Markdown Content:|-|\||\d+\. )/.test(t)) continue;
      t = t.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').replace(/\*\*/g, '').replace(/^>\s*/, '');
      if (t.length < 40) continue;
      var low = t.toLowerCase();
      var skip = false;
      for (var b = 0; b < BOILERPLATE.length; b++) { if (low.indexOf(BOILERPLATE[b]) === 0) { skip = true; break; } }
      if (skip || seen[low.slice(0, 120)]) continue;
      seen[low.slice(0, 120)] = true;
      out.push(t);
    }
    return out;
  }

  /* article full text: direct first (free when the site allows CORS, instant
     fail when it doesn't), then all backends race — first with paragraphs wins */
  function backendJina(url) { return tryFetchText(RELAY3 + url).then(parseJinaMarkdown); }
  function backendHtml(relay) {
    return function (url) { return tryFetchText(relay + encodeURIComponent(url)).then(extractParas); };
  }

  /* full-text cache: 12h TTL in localStorage, keyed by URL hash */
  var FT_TTL_MS = 12 * 3600 * 1000;
  function ftCacheKey(url) {
    var h = 0, s = String(url);
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    return 'gazette-ft-' + (h >>> 0).toString(36);
  }
  function ftCacheGet(url) {
    try {
      var rec = JSON.parse(localStorage.getItem(ftCacheKey(url)) || 'null');
      if (!rec || !rec.t || (Date.now() - rec.t) > FT_TTL_MS) return null;
      return Array.isArray(rec.p) && rec.p.length ? rec.p : null;
    } catch (e) { return null; }
  }
  function ftCacheSet(url, paras) {
    try { localStorage.setItem(ftCacheKey(url), JSON.stringify({ t: Date.now(), p: paras })); }
    catch (e) {} // quota: drop silently
  }

  function fetchArticleParas(url) {
    var cached = ftCacheGet(url);
    if (cached) return Promise.resolve(cached);
    return tryFetchText(url).then(extractParas, function () { return []; }).then(function (direct) {
      if (direct.length) { ftCacheSet(url, direct); return direct; }
      var racers = [backendJina, backendHtml(RELAY), backendHtml(RELAY2)].map(function (b) {
        return b(url).then(function (paras) {
          if (!paras || !paras.length) throw new Error('empty extraction');
          return paras;
        });
      });
      return Promise.any(racers).then(function (paras) { ftCacheSet(url, paras); return paras; })
        .catch(function () { return []; });
    });
  }

  /* small concurrency pool so large editions don't fire 20 parallel fetches */
  function pooled(tasks, limit) {
    var results = new Array(tasks.length);
    var i = 0;
    function worker() {
      if (i >= tasks.length) return Promise.resolve();
      var cur = i++;
      return Promise.resolve(tasks[cur]()).then(function (r) { results[cur] = r; }).then(worker);
    }
    var workers = [];
    for (var k = 0; k < Math.min(limit, tasks.length); k++) workers.push(worker());
    return Promise.all(workers).then(function () { return results; });
  }

  function articleCard(a, idx, lead, withImg) {
    var img = '';
    if (withImg && a.thumb) {
      img = '<figure class="art-fig"><img src="' + esc(a.thumb) + '" alt="" loading="lazy" onerror="this.parentElement.remove()"></figure>';
    }
    var cls = lead ? 'article lead' : 'article';
    var also = a.ai && a.ai.length ? '<p class="also-in">Also in: ' + esc(a.ai.join(', ')) + '</p>' : '';
    return '<article class="' + cls + '">' + img +
      '<h2><a href="' + esc(safeLink(a.u)) + '" data-idx="' + esc(idx) + '" class="art-link">' + esc(a.t) + '</a></h2>' +
      '<p class="dateline">' + esc(a.d) + '</p>' + also +
      '<p class="excerpt" data-idx="' + esc(idx) + '">' + esc(a.s) + '</p></article>';
  }

  function loadEdition() {
    var container = document.getElementById('sections');
    var label = document.getElementById('edition-label');
    var upd = document.getElementById('updated-label');
    if (label) label.textContent = (sharedPreview ? 'Shared preview · ' : 'Custom edition · ') + viewing.length + ' feed' + (viewing.length === 1 ? '' : 's');
    if (upd) upd.textContent = 'Updated just now';
    container.innerHTML = '<p class="loading-note">Setting the type… loading your feeds.</p>';
    var tasks = viewing.map(function (f) {
      return function () {
        return fetchFeedText(f.u).then(function (t) { return { f: f, items: parseFeedXml(t) }; })
          .catch(function (err) { return { f: f, error: (err && err.message) || 'failed to load' }; });
      };
    });
    pooled(tasks, 4).then(function (results) {
      var html = '';
      liveData = {}; liveIdx = 0;
      // dedupe + cluster across overlapping feeds: same link path or same long title
      var firstByKey = {};
      function dedupeKeys(a) {
        var keys = [];
        var m = /^https?:\/\/([^\/?#]+)([^?#]*)/i.exec(a.u || '');
        if (m) {
          var path = m[2].replace(/\/+$/, '').toLowerCase();
          if (path) keys.push((m[1] + path).toLowerCase());
        }
        var t = (a.t || '').toLowerCase().replace(/\s+/g, ' ').trim();
        if (t.length >= 25) keys.push(t);
        return keys;
      }
      results.forEach(function (r) {
        var head = '<div class="section-head"><span class="section-title">' + esc(r.f.n || 'Feed') + '</span>';
        if (r.error) {
          html += '<section class="section">' + head + '<span class="section-count">error</span></div>' +
            '<p class="loading-note">Could not load this feed (' + esc(r.error) + '). It may block the relay — check the URL or try another source.</p></section>';
          return;
        }
        var kept = [];
        r.items.forEach(function (a) {
          var ks = dedupeKeys(a);
          var first = null;
          for (var i = 0; i < ks.length; i++) { if (firstByKey[ks[i]]) { first = firstByKey[ks[i]]; break; } }
          if (first) {
            if (!first.ai) first.ai = [];
            if (first.ai.indexOf(r.f.n) < 0) first.ai.push(r.f.n);
            return;
          }
          for (var j = 0; j < ks.length; j++) firstByKey[ks[j]] = a;
          kept.push(a);
        });
        if (!kept.length) {
          html += '<section class="section">' + head + '<span class="section-count">0 stories</span></div>' +
            '<p class="loading-note">Feed loaded but returned no items.</p></section>';
          return;
        }
        var cards = kept.map(function (a, i) {
          var idx = String(liveIdx++);
          liveData[idx] = a;
          return articleCard(a, idx, i === 0, i < 3);
        }).join('');
        html += '<section class="section">' + head + '<span class="section-count">' + kept.length + ' stories</span></div>' +
          '<div class="columns">' + cards + '</div></section>';
      });
      container.innerHTML = html;
      if (window.__gazetteResetSearchCache) window.__gazetteResetSearchCache();
      if (window.__gazetteRunSearch) window.__gazetteRunSearch();

      // warm the reader: prefetch the top stories' full text in idle time
      var prefetchTasks = [];
      results.forEach(function (r) {
        if (r.items) r.items.slice(0, 2).forEach(function (a) {
          if (a.u && !a.p.length) prefetchTasks.push(function () {
            return fetchArticleParas(a.u).then(function (paras) {
              if (paras.length) {
                a.p = paras;
                if (window.__gazetteResetSearchCache) window.__gazetteResetSearchCache();
              }
            });
          });
        });
      });
      if (prefetchTasks.length) {
        var idle = window.requestIdleCallback || function (fn) { setTimeout(fn, 1200); };
        idle(function () { pooled(prefetchTasks, 2); });
      }
    });
  }

  window.__gazetteResolve = function (idx) { return liveData[idx]; };

  window.__gazetteOpen = function (idx) {
    var a = liveData[idx];
    if (!a) return;
    var savedY = window.scrollY;
    window.__gazetteScrollY = savedY;  // script 2's close() restores from this
    var dlg = document.getElementById('reader');
    var body = document.getElementById('r-body');
    var kicker = document.getElementById('r-kicker');
    var titleEl = document.getElementById('r-title');
    document.getElementById('r-date').textContent = a.d;
    document.getElementById('r-src').href = safeLink(a.u);
    titleEl.textContent = a.t;
    kicker.textContent = a.p.length >= 3 ? 'Full story' : (a.p.length ? 'Partial story' : 'Summary');
    function draw(paras, note) {
      body.innerHTML = '';
      if (a.ai && a.ai.length) {
        var ai = document.createElement('p');
        ai.className = 'also-in';
        ai.textContent = 'Also in: ' + a.ai.join(', ');
        body.appendChild(ai);
      }
      var ps = paras && paras.length ? paras : [a.s || '(no summary available)'];
      ps.forEach(function (p) {
        var el = document.createElement('p');
        el.textContent = p;
        body.appendChild(el);
      });
      if (note) {
        var p = document.createElement('p');
        p.className = 'loading-note';
        if (note === 'Loading full story…') {
          var sp = document.createElement('span');
          sp.className = 'loading-spinner';
          sp.setAttribute('aria-hidden', 'true');
          p.appendChild(sp);
        }
        p.appendChild(document.createTextNode(note));
        body.appendChild(p);
      }
      body.scrollTop = 0;
    }
    draw(a.p, a.p.length ? '' : 'Loading full story…');
    if (typeof dlg.showModal === 'function') { dlg.showModal(); window.scrollTo(0, savedY); }
    else dlg.setAttribute('open', '');
    if (!a.p.length && a.u) {
      var cachedFt = ftCacheGet(a.u);
      if (cachedFt) {
        a.p = cachedFt;
        kicker.textContent = a.p.length >= 3 ? 'Full story' : 'Partial story';
        draw(a.p, '');
      }
    }
    if (!a.p.length && !a.tried) {
      a.tried = true;
      if (!a.u) { draw(null, 'No article link in this feed item — summary only.'); return; }
      fetchArticleParas(a.u).then(function (paras) {
        if (paras.length) a.p = paras;
        if (window.__gazetteInvalidateSearch) window.__gazetteInvalidateSearch(idx);
        if (dlg.open && titleEl.textContent === a.t) {
          kicker.textContent = a.p.length >= 3 ? 'Full story' : (a.p.length ? 'Partial story' : 'Summary');
          draw(a.p, a.p.length ? '' : 'Full text unavailable for this site — summary shown. Use the link below for the source.');
        }
      }).catch(function () {
        if (dlg.open && titleEl.textContent === a.t) draw(null, 'Could not load full text — summary shown.');
      });
    }
  };

  /* settings dialog */
  var sdlg = document.getElementById('settings');
  var listEl = document.getElementById('feed-list');

  /* shared-edition preview: nothing is saved until the visitor says yes */
  function showSharedBanner() {
    var banner = document.createElement('div');
    banner.id = 'shared-banner';
    banner.innerHTML = '<span>Someone shared this edition (' + viewing.length + ' feed' +
      (viewing.length === 1 ? '' : 's') + '). Preview only — your settings are untouched.</span>' +
      '<button id="shared-use" type="button">Use this edition</button>' +
      '<button id="shared-dismiss" type="button">Dismiss</button>';
    var bar = document.querySelector('.dateline-bar');
    bar.parentNode.insertBefore(banner, bar.nextSibling);
    document.getElementById('shared-use').addEventListener('click', function () {
      adoptPreview();
      try { localStorage.setItem('gazette-feeds', JSON.stringify(active)); } catch (e) {}
      var label = document.getElementById('edition-label');
      if (label) label.textContent = 'Custom edition · ' + active.length + ' feed' + (active.length === 1 ? '' : 's');
    });
    document.getElementById('shared-dismiss').addEventListener('click', function () {
      location.hash = '';
      location.reload();
    });
  }
  function adoptPreview() {
    if (!sharedPreview) return;
    active = sharedPreview.slice();
    viewing = active;
    sharedPreview = null;
    var b = document.getElementById('shared-banner');
    if (b) b.remove();
  }
  function renderList() {
    var feeds = active || DEFAULTS;
    listEl.innerHTML = feeds.map(function (f, i) {
      var right = active
        ? '<button class="feed-del" type="button" data-i="' + i + '">Remove</button>'
        : '<span class="feed-tag">default</span>';
      return '<div class="feed-row"><span class="feed-name">' + esc(f.n || 'Feed') + '</span>' +
        '<span class="feed-url">' + esc(f.u) + '</span>' + right + '</div>';
    }).join('');
  }
  renderList();
  document.getElementById('feed-add').addEventListener('click', function () {
    var n = document.getElementById('feed-name').value.trim() || 'Feed';
    var u = document.getElementById('feed-url').value.trim();
    if (!/^https?:\/\//i.test(u)) { alert('Feed URL must start with http:// or https://'); return; }
    if (u.length > MAX_URL_LEN) { alert('That URL is implausibly long for a feed (max ' + MAX_URL_LEN + ' chars).'); return; }
    var feeds = (active || []).slice();
    if (feeds.length >= MAX_FEEDS) { alert('Editions are capped at ' + MAX_FEEDS + ' feeds. Remove one first.'); return; }
    feeds.push({ n: n, u: u });
    active = feeds;
    viewing = active;
    renderList();
    document.getElementById('feed-name').value = '';
    document.getElementById('feed-url').value = '';
  });
  listEl.addEventListener('click', function (e) {
    var b = e.target.closest('.feed-del');
    if (!b) return;
    active.splice(parseInt(b.getAttribute('data-i'), 10), 1);
    renderList();
  });
  document.getElementById('feed-save').addEventListener('click', function () {
    if (active && active.length) localStorage.setItem('gazette-feeds', JSON.stringify(active));
    else localStorage.removeItem('gazette-feeds');
    location.reload();
  });
  document.getElementById('feed-reset').addEventListener('click', function () {
    localStorage.removeItem('gazette-feeds');
    location.reload();
  });

  /* OPML import/export + shareable edition links */
  function toOpml(feeds) {
    function x(s) {
      return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
    var lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<opml version="2.0">',
      '<head><title>Feed Gazette edition</title></head>', '<body>'];
    feeds.forEach(function (f) {
      lines.push('<outline type="rss" text="' + x(f.n) + '" title="' + x(f.n) + '" xmlUrl="' + x(f.u) + '"/>');
    });
    lines.push('</body>', '</opml>');
    return lines.join('\n');
  }
  document.getElementById('opml-export').addEventListener('click', function () {
    var blob = new Blob([toOpml(viewing || DEFAULTS)], { type: 'text/x-opml' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'feed-gazette.opml';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
  });
  document.getElementById('opml-import-btn').addEventListener('click', function () {
    document.getElementById('opml-import').click();
  });
  document.getElementById('opml-import').addEventListener('change', function () {
    var file = this.files[0];
    this.value = '';
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var doc = new DOMParser().parseFromString(reader.result, 'text/xml');
        if (doc.querySelector('parsererror')) throw new Error('not valid XML');
        var feeds = [];
        var outlines = doc.querySelectorAll('outline[xmlUrl]');
        for (var i = 0; i < outlines.length; i++) {
          feeds.push({
            n: outlines[i].getAttribute('text') || outlines[i].getAttribute('title') || 'Feed',
            u: outlines[i].getAttribute('xmlUrl').trim()
          });
        }
        if (!feeds.length) { alert('No feeds found in that OPML file.'); return; }
        if (feeds.length > MAX_FEEDS) {
          alert('That OPML has ' + feeds.length + ' feeds; keeping the first ' + MAX_FEEDS + '.');
          feeds = feeds.slice(0, MAX_FEEDS);
        }
        active = feeds;
        viewing = active;
        renderList();
      } catch (e) { alert('Could not read OPML: ' + e.message); }
    };
    reader.readAsText(file);
  });
  document.getElementById('share-link').addEventListener('click', function () {
    var url = location.origin + location.pathname + '#feeds=' + b64uEncode(JSON.stringify(viewing || DEFAULTS));
    var btn = this;
    function done() {
      btn.textContent = 'Link copied!';
      setTimeout(function () { btn.textContent = 'Copy share link'; }, 2000);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(done, function () { prompt('Copy this link:', url); });
    } else {
      prompt('Copy this link:', url);
    }
  });

  document.getElementById('settings-btn').addEventListener('click', function () {
    adoptPreview(); // opening Settings means engaging with the shared edition
    if (typeof sdlg.showModal === 'function') sdlg.showModal();
    else sdlg.setAttribute('open', '');
  });
  document.getElementById('settings-close').addEventListener('click', function () {
    if (sdlg.open && typeof sdlg.close === 'function') sdlg.close();
    else sdlg.removeAttribute('open');
  });
  sdlg.addEventListener('click', function (e) {
    if (e.target === sdlg) { if (typeof sdlg.close === 'function') sdlg.close(); else sdlg.removeAttribute('open'); }
  });

  if (viewing) {
    if (sharedPreview) showSharedBanner();
    loadEdition();
  }
})();
</script>
'''

def render(sections, generated):
    defaults_js = json.dumps([{"n": n, "u": u} for n, u in FEEDS], ensure_ascii=False)
    feeds_script = SCRIPT_FEEDS.replace("__DEFAULT_FEEDS__", defaults_js)
    now = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    body = []
    for sec in sections:
        if sec["error"]:
            body.append(f'''<section class="section">
<div class="section-head"><span class="section-title">{esc(sec["name"])}</span>
<span class="section-count">unavailable</span></div>
<p class="loading-note">This feed could not be loaded ({esc(sec["error"])}). The rest of the edition is unaffected.</p>
</section>''')
            continue
        items = sec["items"]
        cards = []
        for i, a in enumerate(items):
            cards.append(article_html(a, lead=(i == 0), with_img=(i < 3)))
        body.append(f'''<section class="section">
<div class="section-head"><span class="section-title">{esc(sec["name"])}</span>
<span class="section-count">{len(items)} stories</span></div>
<div class="columns">{"".join(cards)}</div>
</section>''')

    # article data for the reader popup, keyed by idx
    data = {str(a["idx"]): {"t": a["title"], "d": fmt_date(a["pub"]),
                            "u": safe_link(a["link"]), "p": a.get("paras") or [],
                            "s": a["desc"], "ai": a.get("also_in") or []}
            for sec in sections for a in sec["items"]}
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Feed Gazette</title>
<script>
try {{
  var t = localStorage.getItem('gazette-theme');
  if (t === 'dark' || t === 'eink') document.documentElement.setAttribute('data-theme', t);
  var f = localStorage.getItem('gazette-font');
  if (f === 'serif' || f === 'sans' || f === 'plex') document.documentElement.setAttribute('data-font', f);
}} catch (e) {{}}
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,500;0,700;0,900;1,500&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400&family=Inter:wght@400;600;700&family=IBM+Plex+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<style>
:root {{
  --ink: #1a1714;
  --paper: #f6f1e5;
  --rule: #1a1714;
  --faint: #6b6257;
  --accent: #7a1f1f;
  --colrule: #cfc4ae;
  --imgborder: #cfc4ae;
  --img-filter: sepia(0.15) contrast(0.95);
  --backdrop: rgba(20,16,10,0.6);
  --shadow: 0 12px 60px rgba(0,0,0,0.45);
}}
html[data-theme="dark"] {{
  --ink: #e8e0d0;
  --paper: #191612;
  --rule: #b8ad99;
  --faint: #9a9084;
  --accent: #d98b6a;
  --colrule: #3a342c;
  --imgborder: #3a342c;
  --img-filter: contrast(0.92) brightness(0.92);
  --backdrop: rgba(0,0,0,0.7);
  --shadow: 0 12px 60px rgba(0,0,0,0.8);
}}
html[data-theme="eink"] {{
  --ink: #000000;
  --paper: #ffffff;
  --rule: #000000;
  --faint: #444444;
  --accent: #000000;
  --colrule: #999999;
  --imgborder: #000000;
  --img-filter: grayscale(1) contrast(1.05);
  --backdrop: rgba(255,255,255,0.75);
  --shadow: none;
}}
:root {{
  --font-head: "Playfair Display", Georgia, serif;
  --font-body: "Source Serif 4", Georgia, serif;
}}
html[data-font="serif"] {{
  --font-head: "Source Serif 4", Georgia, serif;
  --font-body: "Source Serif 4", Georgia, serif;
}}
html[data-font="sans"] {{
  --font-head: "Inter", system-ui, sans-serif;
  --font-body: "Inter", system-ui, sans-serif;
}}
html[data-font="plex"] {{
  --font-head: "IBM Plex Sans", system-ui, sans-serif;
  --font-body: "IBM Plex Sans", system-ui, sans-serif;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  background: var(--paper);
  color: var(--ink);
  font-family: var(--font-body);
  line-height: 1.45;
  padding: 2rem 1.5rem 4rem;
}}
.sheet {{ max-width: 1200px; margin: 0 auto; }}
.masthead {{ text-align: center; border-bottom: 4px double var(--rule); padding-bottom: 1rem; }}
.masthead h1 {{
  font-family: var(--font-head);
  font-weight: 900;
  font-size: clamp(2.5rem, 8vw, 5rem);
  letter-spacing: 0.02em;
  line-height: 1.05;
}}
.masthead .tagline {{ font-style: italic; color: var(--faint); margin-top: 0.35rem; }}
.dateline-bar {{
  display: flex; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem;
  border-bottom: 1px solid var(--rule);
  padding: 0.4rem 0;
  font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.12em;
  color: var(--faint);
}}
.theme-toggle {{
  background: none; border: 1px solid var(--rule); color: var(--faint);
  font: inherit; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.12em;
  padding: 0.2rem 0.7rem; cursor: pointer;
}}
.theme-toggle:hover {{ color: var(--accent); border-color: var(--accent); }}
/* settings panel */
.feed-row {{ display: flex; align-items: center; gap: 0.6rem; padding: 0.5rem 0; border-bottom: 1px solid var(--colrule); font-size: 0.9rem; }}
.feed-name {{ font-weight: 600; min-width: 8rem; }}
.feed-url {{ color: var(--faint); font-size: 0.78rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }}
.feed-del, .feed-add button, .reader-foot button {{
  background: none; border: 1px solid var(--rule); color: var(--ink);
  font: inherit; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em;
  padding: 0.3rem 0.7rem; cursor: pointer;
}}
.feed-del:hover, .feed-add button:hover, .reader-foot button:hover {{ color: var(--accent); border-color: var(--accent); }}
.feed-tag {{ font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--faint); }}
.feed-add {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 1rem; }}
.feed-add input {{ flex: 1 1 10rem; background: var(--paper); border: 1px solid var(--rule); color: var(--ink); font: inherit; font-size: 0.85rem; padding: 0.4rem 0.6rem; }}
.settings-note {{ margin-top: 1rem; font-size: 0.78rem; color: var(--faint); font-style: italic; }}
.loading-note {{ color: var(--faint); font-style: italic; display: flex; align-items: center; gap: 0.55rem; }}
.loading-spinner {{
  width: 0.95em; height: 0.95em; border-radius: 50%; flex: 0 0 auto;
  border: 2px solid var(--colrule); border-top-color: var(--ink);
  animation: gazette-spin 0.75s linear infinite;
}}
@keyframes gazette-spin {{ to {{ transform: rotate(360deg); }} }}
@media (prefers-reduced-motion: reduce) {{ .loading-spinner {{ animation: none; }} }}
@media (max-width: 640px) {{
  .feed-name {{ min-width: 0; }}
}}
.section {{ margin-top: 2.5rem; }}
.section-head {{
  display: flex; align-items: baseline; justify-content: space-between;
  border-top: 3px solid var(--rule); border-bottom: 1px solid var(--rule);
  padding: 0.45rem 0; margin-bottom: 1.25rem;
}}
.section-title {{
  font-family: var(--font-head);
  font-weight: 700; font-size: 1.4rem; text-transform: uppercase; letter-spacing: 0.08em;
}}
.section-count {{ font-size: 0.75rem; color: var(--faint); text-transform: uppercase; letter-spacing: 0.12em; }}
.columns {{ columns: 3 280px; column-gap: 2rem; column-rule: 1px solid var(--colrule); }}
.article {{ break-inside: avoid; margin-bottom: 1.75rem; }}
.article h2 {{
  font-family: var(--font-head);
  font-weight: 700; font-size: 1.25rem; line-height: 1.2; margin-bottom: 0.3rem;
}}
.article.lead h2 {{ font-size: 1.7rem; }}
.article.lead .excerpt {{ font-size: 1.02rem; }}
.article h2 a {{ color: inherit; text-decoration: none; cursor: pointer; }}
.article h2 a:hover {{ color: var(--accent); }}
.dateline {{ font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.1em; color: var(--faint); margin-bottom: 0.4rem; }}
.excerpt {{ font-size: 0.92rem; cursor: pointer; }}
.also-in {{ font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.1em; color: var(--faint); margin-bottom: 0.4rem; }}
.search-box {{
  flex: 1 1 12rem; min-width: 8rem; max-width: 16rem;
  background: var(--paper); border: 1px solid var(--rule); color: var(--ink);
  font: inherit; font-size: 0.78rem; padding: 0.25rem 0.6rem;
}}
.search-box:focus {{ outline: none; border-color: var(--accent); }}
#search-status {{ font-size: 0.75rem; color: var(--faint); font-style: italic; margin: 0.4rem 0 0; }}
#search-status:empty {{ display: none; }}
.feed-actions {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 1rem; }}
#shared-banner {{
  display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap;
  border: 1px solid var(--rule); background: var(--paper);
  padding: 0.5rem 0.8rem; margin: 0.5rem 0 0.25rem;
  font-size: 0.82rem; font-style: italic; color: var(--faint);
}}
#shared-banner button {{
  background: none; border: 1px solid var(--rule); color: var(--ink);
  font: inherit; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.08em;
  padding: 0.25rem 0.7rem; cursor: pointer; font-style: normal;
}}
#shared-banner button:hover {{ color: var(--accent); border-color: var(--accent); }}
/* reader dialog */
dialog.reader {{
  border: none; padding: 0; max-width: 720px; width: calc(100% - 2rem);
  max-height: min(85vh, 900px); background: var(--paper); color: var(--ink);
  box-shadow: var(--shadow);
  /* explicit modal centering: the global * reset kills the UA's margin:auto,
     and anything but fixed makes the dialog sit in document flow, scrolling the page */
  position: fixed; inset: 0; margin: auto;
}}
html[data-theme="eink"] dialog.reader {{ border: 2px solid var(--ink); }}
dialog.reader::backdrop {{ background: var(--backdrop); }}
.art-fig {{ margin: 0 0 0.5rem; }}
.art-fig img {{ width: 100%; height: auto; display: block; border: 1px solid var(--imgborder); filter: var(--img-filter); }}
.reader-inner {{ display: flex; flex-direction: column; max-height: inherit; }}
.reader-head {{
  border-bottom: 4px double var(--rule); padding: 1.1rem 1.4rem 0.8rem;
  flex: 0 0 auto;
}}
.reader-head .kicker {{
  font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.14em;
  color: var(--accent); margin-bottom: 0.3rem;
}}
.reader-head h2 {{
  font-family: var(--font-head);
  font-weight: 700; font-size: 1.5rem; line-height: 1.2;
}}
.reader-head .dateline {{ margin: 0.4rem 0 0; }}
.reader-close {{
  position: absolute; top: 0.7rem; right: 0.8rem;
  width: 44px; height: 44px; border-radius: 50%;
  border: 1px solid var(--colrule);
  background: none; font-size: 1.35rem; line-height: 1;
  color: var(--faint); cursor: pointer; font-family: var(--font-head);
  display: flex; align-items: center; justify-content: center;
}}
.reader-close:hover {{ color: var(--accent); border-color: var(--accent); }}
.reader-body {{ overflow-y: auto; padding: 1.2rem 1.4rem 1.4rem; }}
.reader-body p {{ margin-bottom: 0.9rem; font-size: 1.02rem; }}
.reader-body p:first-child {{ font-size: 1.1rem; }}
.reader-foot {{
  flex: 0 0 auto; border-top: 1px solid var(--rule);
  padding: 0.6rem 1.4rem; display: flex; justify-content: space-between; align-items: center;
  font-size: 0.78rem; color: var(--faint); gap: 1rem;
}}
.reader-foot a {{ color: var(--accent); text-decoration: none; text-transform: uppercase; letter-spacing: 0.08em; white-space: nowrap; }}
.reader-foot a:hover {{ text-decoration: underline; }}
footer {{
  margin-top: 3rem; border-top: 4px double var(--rule); padding-top: 0.75rem;
  text-align: center; font-size: 0.75rem; color: var(--faint); font-style: italic;
}}
@media (max-width: 640px) {{
  body {{ padding: 1.25rem 1rem 3rem; }}
  .columns {{ columns: 1; }}
  .section {{ margin-top: 1.5rem; }}
  .masthead {{ padding-bottom: 0.75rem; }}
  dialog.reader {{ width: 100%; max-height: none; height: 100dvh; max-width: none; }}
  /* FAB close: thumb-sized, bottom right, clear of the footer bar */
  .reader-close {{
    position: fixed; top: auto; right: 1.1rem; bottom: 4.2rem;
    width: 56px; height: 56px; border-radius: 50%; border: none;
    background: var(--ink); color: var(--paper);
    font-size: 1.9rem; line-height: 1; z-index: 10;
    box-shadow: var(--shadow);
  }}
  html[data-theme="eink"] .reader-close {{ border: 2px solid var(--ink); }}
}}
</style>
</head>
<body>
<div class="sheet">
<header class="masthead">
  <h1>{esc(TITLE)}</h1>
  <p class="tagline">{esc(TAGLINE)}</p>
</header>
<div class="dateline-bar">
  <span>{esc(now)}</span>
  <span id="edition-label">Compiled from RSS feeds</span>
  <span id="updated-label">Updated {esc(generated)} UTC</span>
  <button class="theme-toggle" id="theme-toggle" type="button" aria-label="Switch theme">Theme: Paper</button>
  <button class="theme-toggle" id="font-toggle" type="button" aria-label="Switch font">Font: Classic</button>
  <button class="theme-toggle" id="settings-btn" type="button">Settings</button>
  <input class="search-box" id="search-box" type="search" placeholder="Search stories ( / )" aria-label="Search stories">
</div>
<div id="search-status" role="status"></div>
<div id="sections">
{"".join(body)}
</div>
<footer>Sources: linked feeds &middot; Tap a story to read &middot; Rendered {esc(generated)} UTC</footer>
</div>

<dialog class="reader" id="reader" aria-modal="true">
  <div class="reader-inner">
    <div class="reader-head">
      <button class="reader-close" id="reader-close" aria-label="Close">&times;</button>
      <p class="kicker" id="r-kicker"></p>
      <h2 id="r-title"></h2>
      <p class="dateline" id="r-date"></p>
    </div>
    <div class="reader-body" id="r-body"></div>
    <div class="reader-foot">
      <span>Archived snapshot · {esc(generated)} UTC</span>
      <a id="r-src" href="#" target="_blank" rel="noopener">Read at source &rarr;</a>
    </div>
  </div>
</dialog>

<dialog class="reader" id="settings" aria-modal="true">
  <div class="reader-inner">
    <div class="reader-head">
      <button class="reader-close" id="settings-close" aria-label="Close">&times;</button>
      <p class="kicker">Settings</p>
      <h2>Your edition</h2>
      <p class="dateline">Stored in this browser &middot; each visitor gets their own edition</p>
    </div>
    <div class="reader-body">
      <div id="feed-list"></div>
      <div class="feed-add">
        <input id="feed-name" type="text" placeholder="Section name (e.g. Science)" maxlength="60">
        <input id="feed-url" type="url" placeholder="https://example.com/feed.xml">
        <button id="feed-add" type="button">Add feed</button>
      </div>
      <div class="feed-actions">
        <button id="opml-export" type="button">Export OPML</button>
        <button id="opml-import-btn" type="button">Import OPML</button>
        <input id="opml-import" type="file" accept=".opml,.xml,text/xml" hidden>
        <button id="share-link" type="button">Copy share link</button>
      </div>
      <p class="settings-note">RSS and Atom both work. When a feed or article can&rsquo;t be fetched directly, your browser asks public relays (AllOrigins, then CodeTabs, then Jina Reader for article text) &mdash; those services can see the URLs of your feeds and any articles you open. Editions are capped at 20 feeds. Import replaces the list above; click Save &amp; rebuild to apply. Removing every feed restores the default edition.</p>
    </div>
    <div class="reader-foot">
      <button id="feed-reset" type="button">Reset to defaults</button>
      <button id="feed-save" type="button">Save &amp; rebuild edition</button>
    </div>
  </div>
</dialog>

<script id="articles-json" type="application/json">{data_json}</script>
<script>
(function () {{
  var DATA = JSON.parse(document.getElementById('articles-json').textContent);
  var dlg = document.getElementById('reader');

  /* theme cycling: paper -> dark -> eink */
  var THEMES = ['paper', 'dark', 'eink'];
  var LABELS = {{ paper: 'Paper', dark: 'Dark', eink: 'E-ink' }};
  var toggleBtn = document.getElementById('theme-toggle');
  function currentTheme() {{
    var t = document.documentElement.getAttribute('data-theme');
    return THEMES.indexOf(t) >= 0 ? t : 'paper';
  }}
  function updateToggleLabel() {{
    toggleBtn.textContent = 'Theme: ' + LABELS[currentTheme()];
  }}
  toggleBtn.addEventListener('click', function () {{
    var next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
    if (next === 'paper') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', next);
    try {{ localStorage.setItem('gazette-theme', next); }} catch (e) {{}}
    updateToggleLabel();
  }});
  updateToggleLabel();

  /* font cycling: classic -> serif -> sans -> plex */
  var FONTS = ['classic', 'serif', 'sans', 'plex'];
  var FLABELS = {{ classic: 'Classic', serif: 'Serif', sans: 'Sans', plex: 'Plex' }};
  var fontBtn = document.getElementById('font-toggle');
  function currentFont() {{
    var f = document.documentElement.getAttribute('data-font');
    return FONTS.indexOf(f) >= 0 ? f : 'classic';
  }}
  function updateFontLabel() {{
    fontBtn.textContent = 'Font: ' + FLABELS[currentFont()];
  }}
  fontBtn.addEventListener('click', function () {{
    var next = FONTS[(FONTS.indexOf(currentFont()) + 1) % FONTS.length];
    if (next === 'classic') document.documentElement.removeAttribute('data-font');
    else document.documentElement.setAttribute('data-font', next);
    try {{ localStorage.setItem('gazette-font', next); }} catch (e) {{}}
    updateFontLabel();
  }});
  updateFontLabel();

  /* instant search across the embedded article data */
  var searchBox = document.getElementById('search-box');
  var searchStatus = document.getElementById('search-status');
  var textCache = {{}};
  function articleText(idx) {{
    var a = resolveArticle(idx);
    if (!a) return '';
    var parts = [a.t, a.s];
    if (a.p) for (var i = 0; i < a.p.length; i++) parts.push(a.p[i]);
    if (a.ai) parts.push(a.ai.join(' '));
    return parts.join(' ').toLowerCase();
  }}
  window.__gazetteInvalidateSearch = function (idx) {{ delete textCache[idx]; }};
  window.__gazetteResetSearchCache = function () {{ textCache = {{}}; }};
  function runSearch() {{
    var q = (searchBox.value || '').trim().toLowerCase();
    var total = 0;
    var secs = document.querySelectorAll('#sections > section');
    for (var s = 0; s < secs.length; s++) {{
      var arts = secs[s].querySelectorAll('article');
      var shown = 0;
      for (var i = 0; i < arts.length; i++) {{
        var idx = arts[i].querySelector('.art-link').getAttribute('data-idx');
        var hit = !q || articleText(idx).indexOf(q) >= 0;
        arts[i].style.display = hit ? '' : 'none';
        if (hit) shown++;
      }}
      secs[s].style.display = shown ? '' : 'none';
      total += shown;
    }}
    searchStatus.textContent = q ? (total + ' result' + (total === 1 ? '' : 's') + ' for "' + searchBox.value.trim() + '"') : '';
  }}
  window.__gazetteRunSearch = runSearch;
  searchBox.addEventListener('input', runSearch);
  searchBox.addEventListener('keydown', function (e) {{
    if (e.key === 'Escape') {{ searchBox.value = ''; runSearch(); searchBox.blur(); }}
  }});
  document.addEventListener('keydown', function (e) {{
    if (e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {{
      e.preventDefault();
      searchBox.focus();
    }}
  }});

  var els = {{
    kicker: document.getElementById('r-kicker'),
    title: document.getElementById('r-title'),
    date: document.getElementById('r-date'),
    body: document.getElementById('r-body'),
    src: document.getElementById('r-src')
  }};

  function resolveArticle(idx) {{
    if (typeof window.__gazetteResolve === 'function') {{
      var live = window.__gazetteResolve(idx);
      if (live) return live;
    }}
    return DATA[idx];
  }}

  // scroll position shared with the client-edition script, which has its own
  // open() path — without this, closing a custom-edition reader jumps to top
  function saveScroll() {{ window.__gazetteScrollY = window.scrollY; }}
  function restoreScroll() {{ window.scrollTo(0, window.__gazetteScrollY || 0); }}

  function open(idx) {{
    var a = resolveArticle(idx);
    if (!a) return;
    if (a.live && typeof window.__gazetteOpen === 'function') {{ window.__gazetteOpen(idx); return; }}
    saveScroll();
    els.kicker.textContent = a.p.length >= 3 ? 'Full story' : (a.p.length ? 'Partial story' : 'Summary');
    els.title.textContent = a.t;
    els.date.textContent = a.d;
    els.src.href = a.u;
    els.body.innerHTML = '';
    if (a.ai && a.ai.length) {{
      var ai = document.createElement('p');
      ai.className = 'also-in';
      ai.textContent = 'Also in: ' + a.ai.join(', ');
      els.body.appendChild(ai);
    }}
    var paras = a.p.length ? a.p : [a.s];
    paras.forEach(function (p) {{
      var el = document.createElement('p');
      el.textContent = p;
      els.body.appendChild(el);
    }});
    if (typeof dlg.showModal === 'function') {{
      dlg.showModal();
      // some mobile browsers scroll the page when the modal grabs focus
      restoreScroll();
    }} else {{
      dlg.setAttribute('open', '');
    }}
    els.body.scrollTop = 0;
  }}

  function close() {{
    if (typeof dlg.close === 'function' && dlg.open) dlg.close();
    else dlg.removeAttribute('open');
    restoreScroll();
  }}

  document.addEventListener('click', function (e) {{
    // let modified clicks and non-left clicks through (cmd-click, middle-click, etc.)
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var link = e.target.closest('.art-link');
    if (link) {{
      e.preventDefault();
      open(link.getAttribute('data-idx'));
      return;
    }}
    var exc = e.target.closest('.excerpt');
    if (exc && !e.target.closest('a')) {{
      e.preventDefault();
      open(exc.getAttribute('data-idx'));
    }}
  }});

  document.getElementById('reader-close').addEventListener('click', close);
  dlg.addEventListener('click', function (e) {{
    if (e.target === dlg) close();  // backdrop tap
  }});
  // Esc closes natively via dialog; nothing extra needed

  /* offline: cache the edition for the next visit */
  if ('serviceWorker' in navigator) {{
    try {{ navigator.serviceWorker.register('./sw.js'); }} catch (e) {{}}
  }}
}})();
</script>
{feeds_script}
</body>
</html>'''

def dedupe_keys(a):
    """All stable identities for a story: normalized link path and/or long title."""
    keys = []
    link = (a.get("link") or "").strip()
    m = re.match(r"^https?://([^/?#]+)([^?#]*)", link, re.I)
    if m:
        host = m.group(1).lower()
        path = m.group(2).rstrip("/").lower()
        if path:
            keys.append(host + path)
    t = re.sub(r"\s+", " ", (a.get("title") or "")).strip().lower()
    # short titles are too generic to dedupe on safely
    if len(t) >= 25:
        keys.append(t)
    return keys

def dedupe_sections(sections):
    """Drop stories already seen in an earlier section, recording cross-feed coverage
    on the first occurrence as article["also_in"] = [section names]. Mutates in place."""
    seen = {}  # key -> first article that claimed it
    removed = 0
    for sec in sections:
        kept = []
        for a in sec["items"]:
            ks = dedupe_keys(a)
            first = None
            for k in ks:
                if k in seen:
                    first = seen[k]
                    break
            if first is not None:
                removed += 1
                if sec["name"] not in first.setdefault("also_in", []):
                    first.setdefault("also_in", []).append(sec["name"])
                continue
            for k in ks:
                seen[k] = a
            kept.append(a)
        sec["items"] = kept
    return removed

def collect_sections():
    """Fetch and parse every feed. A dead feed becomes an error entry, not a crash."""
    out = []
    for name, url in FEEDS:
        try:
            items = parse_feed(url)
        except Exception as e:
            print(f"feed error: {name} ({url}): {e}")
            out.append({"name": name, "url": url, "items": [], "error": str(e)})
            continue
        if not items:
            print(f"feed error: {name} ({url}): feed returned no items")
            out.append({"name": name, "url": url, "items": [], "error": "feed returned no items"})
            continue
        print(f"{name}: {len(items)} items")
        out.append({"name": name, "url": url, "items": items, "error": None})
    removed = dedupe_sections(out)
    return out, removed

def main():
    sections, removed = collect_sections()
    if removed:
        print(f"dedupe: removed {removed} duplicate stories")
    idx = 0
    jobs = []
    for sec in sections:
        for a in sec["items"]:
            a["idx"] = idx
            idx += 1
            jobs.append(a)

    with ThreadPoolExecutor(max_workers=8) as ex:
        paras_list = list(ex.map(lambda a: fetch_fulltext(a["link"]), jobs))
    got = 0
    for a, paras in zip(jobs, paras_list):
        a["paras"] = paras
        if paras:
            got += 1
    print(f"full text: {got}/{len(jobs)} articles")

    generated = datetime.now(timezone.utc).strftime("%H:%M")
    (OUT).mkdir(parents=True, exist_ok=True)
    (OUT / "index.html").write_text(render(sections, generated), encoding="utf-8")
    sw_version = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    (OUT / "sw.js").write_text(SW_JS.replace("__VERSION__", sw_version), encoding="utf-8")
    print(f"wrote {OUT / 'index.html'} + sw.js")

if __name__ == "__main__":
    main()
