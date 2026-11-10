"""Watches library folders for changes and asks for a scan (like Jellyfin's
real-time monitoring), using Linux inotify directly (no extra package).

What is watched: each library path and every folder directly inside it (the
manga folders). That catches a new manga, and a new/renamed/removed chapter
or volume (folder or file) inside a manga, which is what matters; changes
deeper down (pages inside a chapter folder) are left to manual/periodic scans.
Watching every folder would need ~46,000 inotify watches on the NAS, over the
per-user limit (60,132, shared with Jellyfin which runs as the same uid); this
needs ~700 for both instances. Measured 2026-11-10; a copy made over the SMB
share from a PC is reported inside the container.

Only write events are asked for (no open/read events), so reading manga never
triggers anything. A library's changes are collected until SETTLE_SECONDS
pass without a new one, then on_change(library_id, path) is called; when it
returns False (that library is scanning right now) it is asked again later.
"""
import ctypes
import ctypes.util
import os
import re
import select
import struct
import threading
import time

IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM  = 0x00000040
IN_MOVED_TO    = 0x00000080
IN_CREATE      = 0x00000100
IN_DELETE      = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF   = 0x00000800
IN_Q_OVERFLOW  = 0x00004000
IN_IGNORED     = 0x00008000
IN_ONLYDIR     = 0x01000000
IN_ISDIR       = 0x40000000

WATCH_MASK = (IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
              | IN_DELETE_SELF | IN_MOVE_SELF | IN_ONLYDIR)

SETTLE_SECONDS = 30          # quiet time before a library's changes are handed on
FULL_SYNC_SECONDS = 30 * 60  # re-list manga folders now and then (missed adds/moves)

# Files Kinsho writes into manga folders itself: Save metadata to ComicInfo.xml,
# and a Fetch Metadata cover (main.fetch_and_set_cover: "{provider}_{id}.ext",
# written through ".{name}.kinsho-tmp"; also deleted from the cover picker).
# Neither needs a scan: both move the manga's saved folder date along, so Last
# Updated doesn't change, and skipping them saves a whole-library scan per fetch.
_OWN_FILE_RE = re.compile(
    r"^(?:ComicInfo\.xml"
    r"|\..*\.kinsho-tmp"
    r"|(?:(?:anilist|mangadex|mangaupdates|googlebooks|openlibrary)_[A-Za-z0-9_-]+|metadata_cover_\d+)"
    r"\.(?:jpg|jpeg|png|webp))$")

_EVENT = struct.Struct("iIII")


