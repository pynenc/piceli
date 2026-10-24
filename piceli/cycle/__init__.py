"""Cycle metrics: how long it takes from a push to running and checked.

A **cycle** is one commit's way through one target: an environment of the
GitOps controller, a Nix release it publishes, a Nix host that switches to
it, or a merge train run. Each cycle is a small record
(:func:`piceli.cycle.record.cycle`): the commit, when it was pushed (when
Piceli knows it), when Piceli first saw it, when it ran with its checks
passed, and how long each phase took.

Where the records come from:

- the controller keeps one per deploy step in its history (``cycle`` of a
  run, ``piceli-gitops-history``) and the last one per environment in its
  status: queued, approval, waiting for a build host, building, delivering,
  applying, checking;
- Nix releases: the stages the controller already records (approval,
  queue, fetch, build, publish);
- a train run keeps one in its run record (``cycle`` of ``run.json``):
  queued, merging, testing per step, promoting, images, publishing, pushing;
- ``piceli host wait`` keeps the host switches it saw in a small local file
  (:mod:`piceli.cycle.local`): polling, fetching, activating, checking,
  boot.

``piceli cycle report`` reads them all (:mod:`piceli.cycle.report`).

Importing this module is side-effect free.
"""

from __future__ import annotations

#: The JSON document of ``piceli cycle report --json``.
REPORT_SCHEMA = "piceli.cycle-report.v1"
#: Target kinds, in report order.
KINDS = ("environment", "nix-release", "nix-host", "train")
