from cron_validator import CronValidator
from pydantic import AfterValidator
from typing_extensions import Annotated


def check_crontab(v: str) -> str:
    """Validates that the string is a valid crontab expression"""
    try:
        CronValidator.parse(v)
        return v
    except ValueError as ex:
        raise ValueError(f"{v} is not a valid crontab expression") from ex


CronTab = Annotated[str, AfterValidator(check_crontab)]


def every_x_minutes(minutes: int) -> CronTab:
    """Runs every x minutes"""
    return f"*/{minutes} * * * *"


def every_x_hours(hours: int) -> CronTab:
    "Runs every x hours (at minute 0)"
    return f"0 */{hours} * * *"


def every_x_days(days: int) -> CronTab:
    "Runs every x days (at minute 0)"
    return f"0 0 */{days} * *"


def daily_at_x(hour: int, minute: int) -> CronTab:
    """Runs every day at hour:minutes"""
    return f"{minute} {hour} * * *"


def hourly_at_minutes_x(minutes: list[int]) -> CronTab:
    """Runs every hour at the minutes specified
    ie. minutes:[1,31,51] -> it will run every hour at minutes 1, 31 and 51
    """
    return ",".join(str(min) for min in minutes) + " * * * *"
