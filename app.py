"""
Greenlight: PTO request app.

Streamlit hosts the HTML front end (greenlight_component/index.html) as a custom
component and does all MongoDB work here. The front end sends an action, this
script applies it, then sends the fresh data back.

Secrets (.streamlit/secrets.toml or the Streamlit Cloud secrets box):
    MONGODB_URI    = "mongodb+srv://user:pass@cluster.mongodb.net"
    ADMIN_PASSWORD = "choose-a-strong-passcode"   # first passcode; admins can change it in Settings
    DEV_PASSWORD   = "long-random-developer-key"  # optional; always signs in, even after the admin passcode changes
    MONGODB_DB     = "greenlight"      # optional
"""
import hashlib
import hmac
import random
import re
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
import streamlit.components.v1 as components
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ASCENDING, MongoClient, UpdateOne
from pymongo.errors import DuplicateKeyError

from greenlight_rules import (LEAVE_TYPES, PLANNED, UNPLANNED, WEEKDAYS, evaluate, leave_type, pool_slots,
                              resolve_allocation, wellness_day, wellness_month)

# Browser tab: the Greenlight logo, then the name in script letters (a tab title cannot use a web font,
# so it uses the Unicode script alphabet, which every browser shows as cursive).
_LOGO = Path(__file__).with_name("greenlight_logo.png")
st.set_page_config(page_title="\U0001d4d6\U0001d4fb\U0001d4ee\U0001d4ee\U0001d4f7\U0001d4f5\U0001d4f2\U0001d4f0\U0001d4f1\U0001d4fd",
                   page_icon=str(_LOGO) if _LOGO.exists() else "🟢", layout="wide", initial_sidebar_state="collapsed")

