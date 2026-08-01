# Kinsho

Self-hosted manga/comic reader.

Kinsho is a self-hosted manga/comic reader built for people who want to
serve their own library — CBZ/CBR archives, PDFs, EPUBs, or plain folders of
images — without a database, without an account with a third party, and
without giving up control of their own files. The backend is a single
FastAPI application; the frontend is plain JavaScript. Everything it stores
about your library — reading progress, tags, collections, users — lives in
plain JSON files under a data folder you choose.

## Features

- **Reads almost anything**: CBZ/ZIP out of the box, plus optional CBR/RAR,
  PDF, and EPUB support (EPUBs get a real paginated text reader, not just
  their embedded images).
- **No database** — your whole library's metadata lives in JSON files you
  can read, back up, or edit by hand.
- **Metadata fetching** from AniList and MangaDex, plus automatic reading of
  `ComicInfo.xml` sidecars (the same convention Komga/Kavita/ComicRack use).
- **Collections** — group manga across libraries under one name, shared
  (visible to everyone) or private, with per-viewer cover overrides.
- **Themes** — a built-in accent-color system plus a Default/Sharp/Custom
  visual theme system with a live CSS editor for full customization.
- **OPDS + OPDS-PSE catalog**, so OPDS-aware readers (Chunky, KOReader) can
  browse and stream your library directly, page by page.
- **Background integrity checking** — flags corrupt archives and duplicate
  pages (including near-duplicates via perceptual hashing + SSIM) during
  idle periods, surfaced to admins with one-click rechecking.
- **Multi-user with real permission boundaries** — per-library visibility
  and per-tag blocking for restricted accounts, enforced on every route.
- **Long-strip and single-page reading modes**, tuned for both webtoon-style
  scrolling and traditional page-by-page manga/comics.

## Running it

```bash
pip install -r requirements.txt
python main.py
```

This starts a server on port 8000 and prints the LAN IP it's reachable at.
The first run auto-creates an `admin`/`admin` account (you'll be required to
change the password immediately).

### Docker

```bash
docker build -t kinsho .
docker run -p 8000:8000 -v /path/to/data:/data kinsho
```

No build step beyond installing dependencies — there's no bundler, no
package manager beyond pip.

### Optional format support

| Format | Needs | Install |
|---|---|---|
| CBZ / ZIP | nothing extra | included |
| CBR / RAR | `rarfile` + `unrar` binary | `pip install rarfile` |
| PDF | `pymupdf` | already in requirements.txt |
| EPUB | `ebooklib` | already in requirements.txt |

Missing optional libraries degrade gracefully — Kinsho just skips files it
can't read and logs why.

## License

Kinsho is free to use, modify, and self-host for personal or non-commercial
purposes. Commercial use is allowed, but requires contacting the author
first to arrange terms. See [LICENSE.md](LICENSE.md) for the full text.

Questions about a specific use case: monkeyddarko@gmail.com.
