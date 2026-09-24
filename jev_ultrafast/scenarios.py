"""Built-in presets and their independent terminal checks. A DONE choice is never proof of success.

A check runs on a fresh read of the final page, separate from the model's observations and decisions,
and only for an unedited preset goal: an edited or custom goal has no independent check.
"""

import base64
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

# English names without the process locale: the flights goal and its checks must not depend on it.
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
FLIGHTS_URL = "https://www.google.com/travel/flights?hl=en"
FLIGHTS_LEAD_DAYS = 30


def flight_day(today=None):
    """Always in the future: the upstream fixed date has passed and past dates cannot be searched."""
    return (today or date.today()) + timedelta(days=FLIGHTS_LEAD_DAYS)


def presets(today=None):
    day = flight_day(today)
    return {
        "flights": {
            "label": "Google Flights · real web",
            "url": FLIGHTS_URL,
            "fixture": False,
            "date": day.isoformat(),
            "goal": (
                f"Find one-way flights from Zurich to London on {MONTHS[day.month - 1]} {day.day}, {day.year}, "
                "for one adult in economy. Stop when matching flight options are visible. "
                "Do not select or book a flight."
            ),
        },
        "travel": {
            "label": "Travel planner · fixture",
            "url": None,
            "fixture": True,
            "goal": "Find a Design stay in Lisbon with Free cancellation and open Casa Flora.",
        },
        "research": {
            "label": "Reading room · fixture",
            "url": None,
            "fixture": True,
            "goal": "Open the article about using finite choices to control browser agents.",
        },
    }


def verify_flights(page, day):
    """Route, one-way setting, date and visible results on the final Google Flights page."""
    parsed = urlparse(page["url"])
    encoded = parse_qs(parsed.query).get("tfs", [""])[0]
    try:
        date_in_url = day.isoformat().encode() in base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except ValueError:
        date_in_url = False
    values = {a["label"].strip(): a.get("value") for a in page["actions"]}
    shown = f"{WEEKDAYS[day.weekday()][:3]}, {MONTHS[day.month - 1][:3]} {day.day}"
    spelled = f"{WEEKDAYS[day.weekday()]}, {MONTHS[day.month - 1]} {day.day}"
    flights = [a["label"] for a in page["actions"] if "Select flight" in a["label"]]
    checks = {
        "search_page": parsed.hostname == "www.google.com" and parsed.path == "/travel/flights/search",
        "one_way": values.get("Change ticket type. One way") == "One way",
        "origin": values.get("Where from?") == "Zürich",
        "destination": values.get("Where to?") == "London",
        "date": values.get("Departure") == shown,
        "year": date_in_url or f"departing {day.isoformat()}" in page["text"],
        "results": bool(flights) and all(spelled in f for f in flights),
    }
    return {"passed": all(checks.values()), "checks": checks, "visible_flights": flights}


def verify_travel(url, text):
    """Casa Flora opened with the Design and Free cancellation filters actually applied."""
    checks = {
        "opened_casa_flora": urlparse(url).fragment == "casa-flora" and "Casa Flora" in text,
        "design_filter": "Your filters: Design ·" in text,
        "free_cancellation": "Free cancellation enabled" in text,
    }
    return {"passed": all(checks.values()), "checks": checks}


def verify_research(url, title, text):
    """The finite-choices article is open, not merely listed."""
    checks = {
        "opened_article": urlparse(url).fragment == "choices",
        "title": title == "A browser is a choice, not a conversation · Forma",
        "article_body": "Observe the browser’s accessibility tree" in text,
    }
    return {"passed": all(checks.values()), "checks": checks}


def verify(scenario, preset, browser):
    """Run a preset's independent check on a fresh, read-only observation of the final page."""
    page = browser.observe(screenshot=False)
    if scenario == "flights":
        result = verify_flights(page, date.fromisoformat(preset["date"]))
    else:
        text = browser.evaluate("document.body.innerText") or ""
        if scenario == "travel":
            result = verify_travel(page["url"], text)
        else:
            result = verify_research(page["url"], page["title"], text)
    return {"scenario": scenario, "checked_url": page["url"], **result}
