"""
auth.py — KINSHO authentication layer
Handles: users.json, sessions.json, login, admin-only account creation,
         session validation, change password, and per-user data file helpers.
"""

import json
import os
import re
import secrets
import uuid
import hashlib
import hmac
import base64
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

# ── CONFIG ──────────────────────────────────────────────────────────────────

SESSION_DURATION_DAYS = 30
COOKIE_NAME           = "kinsho_session"

# Usernames become JSON filenames (`{username}.json`) under data_path, so the
# charset is restricted to what's safe there and the reserved set covers
# every fixed JSON filename data_path already uses.
VALID_USERNAME_RE  = re.compile(r'^[a-z0-9_-]{3,32}$')
RESERVED_USERNAMES = {"data", "bootstrap", "sessions", "users", "permissions"}

# ── FILE HELPERS ─────────────────────────────────────────────────────────────

def _get_data_path() -> Optional[str]:
    if not os.path.exists("bootstrap.json"):
        return None
    with open("bootstrap.json", "r") as f:
        return json.load(f).get("data_path")


def _users_file() -> str:
    data_path = _get_data_path()
    if data_path:
        return os.path.join(data_path, "users.json")
    return "users.json"


def _sessions_file() -> str:
    data_path = _get_data_path()
    if data_path:
        return os.path.join(data_path, "sessions.json")
    return "sessions.json"


def _user_data_file(username: str) -> Optional[str]:
    """Path to [username].json — the per-user equivalent of admin.json."""
    data_path = _get_data_path()
    if not data_path:
        return None
    return os.path.join(data_path, f"{username}.json")


# ── USERS.JSON ───────────────────────────────────────────────────────────────

def _load_users() -> dict:
    path = _users_file()
    if not os.path.exists(path):
        return {"users": []}
    with open(path, "r") as f:
        return json.load(f)


def _save_users(data: dict):
    path = _users_file()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# PBKDF2-HMAC-SHA256 with a random per-user salt, per OWASP's current
# minimum iteration count for that algorithm. Stored as
# "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>" so the iteration count
# travels with the hash and can be raised later without breaking old rows.
_PBKDF2_ITERATIONS = 600_000


def _pbkdf2(password: str, salt_hex: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), iterations).hex()


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = _pbkdf2(password, salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest}"


def _hash_password_legacy(password: str) -> str:
    """
    The original single-round HMAC-SHA256 with one hardcoded global salt.
    Kept only to verify accounts created before the PBKDF2 migration —
    _verify_password upgrades them to the new format on next successful login.
    """
    return hmac.new(b"kinsho_salt_v1", password.encode(), digestmod=hashlib.sha256).hexdigest()


def _verify_password(stored_hash: str, password: str) -> bool:
    if stored_hash.startswith("pbkdf2_sha256$"):
        try:
            _, iterations, salt, expected = stored_hash.split("$")
            candidate = _pbkdf2(password, salt, int(iterations))
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(candidate, expected)
    return hmac.compare_digest(_hash_password_legacy(password), stored_hash)


def _find_user(username: str) -> Optional[dict]:
    data = _load_users()
    return next((u for u in data["users"] if u["username"] == username), None)


def _ensure_admin_exists():
    """Create the default admin account if no users exist yet."""
    data = _load_users()
    if not data["users"]:
        admin = {
            "username":            "admin",
            "password_hash":       _hash_password("admin"),
            "role":                "admin",
            "allowed_tabs":        None,
            "created_at":          datetime.now().isoformat(),
            "must_change_password": True,
        }
        data["users"].append(admin)
        _save_users(data)
        print("[Auth] Default admin account created (password: admin). "
              "You will be required to change it on first login.")

        admin_file = _user_data_file("admin")
        if admin_file and not os.path.exists(admin_file):
            os.makedirs(os.path.dirname(os.path.abspath(admin_file)), exist_ok=True)
            with open(admin_file, "w") as f:
                json.dump({"is_admin": True}, f, indent=2)


# ── SESSIONS.JSON ─────────────────────────────────────────────────────────────

def _load_sessions() -> dict:
    path = _sessions_file()
    if not os.path.exists(path):
        return {"sessions": {}}
    with open(path, "r") as f:
        return json.load(f)