# Let the app fill the window: hide Streamlit's own chrome and stretch the component.
st.markdown(
    """
    <style>
      #MainMenu, footer, header[data-testid="stHeader"], [data-testid="stToolbar"],
      [data-testid="stDecoration"], [data-testid="stStatusWidget"] { display: none !important; }
      .block-container, [data-testid="stMainBlockContainer"] { padding: 0 !important; max-width: 100% !important; }
      [data-testid="stVerticalBlock"] { gap: 0 !important; }
      [data-testid="stMain"], [data-testid="stAppViewContainer"] > section, .main { padding: 0 !important; }
      [data-testid="stMainBlockContainer"], .block-container { padding-top: 0 !important; padding-bottom: 0 !important; }
      [data-testid="stElementContainer"], .element-container { margin: 0 !important; }
      [data-testid="stMarkdownContainer"]:has(style) { display: none; }
      /* No grey-out or fade while the server works: the page stays live and updates in place. */
      [data-stale="true"], .stale-element, [data-testid="stElementContainer"][data-stale] { opacity: 1 !important; transition: none !important; filter: none !important; }
      html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"] { overflow: hidden !important; }
      iframe[title*="greenlight"], iframe[data-testid="stCustomComponentV1"] {
        height: 100vh !important; height: 100dvh !important; width: 100% !important; display: block; border: 0;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

greenlight_ui = components.declare_component(
    "greenlight", path=str(Path(__file__).parent / "greenlight_component")
)

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_MONTH = re.compile(r"^\d{4}-\d{2}$")
ADMIN_ACTIONS = {
    "saveSettings", "setDefault", "setDefaults", "setRequired", "changePassword", "setOverride", "clearOverrides", "uploadRoster",
}
OVERRIDE_ACTIONS = {"overrideRequest", "declineOverride", "bulkOverride", "saveOvrSettings"}  # decided by WFM (or the developer key)
MANAGER_ACTIONS = {"mgrDecide", "mgrBulk"}
MGR_MAX_DEFAULT = 72        # the manager approval window in hours (WFM can change it in Settings)
OVR_REJECT_DEFAULT = 24     # unreviewed override requests close this many hours before the requested date (WFM Settings)
SESSION_DAYS = 30           # how long a sign-in lasts until the person signs out
HELD = ("approved", "awaiting")  # statuses that use up a slot
WELLNESS_DEFAULTS = {"wellness_on": True, "wellness_from": 5, "wellness_to": 8, "wellness_days_off": [],
                     "wellness_per_year": 4, "wellness_share": 50}
ROSTER_FIELDS = ("name", "manager", "region", "department", "work_days", "shift")


# ---------------------------------------------------------------- database

@st.cache_resource(show_spinner=False)
def get_db():
    # One shared, thread-safe client for every visitor. Writes wait for a majority of the cluster,
    # so a saved request is never lost on failover, and brief network blips are retried.
    client = MongoClient(st.secrets["MONGODB_URI"], serverSelectionTimeoutMS=8000, connectTimeoutMS=8000,
                         w="majority", retryWrites=True, retryReads=True, maxPoolSize=50, appname="greenlight")
    db = client[st.secrets.get("MONGODB_DB", "greenlight")]
    db.roster.create_index([("employee_id", ASCENDING)], unique=True)
    db.requests.create_index([("department", ASCENDING), ("region", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)])
    db.requests.create_index([("employee_id", ASCENDING), ("date", ASCENDING)])
    db.requests.create_index([("status", ASCENDING), ("mgr_deadline", ASCENDING)])
    db.sessions.create_index("expires", expireAfterSeconds=0)
    db.locks.create_index("expires", expireAfterSeconds=0)  # clears locks left behind by a crashed run
    db.attachments.create_index([("request_ids", ASCENDING)])
    db.allocations.create_index([("kind", ASCENDING), ("department", ASCENDING), ("region", ASCENDING), ("key", ASCENDING)], unique=True)
    return db


def safe_zone(name: str) -> str:
    try:
        ZoneInfo(str(name))
        return str(name)
    except Exception:
        return "Asia/Manila"


def today_iso(db=None) -> str:
    """'Today' in the app timezone set in Settings (Manila unless an admin changes it).
    Roster shift times are always entered in Manila time; the page converts them for display."""
    tz = safe_zone(get_settings(db)["timezone"]) if db is not None else "Asia/Manila"
    return datetime.now(ZoneInfo(tz)).date().isoformat()


def get_settings(db) -> dict:
    cfg = db.settings.find_one({"_id": "config"}) or {}
    return {
        "approval_mode": cfg.get("approval_mode", "auto"),
        "min_notice_days": int(cfg.get("min_notice_days", 14)),
        "timezone": cfg.get("timezone", "Asia/Manila"),
        "mgr_max_hours": int(cfg.get("mgr_max_hours", MGR_MAX_DEFAULT)),
        "mgr_window_off": bool(cfg.get("mgr_window_off", False)),
        "ovr_reject_hours": int(cfg.get("ovr_reject_hours", OVR_REJECT_DEFAULT)),
        "wellness_on": bool(cfg.get("wellness_on", True)),
        "wellness_from": int(cfg.get("wellness_from", 5)),
        "wellness_to": int(cfg.get("wellness_to", 8)),
        "wellness_days_off": [d for d in (cfg.get("wellness_days_off") or []) if d in WEEKDAYS],
        "wellness_per_year": int(cfg.get("wellness_per_year", 4)),
        "wellness_per_month": int(cfg.get("wellness_per_month", 1)),
        "sick_on": bool(cfg.get("sick_on", True)),
        "emergency_on": bool(cfg.get("emergency_on", True)),
        "wellness_share": int(cfg.get("wellness_share", 50)),
    }


def allocation_for(db, department: str, region: str, iso: str) -> int:
    """Slots for one department, region and day."""
    found = {
        (a["kind"], a["key"]): a["slots"]
        for a in db.allocations.find(
            {"department": department, "region": region,
             "$or": [{"kind": "day", "key": iso}, {"kind": {"$in": ["default", "required", "requiredpct"]}, "key": {"$in": [iso[:7], "*"]}}]}
        )
    }
    required = found.get(("required", iso[:7]), found.get(("required", "*")))
    required_pct = found.get(("requiredpct", iso[:7]), found.get(("requiredpct", "*")))
    scheduled = 0
    if (required is not None or required_pct is not None) and found.get(("day", iso)) is None:
        weekday = date.fromisoformat(iso).strftime("%a")  # Mon, Tue, ...
        scheduled = db.roster.count_documents({"department": department, "region": region,
                                               "work_days": {"$regex": f"(^|,){weekday}(,|$)"}})
    return resolve_allocation(found.get(("day", iso)), found.get(("default", iso[:7])), found.get(("default", "*")),
                              required=required, scheduled=scheduled, required_pct=required_pct)


def _iso(stamp) -> str:
    return stamp.replace(tzinfo=timezone.utc).isoformat() if isinstance(stamp, datetime) else ""


def mgr_deadline(settings: dict, submitted: datetime, iso: str, now: datetime):
    """When the manager must answer by: one fixed window from when the request was sent (72 hours by default).
    None when WFM turned the window off: the request then waits for the manager with no deadline."""
    if settings.get("mgr_window_off"):
        return None
    return submitted + timedelta(hours=settings["mgr_max_hours"])


def agent_notice(kind: str, now: datetime, reason: str = "") -> dict:
    """A message for the agent, shown under the bell on their dashboard."""
    return {"kind": kind, "at": now, "reason": reason}


def sweep(db) -> int:
    """Time-based steps, run whenever anyone has the app open:
    1. a day before the manager deadline, remind the agent their manager has not answered yet;
    2. at the deadline, reject the request (the held slot is freed) and tell the agent;
    3. an override request the Admin has not answered by the cut-off (24 hours before the date unless the Admin
       changes it in Settings) is rejected, and the agent is told."""
    now = datetime.now(timezone.utc)
    changed = 0
    r1 = db.requests.update_many(
        {"status": "awaiting", "mgr_reminded": {"$ne": True}, "mgr_deadline": {"$lte": now + timedelta(hours=24), "$gt": now}},
        {"$set": {"mgr_reminded": True}, "$push": {"notices": agent_notice("mgr_reminder", now)}})
    r2 = db.requests.update_many(
        {"status": "awaiting", "mgr_deadline": {"$lte": now}},
        {"$set": {"status": "rejected", "reason": "No manager response before the deadline", "decided_by": "system",
                  "mgr_status": "expired", "decided_at": now},
         "$push": {"notices": agent_notice("mgr_expired", now)}})
    # A date "starts" at midnight in the app timezone; the request closes ovr_reject_hours before that.
    cfg = get_settings(db)
    local_now = datetime.now(ZoneInfo(safe_zone(cfg["timezone"])))
    last_date = (local_now + timedelta(hours=cfg["ovr_reject_hours"])).date().isoformat()
    r3 = db.requests.update_many(
        {"override_status": "open", "date": {"$lte": last_date}},
        {"$set": {"status": "rejected", "override_status": "expired", "decided_by": "system",
                  "reason": "Override request not reviewed in time", "decided_at": now},
         "$push": {"notices": agent_notice("ovr_expired", now)}})
    changed = r1.modified_count + r2.modified_count + r3.modified_count
    if changed:
        bump_version(db)
    return changed


def serialize_request(doc: dict) -> dict:
    stamp = doc.get("submitted_at")
    return {
        "id": str(doc["_id"]),
        "notices": [{"kind": n.get("kind", ""), "at": _iso(n.get("at")), "reason": n.get("reason", "")} for n in (doc.get("notices") or [])][-10:],
        "employee_id": doc.get("employee_id", ""),
        "name": doc.get("name", ""),
        "manager": doc.get("manager", ""),
        "department": doc.get("department", ""),
        "region": doc.get("region", ""),
        "date": doc.get("date", ""),
        "status": doc.get("status", "pending"),
        "type": leave_type(doc.get("type")),
        "reason": doc.get("reason", ""),
        "short_notice": bool(doc.get("short_notice", False)),
        "override_request": doc.get("override_request", ""),
        "override_status": doc.get("override_status", ""),
        "override_type": doc.get("override_type", ""),
        "decided_by": doc.get("decided_by", "auto"),
        "override_reason": doc.get("override_reason", ""),
        "note": doc.get("note", ""),
        "attachments": [{"id": str(a.get("id", "")), "name": a.get("name", ""), "type": a.get("type", ""), "size": int(a.get("size", 0))}
                        for a in (doc.get("attachments") or [])][:5],
        "submitted_at": _iso(stamp),
        "batch_id": doc.get("batch_id", ""),
        "mgr_deadline": _iso(doc.get("mgr_deadline")),
        "mgr_by": doc.get("mgr_by", ""),
        "mgr_status": doc.get("mgr_status", ""),
        "decided_at": _iso(doc.get("decided_at")),
        "cancelled_from": doc.get("cancelled_from", ""),
    }


def _utc(stamp):
    return stamp.replace(tzinfo=timezone.utc) if isinstance(stamp, datetime) and stamp.tzinfo is None else stamp


def manager_list(db) -> list:
    """Every manager on the roster: whether a passcode is set, whether they asked for a reset, and until when
    their set-up / reset link works."""
    names = sorted({n for n in db.roster.distinct("manager") if n})
    now = datetime.now(timezone.utc)
    docs = {m["_id"]: m for m in db.managers.find({}, {"passcode": 1, "reset_requested": 1, "setup_expires": 1, "setup_hash": 1})}
    out = []
    for n in names:
        m = docs.get(n) or {}
        until = _utc(m.get("setup_expires")) if m.get("setup_hash") else None
        out.append({"name": n, "set": bool(m.get("passcode")), "reset_at": _iso(m.get("reset_requested")),
                    "link_until": _iso(until) if until and until > now else ""})
    return out


SETUP_HOURS = 48   # how long a passcode set-up / reset link works


def app_url() -> str:
    """The address people open the app at, used to build manager set-up links."""
    try:
        url = getattr(st.context, "url", None)
        if url:
            return str(url).split("?")[0].replace("/~/+", "")
    except Exception:
        pass
    try:
        host = st.context.headers.get("host")
        if host:
            return f"https://{host}/"
    except Exception:
        pass
    return ""


def setup_lookup(db, token: str):
    """What a set-up link points to: the manager's name, or why it no longer works."""
    if not token:
        return None
    doc = db.managers.find_one({"setup_hash": hashlib.sha256(token.encode()).hexdigest()})
    if not doc or not db.roster.find_one({"manager": doc["_id"]}):
        return {"ok": False, "reason": "used"}
    if _utc(doc.get("setup_expires")) is None or _utc(doc["setup_expires"]) < datetime.now(timezone.utc):
        return {"ok": False, "reason": "expired", "name": doc["_id"]}
    return {"ok": True, "name": doc["_id"], "reset": bool(doc.get("passcode"))}


def do_manager_link(db, payload: dict) -> dict:
    """WFM makes a one-time link for a manager to set (or reset) their own passcode. A new link replaces the old one.
    The current passcode keeps working until the manager sets a new one."""
    name = clean(payload.get("name"))
    if not db.roster.find_one({"manager": name}):
        return {"error": "That manager is not on the roster."}
    token = secrets.token_urlsafe(24)
    until = datetime.now(timezone.utc) + timedelta(hours=SETUP_HOURS)
    db.managers.update_one({"_id": name}, {"$set": {"setup_hash": hashlib.sha256(token.encode()).hexdigest(), "setup_expires": until},
                                           "$unset": {"reset_requested": ""}}, upsert=True)
    return {"ok": True, "token": token, "expires": _iso(until), "base": app_url()}


def do_manager_setup(db, payload: dict) -> dict:
    """The manager opened their link and chose a passcode: save it, close the link, and sign them in."""
    token = clean(st.query_params.get("mgrsetup", "") or payload.get("token"), 200)
    info = setup_lookup(db, token)
    if not info or not info["ok"]:
        return {"error": "This link has expired or was already used. Ask WFM for a new one."}
    code = str(payload.get("passcode", ""))
    if len(code) < 8:
        return {"error": "Use 8 characters or more for the passcode."}
    name = info["name"]
    db.managers.update_one({"_id": name}, {"$set": {"passcode": hash_password(code)},
                                           "$unset": {"setup_hash": "", "setup_expires": "", "reset_requested": ""}})
    try:
        del st.query_params["mgrsetup"]
    except Exception:
        pass
    st.session_state.update({"manager": name, "is_admin": False, "is_dev": False, "is_override": False, "login_tries": 0})
    return {"ok": True, "token": new_session(db, manager=name), "name": name}


def do_manager_forgot(db, payload: dict) -> dict:
    """A manager forgot their passcode: flag it for WFM, who sends a reset link. Nothing else changes."""
    name = clean(payload.get("name"))
    if st.session_state.get("forgot_tries", 0) >= 5:
        return {"error": "WFM has already been told. Please contact them directly."}
    if not db.roster.find_one({"manager": name}):
        return {"error": "That manager is not on the roster."}
    st.session_state["forgot_tries"] = st.session_state.get("forgot_tries", 0) + 1
    db.managers.update_one({"_id": name}, {"$set": {"reset_requested": datetime.now(timezone.utc)}}, upsert=True)
    return {"ok": True}


def do_manager_change(db, payload: dict) -> dict:
    """A signed-in manager changes their own passcode."""
    name = st.session_state.get("manager")
    if not name:
        return {"error": "Sign in as a manager first."}
    doc = db.managers.find_one({"_id": name}) or {}
    salt, _, digest = str(doc.get("passcode", "")).partition("$")
    if not digest or not hmac.compare_digest(hash_password(str(payload.get("current", "")), salt), doc["passcode"]):
        return {"error": "The current passcode is not right."}
    code = str(payload.get("next", ""))
    if len(code) < 8:
        return {"error": "Use 8 characters or more for the new passcode."}
    db.managers.update_one({"_id": name}, {"$set": {"passcode": hash_password(code)}, "$unset": {"reset_requested": ""}})
    return {"ok": True}


# ---------------------------------------------------------------- actions

def clean(value, limit: int = 120) -> str:
    return str(value if value is not None else "").strip()[:limit]


def slots_value(value):
    if value is None:
        return None
    return max(0, min(999, int(value)))


def request_id(payload: dict):
    try:
        return ObjectId(str(payload.get("id", "")))
    except (InvalidId, TypeError):
        return None


def hash_password(password: str, salt: str = "") -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${digest}"


def dev_ok(password: str) -> bool:
    """The developer key lives only in secrets, so it keeps working whatever the admins do in the app."""
    key = str(st.secrets.get("DEV_PASSWORD", ""))
    return len(key) >= 8 and hmac.compare_digest(password.encode(), key.encode())


def password_ok(db, password: str) -> bool:
    """A passcode changed in Settings is stored (hashed) in MongoDB and wins over the one in secrets."""
    if dev_ok(password):
        return True
    stored = (db.settings.find_one({"_id": "config"}) or {}).get("admin_password")
    if stored:
        return hmac.compare_digest(hash_password(password, stored.split("$")[0]), stored)
    expected = str(st.secrets.get("ADMIN_PASSWORD", ""))
    return bool(expected) and hmac.compare_digest(password.encode(), expected.encode())


def override_ok(db, password: str) -> bool:
    """The Admin role (decides override requests). Its passcode is OVERRIDE_PASSWORD in secrets; the developer key also works."""
    if dev_ok(password):
        return True
    expected = str(st.secrets.get("OVERRIDE_PASSWORD", ""))
    return bool(expected) and hmac.compare_digest(password.encode(), expected.encode())


def new_session(db, admin: bool = False, dev: bool = False, manager: str = "", override: bool = False) -> str:
    """A sign-in that lasts until the person signs out, so they do not have to enter the passcode every visit."""
    token = secrets.token_urlsafe(32)
    db.sessions.insert_one({"_id": hashlib.sha256(token.encode()).hexdigest(), "admin": admin, "dev": dev, "manager": manager, "override": override,
                            "expires": datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)})
    return token