class LibraryWatcher:
    def __init__(self, get_libraries, on_change, is_enabled):
        """get_libraries() -> list of library dicts (id, paths); on_change(id, path)
        -> bool; is_enabled() -> bool (switch in Settings)."""
        self.get_libraries = get_libraries
        self.on_change = on_change
        self.is_enabled = is_enabled
        self.fd = -1
        self.libc = None
        self.wds = {}     # wd -> (library_id, path, depth 0 = library path, 1 = manga folder)
        self.by_path = {}  # path -> wd
        self.dirty = {}   # library_id -> [last event time, a changed path]
        self.limit_warned = False
        self.status = "off"

    # ── setup ──
    def start(self):
        threading.Thread(target=self._run, name="library-watch", daemon=True).start()

    def _open(self) -> bool:
        if not hasattr(os, "O_NONBLOCK"):
            self.status = "not available on this system"
            print("[Watch] Folder watching needs Linux (inotify); not available here.")
            return False
        try:
            self.libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        except (OSError, AttributeError) as e:
            self.status = "not available on this system"
            print(f"[Watch] inotify not available: {e}")
            return False
        if self.fd < 0:
            self.status = f"inotify error {ctypes.get_errno()}"
            print(f"[Watch] inotify_init1 failed (errno {ctypes.get_errno()}).")
            return False
        return True

    def _add(self, path: str, library_id, depth: int):
        wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path), WATCH_MASK)
        if wd < 0:
            err = ctypes.get_errno()
            if err == 28 and not self.limit_warned:  # ENOSPC: max_user_watches reached
                self.limit_warned = True
                print("[Watch] The system's inotify watch limit is reached "
                      "(fs.inotify.max_user_watches); some folders are not watched.")
            return
        self.wds[wd] = (library_id, path, depth)
        self.by_path[path] = wd

    def _remove(self, path: str):
        wd = self.by_path.pop(path, None)
        if wd is not None:
            self.wds.pop(wd, None)
            self.libc.inotify_rm_watch(self.fd, wd)

    def _wanted(self) -> dict:
        """path -> (library_id, depth) for every folder that should be watched."""
        wanted = {}
        if not self.is_enabled():
            return wanted
        for lib in self.get_libraries():
            for root in lib.get("paths", []) or []:
                if not root or not os.path.isdir(root):
                    continue
                root = os.path.normpath(root)
                wanted[root] = (lib.get("id"), 0)
                try:
                    with os.scandir(root) as it:
                        for e in it:
                            if e.is_dir(follow_symlinks=False):
                                wanted.setdefault(os.path.normpath(e.path), (lib.get("id"), 1))
                except OSError:
                    pass
        return wanted

    def _sync(self):
        wanted = self._wanted()
        for path in [p for p in self.by_path if p not in wanted]:
            self._remove(path)
        for path, (lib_id, depth) in wanted.items():
            wd = self.by_path.get(path)
            if wd is None or self.wds.get(wd, (None,))[0] != lib_id:
                self._add(path, lib_id, depth)
        self.status = f"watching {len(self.by_path)} folders" if self.by_path else "off"
        if self.by_path:
            print(f"[Watch] Watching {len(self.by_path)} library and manga folders.")

    @staticmethod
    def _signature(libraries, enabled) -> tuple:
        return (enabled,) + tuple(sorted((str(l.get("id")), tuple(l.get("paths", []) or []))
                                         for l in libraries))

    # ── loop ──
    def _run(self):
        if not self._open():
            return
        sig = None
        next_full = 0.0
        while True:
            try:
                now = time.time()
                new_sig = self._signature(self.get_libraries(), self.is_enabled())
                if new_sig != sig or now >= next_full:
                    sig = new_sig
                    next_full = now + FULL_SYNC_SECONDS
                    self._sync()
                ready, _, _ = select.select([self.fd], [], [], 1 if self.dirty else 5)
                if ready:
                    self._read_events()
                self._flush()
            except Exception as e:
                print(f"[Watch] error: {e!r}")
                time.sleep(5)

    def _read_events(self):
        try:
            buf = os.read(self.fd, 64 * 1024)
        except BlockingIOError:
            return
        i = 0
        now = time.time()
        while i + _EVENT.size <= len(buf):
            wd, mask, _cookie, ln = _EVENT.unpack_from(buf, i)
            name = buf[i + _EVENT.size:i + _EVENT.size + ln].rstrip(b"\0").decode("utf-8", "replace")
            i += _EVENT.size + ln
            if mask & IN_Q_OVERFLOW:
                # Events were lost: every watched library may have changed.
                for lib_id, _p, _d in set(self.wds.values()):
                    self.dirty[lib_id] = [now, "(event queue overflow)"]
                continue
            info = self.wds.get(wd)
            if info is None:
                continue
            lib_id, path, depth = info
            if mask & IN_IGNORED:  # the folder is gone (deleted / unmounted)
                self.wds.pop(wd, None)
                if self.by_path.get(path) == wd:
                    self.by_path.pop(path, None)
                continue
            if name and not mask & IN_ISDIR and _OWN_FILE_RE.match(name):
                continue
            full = os.path.join(path, name) if name else path
            if depth == 0 and mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO):
                self._add(os.path.normpath(full), lib_id, 1)  # a new manga folder
            elif depth == 0 and mask & IN_ISDIR and mask & IN_MOVED_FROM:
                self._remove(os.path.normpath(full))
            self.dirty[lib_id] = [now, full]

    def _flush(self):
        now = time.time()
        for lib_id, (last, path) in list(self.dirty.items()):
            if now - last < SETTLE_SECONDS:
                continue
            if not self.is_enabled():
                self.dirty.pop(lib_id, None)
                continue
            if self.on_change(lib_id, path):
                self.dirty.pop(lib_id, None)
            else:
                self.dirty[lib_id] = [now, path]  # busy: ask again after another settle
