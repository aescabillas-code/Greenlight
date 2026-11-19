"""Decision rules for Greenlight. No Streamlit or MongoDB imports, so they are easy to test."""
from datetime import date


def notice_text(days: int) -> str:
    if days > 0 and days % 7 == 0:
        weeks = days // 7
        return f"{weeks} week" + ("" if weeks == 1 else "s")
    return f"{days} day" + ("" if days == 1 else "s")


def resolve_allocation(day_override, month_default, standing_default, required=None, scheduled=0, required_pct=None) -> int:
    """A day's own number wins. Next, if a required headcount is set, slots are the agents
    scheduled that day minus the agents who must stay on duty (never below 0).
    Otherwise the month's default, then the every-month default."""
    if day_override is not None:
        return int(day_override)
    if required_pct is not None:
        # Percent of headcount needed on duty, rounded up: slots = scheduled - ceil(scheduled * pct / 100).
        needed = (int(scheduled) * int(required_pct) + 99) // 100
        return max(0, int(scheduled) - needed)
    if required is not None:
        return max(0, int(scheduled) - int(required))
    for value in (month_default, standing_default):
        if value is not None:
            return int(value)
    return 0


def evaluate(today: str, requested: str, min_notice_days: int, mode: str, allocation: int, used: int):
    """Return (status, reason, short_notice) for one requested date."""
    lead = (date.fromisoformat(requested) - date.fromisoformat(today)).days
    if lead < 0:
        return "rejected", "Date has already passed", False
    short = lead < min_notice_days
    if mode == "manual":
        # Manual review: nothing is decided here. Short-notice requests are only flagged.
        return "pending", "Waiting for admin review" + (" (short notice)" if short else ""), short
    if short:
        text = notice_text(min_notice_days)
        possessive = "'" if text.endswith("s") else "'s"
        return "rejected", f"Less than {text}{possessive} notice", True
    if used < allocation:
        return "approved", "Slot available", False
    # A request for a full day is still recorded, so an admin can override it later.
    if allocation > 0:
        return "rejected", "No allocation left for this day", False
    return "rejected", "No allocation set for this day", False


# ---------------------------------------------------------------- leave types

LEAVE_TYPES = {"pto": "PTO", "wellness": "Wellness", "sick": "Sick Leave", "emergency": "Emergency Leave"}
PLANNED = ("pto", "wellness")      # use the WFM allocation, need the minimum notice
UNPLANNED = ("sick", "emergency")  # no notice, no slot: approved by the system once the manager approves
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def leave_type(value) -> str:
    return value if value in LEAVE_TYPES else "pto"


def wellness_month(settings: dict, iso: str) -> bool:
    """Is this date inside the WFM wellness months (May to August unless WFM changes it)? Ranges may wrap the year."""
    if not settings.get("wellness_on", True):
        return False
    month, start, end = int(iso[5:7]), int(settings.get("wellness_from", 5)), int(settings.get("wellness_to", 8))
    return start <= month <= end if start <= end else (month >= start or month <= end)


def wellness_day(settings: dict, iso: str) -> bool:
    """A date that can be booked as Wellness: inside the wellness months and not on a locked weekday."""
    return wellness_month(settings, iso) and WEEKDAYS[date.fromisoformat(iso).weekday()] not in (settings.get("wellness_days_off") or [])


def pool_slots(total: int, settings: dict, iso: str, kind: str) -> int:
    """In wellness months each day's leave slots are split: wellness_share % (rounded down) for Wellness,
    the rest for PTO. On a day blocked for Wellness, and outside those months, every slot is PTO."""
    total = max(0, int(total))
    if not wellness_day(settings, iso):
        return 0 if kind == "wellness" else total
    wellness = total * int(settings.get("wellness_share", 50)) // 100
    return wellness if kind == "wellness" else total - wellness