def resume_session(db, token: str) -> str:
    """Restore a sign-in from the token the browser kept. Returns "ok" or "bad"."""
    key = hashlib.sha256(str(token).encode()).hexdigest()
    doc = db.sessions.find_one({"_id": key})
    if not doc or doc["expires"] < datetime.now(timezone.utc).replace(tzinfo=None):
        return "bad"
    if doc.get("manager"):
        if not db.roster.find_one({"manager": doc["manager"]}):
            return "bad"
        st.session_state.update({"manager": doc["manager"], "is_admin": False, "is_dev": False, "is_override": False})
    elif doc.get("admin"):
        st.session_state.update({"is_admin": True, "is_dev": bool(doc.get("dev")), "manager": "", "is_override": False})
    else:  # includes old Admin-role sessions: that role was retired, so they sign in again as WFM
        return "bad"
    db.sessions.update_one({"_id": key}, {"$set": {"expires": datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)}})
    return "ok"


def do_login(db, payload: dict) -> dict:
    if payload.get("role") == "manager":
        if st.session_state.get("login_tries", 0) >= 8:
            return {"error": "Too many attempts. Reload the page and try again."}
        name = clean(payload.get("manager"))
        doc = db.managers.find_one({"_id": name})
        if not doc or not doc.get("passcode") or not db.roster.find_one({"manager": name}):
            return {"error": "You have no passcode yet. Open the set-up link WFM sent you, or tap Forgot passcode to ask for one."}
        salt, _, digest = str(doc.get("passcode", "")).partition("$")
        ok = bool(digest) and hmac.compare_digest(hash_password(str(payload.get("password", "")), salt), doc["passcode"])
        if not ok:
            st.session_state["login_tries"] = st.session_state.get("login_tries", 0) + 1
            return {"error": "That passcode is not right."}
        st.session_state.update({"manager": name, "is_admin": False, "is_dev": False, "is_override": False, "login_tries": 0})
        return {"ok": True, "token": new_session(db, manager=name)}
    if payload.get("role") == "ovadmin":  # the separate Admin role was retired: WFM decides override requests
        return {"error": "Override requests are now decided by WFM. Sign in as WFM."}
    if False:
        if st.session_state.get("login_tries", 0) >= 8:
            return {"error": "Too many attempts. Reload the page and try again."}
        if not st.secrets.get("OVERRIDE_PASSWORD", "") and not st.secrets.get("DEV_PASSWORD", ""):
            return {"error": "Admin access is not set up. Add OVERRIDE_PASSWORD to the app secrets."}
        if override_ok(db, str(payload.get("password", ""))):
            st.session_state.update({"manager": "", "is_admin": False, "is_dev": False, "is_override": True, "login_tries": 0})
            return {"ok": True, "token": new_session(db, override=True)}
        st.session_state["login_tries"] = st.session_state.get("login_tries", 0) + 1
        return {"error": "That passcode is not right."}
    has_stored = bool((db.settings.find_one({"_id": "config"}) or {}).get("admin_password"))
    if not has_stored and not st.secrets.get("ADMIN_PASSWORD", "") and not st.secrets.get("DEV_PASSWORD", ""):
        return {"error": "Admin access is not set up. Add ADMIN_PASSWORD to the app secrets."}
    if st.session_state.get("login_tries", 0) >= 8:
        return {"error": "Too many attempts. Reload the page and try again."}
    if password_ok(db, str(payload.get("password", ""))):
        st.session_state["manager"] = ""
        st.session_state["is_override"] = False
        st.session_state["is_admin"] = True
        st.session_state["is_dev"] = dev_ok(str(payload.get("password", "")))
        st.session_state["login_tries"] = 0
        return {"ok": True, "token": new_session(db, admin=True, dev=st.session_state["is_dev"])}
    st.session_state["login_tries"] = st.session_state.get("login_tries", 0) + 1
    return {"error": "That passcode is not right."}


