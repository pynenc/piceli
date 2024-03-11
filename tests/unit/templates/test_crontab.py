from typing import Callable

import pytest
from pydantic import BaseModel, ValidationError

from piceli.k8s.templates.auxiliary import crontab


class CheckCronTab(BaseModel):
    cron: crontab.CronTab


class TestCronTab:
    @pytest.mark.parametrize(
        "valid_cron",
        [
            "*/15 * * * *",  # Every 15 minutes
            "0 */2 * * *",  # Every 2 hours
            "0 0 */1 * *",  # Every day
            "0 0 * * 0",  # Every Sunday
            "0 22 * * 1-5",  # Every weekday at 10pm
        ],
    )
    def test_valid_cron_expressions(self, valid_cron: str) -> None:
        try:
            cron = crontab.CronTab(valid_cron)
            assert (
                str(cron) == valid_cron
            ), "CronTab should store the valid cron expression"
        except ValidationError:
            pytest.fail("CronTab raised ValidationError unexpectedly!")

    @pytest.mark.parametrize(
        "invalid_cron",
        [
            "77 * * * *",  # Invalid minute
            "* 25 * * *",  # Invalid hour
            "not a cron",  # Completely invalid format
            "",  # Empty string
            "* * *",  # Incomplete expression
        ],
    )
    def test_invalid_cron_expressions(self, invalid_cron: str) -> None:
        with pytest.raises(ValidationError):
            CheckCronTab(cron=invalid_cron)


@pytest.mark.parametrize(
    "method, args, expected",
    [
        (crontab.every_x_minutes, (30,), "*/30 * * * *"),
        (crontab.every_x_hours, (4,), "0 */4 * * *"),
        (crontab.every_x_days, (2,), "0 0 */2 * *"),
        (crontab.daily_at_x, (14, 15), "15 14 * * *"),
        (crontab.hourly_at_minutes_x, ([5, 15, 45],), "5,15,45 * * * *"),
    ],
)
def test_crontab_helper_methods(method: Callable, args: tuple, expected: str) -> None:
    cron = method(*args)
    assert str(cron) == expected, f"Expected {expected}, got {str(cron)}"
