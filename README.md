# Kinsho

A self-hosted manga, webtoon and comic reader for your own library.

> **Beta**: Kinsho is in active development and gets frequent updates.

> **Built first for loose image folders**: Kinsho was designed around manga and
> webtoons stored as plain folders of images, and that is where it is fastest. CBZ/CBR
> archives, PDFs and EPUBs work too.

Kinsho serves your own files to any browser on your network: phone, tablet or PC. There
is no database and no third-party account. Everything it stores (users, reading progress,
tags, collections) is plain JSON in a folder you choose, easy to back up or edit by hand.

## Support

Kinsho is free to use. If it's useful to you, consider
[supporting development on Ko-fi](https://ko-fi.com/kalako99).

## Features

- **Two readers, tuned for long reading sessions**: a long-strip reader for webtoons that
  stays smooth through thousands of pages, and a single-page reader with swipes, taps or
  keys. Both flow from one chapter into the next without reloading.
- **Reads almost anything**: plain image folders, CBZ/ZIP, CBR/RAR, PDF and EPUB. EPUB
  novels get a real text reader, not just their pictures. Kinsho works out on its own
  whether a folder holds chapters or volumes.
- **Reading statistics**: reading time per manga, where you are in the whole series, the
  average time per chapter, and a reading graph by month and year.
- **Progress that follows you**: continue on any device from the exact page, with
  bookmarks, favourites and a Last Read row.
- **Metadata**: descriptions, genres, tags and covers from AniList and MangaDex, or from
  `ComicInfo.xml` files (the Komga / Kavita / ComicRack convention). Your own edits are
  kept.
- **Collections**: group manga, even across libraries, shared with everyone or private.
- **Multi-user with real permissions**: admin-created accounts, per-user library access
  and blocked tags, checked on every request.
- **Library tools**: automatic rescans, background integrity checks for corrupt files,
  optional conversion of archives/PDFs to image folders, volume-name cleanup, and oneshot
  libraries.
- **Themes**: accent colours, a polished default theme, or your own CSS in a live editor.
- **OPDS catalog**: browse and read your library from OPDS apps like Chunky and KOReader.
- **Bluetooth scroller** *(experimental)*: hands-free scrolling with a small DIY device.
- **No database**: one Python app or one Docker container.

## Install with Docker (recommended)

A ready-made image is published for `amd64` and `arm64` (e.g. Raspberry Pi 4/5):

```bash
docker run -d --name kinsho --restart unless-stopped \
  --user 1000:1000 \
  -p 8088:8000 \
  -v /path/to/kinsho-data:/data \
  -v /path/to/your/manga:/manga \
  ghcr.io/kalako99/kinsho:latest
```

Or download [`docker-compose.yml`](docker-compose.yml), set your manga folder in it,
and run `docker compose up -d`.

- `/data` holds Kinsho's own data (users, progress, covers). Put it on a drive you back
  up, and make sure it belongs to the user in `--user` (`id -u` / `id -g` show yours).
- `/manga` is your library. Mount as many folders as you like (for example
  `-v /mnt/disk2/comics:/comics`); you choose which ones become libraries in Kinsho's
  Settings.

Then open `http://<your-server>:8088`, log in as `admin` / `admin` (you'll be asked to
change the password right away), and go to **Settings → Libraries** to add your
folders.

**Updating:** `docker pull ghcr.io/kalako99/kinsho:latest` and recreate the container (or
`docker compose pull && docker compose up -d`). Your data in `/data` is untouched.

## Install without Docker

```bash
pip install -r requirements.txt
python main.py
```

This starts the server on port 8000 and prints the LAN address it can be reached at. The
first run creates the `admin` / `admin` account. For CBR/RAR files also install the `unrar`
program (included in the Docker image).

## File formats

| Format | Support |
|---|---|
| Folders of images (JPG, PNG, WEBP, ...) | built in |
| CBZ / ZIP | built in |
| CBR / RAR | built in with Docker; needs `unrar` otherwise |
| PDF | built in |
| EPUB | built in (text reader for novels, image reader for comics) |

If a format's library is missing, Kinsho skips those files and logs why.

## License

Kinsho is free to use, modify and self-host for personal or non-commercial purposes.
Commercial use is allowed, but requires contacting the author first to arrange terms. See
[LICENSE.md](LICENSE.md) for the full text.

Questions about a specific use case: monkeyddarko@gmail.com.