def do_change_password(db, payload: dict) -> dict:
    new = str(payload.get("next", ""))
    if not password_ok(db, str(payload.get("current", ""))):
        return {"error": "The current passcode is not right."}
    if len(new) < 8:
        return {"error": "Use 8 characters or more for the new passcode."}
    db.settings.update_one({"_id": "config"}, {"$set": {"admin_password": hash_password(new)}}, upsert=True)
    db.sessions.delete_many({"admin": True, "dev": False})  # other admin devices must sign in again with the new passcode
    return {"ok": True}


LOCK_SECONDS = 20  # a lock older than this is treated as abandoned


@contextmanager
def day_lock(db, department: str, region: str, iso: str, wait: float = 15.0):
    """Only one person at a time may check and take a slot for one team on one day.
    Many people can submit at the same moment: each waits a few milliseconds for the one ahead,
    so two people can never both get the last slot. Different teams or days never wait for each other."""
    key, me = f"{department}|{region}|{iso}", uuid.uuid4().hex
    give_up = time.time() + wait
    while True:
        now = datetime.now(timezone.utc)
        try:
            db.locks.insert_one({"_id": key, "owner": me, "expires": now + timedelta(seconds=LOCK_SECONDS)})
            break
        except DuplicateKeyError:
            # Take over a lock whose owner crashed or timed out.
            if db.locks.find_one_and_update({"_id": key, "expires": {"$lt": now}},
                                            {"$set": {"owner": me, "expires": now + timedelta(seconds=LOCK_SECONDS)}}):
                break
            if time.time() > give_up:
                raise TimeoutError("Many people are booking this day right now. Please try again in a moment.")
            time.sleep(0.03 + random.random() * 0.07)
    try:
        yield
    finally:
        db.locks.delete_one({"_id": key, "owner": me})


def pool_filter(kind: str) -> dict:
    """Which requests share a slot pool: Wellness has its own; PTO (and old requests with no type) the other.
    Sick and Emergency leave never take a slot."""
    return {"type": "wellness"} if kind == "wellness" else {"type": {"$nin": ["wellness", "sick", "emergency"]}}


def team_used(db, team: dict, iso: str, exclude=None, before=None, kind: str = "pto") -> int:
    """Slots in use on a day in one pool (PTO or Wellness): approved requests plus requests held while they wait for a manager."""
    pool = pool_filter(kind)
    query = {**team, **pool, "date": iso, "status": {"$in": list(HELD)}}
    if exclude is not None:
        query["_id"] = {"$ne": exclude}
    count = db.requests.count_documents(query)
    if before is not None:  # for a held request, only earlier holders keep priority over it
        count = db.requests.count_documents({**team, **pool, "date": iso, "status": "approved"}) + \
            db.requests.count_documents({**team, **pool, "date": iso, "status": "awaiting", "_id": {"$lt": before}})
    return count


def pool_allocation(db, team: dict, iso: str, settings: dict, kind: str) -> int:
    """The slots for one pool on a day: the WFM allocation, split between PTO and Wellness in wellness months."""
    return pool_slots(allocation_for(db, team["department"], team["region"], iso), settings, iso, kind)


def wellness_taken(db, employee_id: str, year: str, exclude=None) -> int:
    """Wellness days an agent has used or has on the way this year (approved, waiting, or waiting on an override)."""
    query = {"employee_id": employee_id, "type": "wellness", "date": {"$gte": f"{year}-01-01", "$lte": f"{year}-12-31"},
             "$or": [{"status": {"$in": ["approved", "awaiting", "pending"]}}, {"override_status": "open"}]}
    if exclude is not None:
        query["_id"] = {"$ne": exclude}
    return db.requests.count_documents(query)


def wellness_taken_month(db, employee_id: str, month: str, exclude=None) -> int:
    """Wellness days an agent has used or has on the way in one month (YYYY-MM)."""
    query = {"employee_id": employee_id, "type": "wellness", "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
             "$or": [{"status": {"$in": ["approved", "awaiting", "pending"]}}, {"override_status": "open"}]}
    if exclude is not None:
        query["_id"] = {"$ne": exclude}
    return db.requests.count_documents(query)


def type_problem(db, settings: dict, kind: str, iso: str, employee_id: str, exclude=None):
    """Rules that depend on the leave type alone. Returns a reason the date cannot be booked, or None."""
    if kind == "wellness":
        if not wellness_month(settings, iso):
            return "Wellness is not available in this month"
        if not wellness_day(settings, iso):
            return "Wellness is not available on " + date.fromisoformat(iso).strftime("%A") + "s"
        if wellness_taken(db, employee_id, iso[:4], exclude) >= settings["wellness_per_year"]:
            return f"No Wellness days left for {iso[:4]} ({settings['wellness_per_year']} a year)"
        per_month = settings.get("wellness_per_month", 1)
        if wellness_taken_month(db, employee_id, iso[:7], exclude) >= per_month:
            return (f"Only {per_month} Wellness day{'s' if per_month != 1 else ''} a month: "
                    f"{date.fromisoformat(iso).strftime('%B %Y')} already has {'one' if per_month == 1 else 'the limit'}")
    return None


def submit_one(db, agent: dict, team: dict, iso: str, settings: dict, today: str, now: datetime, ask: str, batch: str,
               kind: str = "pto") -> dict:
    """Check and save one date. Runs while holding the day lock, so the slot count cannot change underneath it."""
    if db.requests.find_one({"employee_id": agent["employee_id"], "date": iso, "status": {"$nin": ["rejected", "cancelled"]}}):
        return {"date": iso, "status": "skipped", "reason": "Already requested for this date"}
    problem = type_problem(db, settings, kind, iso, agent["employee_id"])
    if problem:
        return {"date": iso, "status": "skipped", "reason": problem}
    if kind in UNPLANNED:
        # Sick and Emergency leave: no notice and no slot. The manager approves, then the system approves it.
        if iso < today:
            return {"date": iso, "status": "skipped", "reason": "Date has already passed"}
        allocation, used = 0, 0
        status, reason, short, otype = "approved", "", False, ""
    else:
        allocation = pool_allocation(db, team, iso, settings, kind)
        used = team_used(db, team, iso, kind=kind)
        status, reason, short = evaluate(today, iso, settings["min_notice_days"], settings["approval_mode"], allocation, used)
        otype = "" if not ask or status == "approved" else "notice" if short else "allocation" if used >= allocation else ""
    deadline = None
    if status in ("approved", "pending") and not short:
        # Layer one: the agent's manager answers first. The slot is held meanwhile.
        deadline = mgr_deadline(settings, now, iso, now)
        status, reason = "awaiting", "Waiting for manager approval"
    db.requests.insert_one({
        "employee_id": agent["employee_id"], "name": agent["name"], "manager": agent.get("manager", ""),
        **team, "date": iso, "type": kind, "status": status, "reason": reason, "short_notice": short, "decided_by": "auto",
        "override_request": ask if otype else "", "override_status": "open" if otype else "", "override_type": otype,
        "override_reason": "", "note": "", "submitted_at": now, "batch_id": batch,
        **({"mgr_deadline": deadline} if deadline else {}),
    })
    return {"date": iso, "type": kind, "status": status, "reason": reason, "override_requested": bool(otype),
            "mgr_deadline": _iso(deadline) if deadline else ""}


MAX_FILES = 3
MAX_FILE_BYTES = 5 * 1024 * 1024   # 5 MB per file