def _save_sessions(data: dict):
    path = _sessions_file()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _create_session(username: str) -> str:
    token   = str(uuid.uuid4())
    expires = (datetime.now() + timedelta(days=SESSION_DURATION_DAYS)).isoformat()
    data    = _load_sessions()
    now     = datetime.now().isoformat()
    data["sessions"] = {
        t: s for t, s in data["sessions"].items()
        if s.get("expires_at", "") > now
    }
    data["sessions"][token] = {
        "username":   username,
        "created_at": datetime.now().isoformat(),
        "expires_at": expires,
    }
    _save_sessions(data)
    return token


def _resolve_session(token: str) -> Optional[str]:
    if not token:
        return None
    data    = _load_sessions()
    session = data["sessions"].get(token)
    if not session:
        return None
    if session.get("expires_at", "") < datetime.now().isoformat():
        del data["sessions"][token]
        _save_sessions(data)
        return None
    return session["username"]


def _delete_session(token: str):
    data = _load_sessions()
    data["sessions"].pop(token, None)
    _save_sessions(data)


def _delete_sessions_for_user(username: str, except_token: Optional[str] = None):
    """Remove every session belonging to this user, optionally keeping one token alive."""
    data = _load_sessions()
    data["sessions"] = {
        t: s for t, s in data["sessions"].items()
        if s.get("username") != username or t == except_token
    }
    _save_sessions(data)


def _cookie_name_for_request(request: Request) -> str:
    """
    Session cookie name, suffixed with the port the client addressed us on.

    Browsers scope cookies to the hostname only — ports are ignored — so two
    Kinsho instances on the same host (e.g. :8098 and :8099 on one home
    server) would otherwise fight over a single `kinsho_session` cookie:
    logging into one instance clobbers the other's cookie with a token it
    doesn't recognize, which surfaces as "it randomly logs me out".
    Suffixing the name with the port gives each instance its own cookie.
    """
    host = request.headers.get("host", "")
    if ":" in host:
        port = host.rsplit(":", 1)[1]
        if port.isdigit():
            return f"{COOKIE_NAME}_{port}"
    return COOKIE_NAME


def _get_request_token(request: Request) -> Optional[str]:
    # Bare COOKIE_NAME fallback keeps sessions from before the per-port
    # naming alive until they naturally expire or re-login.
    return (request.headers.get('X-Auth-Token')
            or request.cookies.get(_cookie_name_for_request(request))
            or request.cookies.get(COOKIE_NAME))


# ── REQUEST HELPERS ──────────────────────────────────────────────────────────

def get_current_user(request: Request) -> Optional[str]:
    token = _get_request_token(request)
    return _resolve_session(token) if token else None


