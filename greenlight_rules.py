"""Decision rules for Greenlight. No Streamlit or MongoDB imports, so they are easy to test."""
from datetime import date


def notice_text(days: int) -> str:
    if days > 0 and days % 7 == 0:
        weeks = days // 7
        return f"{weeks} week" + ("" if weeks == 1 else "s")
    return f"{days} day" + ("" if days == 1 else "s")


def resolve_allocation(day_override, month_default, standing_default, required=None, scheduled=0) -> int:
    """A day's own number wins. Next, if a required headcount is set, slots are the agents
    scheduled that day minus the agents who must stay on duty (never below 0).
    Otherwise the month's default, then the every-month default."""
    if day_override is not None:
        return int(day_override)
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
