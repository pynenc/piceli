# CronTab

Defines a set of utilities for specifying cron schedules in Kubernetes `CronJob` objects with Piceli.

Cron expressions are a powerful way to specify the timing and frequency of actions within a Kubernetes cluster. The CronTab module in Piceli provides validation for cron expressions and convenience functions to generate common scheduling patterns.

## Features

- **Cron Expression Validation**: Ensures that cron expressions are valid before applying them to `CronJob` definitions.
- **Utility Functions**: Simplify the creation of common cron scheduling patterns, such as running jobs every few minutes, hours, or days.

## Usage

The module is utilized in defining the `schedule` attribute for Kubernetes `CronJob` objects, as seen in the {cronjob}`../deployable/cronjob` module. It ensures that schedules are correctly formatted and valid according to cron syntax.

### Examples

- **Every X Minutes**:
  Runs a job every 10 minutes.

  ```python
  schedule = crontab.every_x_minutes(10)
  ```

- **Every X Hours**:
  Runs a job every hour, on the hour.

  ```python
  schedule = crontab.every_x_hours(1)
  ```

- **Every X Days**:
  Runs a job daily at midnight.

  ```python
  schedule = crontab.every_x_days(1)
  ```

- **Daily at Specific Time**:
  Runs a job every day at 14:30.

  ```python
  schedule = crontab.daily_at_x(14, 30)
  ```

- **Hourly at Specific Minutes**:
  Runs a job at minutes 15 and 45 of every hour.

  ```python
  schedule = crontab.hourly_at_minutes_x([15, 45])
  ```

These utility functions provide a straightforward way to define the timing and frequency of CronJob tasks in a Kubernetes cluster using Piceli.
