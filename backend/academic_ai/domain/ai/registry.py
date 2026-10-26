"""Compatibility: whether this version may be asked anything at all.

A model is not a function of its weights. It is a function of its weights
*and* the features it was trained on, the contract it implements, and the
optimizer it was benchmarked against. Change any of those under it and it
keeps answering — confidently, and wrongly, with no error anywhere.

That is the failure this module exists to prevent, and it is why
`AIModelVersion` stores `feature_schema_version`, `model_api_version` and
`optimizer_compatibility` rather than inferring them. An inferred value is
whatever is true today, which is exactly the value that cannot detect drift.

Compatibility is checked at **activation**, not at prediction time. A check
that runs per prediction fails in the middle of a solve, on a worker, where
the only visible symptom is a schedule that quietly got worse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

__all__ = [
    "CompatibilityReport",
    "FeatureSchemaRef",
    "ModelContract",
    "ModelEvaluationRecord",
    "OptimizerCompatibility",
    "RuntimeEnvironment",
    "check_compatibility",
]

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class ModelContract(str, Enum):
    """Which optimizer interface a model implements.

    Exactly the three named in ADR-0001. A model that implements none of them
    has nowhere to be called from, and registering one would be recording an
    intention rather than a capability.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    NEIGHBORHOOD_POLICY = "NeighborhoodPolicy"
    RECOMMENDATION_PROVIDER = "RecommendationProvider"
    CONFLICT_PRIORITY_PROVIDER = "ConflictPriorityProvider"


@dataclass(frozen=True, slots=True)
class FeatureSchemaRef:
    """A named, versioned set of features, and its hash.

    The hash is what makes this useful. A schema can be edited without its
    version being bumped — that is the normal way this goes wrong, not an
    exotic one — and comparing hashes catches it where comparing versions
    would not.
    """

    code: str
    version: str
    #: Hash over the ordered feature names and their types.
    content_hash: str
    feature_names: tuple[str, ...] = ()

    def matches(self, other: FeatureSchemaRef) -> bool:
        return (
            self.code == other.code
            and self.version == other.version
            and self.content_hash == other.content_hash
        )


@dataclass(frozen=True, slots=True)
class OptimizerCompatibility:
    """The range of optimizer versions a model was validated against.

    Inclusive lower bound, exclusive upper — the usual reading, stated because
    an off-by-one here activates a model against an optimizer nobody tested it
    with and the symptom is a slightly worse timetable.
    """

    minimum: str
    #: ``None`` means "no upper bound yet". Honest for a model benchmarked
    #: against the current optimizer and not since re-checked, and it is the
    #: caller's job to narrow it, not this module's job to guess.
    below: str | None = None

    def accepts(self, version: str) -> bool:
        target = _parse(version)
        if target < _parse(self.minimum):
            return False
        if self.below is not None and target >= _parse(self.below):
            return False
        return True

    def describe(self) -> str:
        return f">={self.minimum}" + (f",<{self.below}" if self.below else "")


@dataclass(frozen=True, slots=True)
class RuntimeEnvironment:
    """What is actually running, right now, where the model would be used."""

    optimizer_version: str
    model_api_version: str
    feature_schema: FeatureSchemaRef
    #: Contracts this runtime knows how to call. A model implementing a
    #: contract the runtime cannot invoke is not activatable here, whatever
    #: its metrics say.
    supported_contracts: frozenset[ModelContract] = frozenset(ModelContract)


@dataclass(frozen=True, slots=True)
class ModelEvaluationRecord:
    """One measured comparison against a named baseline.

    ``baseline_code`` is required. A metric with no baseline is a number, and
    the question activation turns on is not "is this model good" but "is it
    better than the heuristic it replaces" — which cannot be answered without
    naming the heuristic.
    """

    model_code: str
    version: str
    baseline_code: str
    dataset_version: str
    #: Tier-wise, best-first, matching `ScoreVector`. Negative is better.
    score_delta: tuple[int, ...] = ()
    tier_codes: tuple[str, ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)
    #: Whether the comparison used a fixed seed and a recorded dataset
    #: version. SESSION 4 forbids claiming an improvement without
    #: reproducible evidence; this is the flag that says whether it is.
    is_reproducible: bool = False

    @property
    def beats_baseline(self) -> bool:
        """Lexicographic, like everything else. The first tier that moves
        decides, and a tie is not a win."""
        for delta in self.score_delta:
            if delta != 0:
                return delta < 0
        return False


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    """Why a version may or may not be activated here."""

    is_compatible: bool
    blocking: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def describe(self) -> str:
        if self.is_compatible and not self.warnings:
            return "compatible"
        parts = [f"BLOCKING: {b}" for b in self.blocking]
        parts += [f"WARNING: {w}" for w in self.warnings]
        return "; ".join(parts)


