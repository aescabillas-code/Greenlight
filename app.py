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
import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
import streamlit.components.v1 as components
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ASCENDING, MongoClient, UpdateOne

from greenlight_rules import evaluate, resolve_allocation

st.set_page_config(page_title="Greenlight", layout="wide", initial_sidebar_state="collapsed")

# Let the app fill the window: hide Streamlit's own chrome and stretch the component.
st.markdown(
    """
    <style>
      #MainMenu, footer, header[data-testid="stHeader"], [data-testid="stToolbar"],
      [data-testid="stDecoration"], [data-testid="stStatusWidget"] { display: none !important; }
      .block-container, [data-testid="stMainBlockContainer"] { padding: 0 !important; max-width: 100% !important; }
      [data-testid="stVerticalBlock"] { gap: 0 !important; }
      [data-testid="stMarkdownContainer"]:has(style) { display: none; }
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
    "overrideRequest", "declineOverride", "bulkOverride", "decide", "editRequest", "deleteRequest",
}
ROSTER_FIELDS = ("name", "employee_id", "manager", "region", "department", "work_days", "shift")


# ---------------------------------------------------------------- database

@st.cache_resource(show_spinner=False)
def get_db():
    client = MongoClient(st.secrets["MONGODB_URI"], serverSelectionTimeoutMS=8000)
    db = client[st.secrets.get("MONGODB_DB", "greenlight")]
    db.roster.create_index([("employee_id", ASCENDING)], unique=True)
    db.requests.create_index([("department", ASCENDING), ("region", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)])
    db.requests.create_index([("employee_id", ASCENDING), ("date", ASCENDING)])
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
    }


def allocation_for(db, department: str, region: str, iso: str) -> int:
    """Slots for one department, region and day."""
    found = {
        (a["kind"], a["key"]): a["slots"]
        for a in db.allocations.find(
            {"department": department, "region": region,
             "$or": [{"kind": "day", "key": iso}, {"kind": {"$in": ["default", "required"]}, "key": {"$in": [iso[:7], "*"]}}]}
        )
    }
    required = found.get(("required", iso[:7]), found.get(("required", "*")))
    scheduled = 0
    if required is not None and found.get(("day", iso)) is None:
        weekday = date.fromisoformat(iso).strftime("%a")  # Mon, Tue, ...
        scheduled = db.roster.count_documents({"department": department, "region": region,
                                               "work_days": {"$regex": f"(^|,){weekday}(,|$)"}})
    return resolve_allocation(found.get(("day", iso)), found.get(("default", iso[:7])), found.get(("default", "*")),
                              required=required, scheduled=scheduled)


def serialize_request(doc: dict) -> dict:
    stamp = doc.get("submitted_at")
    return {
        "id": str(doc["_id"]),
        "employee_id": doc.get("employee_id", ""),
        "name": doc.get("name", ""),
        "manager": doc.get("manager", ""),
        "department": doc.get("department", ""),
        "region": doc.get("region", ""),
        "date": doc.get("date", ""),
        "status": doc.get("status", "pending"),
        "reason": doc.get("reason", ""),
        "short_notice": bool(doc.get("short_notice", False)),
        "override_request": doc.get("override_request", ""),
        "override_status": doc.get("override_status", ""),
        "override_type": doc.get("override_type", ""),
        "decided_by": doc.get("decided_by", "auto"),
        "override_reason": doc.get("override_reason", ""),
        "note": doc.get("note", ""),
        "submitted_at": stamp.replace(tzinfo=timezone.utc).isoformat() if isinstance(stamp, datetime) else "",
        "batch_id": doc.get("batch_id", ""),
    }


def load_state(db, is_admin: bool) -> dict:
    """Everything the front end needs to draw itself."""
    today = today_iso(db)
    cutoff = (date.fromisoformat(today) - timedelta(days=400)).isoformat()
    settings = get_settings(db)
    defaults, required, overrides = {}, {}, {}
    for a in db.allocations.find({}):
        pair = f"{a['department']}|{a['region']}"
        if a["kind"] == "default":
            defaults.setdefault(pair, {})[a["key"]] = int(a["slots"])
        elif a["kind"] == "required":
            required.setdefault(pair, {})[a["key"]] = int(a["slots"])
        elif a["key"] >= cutoff:
            overrides[f"{pair}|{a['key']}"] = int(a["slots"])
    settings["defaults"] = defaults
    settings["required"] = required
    return {
        "today": today,
        "isAdmin": is_admin,
        "isDev": bool(st.session_state.get("is_dev")),
        "settings": settings,
        "overrides": overrides,
        "roster": list(db.roster.find({}, {"_id": 0}).sort("name", ASCENDING)),
        "requests": [serialize_request(r) for r in db.requests.find({"date": {"$gte": cutoff}}).sort("date", ASCENDING)],
    }


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


def do_login(db, payload: dict) -> dict:
    has_stored = bool((db.settings.find_one({"_id": "config"}) or {}).get("admin_password"))
    if not has_stored and not st.secrets.get("ADMIN_PASSWORD", "") and not st.secrets.get("DEV_PASSWORD", ""):
        return {"error": "Admin access is not set up. Add ADMIN_PASSWORD to the app secrets."}
    if st.session_state.get("login_tries", 0) >= 8:
        return {"error": "Too many attempts. Reload the page and try again."}
    if password_ok(db, str(payload.get("password", ""))):
        st.session_state["is_admin"] = True
        st.session_state["is_dev"] = dev_ok(str(payload.get("password", "")))
        st.session_state["login_tries"] = 0
        return {"ok": True}
    st.session_state["login_tries"] = st.session_state.get("login_tries", 0) + 1
    return {"error": "That passcode is not right."}


def do_change_password(db, payload: dict) -> dict:
    new = str(payload.get("next", ""))
    if not password_ok(db, str(payload.get("current", ""))):
        return {"error": "The current passcode is not right."}
    if len(new) < 8:
        return {"error": "Use 8 characters or more for the new passcode."}
    db.settings.update_one({"_id": "config"}, {"$set": {"admin_password": hash_password(new)}}, upsert=True)
    return {"ok": True}


def do_submit(db, payload: dict) -> dict:
    agent = db.roster.find_one({"employee_id": clean(payload.get("employee_id"))})
    if not agent:
        return {"error": "That person is not on the roster."}
    dates = sorted({d for d in payload.get("dates", []) if isinstance(d, str) and ISO_DATE.match(d)})[:62]
    if not dates:
        return {"error": "Pick at least one date."}

    settings, today, batch, results = get_settings(db), today_iso(db), uuid.uuid4().hex, []
    ask = clean(payload.get("override_request"), 500)  # the agent's reason for an exception (short notice or no slot)
    team = {"department": agent["department"], "region": agent["region"]}
    for iso in dates:
        if db.requests.find_one({"employee_id": agent["employee_id"], "date": iso, "status": {"$ne": "rejected"}}):
            results.append({"date": iso, "status": "skipped", "reason": "Already requested for this date"})
            continue
        allocation = allocation_for(db, team["department"], team["region"], iso)
        used = db.requests.count_documents({**team, "date": iso, "status": "approved"})
        status, reason, short = evaluate(today, iso, settings["min_notice_days"], settings["approval_mode"], allocation, used)
        otype = "" if not ask or status == "approved" else "notice" if short else "allocation" if used >= allocation else ""
        new_id = db.requests.insert_one({
            "employee_id": agent["employee_id"], "name": agent["name"], "manager": agent.get("manager", ""),
            **team, "date": iso, "status": status, "reason": reason, "short_notice": short, "decided_by": "auto",
            "override_request": ask if otype else "", "override_status": "open" if otype else "", "override_type": otype,
            "override_reason": "", "note": "", "submitted_at": datetime.now(timezone.utc), "batch_id": batch,
        }).inserted_id
        if status == "approved":
            # Two people can submit at the same moment. Whoever was written first keeps the slot.
            ahead = db.requests.count_documents({**team, "date": iso, "status": "approved", "_id": {"$lt": new_id}})
            if ahead >= allocation:
                status, reason = "rejected", "No allocation left for this day"
                otype = "allocation" if ask else ""
                db.requests.update_one({"_id": new_id}, {"$set": {"status": status, "reason": reason, "override_request": ask if otype else "",
                                                                   "override_status": "open" if otype else "", "override_type": otype}})
        results.append({"date": iso, "status": status, "reason": reason, "override_requested": bool(otype)})
    return {"results": results}


def do_upload_roster(db, payload: dict) -> dict:
    rows, seen = [], set()
    for raw in (payload.get("rows") or [])[:5000]:
        row = {field: clean(raw.get(field)) for field in ROSTER_FIELDS}
        if row["name"] and row["employee_id"] and row["employee_id"] not in seen:
            seen.add(row["employee_id"])
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
        st.session_state["is_admin"] = False
        st.session_state["is_dev"] = False
        return {"ok": True}
    if kind == "submit":
        return do_submit(db, payload)
    if kind not in ADMIN_ACTIONS:
        return {"error": "Unknown action."}
    if not st.session_state.get("is_admin"):
        return {"error": "Admin sign-in required."}

    if kind == "saveSettings":
        mode = "manual" if payload.get("approval_mode") == "manual" else "auto"
        notice = max(0, min(365, int(payload.get("min_notice_days", 14))))
        change = {"approval_mode": mode, "min_notice_days": notice}
        if payload.get("timezone"):
            if safe_zone(payload["timezone"]) != payload["timezone"]:
                return {"error": "That timezone is not recognised."}
            change["timezone"] = payload["timezone"]
        db.settings.update_one({"_id": "config"}, {"$set": change}, upsert=True)
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
            gone = {"kind": "required", "department": clean(one["department"]), "region": clean(region)}
            db.allocations.delete_many(gone if month == "*" else {**gone, "key": month})
            # "All months" replaces any single-month defaults, so every calendar shows the new number.
            if month == "*" and payload.get("clear_all"):
                db.allocations.delete_many({"kind": "default", "department": clean(one["department"]),
                                            "region": clean(region), "key": {"$ne": "*"}})
        return {"ok": True}
    if kind == "setRequired":  # agents that must stay on duty; slots are worked out from the roster each day
        month = str(payload.get("month", ""))
        if month != "*" and not ISO_MONTH.match(month):
            return {"error": "That month is not valid."}
        for region in (payload.get("regions") or [])[:50]:
            spot = {"kind": "required", "department": clean(payload.get("department")), "region": clean(region)}
            if not spot["department"] or not spot["region"]:
                return {"error": "Choose a department and region first."}
            if payload.get("headcount") is None:
                db.allocations.delete_many(spot if month == "*" else {**spot, "key": month})
                continue
            n = max(0, min(9999, int(payload.get("headcount"))))
            db.allocations.update_one({**spot, "key": month}, {"$set": {"slots": n}}, upsert=True)
            if month == "*" and payload.get("clear_all"):
                db.allocations.delete_many({**spot, "key": {"$ne": "*"}})
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
        done = db.requests.update_many({"_id": {"$in": ids}, "override_status": "open"}, {"$set": change})
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
        if not ISO_DATE.match(day) or status not in ("approved", "rejected", "pending"):
            return {"error": "Check the date and status, then save again."}
        change = {"date": day, "status": status, "note": clean(payload.get("note"), 500)}
        if status != doc.get("status"):
            change.update({"reason": "Edited by admin", "decided_by": "admin", "decided_at": datetime.now(timezone.utc)})
    else:  # deleteRequest
        db.requests.delete_one({"_id": rid})
        return {"ok": True}
    db.requests.update_one({"_id": rid}, {"$set": change})
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
    state = load_state(database, bool(st.session_state.get("is_admin")))
except Exception as exc:  # connection problems, bad URI, blocked IP
    st.error(f"Greenlight could not reach MongoDB. Check the URI and the cluster's network access list.\n\n{exc}")
    st.stop()

event = greenlight_ui(state=state, response=st.session_state.get("response"), key="greenlight", default=None)

# The component keeps returning its last value, so each action is applied once, by id.
if event and event.get("id") != st.session_state.get("last_event"):
    st.session_state["last_event"] = event.get("id")
    if event.get("type") != "poll":  # a poll only needs the fresh data this run already sent
        try:
            result = handle(database, event)
        except Exception as exc:
            result = {"error": f"That did not save: {exc}"}
        st.session_state["response"] = {"id": event.get("id"), "result": result}
        st.rerun()
