"""
Provides utilities for specifying and validating cron schedules for Kubernetes `CronJob` objects in Piceli.

This module includes a CronTab type for ensuring cron expressions are valid and utility functions for generating common cron scheduling patterns. It is particularly useful in conjunction with the {cronjob}`../deployable/cronjob` module for defining the timing of scheduled tasks within a Kubernetes cluster.

Utility Functions:
- `every_x_minutes(minutes: int) -> CronTab`: Generates a cron expression to run a job every X minutes.
- `every_x_hours(hours: int) -> CronTab`: Generates a cron expression to run a job every X hours.
- `every_x_days(days: int) -> CronTab`: Generates a cron expression to run a job every X days.
- `daily_at_x(hour: int, minute: int) -> CronTab`: Generates a cron expression to run a job daily at a specific hour and minute.
- `hourly_at_minutes_x(minutes: list[int]) -> CronTab`: Generates a cron expression to run a job hourly at specified minutes.

The `CronTab` type represents an annotated string that has been validated as a correct cron expression, ensuring the scheduling pattern is valid according to cron syntax.
"""
from cron_validator import CronValidator
from pydantic import AfterValidator
from typing_extensions import Annotated


def check_crontab(v: str) -> str:
    """
    Validates that the string is a valid crontab expression.

    :param str v: The crontab expression to validate.
    :return: The validated crontab expression.
    :raises ValueError: If the crontab expression is not valid.
    """
    try:
        CronValidator.parse(v)
        return v
    except ValueError as ex:
        raise ValueError(f"{v} is not a valid crontab expression") from ex


CronTab = Annotated[str, AfterValidator(check_crontab)]


def every_x_minutes(minutes: int) -> CronTab:
    """
    Generates a crontab expression to run a job every X minutes.

    :param int minutes: The number of minutes between each job execution.
    :return: A crontab expression.
    """
    return f"*/{minutes} * * * *"


def every_x_hours(hours: int) -> CronTab:
    """
    Generates a crontab expression to run a job every X hours.

    :param int hours: The number of hours between each job execution.
    :return: A crontab expression.
    """
    return f"0 */{hours} * * *"


def every_x_days(days: int) -> CronTab:
    """
    Generates a crontab expression to run a job every X days.

    :param int days: The number of days between each job execution.
    :return: A crontab expression.
    """
    return f"0 0 */{days} * *"


def daily_at_x(hour: int, minute: int) -> CronTab:
    """
    Generates a crontab expression to run a job every day at a specific time.

    :param int hour: The hour at which the job should run.
    :param int minute: The minute at which the job should run.
    :return: A crontab expression.
    """
    return f"{minute} {hour} * * *"


def hourly_at_minutes_x(minutes: list[int]) -> CronTab:
    """
    Generates a crontab expression to run a job at specific minutes past every hour.

    :param list[int] minutes: A list of minutes past the hour at which the job should run.
    :return: A crontab expression.
    """
    return ",".join(str(min) for min in minutes) + " * * * *"