def read_files(payload: dict):
    """Files sent with a request (any type): [{name, type, data (base64)}]. Returns (files, error)."""
    files = []
    for f in (payload.get("attachments") or [])[:MAX_FILES + 1]:
        if not isinstance(f, dict):
            continue
        data = str(f.get("data", ""))
        if "," in data[:100] and data.startswith("data:"):
            data = data.split(",", 1)[1]
        size = len(data) * 3 // 4 - data[-2:].count("=")
        if not data or not re.fullmatch(r"[A-Za-z0-9+/=\s]+", data[:4000]):
            return [], "One of the files could not be read. Attach it again."
        if size > MAX_FILE_BYTES:
            return [], f"{clean(f.get('name'), 80) or 'A file'} is larger than 5 MB."
        files.append({"name": clean(f.get("name"), 120) or "attachment", "type": clean(f.get("type"), 100) or "application/octet-stream",
                      "size": size, "data": data})
    if len(files) > MAX_FILES:
        return [], f"Attach up to {MAX_FILES} files."
    return files, ""


def save_files(db, files: list, request_ids: list, now: datetime) -> list:
    """Each file is stored once, in its own collection, and linked to every request it belongs to.
    Requests only keep the name, type and size, so the app stays fast."""
    meta = []
    for f in files:
        aid = db.attachments.insert_one({**f, "request_ids": request_ids, "uploaded_at": now}).inserted_id
        meta.append({"id": str(aid), "name": f["name"], "type": f["type"], "size": f["size"]})
    if meta and request_ids:
        db.requests.update_many({"_id": {"$in": request_ids}}, {"$set": {"attachments": meta}})
    return meta


def do_get_attachment(db, payload: dict) -> dict:
    """The content of one attachment, for signed-in staff only: the agent's manager and WFM.
    Agents have no sign-in, so files (often medical) are never opened from the agent screen."""
    try:
        doc = db.attachments.find_one({"_id": ObjectId(str(payload.get("id", "")))})
    except (InvalidId, TypeError):
        doc = None
    if not doc:
        return {"error": "That file is no longer available."}
    reqs = list(db.requests.find({"_id": {"$in": doc.get("request_ids") or []}}, {"employee_id": 1, "manager": 1}))
    ok = bool(st.session_state.get("is_admin") or st.session_state.get("is_override") or st.session_state.get("is_dev"))
    ok = ok or any(st.session_state.get("manager") and r.get("manager") == st.session_state.get("manager") for r in reqs)
    if not ok:
        return {"error": "You cannot open this file."}
    return {"ok": True, "id": str(doc["_id"]), "name": doc.get("name", ""), "type": doc.get("type", ""), "size": doc.get("size", 0), "data": doc.get("data", "")}


def do_submit(db, payload: dict) -> dict:
    agent = db.roster.find_one({"employee_id": clean(payload.get("employee_id"))})
    if not agent:
        return {"error": "That person is not on the roster."}
    dates = sorted({d for d in payload.get("dates", []) if isinstance(d, str) and ISO_DATE.match(d)})[:62]
    if not dates:
        return {"error": "Pick at least one date."}
    files, problem = read_files(payload)
    if problem:
        return {"error": problem}
    comment = clean(payload.get("comment"), 1000)
    asked = leave_type(payload.get("type"))
    if asked in UNPLANNED and not get_settings(db).get(asked + "_on", True):
        return {"error": f"{LEAVE_TYPES[asked]} is turned off. Contact WFM."}
    if asked in UNPLANNED and not comment:
        return {"error": "Add a reason or comment for this leave."}

    settings, today, batch, results = get_settings(db), today_iso(db), uuid.uuid4().hex, []
    now = datetime.now(timezone.utc)
    ask = clean(payload.get("override_request"), 500)  # the agent's reason for an exception (short notice or no slot)
    per_date = payload.get("override_reasons") if isinstance(payload.get("override_reasons"), dict) else {}
    team = {"department": agent["department"], "region": agent["region"]}
    kind = leave_type(payload.get("type"))
    if kind in UNPLANNED:
        ask, per_date = "", {}  # unplanned leave never needs an override
    for iso in dates:
        reason_for_date = clean(per_date.get(iso), 500) or ask  # each date that needs an override has its own reason
        try:
            with day_lock(db, team["department"], team["region"], iso):
                if kind == "wellness":  # one agent's yearly Wellness count is checked one date at a time
                    with day_lock(db, "wellness", agent["employee_id"], iso[:4]):
                        results.append(submit_one(db, agent, team, iso, settings, today, now, reason_for_date, batch, kind))
                else:
                    results.append(submit_one(db, agent, team, iso, settings, today, now, reason_for_date, batch, kind))
        except TimeoutError as exc:
            results.append({"date": iso, "status": "skipped", "reason": str(exc)})
    made = list(db.requests.find({"batch_id": batch}, {"_id": 1, "date": 1, "override_status": 1}))
    if kind in UNPLANNED and made:
        db.requests.update_many({"batch_id": batch}, {"$set": {"note": comment}})  # the reason or comment, shown to the manager
    if files:
        # Unplanned leave: the files go with every date. Planned leave: only with the dates that need an override.
        owners = [m["_id"] for m in made if kind in UNPLANNED or m.get("override_status") == "open"]
        meta = save_files(db, files, owners, now) if owners else []
        got = {m["date"] for m in made if m["_id"] in owners}
        for r in results:
            if r["date"] in got:
                r["attachments"] = len(meta)
    return {"results": results}


def after_manager(db, doc: dict, settings: dict, today: str) -> tuple:
    """The manager said yes: now the usual rules (notice, allocation, approval mode) decide.
    Sick and Emergency leave are approved by the system straight away."""
    kind = leave_type(doc.get("type"))
    if kind in UNPLANNED:
        return "approved", f"{LEAVE_TYPES[kind]} approved (unplanned leave)", False
    team = {"department": doc["department"], "region": doc["region"]}
    allocation = pool_allocation(db, team, doc["date"], settings, kind)
    used = team_used(db, team, doc["date"], before=doc["_id"], kind=kind)
    status, reason, short = evaluate(today, doc["date"], settings["min_notice_days"], settings["approval_mode"], allocation, used)
    return status, reason, short


def can_manage(doc: dict) -> bool:
    return bool(st.session_state.get("is_admin")) or (
        bool(st.session_state.get("manager")) and st.session_state.get("manager") == doc.get("manager"))


def do_manager_decide(db, ids: list, status: str, reason: str) -> dict:
    """The manager approval step only. Only the agent's own manager answers it. When the manager approves,
    the system rules (minimum notice and the WFM allocation, or Manual review) still give the final answer."""
    if not st.session_state.get("manager"):
        return {"error": "Only the agent's manager can answer the manager approval step."}
    settings, today, now = get_settings(db), today_iso(db), datetime.now(timezone.utc)
    who = "manager" if st.session_state.get("manager") else "admin"
    by = st.session_state.get("manager") or "WFM"
    out = []
    for rid in ids:
        try:
            doc = db.requests.find_one({"_id": ObjectId(str(rid)), "status": "awaiting"})
        except (InvalidId, TypeError):
            doc = None
        if not doc or not can_manage(doc):
            continue
        if status == "approved":
            try:
                with day_lock(db, doc["department"], doc["region"], doc["date"]):
                    new_status, why, short = after_manager(db, doc, settings, today)
                    change = {"status": new_status, "reason": "Manager approved. " + why, "short_notice": short,
                              "decided_by": "auto" if new_status != "pending" else who, "mgr_status": "approved", "mgr_by": by, "decided_at": now}
                    done = db.requests.update_one({"_id": doc["_id"], "status": "awaiting"}, {"$set": change})
            except TimeoutError:
                continue
        else:
            new_status = "rejected"
            change = {"status": "rejected", "reason": reason or "Rejected by manager", "decided_by": who,
                      "mgr_status": "rejected", "mgr_by": by, "decided_at": now}
            done = db.requests.update_one({"_id": doc["_id"], "status": "awaiting"}, {"$set": change})
        if not done.matched_count:  # answered or expired a moment ago
            continue
        out.append({"id": str(doc["_id"]), "status": new_status, "reason": change["reason"]})
    if not out:
        return {"error": "Nothing to decide. These requests were already answered."}
    return {"ok": True, "results": out, "status": out[0]["status"], "reason": out[0]["reason"], "count": len(out)}


