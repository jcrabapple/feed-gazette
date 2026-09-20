# Feed Gazette

A single-file RSS-to-newspaper generator. Point it at any RSS or Atom feeds and it
renders a broadsheet-style static page: serif masthead, three-column layout, section
rules, and a reader-mode popup for every story.

**Live demo:** https://waxen-thistle-ckbr.here.now/

No framework, no dependencies, no build tooling. One Python script (stdlib only),
one output HTML file you can host anywhere static files are served.

## Features

- **Newspaper layout** — masthead, double rules, three text columns with column
  rules, lead-story treatment per section. Collapses to a single column on mobile.
- **Reader-mode popup** — tapping a story opens the full article text in an overlay.
  Full text is extracted from each article at build time (BBC-style pages) or
  lazily in the browser (visitor-configured feeds). The page behind the popup
  keeps its scroll position.
- **Per-visitor feed settings** — a Settings panel lets any visitor add their own
  RSS/Atom feeds (stored in their browser, not the server), turning the page into
  a personal RSS reader without touching anyone else's edition.
- **Three themes** — Paper (default), Dark, and E-ink (pure black/white, grayscale
  images, no shadows). User choice persists in localStorage and applies pre-paint.
- **Full text at build time** — article bodies are pulled and embedded when the
  page is built, so the reader opens instantly with no network round-trip.

## Quick start

```bash
python3 build.py
# -> site/index.html — open it in a browser or upload it to any static host
```

Requires Python 3.10+. Nothing else.

## Configuring feeds

By default the page builds from two BBC News feeds. To use your own, create a
`feeds.json` next to `build.py` (see `feeds.example.json`):

```json
[
  {"name": "World", "url": "https://example.org/world.xml"},
  {"name": "Tech", "url": "https://example.org/tech.xml"}
]
```

Run `python3 build.py` again and each entry becomes a newspaper section. To change
the masthead, edit `TITLE` and `TAGLINE` at the top of `build.py`.

## How full-text extraction works

Build-time extraction targets BBC News article markup (paragraphs inside `<main>`).
For other feeds, full text depends on the source site: cleanly structured article
pages extract fine; JS-only pages fall back to the feed summary. The reader popup
labels which you're getting ("Full story" vs "Summary") and always links to the
source.

Visitor-added feeds (via the Settings panel) are fetched in the browser through a
public CORS relay ([allorigins](https://allorigins.win)), since most feeds don't
send CORS headers. Sites that block the relay show a visible error in their
section.

## Refreshing on a schedule

The page is a snapshot of whenever you last ran `build.py`. To keep it fresh, run
it on a cron and upload `site/` to your static host. For example, with
[here.now](https://here.now) (any static host with a CLI works the same way):

```bash
python3 build.py && your-publish-command site/
```

## License

[MIT](LICENSE)
