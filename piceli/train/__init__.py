"""A merge train: candidate branches join a base only after their merge passes.

Maturity: **preview** (the API may change before 1.0).

Declare a :class:`Train` in a module of the repository it tests (usually
the app's composition module), then run ``piceli train MODULE[:ATTR]
--branches a b c``. Each candidate set is merged (``git merge --no-ff``)
onto the train's current head in a temporary worktree, the spec is read
from that merged tree, and its steps run there: :meth:`Step.command`,
:class:`CargoCheck` and :class:`CargoTests` with an :class:`Impact`
selection (failed tests retried once by exact name; a pass on the retry is
recorded as flaky). A failing set is bisected; the failing branches are
ejected with the step that failed. ``--promote`` tags the head and moves
``tested`` and the base by fast-forward only, then runs the
:meth:`Step.publish` steps; ``--push`` pushes them.

Example::

    from piceli.train import CargoCheck, CargoTests, Impact, Step, Train

    train = Train(
        "shop",
        base="main",
        tested="tested",
        steps=[
            CargoCheck(),
            CargoTests(select=Impact()),
            Step.command("fitness", ["scripts/fitness.sh"]),
            Step.publish("prebuilt", ["scripts/publish_prebuilt.sh"]),
        ],
    )

See ``docs/train.md``. Importing this package has no side effects.
"""

from piceli.train.model import (
    DEFAULT_IGNORE,
    DEFAULT_ROOTS,
    CargoCheck,
    CargoTests,
    CommandStep,
    Impact,
    PublishStep,
    Retention,
    Step,
    Train,
    TrainError,
)
from piceli.train.notify import Notify

__all__ = [
    "DEFAULT_IGNORE",
    "DEFAULT_ROOTS",
    "CargoCheck",
    "CargoTests",
    "CommandStep",
    "Impact",
    "Notify",
    "PublishStep",
    "Retention",
    "Step",
    "Train",
    "TrainError",
]
