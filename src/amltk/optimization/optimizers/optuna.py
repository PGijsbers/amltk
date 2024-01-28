"""[Optuna](https://optuna.org/) is an automatic hyperparameter optimization
software framework, particularly designed for machine learning.

!!! tip "Requirements"

    This requires `Optuna` which can be installed with:

    ```bash
    pip install amltk[optuna]

    # Or directly
    pip install optuna
    ```

We provide a thin wrapper called
[`OptunaOptimizer`][amltk.optimization.optimizers.optuna.OptunaOptimizer] from which
you can integrate `Optuna` into your workflow.

This uses an Optuna-like [`search_space()`][amltk.pipeline.Node.search_space] for
its optimization.

Users should report results using
[`trial.success()`][amltk.optimization.Trial.success]
with either `cost=` or `values=` depending on any optimization directions
given to the underyling optimizer created. Please see their documentation
for more.

Visit their documentation for what you can pass to
[`OptunaOptimizer.create()`][amltk.optimization.optimizers.optuna.OptunaOptimizer.create],
which is forward to [`optun.create_study()`][optuna.create_study].

```python exec="True" source="material-block" result="python"
from __future__ import annotations

import logging

from sklearn.datasets import load_iris
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split

from amltk.optimization.optimizers.optuna import OptunaOptimizer
from amltk.scheduling import Scheduler
from amltk.optimization import History, Trial, Metric
from amltk.pipeline import Component

logging.basicConfig(level=logging.INFO)


def target_function(trial: Trial, pipeline: Pipeline) -> Trial.Report:
    X, y = load_iris(return_X_y=True)
    X_train, X_test, y_train, y_test = train_test_split(X, y)
    clf = pipeline.configure(trial.config).build("sklearn")

    with trial.profile("trial"):
        try:
            clf.fit(X_train, y_train)
            y_pred = clf.predict(X_test)
            accuracy = accuracy_score(y_test, y_pred)
            return trial.success(accuracy=accuracy)
        except Exception as e:
            return trial.fail(e)

from amltk._doc import make_picklable; make_picklable(target_function)  # markdown-exec: hide

pipeline = Component(RandomForestClassifier, space={"n_estimators": (10, 100)})

accuracy_metric = Metric("accuracy", minimize=False, bounds=(0, 1))
optimizer = OptunaOptimizer.create(space=pipeline, metrics=accuracy_metric, bucket="optuna-doc-example")

N_WORKERS = 2
scheduler = Scheduler.with_processes(N_WORKERS)
task = scheduler.task(target_function)

history = History()

@scheduler.on_start(repeat=N_WORKERS)
def on_start():
    trial = optimizer.ask()
    task.submit(trial, pipeline)

@task.on_result
def tell_and_launch_trial(_, report: Trial.Report):
    if scheduler.running():
        optimizer.tell(report)
        trial = optimizer.ask()
        task.submit(trial, pipeline)


@task.on_result
def add_to_history(_, report: Trial.Report):
    history.add(report)

scheduler.run(timeout=3, wait=False)

print(history.df())
optimizer.bucket.rmdir()  # markdown-exec: hide
```

!!! todo "Some more documentation"

    Sorry!

"""  # noqa: E501
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, overload
from typing_extensions import Self, override

import optuna
from more_itertools import first_true
from optuna.samplers import BaseSampler, NSGAIISampler, TPESampler
from optuna.study import Study, StudyDirection
from optuna.trial import (
    Trial as OptunaTrial,
    TrialState,
)

import amltk.randomness
from amltk.optimization import Optimizer, Trial
from amltk.optimization.metric import Metric
from amltk.pipeline import Node
from amltk.pipeline.parsers.optuna import parser
from amltk.store import PathBucket

if TYPE_CHECKING:
    from typing import Protocol

    from amltk.pipeline.parsers.optuna import OptunaSearchSpace
    from amltk.types import Seed

    class OptunaParser(Protocol):
        """A protocol for Optuna search space parser."""

        def __call__(
            self,
            node: Node,
            *,
            flat: bool = False,
            delim: str = ":",
        ) -> OptunaSearchSpace:
            """See [`optuna_parser`][amltk.pipeline.parsers.optuna.parser]."""
            ...


