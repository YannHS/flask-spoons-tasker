import re
import uuid
from functools import wraps
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from cachelib.simple import SimpleCache
from flask import abort, current_app, redirect, request
from flask_calendar.authorization import Authorization
from flask_calendar.calendar_data import CalendarData
from flask_calendar.constants import SESSION_ID
from flask_calendar.gregorian_calendar import GregorianCalendar

cache = SimpleCache()

# see `app_utils` tests for details, but TL;DR is that urls must start with `http://` or `https://` to match
URLS_REGEX_PATTERN = r"(https?\:\/\/[\w/\-?=%.]+\.[\w/\+\-?=%.~&\[\]\#]+)"
DECORATED_URL_FORMAT = '<a href="{}" target="_blank">{}</a>'


def authenticated(decorated_function: Callable) -> Any:
    @wraps(decorated_function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        session_id = request.cookies.get(SESSION_ID)
        if session_id is None or not is_session_valid(str(session_id)):
            if request.headers.get("Content-Type", "") == "application/json":
                abort(401)
            return redirect("/login")
        return decorated_function(*args, **kwargs)

    return wrapper


def authorized(decorated_function: Callable) -> Any:
    @wraps(decorated_function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        username = get_session_username(str(request.cookies.get(SESSION_ID)))
        authorization = Authorization(calendar_data=CalendarData(data_folder=current_app.config["DATA_FOLDER"]))
        if "calendar_id" not in kwargs:
            raise ValueError("calendar_id")
        calendar_id = str(kwargs["calendar_id"])
        if not authorization.can_access(username=username, calendar_id=calendar_id):
            abort(403)
        return decorated_function(*args, **kwargs)

    return wrapper


def previous_month_link(year: int, month: int) -> str:
    month, year = GregorianCalendar.previous_month_and_year(year=year, month=month)
    return (
        ""
        if year < current_app.config["MIN_YEAR"] or year > current_app.config["MAX_YEAR"]
        else "?y={}&m={}".format(year, month)
    )


def next_month_link(year: int, month: int) -> str:
    month, year = GregorianCalendar.next_month_and_year(year=year, month=month)
    return (
        ""
        if year < current_app.config["MIN_YEAR"] or year > current_app.config["MAX_YEAR"]
        else "?y={}&m={}".format(year, month)
    )


def new_session_id() -> str:
    return str(uuid.uuid4())


def is_session_valid(session_id: str) -> bool:
    return cache.get(session_id) is not None


def add_session(session_id: str, username: str) -> None:
    cache.set(session_id, username, timeout=2678400)  # 1 month


def get_session_username(session_id: str) -> str:
    return str(cache.get(session_id))


def task_details_for_markup(details: str) -> str:
    if not current_app.config["AUTO_DECORATE_TASK_DETAILS_HYPERLINK"]:
        return details

    decorated_fragments = []

    fragments = re.split(URLS_REGEX_PATTERN, details)
    for index, fragment in enumerate(fragments):
        if index % 2 == 1:
            decorated_fragments.append(DECORATED_URL_FORMAT.format(fragment, fragment))
        else:
            decorated_fragments.append(fragment)

    return "".join(decorated_fragments)

def parse_iso_date(value: Optional[str], default: date) -> date:
    """Parse YYYY-MM-DD, returning the supplied default when invalid."""
    if value is None:
        return default

    try:
        return date.fromisoformat(value)
    except ValueError:
        return default


def start_of_week(value: date, first_weekday: int) -> date:
    """Return the first date of the week containing value."""
    days_since_week_start = (value.weekday() - first_weekday) % 7
    return value - timedelta(days=days_since_week_start)


def time_to_minutes(value: str) -> int:
    """Convert an HH:MM value to minutes after midnight."""
    try:
        parsed_time = datetime.strptime(value, "%H:%M").time()
    except (TypeError, ValueError):
        return 0

    return parsed_time.hour * 60 + parsed_time.minute


def prepare_week_task(
    task: Dict,
    start_hour: int,
    end_hour: int,
    hour_height: int,
    minimum_task_minutes: int,
) -> Optional[Dict]:
    """
    Add pixel positioning information to a timed task.

    Tasks completely outside the visible range are omitted.
    """
    if task.get("is_all_day", False):
        return dict(task)

    visible_start = start_hour * 60
    visible_end = end_hour * 60

    task_start = time_to_minutes(task.get("start_time", "00:00"))
    task_end = time_to_minutes(
        task.get("end_time") or task.get("start_time", "00:00")
    )

    # Treat an end time before the start as passing midnight.
    if task_end < task_start:
        task_end = 24 * 60

    if task_end == task_start:
        task_end += minimum_task_minutes

    clipped_start = max(task_start, visible_start)
    clipped_end = min(task_end, visible_end)

    if clipped_end <= clipped_start:
        return None

    pixels_per_minute = hour_height / 60
    prepared_task = dict(task)
    prepared_task["top"] = (
        clipped_start - visible_start
    ) * pixels_per_minute
    prepared_task["height"] = max(
        (clipped_end - clipped_start) * pixels_per_minute,
        20,
    )

    return prepared_task


def prepare_week_days(
    week_start: date,
    tasks: Dict[str, List[Dict]],
    start_hour: int,
    end_hour: int,
    hour_height: int,
    minimum_task_minutes: int,
) -> List[Dict]:
    """Build the seven day-column objects used by the weekly template."""
    days: List[Dict] = []

    for offset in range(7):
        day_date = week_start + timedelta(days=offset)
        day_tasks = tasks.get(day_date.isoformat(), [])

        all_day_tasks = [
            dict(task)
            for task in day_tasks
            if task.get("is_all_day", False)
        ]

        timed_tasks = []
        for task in day_tasks:
            if task.get("is_all_day", False):
                continue

            prepared_task = prepare_week_task(
                task=task,
                start_hour=start_hour,
                end_hour=end_hour,
                hour_height=hour_height,
                minimum_task_minutes=minimum_task_minutes,
            )

            if prepared_task is not None:
                timed_tasks.append(prepared_task)

        timed_tasks.sort(
            key=lambda task: time_to_minutes(
                task.get("start_time", "00:00")
            )
        )

        days.append(
            {
                "date": day_date,
                "date_key": day_date.isoformat(),
                "all_day_tasks": all_day_tasks,
                "timed_tasks": timed_tasks,
            }
        )

    return days
