from functools import wraps
from typing import Any, Callable, Type

import tenacity


class RetryException(Exception):
    """Exception to raise when retrying."""


def retry_for_exception(exception: Type[Exception]) -> Callable:
    """Decorator to automatically retry a function if a specific exception is raised."""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        @tenacity.retry(
            retry=tenacity.retry_if_exception_type(exception),
            stop=tenacity.stop_after_attempt(10),
            wait=tenacity.wait_fixed(1),
            # after=after_log(log, logging.INFO),
        )
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        return wrapper

    return decorator


retry = retry_for_exception(RetryException)