def get_user_from_basic_auth(request: Request) -> Optional[str]:
    """
    Verify HTTP Basic credentials against users.json — used only by the OPDS
    routes, since practically every OPDS client (Chunky, Panels, KOReader,
    Mihon's generic OPDS source) only knows how to authenticate that way, not
    via the session cookie / X-Auth-Token the rest of the app uses.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return None
    username = username.strip().lower()
    user = _find_user(username)
    if not user or not _verify_password(user["password_hash"], password):
        return None
    return username


def get_opds_user(request: Request) -> Optional[str]:
    """Session cookie/token first (previewing in a browser), Basic Auth fallback (every real OPDS client)."""
    return get_current_user(request) or get_user_from_basic_auth(request)


def require_user(request: Request) -> Optional[JSONResponse]:
    if get_current_user(request) is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return None


def require_admin(request: Request) -> Optional[JSONResponse]:
    username = get_current_user(request)
    if username is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    user = _find_user(username)
    if not user or user.get("role") != "admin":
        return JSONResponse({"error": "Admin access required"}, status_code=403)
    return None


# ── PER-USER DATA FILE ────────────────────────────────────────────────────────

def load_user_data(username: str) -> dict:
    path = _user_data_file(username)
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def save_user_data(username: str, data: dict):
    path = _user_data_file(username)
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def get_allowed_tabs(username: str) -> Optional[list]:
    user = _find_user(username)
    if not user:
        return []
    return user.get("allowed_tabs")

# ── READING SESSION TRACKING ─────────────────────────────────────────────────

def append_reading_session(username: str, manga_name: str, library_id: str):
    """Append a new reading session entry to [username].json."""
    data     = load_user_data(username)
    sessions = data.get("reading_sessions", [])
    sessions.append({
        "start":      datetime.now().isoformat(timespec="seconds"),
        "manga_name": manga_name,
        "library_id": str(library_id),
        "minutes":    0,
    })
    data["reading_sessions"] = sessions
    save_user_data(username, data)


def tick_reading_session(username: str):
    """Increment the minutes counter on the most recent session entry by 1."""
    data     = load_user_data(username)
    sessions = data.get("reading_sessions", [])
    if sessions:
        sessions[-1]["minutes"] = sessions[-1].get("minutes", 0) + 1
        data["reading_sessions"] = sessions
        save_user_data(username, data)

# ── PERMISSIONS ──────────────────────────────────────────────────────────────

def _permissions_file() -> Optional[str]:
    """Path to permissions.json — stores per-user and _default permissions."""
    data_path = _get_data_path()
    if not data_path:
        return None
    return os.path.join(data_path, "permissions.json")


def load_permissions() -> dict:
    path = _permissions_file()
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_permissions(data: dict):
    path = _permissions_file()
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


_DEFAULT_PERMISSIONS: dict = {
    "tags":            True,
    "genres":          True,
    "description":     True,
    # Separate from tags/genres/description on purpose -- lets an admin
    # allow manual tag/genre/description edits while still blocking the
    # AniList/MangaDex Fetch Metadata popup (or the reverse), rather than
    # that popup's access being an implicit side effect of the other three.
    "metadata_fetch":  True,
    "libraries":    {},
    "blocked_tags": [],
}


def resolve_permissions(username: str) -> dict:
    """
    Return fully-resolved permissions for a user.
    Admins always get all permissions.
    Regular users get their explicit entry, or fall back to _default.
    Fails closed (deny everything) if permissions.json is unreadable.
    """
    user = _find_user(username)
    if user and user.get("role") == "admin":
        return {
            "tags": True, "genres": True, "description": True, "metadata_fetch": True,
            "libraries": {}, "is_admin": True,
        }
    try:
        data = load_permissions()
    except Exception:
        return {
            "tags": False, "genres": False, "description": False, "metadata_fetch": False,
            "libraries": {}, "is_admin": False,
        }
    default    = data.get("_default", _DEFAULT_PERMISSIONS.copy())
    user_entry = data.get(username)
    perms      = (default if user_entry is None else user_entry).copy()
    perms.setdefault("libraries", {})
    perms.setdefault("blocked_tags", [])
    # True, not False, if missing -- an existing permissions.json saved
    # before this key existed had Fetch Metadata implicitly available
    # (gated only by tags/genres/description), same opt-out convention as
    # can_access_library ("only an explicit False denies"). Only the
    # unreadable-file branch above fails closed, matching how tags/genres/
    # description already behave there.
    perms.setdefault("metadata_fetch", True)
    perms["is_admin"] = False
    return perms


def can_access_library(username: str, library_id) -> bool:
    """
    Whether this user is allowed to see the given library.
    Admins always can. Absence of an explicit entry means allowed (permissions
    are opt-out, matching the tab-list behavior in manga_list()) — only an
    explicit `False` denies.
    """
    perms = resolve_permissions(username)
    if perms.get("is_admin"):
        return True
    return perms.get("libraries", {}).get(str(library_id), True) is not False


def is_manga_blocked(username: str, tags) -> bool:
    """
    Whether this manga is off-limits to this user because it carries one of
    their blocked tags. Admins are never blocked.
    """
    perms = resolve_permissions(username)
    if perms.get("is_admin"):
        return False
    blocked = perms.get("blocked_tags", [])
    if not blocked:
        return False
    return any(t in blocked for t in (tags or []))


def must_change_password(username: str) -> bool:
    user = _find_user(username)
    return bool(user and user.get("must_change_password"))

# ── AUTH ROUTES ──────────────────────────────────────────────────────────────

async def route_login(request: Request):
    _ensure_admin_exists()
    body     = await request.json()
    username = body.get("username", "").strip().lower()
    password = body.get("password", "")

    if not username or not password:
        return JSONResponse({"ok": False, "error": "Username and password required."}, status_code=400)

    user = _find_user(username)
    if not user or not _verify_password(user["password_hash"], password):
        return JSONResponse({"ok": False, "error": "Invalid username or password."}, status_code=401)

    # Transparently migrate accounts still on the legacy hash format.
    if not user["password_hash"].startswith("pbkdf2_sha256$"):
        data = _load_users()
        stored = next(u for u in data["users"] if u["username"] == username)
        stored["password_hash"] = _hash_password(password)
        _save_users(data)

    token    = _create_session(username)
    response = JSONResponse({
        "ok": True, "username": username, "role": user["role"], "token": token,
        "must_change_password": bool(user.get("must_change_password")),
    })
    response.set_cookie(
        key=_cookie_name_for_request(request), value=token,
        httponly=True, samesite="lax",
        max_age=SESSION_DURATION_DAYS * 86400,
    )
    response.headers["X-Auth-Token"] = token
    return response


async def route_admin_create_user(request: Request):
    """POST /api/admin/users — admin-only account creation. { username, password, role }"""
    err = require_admin(request)
    if err:
        return err

    body     = await request.json()
    username = body.get("username", "").strip().lower()
    password = body.get("password", "")
    role     = body.get("role", "user")
    if role not in ("user", "admin"):
        role = "user"

    if not username or not password:
        return JSONResponse({"ok": False, "error": "Username and password required."}, status_code=400)
    if len(password) < 4:
        return JSONResponse({"ok": False, "error": "Password must be at least 4 characters."}, status_code=400)
    if not VALID_USERNAME_RE.match(username):
        return JSONResponse({"ok": False, "error": "Username must be 3-32 characters: lowercase letters, numbers, underscore, or hyphen only."}, status_code=400)
    if username in RESERVED_USERNAMES:
        return JSONResponse({"ok": False, "error": "That username is reserved."}, status_code=400)
    if _find_user(username):
        return JSONResponse({"ok": False, "error": "Username already taken."}, status_code=409)

    new_user = {
        "username":      username,
        "password_hash": _hash_password(password),
        "role":          role,
        "allowed_tabs":  None,
        "created_at":    datetime.now().isoformat(),
    }
    data = _load_users()
    data["users"].append(new_user)
    _save_users(data)

    new_user_data = {"is_admin": role == "admin"}
    admin_covers = load_user_data(get_current_user(request)).get("covers")
    if admin_covers:
        new_user_data["covers"] = admin_covers
    save_user_data(username, new_user_data)

    perms_data    = load_permissions()
    default_perms = perms_data.get("_default", _DEFAULT_PERMISSIONS.copy())
    perms_data[username] = {k: v for k, v in default_perms.items()}
    save_permissions(perms_data)

    return JSONResponse({"ok": True, "username": username, "role": role})


async def route_admin_delete_user(request: Request, username: str):
    """DELETE /api/admin/users/{username} — permanently removes the account,
    its sessions, its permissions entry, and its per-user data file (reading
    history, favourites, private collections, etc.)."""
    err = require_admin(request)
    if err:
        return err
    current_admin = get_current_user(request)
    if username == current_admin:
        return JSONResponse({"ok": False, "error": "You can't delete your own account."}, status_code=400)
    data = _load_users()
    user = next((u for u in data["users"] if u["username"] == username), None)
    if not user:
        return JSONResponse({"ok": False, "error": "User not found."}, status_code=404)
    if user.get("role") == "admin":
        remaining_admins = [u for u in data["users"] if u.get("role") == "admin" and u["username"] != username]
        if not remaining_admins:
            return JSONResponse({"ok": False, "error": "Can't delete the last admin account."}, status_code=400)

    data["users"] = [u for u in data["users"] if u["username"] != username]
    _save_users(data)
    _delete_sessions_for_user(username)
    perms_data = load_permissions()
    perms_data.pop(username, None)
    save_permissions(perms_data)
    user_file = _user_data_file(username)
    if user_file and os.path.exists(user_file):
        try:
            os.remove(user_file)
        except Exception:
            pass
    return JSONResponse({"ok": True})


async def route_admin_set_role(request: Request, username: str):
    """POST /api/admin/users/{username}/role — { role: "user"|"admin" }"""
    err = require_admin(request)
    if err:
        return err
    current_admin = get_current_user(request)
    if username == current_admin:
        return JSONResponse({"ok": False, "error": "You can't change your own role."}, status_code=400)
    body = await request.json()
    role = body.get("role")
    if role not in ("user", "admin"):
        return JSONResponse({"ok": False, "error": "Invalid role."}, status_code=400)
    data = _load_users()
    user = next((u for u in data["users"] if u["username"] == username), None)
    if not user:
        return JSONResponse({"ok": False, "error": "User not found."}, status_code=404)
    if user.get("role") == "admin" and role != "admin":
        remaining_admins = [u for u in data["users"] if u.get("role") == "admin" and u["username"] != username]
        if not remaining_admins:
            return JSONResponse({"ok": False, "error": "Can't demote the last admin account."}, status_code=400)

    user["role"] = role
    _save_users(data)
    user_data = load_user_data(username)
    user_data["is_admin"] = (role == "admin")
    save_user_data(username, user_data)
    return JSONResponse({"ok": True})


async def route_claim_session(request: Request):
    """
    GET /auth/claim?token=...&next=/ — turn an already-issued session token
    into the session cookie via a top-level NAVIGATION response.

    Exists for the Android app's WebView: it proved unreliable at carrying a
    cookie set on a fetch() response into subsequent page navigations (the
    path desktop browsers use), while a cookie set by a navigation response
    itself — classic form-login style — is the most conservative, universally
    honored mechanism there is. The token is one the client already holds
    from the login response; an invalid or expired one just lands on /login.
    """
    token = request.query_params.get("token", "")
    nxt   = request.query_params.get("next", "/")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    if not token or _resolve_session(token) is None:
        return RedirectResponse("/login", status_code=302)
    response = RedirectResponse(nxt, status_code=302)
    response.set_cookie(
        key=_cookie_name_for_request(request), value=token,
        httponly=True, samesite="lax",
        max_age=SESSION_DURATION_DAYS * 86400,
    )
    return response


async def route_logout(request: Request):
    token = _get_request_token(request)
    if token:
        _delete_session(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(_cookie_name_for_request(request))
    response.delete_cookie(COOKIE_NAME)   # pre-per-port-naming leftover
    return response


async def route_logout_everywhere(request: Request):
    """POST /api/auth/logout-everywhere — end every session for this user, including the current one."""
    username = get_current_user(request)
    if not username:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)
    _delete_sessions_for_user(username)
    response = JSONResponse({"ok": True})
    response.delete_cookie(_cookie_name_for_request(request))
    response.delete_cookie(COOKIE_NAME)   # pre-per-port-naming leftover
    return response


def route_me(request: Request):
    _ensure_admin_exists()
    username = get_current_user(request)
    if not username:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)
    user = _find_user(username)
    perms = resolve_permissions(username)
    return JSONResponse({
        "ok":           True,
        "username":     username,
        "role":         user["role"] if user else "user",
        "allowed_tabs": user.get("allowed_tabs") if user else None,
        "permissions":  perms,
        "must_change_password": bool(user and user.get("must_change_password")),
    })


async def route_change_password(request: Request):
    """POST /api/auth/change-password  — { current_password, new_password }"""
    username = get_current_user(request)
    if not username:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)

    body             = await request.json()
    current_password = body.get("current_password", "")
    new_password     = body.get("new_password", "")

    if not current_password or not new_password:
        return JSONResponse({"ok": False, "error": "Both fields are required."}, status_code=400)
    if len(new_password) < 4:
        return JSONResponse({"ok": False, "error": "New password must be at least 4 characters."}, status_code=400)

    data  = _load_users()
    user  = next((u for u in data["users"] if u["username"] == username), None)
    if not user or not _verify_password(user["password_hash"], current_password):
        return JSONResponse({"ok": False, "error": "Current password is incorrect."}, status_code=401)

    user["password_hash"] = _hash_password(new_password)
    user["must_change_password"] = False
    _save_users(data)

    # A password change is usually a reaction to a suspected leak — no
    # session/token should survive it, including the one making this
    # request. The frontend redirects to /login right after, forcing a
    # real re-authentication with the new password rather than silently
    # continuing on a pre-change session.
    _delete_sessions_for_user(username)

    return JSONResponse({"ok": True})

async def route_change_username(request: Request):
    """
    POST /api/auth/change-username — { current_password, new_username }

    Self-service, mirroring change-password's shape (current-password
    confirmation, same validation change-password doesn't need). Unlike a
    password change, this deliberately does NOT invalidate sessions —
    renaming isn't the kind of security event a password change is (it
    grants an already-logged-in caller nothing they didn't already have),
    so every active session for this user is updated in place to the new
    username instead of forcing a re-login.

    Touches every place a username is used as a key: the users.json record,
    the per-user {username}.json data file (renamed on disk — this is what
    carries reading history/favourites/private collections/cover overrides
    along for free, no separate migration needed for any of that), the
    permissions.json entry if one exists (a user with no explicit entry,
    just falling back to _default, has nothing to move), and every active
    session's username field. Shared collections.json is untouched — shared
    collection records carry no owner/created-by reference at all.
    """
    username = get_current_user(request)
    if not username:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)

    body             = await request.json()
    current_password = body.get("current_password", "")
    new_username     = body.get("new_username", "").strip().lower()

    if not current_password or not new_username:
        return JSONResponse({"ok": False, "error": "Both fields are required."}, status_code=400)

    data = _load_users()
    user = next((u for u in data["users"] if u["username"] == username), None)
    if not user or not _verify_password(user["password_hash"], current_password):
        return JSONResponse({"ok": False, "error": "Current password is incorrect."}, status_code=401)

    if new_username == username:
        return JSONResponse({"ok": True, "username": username})
    if not VALID_USERNAME_RE.match(new_username):
        return JSONResponse({"ok": False, "error": "Username must be 3-32 characters: lowercase letters, numbers, underscore, or hyphen only."}, status_code=400)
    if new_username in RESERVED_USERNAMES:
        return JSONResponse({"ok": False, "error": "That username is reserved."}, status_code=400)
    if _find_user(new_username):
        return JSONResponse({"ok": False, "error": "Username already taken."}, status_code=409)

    user["username"] = new_username
    _save_users(data)

    old_path = _user_data_file(username)
    new_path = _user_data_file(new_username)
    if old_path and new_path and os.path.exists(old_path):
        os.makedirs(os.path.dirname(os.path.abspath(new_path)), exist_ok=True)
        os.rename(old_path, new_path)

    perms_data = load_permissions()
    if username in perms_data:
        perms_data[new_username] = perms_data.pop(username)
        save_permissions(perms_data)

    sessions_data = _load_sessions()
    changed = False
    for sess in sessions_data["sessions"].values():
        if sess.get("username") == username:
            sess["username"] = new_username
            changed = True
    if changed:
        _save_sessions(sessions_data)

    return JSONResponse({"ok": True, "username": new_username})

def route_get_permissions(request: Request):
    """GET /api/admin/permissions — all users with their resolved permissions."""
    err = require_admin(request)
    if err:
        return err
    current_admin = get_current_user(request)
    data          = _load_users()
    perms_data    = load_permissions()
    default_perms = perms_data.get("_default", _DEFAULT_PERMISSIONS.copy())
    users_list    = []
    for user in data["users"]:
        uname = user["username"]
        if uname == current_admin:
            continue   # never show the admin their own row
        entry = perms_data.get(uname)
        perms = (default_perms if entry is None else entry).copy()
        perms.setdefault("libraries", {})
        users_list.append({
            "username":    uname,
            "role":        user["role"],
            "permissions": perms,
        })
    return JSONResponse({
        "ok":      True,
        "users":   users_list,
        "default": default_perms,
    })


async def route_set_user_permissions(request: Request, username: str):
    """POST /api/admin/permissions/{username} — set permissions for a user or _default."""
    err = require_admin(request)
    if err:
        return err
    if username != "_default" and not _find_user(username):
        return JSONResponse({"ok": False, "error": "User not found."}, status_code=404)
    body  = await request.json()
    perms = body.get("permissions", {})
    allowed_keys = {"tags", "genres", "description", "metadata_fetch", "libraries", "blocked_tags"}
    perms = {k: v for k, v in perms.items() if k in allowed_keys}
    perms.setdefault("libraries", {})
    perms_data          = load_permissions()
    perms_data[username] = perms
    save_permissions(perms_data)
    return JSONResponse({"ok": True})