class OptunaOptimizer(Optimizer[OptunaTrial]):
    """An optimizer that uses Optuna to optimize a search space."""

    @override
    def __init__(
        self,
        *,
        study: Study,
        metrics: Metric | Sequence[Metric],
        bucket: PathBucket | None = None,
        seed: Seed | None = None,
        space: OptunaSearchSpace,
    ) -> None:
        """Initialize the optimizer.

        Args:
            study: The Optuna Study to use.
            metrics: The metrics to optimize.
            bucket: The bucket given to trials generated by this optimizer.
            space: Defines the current search space.
            seed: The seed to use for the sampler and trials.
        """
        # Verify the study has the same directions as the metrics
        match metrics:
            case Metric(minimize=minimize):
                _dir = StudyDirection.MINIMIZE if minimize else StudyDirection.MAXIMIZE
                if study.direction != _dir:
                    raise ValueError(
                        f"The study direction is {_dir}, but the metric minimize is "
                        f"{minimize}.",
                    )
            case metrics:
                _dirs = [
                    StudyDirection.MINIMIZE if m.minimize else StudyDirection.MAXIMIZE
                    for m in metrics
                ]
                if study.directions != _dirs:
                    raise ValueError(
                        f"The study directions are {_dirs}, but the metrics minimize "
                        f"are {[m.minimize for m in metrics]}.",
                    )

        metrics = [metrics] if isinstance(metrics, Metric) else metrics
        super().__init__(bucket=bucket, metrics=metrics)
        self.seed = amltk.randomness.as_int(seed)
        self.study = study
        self.metrics = metrics
        self.space = space

    @classmethod
    def create(
        cls,
        *,
        space: OptunaSearchSpace | Node,
        metrics: Metric | Sequence[Metric],
        bucket: PathBucket | str | Path | None = None,
        sampler: BaseSampler | None = None,
        seed: Seed | None = None,
        **kwargs: Any,
    ) -> Self:
        """Create a new Optuna optimizer. For more information, check Optuna
            documentation
            [here](https://optuna.readthedocs.io/en/stable/reference/generated/optuna.study.create_study.html#).

        Args:
            space: Defines the current search space.
            metrics: The metrics to optimize.
            bucket: The bucket given to trials generated by this optimizer.
            sampler: The sampler to use. Default is to use:

                * Single metric: [TPESampler][optuna.samplers.TPESampler]
                * Multiple metrics: [NSGAIISampler][optuna.samplers.NSGAIISampler]

            seed: The seed to use for the sampler and trials.

            **kwargs: Additional arguments to pass to
                [`optuna.create_study`][optuna.create_study].

        Returns:
            Self: The newly created optimizer.
        """
        if "direction" in kwargs:
            raise ValueError(
                "The direction should be provided through the 'metrics' argument.",
            )

        if isinstance(space, Node):
            space = space.search_space(parser=cls.preferred_parser())

        match bucket:
            case None:
                bucket = PathBucket(
                    f"{cls.__name__}-{datetime.now().isoformat()}",
                )
            case str() | Path():
                bucket = PathBucket(bucket)
            case bucket:
                bucket = bucket  # noqa: PLW0127

        match metrics:
            case Metric(minimize=minimize):
                direction = (
                    StudyDirection.MINIMIZE if minimize else StudyDirection.MAXIMIZE
                )
                study = optuna.create_study(direction=direction, **kwargs)
            case metrics:
                directions = [
                    StudyDirection.MINIMIZE if m.minimize else StudyDirection.MAXIMIZE
                    for m in metrics
                ]
                study = optuna.create_study(directions=directions, **kwargs)

        if sampler is None:
            sampler_seed = amltk.randomness.as_int(seed)
            match metrics:
                case Metric():
                    sampler = TPESampler(seed=sampler_seed)  # from `create_study()`
                case metrics:
                    sampler = NSGAIISampler(seed=sampler_seed)  # from `create_study()`

        return cls(study=study, metrics=metrics, space=space, bucket=bucket, seed=seed)

    @overload
    def ask(self, n: int) -> Iterable[Trial[OptunaTrial]]:
        ...

    @overload
    def ask(self, n: None = None) -> Trial[OptunaTrial]:
        ...

    @override
    def ask(
        self,
        n: int | None = None,
    ) -> Trial[OptunaTrial] | Iterable[Trial[OptunaTrial]]:
        """Ask the optimizer for a new config.

        Returns:
            The trial info for the new config.
        """
        if n is not None:
            return (self.ask(n=None) for _ in range(n))

        optuna_trial: optuna.Trial = self.study.ask(self.space)
        config = optuna_trial.params
        trial_number = optuna_trial.number
        unique_name = f"{trial_number=}"
        metrics = [self.metrics] if isinstance(self.metrics, Metric) else self.metrics
        return Trial(
            name=unique_name,
            seed=self.seed,
            config=config,
            info=optuna_trial,
            bucket=self.bucket,
            metrics=metrics,
        )

    @override
    def tell(self, report: Trial.Report[OptunaTrial]) -> None:
        """Tell the optimizer the result of the sampled config.

        Args:
            report: The report of the trial.
        """
        trial = report.trial.info
        assert trial is not None

        match report.status:
            case Trial.Status.CRASHED | Trial.Status.UNKNOWN | Trial.Status.FAIL:
                # NOTE: Can't tell any values if the trial crashed or failed
                self.study.tell(trial=trial, state=TrialState.FAIL)
            case Trial.Status.SUCCESS:
                match self.metrics:
                    case [metric]:
                        metric_value: Metric.Value = first_true(
                            report.metric_values,
                            pred=lambda m: m.metric == metric,
                            default=metric.worst,
                        )
                        self.study.tell(
                            trial=trial,
                            state=TrialState.COMPLETE,
                            values=metric_value.value,
                        )
                    case metrics:
                        # NOTE: We need to make sure that there sorted in the order
                        # that Optuna expects, with any missing metrics filled in
                        _lookup = {v.metric.name: v for v in report.metric_values}
                        values = [
                            _lookup.get(metric.name, metric.worst).value
                            for metric in metrics
                        ]
                        self.study.tell(
                            trial=trial,
                            state=TrialState.COMPLETE,
                            values=values,
                        )

    @override
    @classmethod
    def preferred_parser(cls) -> OptunaParser:
        return parser
