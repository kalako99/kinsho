"""
auth.py — KINSHO authentication layer
Handles: users.json, sessions.json, login, register, session validation,
         change password, and per-user data file helpers.
"""

import json
import os
import uuid
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

# ── CONFIG ──────────────────────────────────────────────────────────────────

SESSION_DURATION_DAYS = 30
COOKIE_NAME           = "kinsho_session"

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


def _hash_password(password: str) -> str:
    salt = b"kinsho_salt_v1"
    return hmac.new(salt, password.encode(), digestmod=hashlib.sha256).hexdigest()


def _find_user(username: str) -> Optional[dict]:
    data = _load_users()
    return next((u for u in data["users"] if u["username"] == username), None)


def _ensure_admin_exists():
    """Create the default admin account if no users exist yet."""
    data = _load_users()
    if not data["users"]:
        admin = {
            "username":      "admin",
            "password_hash": _hash_password("admin"),
            "role":          "admin",
            "allowed_tabs":  None,
            "created_at":    datetime.now().isoformat(),
        }
        data["users"].append(admin)
        _save_users(data)
        print("[Auth] Default admin account created (password: admin). "
              "Change it after first login.")

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


# ── REQUEST HELPERS ──────────────────────────────────────────────────────────

def get_current_user(request: Request) -> Optional[str]:
    token = request.headers.get('X-Auth-Token') or request.cookies.get(COOKIE_NAME)
    return _resolve_session(token) if token else None


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
    "tags":         True,
    "genres":       True,
    "description":  True,
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
            "tags": True, "genres": True, "description": True,
            "libraries": {}, "is_admin": True,
        }
    try:
        data = load_permissions()
    except Exception:
        return {
            "tags": False, "genres": False, "description": False,
            "libraries": {}, "is_admin": False,
        }
    default    = data.get("_default", _DEFAULT_PERMISSIONS.copy())
    user_entry = data.get(username)
    perms      = (default if user_entry is None else user_entry).copy()
    perms.setdefault("libraries", {})
    perms.setdefault("blocked_tags", [])
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

# ── AUTH ROUTES ──────────────────────────────────────────────────────────────

async def route_login(request: Request):
    _ensure_admin_exists()
    body     = await request.json()
    username = body.get("username", "").strip().lower()
    password = body.get("password", "")

    if not username or not password:
        return JSONResponse({"ok": False, "error": "Username and password required."}, status_code=400)

    user = _find_user(username)
    if not user or user["password_hash"] != _hash_password(password):
        return JSONResponse({"ok": False, "error": "Invalid username or password."}, status_code=401)

    token    = _create_session(username)
    response = JSONResponse({"ok": True, "username": username, "role": user["role"], "token": token})
    response.set_cookie(
        key=COOKIE_NAME, value=token,
        httponly=True, samesite="lax",
        max_age=SESSION_DURATION_DAYS * 86400,
    )
    response.headers["X-Auth-Token"] = token
    return response


async def route_register(request: Request):
    _ensure_admin_exists()
    body     = await request.json()
    username = body.get("username", "").strip().lower()
    password = body.get("password", "")

    if not username or not password:
        return JSONResponse({"ok": False, "error": "Username and password required."}, status_code=400)
    if len(username) < 3:
        return JSONResponse({"ok": False, "error": "Username must be at least 3 characters."}, status_code=400)
    if len(password) < 4:
        return JSONResponse({"ok": False, "error": "Password must be at least 4 characters."}, status_code=400)

    reserved = {"data", "bootstrap", "sessions", "users"}
    if username in reserved:
        return JSONResponse({"ok": False, "error": "That username is reserved."}, status_code=400)
    if _find_user(username):
        return JSONResponse({"ok": False, "error": "Username already taken."}, status_code=409)

    new_user = {
        "username":      username,
        "password_hash": _hash_password(password),
        "role":          "user",
        "allowed_tabs":  None,
        "created_at":    datetime.now().isoformat(),
    }
    data = _load_users()
    data["users"].append(new_user)
    _save_users(data)
    save_user_data(username, {"is_admin": False})

    perms_data    = load_permissions()
    default_perms = perms_data.get("_default", _DEFAULT_PERMISSIONS.copy())
    perms_data[username] = {k: v for k, v in default_perms.items()}
    save_permissions(perms_data)

    token    = _create_session(username)
    response = JSONResponse({"ok": True, "username": username, "role": "user", "token": token})
    response.set_cookie(
        key=COOKIE_NAME, value=token,
        httponly=True, samesite="lax",
        max_age=SESSION_DURATION_DAYS * 86400,
    )
    response.headers["X-Auth-Token"] = token
    return response


async def route_logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        _delete_session(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME)
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
    if not user or user["password_hash"] != _hash_password(current_password):
        return JSONResponse({"ok": False, "error": "Current password is incorrect."}, status_code=401)

    user["password_hash"] = _hash_password(new_password)
    _save_users(data)
    return JSONResponse({"ok": True})

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
    body  = await request.json()
    perms = body.get("permissions", {})
    allowed_keys = {"tags", "genres", "description", "libraries", "blocked_tags"}
    perms = {k: v for k, v in perms.items() if k in allowed_keys}
    perms.setdefault("libraries", {})
    perms_data          = load_permissions()
    perms_data[username] = perms
    save_permissions(perms_data)
    return JSONResponse({"ok": True})