def do_cancel(db, payload: dict) -> dict:
    try:
        doc = db.requests.find_one({"_id": ObjectId(str(payload.get("id", "")))})
    except (InvalidId, TypeError):
        doc = None
    if not doc:
        return {"error": "That request no longer exists."}
    # Only the agent who submitted the request can cancel it (WFM can still edit a request in its own screen).
    by_agent = clean(payload.get("employee_id")) == doc.get("employee_id") and not st.session_state.get("manager")
    if not by_agent:
        return {"error": "Only the agent who submitted the request can cancel it."}
    if doc.get("status") not in ("awaiting", "pending", "approved"):
        return {"error": "Only requests that are waiting or approved can be cancelled."}
    if doc.get("date", "") < today_iso(db):
        return {"error": "That date has already passed."}
    who = "agent"
    reason = clean(payload.get("reason"), 300)
    db.requests.update_one({"_id": doc["_id"]}, {"$set": {
        "status": "cancelled", "reason": f"Cancelled by {who}" + (f": {reason}" if reason else ""), "decided_by": who,
        "cancelled_from": doc.get("status"), "decided_at": datetime.now(timezone.utc)}})
    return {"ok": True}


def do_edit_mine(db, payload: dict) -> dict:
    """An agent can change a request until the manager has answered it."""
    try:
        doc = db.requests.find_one({"_id": ObjectId(str(payload.get("id", "")))})
    except (InvalidId, TypeError):
        doc = None
    if not doc or doc.get("employee_id") != clean(payload.get("employee_id")):
        return {"error": "That request no longer exists."}
    if doc.get("status") != "awaiting":
        return {"error": "Your manager has already answered this request, so it can no longer be edited."}
    settings, today, now = get_settings(db), today_iso(db), datetime.now(timezone.utc)
    iso, change = str(payload.get("date") or doc["date"]), {"note": clean(payload.get("note"), 500)}
    if iso != doc["date"]:
        if not ISO_DATE.match(iso):
            return {"error": "Pick a valid date."}
        kind = leave_type(doc.get("type"))
        if kind in UNPLANNED and iso < today:
            return {"error": "That date has already passed."}
        if kind in PLANNED and (date.fromisoformat(iso) - date.fromisoformat(today)).days < settings["min_notice_days"]:
            return {"error": "That date is inside the minimum notice. Cancel this request and submit a new one instead."}
        problem = type_problem(db, settings, kind, iso, doc["employee_id"], exclude=doc["_id"])
        if problem:
            return {"error": problem + "."}
        if db.requests.find_one({"employee_id": doc["employee_id"], "date": iso, "status": {"$nin": ["rejected", "cancelled"]}, "_id": {"$ne": doc["_id"]}}):
            return {"error": "You already have a request for that date."}
        team = {"department": doc["department"], "region": doc["region"]}
        deadline = mgr_deadline(settings, doc["submitted_at"].replace(tzinfo=timezone.utc), iso, now)
        change.update({"date": iso})
        if deadline:
            change["mgr_deadline"] = deadline
        try:
            with day_lock(db, team["department"], team["region"], iso):
                if kind in PLANNED and team_used(db, team, iso, exclude=doc["_id"], kind=kind) >= pool_allocation(db, team, iso, settings, kind):
                    return {"error": "That date has no open slot."}
                done = db.requests.update_one({"_id": doc["_id"], "status": "awaiting"}, {"$set": change})
        except TimeoutError as exc:
            return {"error": str(exc)}
    else:
        done = db.requests.update_one({"_id": doc["_id"], "status": "awaiting"}, {"$set": change})
    if not done.matched_count:
        return {"error": "Your manager has already answered this request, so it can no longer be edited."}
    return {"ok": True}


def do_set_manager_passcode(db, payload: dict) -> dict:
    name, code = clean(payload.get("name")), str(payload.get("passcode", ""))
    if not db.roster.find_one({"manager": name}):
        return {"error": "That manager is not on the roster."}
    if len(code) < 8:
        return {"error": "Use 8 characters or more for the passcode."}
    db.managers.update_one({"_id": name}, {"$set": {"passcode": hash_password(code)}, "$unset": {"reset_requested": ""}}, upsert=True)
    return {"ok": True}