def _parse(version: str) -> tuple[int, int, int]:
    match = _SEMVER.match(version.strip())
    if match is None:
        raise ValueError(
            f"{version!r} is not a semantic version. Comparing versions by "
            "string order puts 1.10.0 before 1.9.0, which would activate a "
            "model against an optimizer it was never validated on."
        )
    return (int(match[1]), int(match[2]), int(match[3]))


def check_compatibility(
    *,
    contract: ModelContract,
    model_api_version: str,
    feature_schema: FeatureSchemaRef,
    optimizer_compatibility: OptimizerCompatibility | None,
    runtime: RuntimeEnvironment,
    evaluations: Sequence[ModelEvaluationRecord] = (),
) -> CompatibilityReport:
    """Everything that must hold before a human is allowed to press ACTIVATE.

    Returns a report rather than raising: the caller is a person deciding, and
    a list of every reason is more use than the first one. Reasons that make
    activation *wrong* are blocking; reasons that make it *unwise* are
    warnings, and a human may proceed past those on their own authority —
    which is the correct place for that judgement to sit.
    """
    blocking: list[str] = []
    warnings: list[str] = []

    if contract not in runtime.supported_contracts:
        blocking.append(
            f"this runtime cannot call a {contract.value}; supported: "
            + ", ".join(sorted(c.value for c in runtime.supported_contracts))
        )

    if model_api_version != runtime.model_api_version:
        blocking.append(
            f"model_api_version {model_api_version} != runtime "
            f"{runtime.model_api_version}: the call signature the model was "
            "built against is not the one it would be called with"
        )

    live = runtime.feature_schema
    if feature_schema.code != live.code:
        blocking.append(
            f"feature schema {feature_schema.code} != runtime {live.code}"
        )
    elif feature_schema.version != live.version:
        blocking.append(
            f"feature schema {live.code} version {feature_schema.version} != "
            f"runtime {live.version}: the model would read different numbers "
            "out of the same field names"
        )
    elif feature_schema.content_hash != live.content_hash:
        blocking.append(
            f"feature schema {live.code} {live.version} has hash "
            f"{live.content_hash[:12]} but the model was trained against "
            f"{feature_schema.content_hash[:12]}: the schema was edited "
            "without its version being bumped, which is the ordinary way this "
            "goes wrong rather than an exotic one"
        )

    if optimizer_compatibility is None:
        warnings.append(
            "no optimizer_compatibility recorded, so nothing is known about "
            "which optimizer versions this model was validated against"
        )
    elif not optimizer_compatibility.accepts(runtime.optimizer_version):
        blocking.append(
            f"optimizer {runtime.optimizer_version} is outside "
            f"{optimizer_compatibility.describe()}"
        )

    if not evaluations:
        blocking.append(
            "no evaluation recorded: activation replaces a heuristic whose "
            "behaviour is known with a model whose behaviour is not, and "
            "without a comparison nobody can say which way that trade goes"
        )
    else:
        reproducible = [e for e in evaluations if e.is_reproducible]
        if not reproducible:
            blocking.append(
                "no reproducible evaluation: every comparison on record was "
                "run without a fixed seed or a recorded dataset version, so "
                "none of them can be repeated"
            )
        elif not any(e.beats_baseline for e in reproducible):
            warnings.append(
                "no reproducible evaluation beats its baseline; activating "
                "would replace the heuristic with something measured to be no "
                "better"
            )

    return CompatibilityReport(
        is_compatible=not blocking,
        blocking=tuple(blocking),
        warnings=tuple(warnings),
    )
