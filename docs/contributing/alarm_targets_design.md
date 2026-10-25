# Design: alarm targets per organisation and environment

Status: **proposal for discussion** (0.29.0). Nothing here is implemented
yet; the alarms of 0.28.0 ({ref}`gitops-alarms`) work as documented.

## The problem

In 0.28.0 a composition has one list of alarm targets:
`Controller(alarms=Alarms(targets=[...]))`. Every alarm of every
environment goes to every target. A team that runs environments for several
groups of people (customers, departments) on one controller needs each
group's alarms to reach that group, in the channel it picked (a chat room,
a phone topic, an inbox), and nobody else's alarms to reach it. Changing a
group's channel should not need a code change or a new controller image.

## Proposal in one paragraph

Webhooks are the general mechanism: every target is a `POST` of the alarm
document `piceli.alarm.v1` (or a short text derived from it) to a URL held in
a Secret. ntfy, Slack and e-mail are presets of that one mechanism, not
separate code paths. Targets get **names** and alarms get **routes**: a route
picks targets by the alarm's environment, organisation label and event. The
routes and target URLs can live in a Secret the controller reads at each
poll, so an organisation's channel changes without a release. `piceli gitops
alarms test TARGET` sends a test alarm and prints the answer.

## Targets

One target type with presets:

| Preset | What is sent | Secret holds |
| --- | --- | --- |
| `Notify.webhook(name=, url_env=, headers_env=)` | the JSON document `piceli.alarm.v1` | the URL, optional header values |
| `Notify.ntfy(name=, url_env=, token_env=)` | the headline as text, `Title` header | the topic URL, optional token |
| `Notify.slack(name=, url_env=)` (new) | `{"text": headline}` to an incoming-webhook URL | the webhook URL |
| `Notify.email(name=, to_env=, via_env=)` (new) | a short mail through an HTTP mail API (a webhook preset), or SMTP if asked for | the API URL and key, or SMTP host and login |
| `Notify.command(name=, argv=)` | the JSON on standard input | nothing (the command finds its own secrets) |

Open questions:

- Is e-mail over an HTTP mail API enough, or is SMTP needed? SMTP means a
  new client and its failure modes in the controller.
- Should a webhook target allow a template (field names a receiver wants)?
  A fixed document plus a small set of presets is easier to keep stable.

## Routes

```python
Alarms(
    targets=[
        Notify.webhook(name="ops", url_env="OPS_HOOK"),
        Notify.slack(name="team-a", url_env="TEAM_A_SLACK"),
        Notify.ntfy(name="team-b", url_env="TEAM_B_NTFY"),
    ],
    routes=[
        Route(targets=["ops"]),                                  # everything
        Route(environments=["team-a-*"], targets=["team-a"]),
        Route(labels={"org": "team-b"}, events=["env-failed"], targets=["team-b"]),
    ],
    secret="piceli-alarms",
)
```

- A route matches on environment name patterns, environment labels (a new
  `Environment(labels={"org": ...})`) and event kinds; every matching route
  sends to its targets, each target once per alarm.
- No route at all keeps today's behaviour: every alarm to every target.
- An alarm no route matches is counted in the status (`unrouted`), so a
  missing route is visible.

## Changing a group's channel without a release

Two levels, to discuss:

1. **Secret only.** Targets and routes are in code; only URLs are in the
   Secret. Changing a URL needs no release; adding a group does.
2. **Routes in the Secret.** The Secret may also hold a `routes.json` (same
   shape as above, targets by name). The controller reads it at each poll
   and validates it; an invalid file keeps the last valid routes and raises
   `gitops-alarm-routes-invalid` in the status. Adding a group is a Secret
   change.

Level 2 is what lets each group pick its own channel. It also means the
routing is not reviewed in Git; a `piceli gitops alarms routes --diff`
could show the change before it is applied.

## Test command

```sh
piceli gitops alarms test TARGET [--env ENV]
```

Sends one alarm of kind `test` through the named target (or through every
target a route picks for `ENV`), from the controller's pod so the same
Secret and network are used, and prints each target's answer (status code
or error). It never opens or closes a real alarm.

## What stays the same

- Secrets are never in the composition, logs, state or status; targets are
  shown by name and host only.
- Each alarm is sent once, with one "recovered" message.
- A failed send never stops the controller and is listed in the status.

## Not in scope

- Paging, escalation or on-call schedules: a webhook to such a service does
  that.
- Alarms from Nix hosts (they report to OpenTelemetry, not to the
  controller).
