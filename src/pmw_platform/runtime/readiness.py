"""Launch-bound required readiness checks.

Unlike advisory preflight, these checks run while the cohort RuntimeClaim is
held and their canonical public evidence becomes part of launch identity.
They remain synchronous, local-only host checks: no backend start, provider
request, OAuth refresh or model call is permitted.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
import json
import re
from typing import Mapping, Protocol, Sequence

from ..world.records import canonical_json
from .auth import PreparedCohort
from .context import ContextWindowPolicy
from .contracts import RuntimeBackend
from .publish import PublicationIdentity


REQUIRED_READINESS_SCHEMA = "PMW_RUNTIME_REQUIRED_READINESS_1"
MAXIMUM_REQUIRED_CHECKS = 22
MAXIMUM_REQUIRED_EVIDENCE_BYTES = 2_048
MAXIMUM_REQUIRED_READINESS_BYTES = 65_536

_NAME = re.compile(r"^[a-z][a-z0-9.-]{0,63}$")
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:access[_-]?token|refresh[_-]?token|bearer[_-]?token|"
    r"api[_-]?key|client[_-]?secret|password|credential[_-]?value|"
    r"private[_-]?key|session[_-]?secret)(?:$|[_-])",
    re.IGNORECASE,
)


class RequiredReadinessError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:2_000]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


class RequiredReadinessChecker(Protocol):
    @property
    def name(self) -> str: ...

    def verify(
        self,
        *,
        prepared: PreparedCohort,
        backend: RuntimeBackend,
        limits: object,
        context_policy: ContextWindowPolicy,
        publication_identity: PublicationIdentity,
    ) -> Mapping[str, object] | None: ...


def _public_evidence(value: object) -> dict[str, object]:
    try:
        raw = canonical_json(value)
    except Exception as error:
        raise RequiredReadinessError("READINESS_EVIDENCE_INVALID") from error
    if len(raw) > MAXIMUM_REQUIRED_EVIDENCE_BYTES:
        raise RequiredReadinessError("READINESS_EVIDENCE_TOO_LARGE")
    stack = [value]
    while stack:
        selected = stack.pop()
        if type(selected) is dict:
            for key, child in selected.items():
                if type(key) is not str or _SENSITIVE_KEY.search(key):
                    raise RequiredReadinessError("READINESS_EVIDENCE_SENSITIVE")
                stack.append(child)
        elif type(selected) is list:
            stack.extend(selected)
    cloned = json.loads(raw.decode("utf-8"))
    if type(cloned) is not dict:
        raise RequiredReadinessError("READINESS_EVIDENCE_INVALID")
    return cloned


@dataclass(frozen=True, slots=True)
class RequiredReadinessIdentity:
    checks: tuple[tuple[str, bytes], ...]

    def __post_init__(self) -> None:
        if len(self.checks) > MAXIMUM_REQUIRED_CHECKS:
            raise ValueError("too many required readiness checks")
        names = [name for name, _raw in self.checks]
        if len(set(names)) != len(names) or any(
            _NAME.fullmatch(name) is None for name in names
        ):
            raise ValueError("required readiness check names are invalid")
        if len(canonical_json(self.to_value())) > MAXIMUM_REQUIRED_READINESS_BYTES:
            raise ValueError("required readiness identity is too large")

    def to_value(self) -> dict[str, object]:
        return {
            "schema": REQUIRED_READINESS_SCHEMA,
            "checks": [
                {
                    "name": name,
                    "evidence": json.loads(raw.decode("utf-8")),
                }
                for name, raw in self.checks
            ],
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.to_value())).hexdigest()


def verify_required_readiness(
    checkers: Sequence[RequiredReadinessChecker],
    *,
    prepared: PreparedCohort,
    backend: RuntimeBackend,
    limits: object,
    context_policy: ContextWindowPolicy,
    publication_identity: PublicationIdentity,
) -> RequiredReadinessIdentity:
    if isinstance(checkers, (str, bytes)) or not isinstance(checkers, Sequence):
        raise TypeError("required readiness checkers must be a sequence")
    if len(checkers) > MAXIMUM_REQUIRED_CHECKS:
        raise ValueError("too many required readiness checkers")
    rows: list[tuple[str, bytes]] = []
    names: set[str] = set()
    for checker in checkers:
        try:
            name = checker.name
            if type(name) is not str or _NAME.fullmatch(name) is None or name in names:
                raise RequiredReadinessError("READINESS_CHECKER_IDENTITY_INVALID")
            names.add(name)
            result = checker.verify(
                prepared=prepared,
                backend=backend,
                limits=limits,
                context_policy=context_policy,
                publication_identity=publication_identity,
            )
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise RequiredReadinessError("READINESS_CHECKER_ASYNC_UNSUPPORTED")
            evidence = _public_evidence({} if result is None else result)
            rows.append((name, canonical_json(evidence)))
        except RequiredReadinessError:
            raise
        except Exception as error:
            code = getattr(error, "code", None)
            detail = code if type(code) is str else type(error).__name__
            raise RequiredReadinessError("REQUIRED_READINESS_FAILED", detail) from error
    return RequiredReadinessIdentity(tuple(rows))


__all__ = [
    "REQUIRED_READINESS_SCHEMA",
    "RequiredReadinessChecker",
    "RequiredReadinessError",
    "RequiredReadinessIdentity",
    "verify_required_readiness",
]