def name_key(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().lower()


def do_upload_roster(db, payload: dict) -> dict:
    """Agents are matched by name. The internal key (employee_id field) is never shown; an agent already on
    the roster keeps theirs so their requests stay attached."""
    existing = {name_key(d.get("name", "")): d.get("employee_id") for d in db.roster.find({}, {"name": 1, "employee_id": 1})}
    rows, seen = [], set()
    for raw in (payload.get("rows") or [])[:5000]:
        row = {field: clean(raw.get(field)) for field in ROSTER_FIELDS}
        key = name_key(row["name"])
        if row["name"] and key not in seen:
            seen.add(key)
            row["employee_id"] = existing.get(key) or key
            row["work_days"] = row["work_days"] or "Mon,Tue,Wed,Thu,Fri"
            rows.append(row)
    if not rows:
        return {"error": "No usable rows found in that file."}
    if payload.get("mode") == "replace":
        db.roster.delete_many({})
    db.roster.bulk_write([UpdateOne({"employee_id": r["employee_id"]}, {"$set": r}, upsert=True) for r in rows])
    return {"ok": True, "count": len(rows)}


def do_set_allocation(db, kind: str, payload: dict, key: str) -> dict:
    spot = {"kind": kind, "department": clean(payload.get("department")), "region": clean(payload.get("region")), "key": key}
    if not spot["department"] or not spot["region"]:
        return {"error": "Choose a department and region first."}
    slots = slots_value(payload.get("slots"))
    if slots is None:
        db.allocations.delete_one(spot)
    else:
        db.allocations.update_one(spot, {"$set": {"slots": slots}}, upsert=True)
    return {"ok": True}


def handle(db, event: dict) -> dict:
    kind, payload = event.get("type"), event.get("payload") or {}
    if kind == "login":
        return do_login(db, payload)
    if kind == "logout":
        token = clean(event.get("tk"), 200)
        if token:
            db.sessions.delete_one({"_id": hashlib.sha256(token.encode()).hexdigest()})
        st.session_state.update({"is_admin": False, "is_dev": False, "is_override": False, "manager": "", "resume_for": "", "resume": "none"})
        return {"ok": True}
    if kind == "getAttachment":
        return do_get_attachment(db, payload)
    if kind == "submit":
        return do_submit(db, payload)
    if kind == "cancelRequest":
        return do_cancel(db, payload)
    if kind == "editMine":
        return do_edit_mine(db, payload)
    if kind in MANAGER_ACTIONS:
        ids = [payload.get("id")] if kind == "mgrDecide" else list(payload.get("ids") or [])[:200]
        return do_manager_decide(db, ids, "approved" if payload.get("status") == "approved" else "rejected", clean(payload.get("reason"), 500))
    if kind == "managerLink":
        return do_manager_link(db, payload) if st.session_state.get("is_admin") else {"error": "Admin sign-in required."}
    if kind == "mgrSetup":
        return do_manager_setup(db, payload)
    if kind == "mgrSetupDismiss":
        try:
            del st.query_params["mgrsetup"]
        except Exception:
            pass
        return {"ok": True}
    if kind == "mgrForgot":
        return do_manager_forgot(db, payload)
    if kind == "mgrChangePass":
        return do_manager_change(db, payload)
    if kind == "setManagerPasscode":
        return do_set_manager_passcode(db, payload) if st.session_state.get("is_admin") else {"error": "Admin sign-in required."}
    can_override = bool(st.session_state.get("is_admin") or st.session_state.get("is_dev"))  # WFM decides overrides
    if kind == "decide":
        # WFM answers ordinary requests and override requests.
        rid0 = request_id(payload)
        d0 = db.requests.find_one({"_id": rid0}) if rid0 else None
        if d0 and d0.get("override_status") == "open":
            if not can_override:
                return {"error": "Override requests are decided by WFM."}
        elif not st.session_state.get("is_admin"):
            return {"error": "Admin sign-in required."}
    elif kind in OVERRIDE_ACTIONS:
        if not can_override:
            return {"error": "Only WFM can decide override requests."}
        if kind == "saveOvrSettings":
            hours = max(1, min(336, int(payload.get("ovr_reject_hours", OVR_REJECT_DEFAULT))))
            db.settings.update_one({"_id": "config"}, {"$set": {"ovr_reject_hours": hours}}, upsert=True)
            return {"ok": True}
    elif kind not in ADMIN_ACTIONS:
        return {"error": "Unknown action."}
    elif not st.session_state.get("is_admin"):
        return {"error": "Admin sign-in required."}

    if kind == "saveSettings":
        mode = "manual" if payload.get("approval_mode") == "manual" else "auto"
        notice = max(0, min(365, int(payload.get("min_notice_days", 14))))
        mx = max(1, min(168, int(payload.get("mgr_max_hours", MGR_MAX_DEFAULT))))
        window_off = bool(payload.get("mgr_window_off", False))
        change = {"approval_mode": mode, "min_notice_days": notice, "mgr_max_hours": mx, "mgr_window_off": window_off}
        if "wellness_from" in payload:  # Wellness: months, locked weekdays, yearly allowance, share of each day's slots
            change.update({
                "wellness_on": bool(payload.get("wellness_on", True)),
                "wellness_from": max(1, min(12, int(payload.get("wellness_from", 5)))),
                "wellness_to": max(1, min(12, int(payload.get("wellness_to", 8)))),
                "wellness_days_off": [d for d in WEEKDAYS if d in (payload.get("wellness_days_off") or [])],
                "wellness_per_year": max(0, min(60, int(payload.get("wellness_per_year", 4)))),
                "wellness_per_month": max(1, min(31, int(payload.get("wellness_per_month", 1)))),
                "sick_on": bool(payload.get("sick_on", True)),
                "emergency_on": bool(payload.get("emergency_on", True)),
                "wellness_share": max(0, min(100, int(payload.get("wellness_share", 50)))),
            })
        if payload.get("timezone"):
            if safe_zone(payload["timezone"]) != payload["timezone"]:
                return {"error": "That timezone is not recognised."}
            change["timezone"] = payload["timezone"]
        before = get_settings(db)
        db.settings.update_one({"_id": "config"}, {"$set": change}, upsert=True)
        if window_off and not before["mgr_window_off"]:
            # No window: requests already waiting lose their deadline, so none of them expires.
            db.requests.update_many({"status": "awaiting"}, {"$unset": {"mgr_deadline": ""}})
        elif not window_off and before["mgr_window_off"]:
            # Window turned back on: waiting requests get a fresh window from now.
            now = datetime.now(timezone.utc)
            db.requests.update_many({"status": "awaiting", "mgr_deadline": {"$exists": False}},
                                    {"$set": {"mgr_deadline": now + timedelta(hours=mx), "mgr_reminded": False}})
        bump_version(db)
        return {"ok": True}
    if kind == "setDefault":
        month = str(payload.get("month", ""))
        if month != "*" and not ISO_MONTH.match(month):
            return {"error": "That month is not valid."}
        return do_set_allocation(db, "default", payload, month)
    if kind == "setDefaults":  # one default for several regions at once
        month = str(payload.get("month", ""))
        if month != "*" and not ISO_MONTH.match(month):
            return {"error": "That month is not valid."}
        for region in (payload.get("regions") or [])[:50]:
            one = {"department": payload.get("department"), "region": region, "slots": payload.get("slots")}
            result = do_set_allocation(db, "default", one, month)
            if result.get("error"):
                return result
            # Fixed slots replace a required headcount for the same months.
            gone = {"kind": {"$in": ["required", "requiredpct"]}, "department": clean(one["department"]), "region": clean(region)}
            db.allocations.delete_many(gone if month == "*" else {**gone, "key": month})
            # "All months" replaces any single-month defaults, so every calendar shows the new number.
            if month == "*" and payload.get("clear_all"):
                db.allocations.delete_many({"kind": "default", "department": clean(one["department"]),
                                            "region": clean(region), "key": {"$ne": "*"}})
        return {"ok": True}
    if kind == "setRequired":  # agents (or % of agents) that must stay on duty; slots are worked out from the roster each day
        month = str(payload.get("month", ""))
        if month != "*" and not ISO_MONTH.match(month):
            return {"error": "That month is not valid."}
        pct_mode = payload.get("mode") == "pct"
        mine, other = ("requiredpct", "required") if pct_mode else ("required", "requiredpct")
        for region in (payload.get("regions") or [])[:50]:
            base = {"department": clean(payload.get("department")), "region": clean(region)}
            if not base["department"] or not base["region"]:
                return {"error": "Choose a department and region first."}
            scope = {} if month == "*" else {"key": month}
            if payload.get("headcount") is None:  # stop auto: remove both kinds
                db.allocations.delete_many({**base, "kind": {"$in": ["required", "requiredpct"]}, **scope})
                continue
            n = max(0, min(100 if pct_mode else 9999, int(payload.get("headcount"))))
            db.allocations.update_one({**base, "kind": mine, "key": month}, {"$set": {"slots": n}}, upsert=True)
            # The other way of setting it is replaced for the same months.
            if month == "*" and payload.get("clear_all"):
                db.allocations.delete_many({**base, "kind": other})
            else:
                db.allocations.delete_many({**base, "kind": other, "key": month})
            if month == "*" and payload.get("clear_all"):
                db.allocations.delete_many({**base, "kind": mine, "key": {"$ne": "*"}})
        return {"ok": True}
    if kind == "changePassword":
        return do_change_password(db, payload)
    if kind == "setOverride":
        day = str(payload.get("date", ""))
        if not ISO_DATE.match(day):
            return {"error": "That date is not valid."}
        return do_set_allocation(db, "day", payload, day)
    if kind == "clearOverrides":
        month = str(payload.get("month", ""))
        if not ISO_MONTH.match(month):
            return {"error": "That month is not valid."}
        db.allocations.delete_many({"kind": "day", "department": clean(payload.get("department")),
                                    "region": clean(payload.get("region")), "key": {"$regex": f"^{month}-"}})
        return {"ok": True}
    if kind == "uploadRoster":
        return do_upload_roster(db, payload)

    if kind == "bulkOverride":
        approve, why = payload.get("action") == "approve", clean(payload.get("reason"), 500)
        if approve and not why:
            return {"error": "Add a reason for the override."}
        ids = [i for i in (request_id({"id": x}) for x in (payload.get("ids") or [])[:500]) if i]
        now = datetime.now(timezone.utc)
        change = ({"status": "approved", "decided_by": "override", "override_reason": why, "override_status": "approved",
                   "reason": "Approved by admin override", "decided_at": now} if approve else
                  {"status": "rejected", "decided_by": "admin", "override_status": "declined",
                   "reason": why or "Override declined by admin", "decided_at": now})
        done = db.requests.update_many({"_id": {"$in": ids}, "override_status": "open"},
                                       {"$set": change, "$push": {"notices": agent_notice("ovr_approved" if approve else "ovr_declined", now, change.get("override_reason") or change["reason"])}})
        return {"ok": True, "count": done.modified_count}

    # Everything below works on one existing request.
    rid = request_id(payload)
    doc = db.requests.find_one({"_id": rid}) if rid else None
    if not doc:
        return {"error": "That request no longer exists."}
    if kind == "overrideRequest":
        reason = clean(payload.get("reason"), 500)
        if not reason:
            return {"error": "Add a reason for the override."}
        change = {"status": "approved", "decided_by": "override", "override_reason": reason,
                  "reason": "Approved by admin override", "decided_at": datetime.now(timezone.utc)}
        if doc.get("override_status"):
            change["override_status"] = "approved"
    elif kind == "declineOverride":
        change = {"status": "rejected", "override_status": "declined", "decided_by": "admin",
                  "reason": clean(payload.get("reason"), 500) or "Override declined by admin", "decided_at": datetime.now(timezone.utc)}
    elif kind == "decide":
        status = "approved" if payload.get("status") == "approved" else "rejected"
        fallback = "Approved by admin" if status == "approved" else "Rejected by admin"
        change = {"status": status, "decided_by": "admin", "reason": clean(payload.get("reason"), 500) or fallback,
                  "decided_at": datetime.now(timezone.utc)}
        if doc.get("override_status") == "open":
            change["override_status"] = "approved" if status == "approved" else "declined"
    elif kind == "editRequest":
        day, status = str(payload.get("date", "")), payload.get("status")
        if not ISO_DATE.match(day) or (status not in ("approved", "rejected", "pending", "cancelled") and status != doc.get("status")):
            return {"error": "Check the date and status, then save again."}
        change = {"date": day, "status": status, "note": clean(payload.get("note"), 500)}
        if status != doc.get("status"):
            change.update({"reason": "Edited by admin", "decided_by": "admin", "decided_at": datetime.now(timezone.utc)})
    else:  # deleteRequest
        db.requests.delete_one({"_id": rid})
        return {"ok": True}
    update = {"$set": change}
    if change.get("override_status") in ("approved", "declined") and change["override_status"] != doc.get("override_status"):
        # Tell the agent how their override request was decided.
        ok = change["override_status"] == "approved"
        update["$push"] = {"notices": agent_notice("ovr_approved" if ok else "ovr_declined", datetime.now(timezone.utc),
                                             change.get("override_reason") or change.get("reason", ""))}
    db.requests.update_one({"_id": rid}, update)
    return {"ok": True}


# ---------------------------------------------------------------- page

try:
    has_uri = "MONGODB_URI" in st.secrets
except Exception:  # no secrets file at all
    has_uri = False
if not has_uri:
    st.error("Add MONGODB_URI to the app secrets, then reload. See README.md for the full list.")
    st.stop()

try:
    database = get_db()
except Exception as exc:  # connection problems, bad URI, blocked IP
    st.error(f"Greenlight could not reach MongoDB. Check the URI and the cluster's network access list.\n\n{exc}")
    st.stop()


def get_version(db) -> int:
    """One tiny read: a counter that goes up whenever anyone changes anything."""
    doc = db.meta.find_one({"_id": "version"}, {"n": 1})
    return int(doc["n"]) if doc else 0


def bump_version(db):
    db.meta.update_one({"_id": "version"}, {"$inc": {"n": 1}}, upsert=True)


# ---------------------------------------------------------------- speed
# Everyone shares one copy of the data, rebuilt only when the version counter changes (any save bumps it),
# when the day rolls over, or after a minute. A sign-in, a poll or a page change therefore costs a single
# tiny read instead of reloading the roster and every request, so the page answers almost at once.

@st.cache_resource(show_spinner=False)
def _shared() -> dict:
    return {"lock": threading.Lock(), "ver": None, "at": 0.0, "today": "", "base": None, "mgrs": None, "swept": 0.0}


def _build_base(db) -> dict:
    today = today_iso(db)
    cutoff = (date.fromisoformat(today) - timedelta(days=400)).isoformat()
    settings = get_settings(db)
    defaults, required, required_pct, overrides = {}, {}, {}, {}
    for a in db.allocations.find({}):
        pair = f"{a['department']}|{a['region']}"
        if a["kind"] == "default":
            defaults.setdefault(pair, {})[a["key"]] = int(a["slots"])
        elif a["kind"] == "required":
            required.setdefault(pair, {})[a["key"]] = int(a["slots"])
        elif a["kind"] == "requiredpct":
            required_pct.setdefault(pair, {})[a["key"]] = int(a["slots"])
        elif a["key"] >= cutoff:
            overrides[f"{pair}|{a['key']}"] = int(a["slots"])
    settings["defaults"] = defaults
    settings["required"] = required
    settings["required_pct"] = required_pct
    return {
        "today": today,
        "settings": settings,
        "overrides": overrides,
        "roster": list(db.roster.find({}, {"_id": 0}).sort("name", ASCENDING)),
        "requests": [serialize_request(r) for r in db.requests.find({"date": {"$gte": cutoff}}).sort("date", ASCENDING)],
    }


def shared_data(db, force: bool = False) -> dict:
    sh = _shared()
    now = time.time()
    if now - sh["swept"] > 20:  # time-based steps (reminders, expiries), once every 20 s for the whole app
        sh["swept"] = now
        try:
            sweep(db)
        except Exception:
            pass
    ver = get_version(db)
    with sh["lock"]:
        if force or sh["base"] is None or ver != sh["ver"] or now - sh["at"] > 60:
            sh["base"] = _build_base(db)
            sh["ver"], sh["at"], sh["mgrs"] = ver, now, None
        return sh


def page_state(db, force: bool = False) -> dict:
    """The shared data plus who this visitor is. Cheap: nothing is reloaded unless something changed."""
    sh = shared_data(db, force)
    is_admin = bool(st.session_state.get("is_admin"))
    if is_admin and sh["mgrs"] is None:
        sh["mgrs"] = manager_list(db)
    st.session_state["ver"] = sh["ver"]
    return {
        **sh["base"],
        "isAdmin": is_admin,
        "isDev": bool(st.session_state.get("is_dev")),
        "isOvAdmin": bool(st.session_state.get("is_override")),
        "resume": st.session_state.get("resume", "none"),
        "isManager": bool(st.session_state.get("manager")),
        "managerName": st.session_state.get("manager", ""),
        "managers": (sh["mgrs"] or []) if is_admin else [],
        "setup": setup_lookup(db, clean(st.query_params.get("mgrsetup", ""), 200)),
        "appUrl": app_url() if is_admin else "",
    }


# The page sends each action as the component's value. Streamlit keeps that value in session_state under the
# component's key before this script runs, so the action is applied first and the page is drawn once with
# the result, with no second run.
event = st.session_state.get("greenlight")
changed = False
if isinstance(event, dict) and event.get("id") and event.get("id") != st.session_state.get("last_event"):
    st.session_state["last_event"] = event.get("id")
    token = clean(event.get("tk"), 200)
    if token and not (st.session_state.get("is_admin") or st.session_state.get("manager") or st.session_state.get("is_override")) and st.session_state.get("resume_for") != token:
        st.session_state["resume_for"] = token  # look a token up once per browser session
        try:
            st.session_state["resume"] = resume_session(database, token)
        except Exception:
            st.session_state["resume"] = "bad"
    old = st.session_state.get("response") or {}
    if isinstance(old.get("result"), dict) and old["result"].get("data"):
        st.session_state["response"] = None  # a file was already delivered: do not send it again with every refresh
    if event.get("type") != "poll":
        try:
            result = handle(database, event)
            if event.get("type") not in ("login", "logout"):
                bump_version(database)
                changed = True
        except Exception as exc:
            result = {"error": f"That did not save: {exc}"}
        st.session_state["response"] = {"id": event.get("id"), "result": result}

try:
    state = page_state(database, force=changed)
except Exception as exc:
    st.error(f"Greenlight could not read from MongoDB.\n\n{exc}")
    st.stop()

greenlight_ui(state=state, response=st.session_state.get("response"), key="greenlight", default=None)
