"""Trusted-host publication of identity-free backend contributions.

Publication authority is part of launch identity.  A runtime is therefore
visibly either read-only or able to admit contributions; the writer capability
itself never crosses into a backend request or durable runtime document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Mapping

from ..sessions import SessionSpec
from ..world import (
    PmwWriterAuthority,
    ResearchContribution,
    ResearchWorld,
)
from ..world.records import canonical_json
from .auth import PreparedCohort
from .contracts import BackendIdentity


PUBLICATION_IDENTITY_SCHEMA = "PMW_RUNTIME_PUBLICATION_IDENTITY_1"
PMW_PUBLICATION_PROTOCOL = "PMW_HOST_PUBLICATION_1"
_AUTHORITY_IDENTITY_DOMAIN = b"PMW_RUNTIME_WRITER_AUTHORITY_IDENTITY_1\0"


@dataclass(frozen=True, init=False)
class PublicationIdentity:
    """Bounded public identity for the host-side publication path."""

    mode: str
    protocol: str
    _public_config_bytes: bytes = field(repr=False)

    def __init__(
        self,
        *,
        mode: str,
        protocol: str,
        public_config: Mapping[str, object],
    ) -> None:
        # Reuse the generic public-identity validator, including its bounded
        # canonical clone and rejection of credential-bearing field names.
        validated = BackendIdentity(
            name=mode,
            protocol=protocol,
            public_config=public_config,
        )
        object.__setattr__(self, "mode", validated.name)
        object.__setattr__(self, "protocol", validated.protocol)
        object.__setattr__(
            self,
            "_public_config_bytes",
            canonical_json(validated.public_config),
        )

    @property
    def public_config(self) -> dict[str, object]:
        value = json.loads(self._public_config_bytes.decode("utf-8"))
        if type(value) is not dict:
            raise AssertionError("publication public config is not an object")
        return value

    def to_value(self) -> dict[str, object]:
        return {
            "schema": PUBLICATION_IDENTITY_SCHEMA,
            "mode": self.mode,
            "protocol": self.protocol,
            "public_config": self.public_config,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.to_value())).hexdigest()

    @classmethod
    def disabled(cls) -> "PublicationIdentity":
        return cls(mode="DISABLED", protocol="NO_PUBLICATION_1", public_config={})


@dataclass(slots=True)
class PmwContributionPublisher:
    """Bind authenticated session identity immediately before PMW admission.

    Neither the writer authority nor the writable world is ever placed in a
    :class:`~pmw_platform.runtime.contracts.SessionRequest`.
    """

    prepared: PreparedCohort
    world: ResearchWorld
    identity: PublicationIdentity

    @classmethod
    def create(
        cls,
        prepared: PreparedCohort,
        authority: PmwWriterAuthority,
    ) -> "PmwContributionPublisher":
        if not isinstance(prepared, PreparedCohort):
            raise TypeError("prepared must be an authenticated PreparedCohort")
        if not isinstance(authority, PmwWriterAuthority):
            raise TypeError("authority must be PmwWriterAuthority")
        world = ResearchWorld.open(
            prepared.registration.repo,
            world_id=prepared.registration.name,
            world_ref=prepared.registration.world_ref,
            writer=authority,
            required_snapshot_ref=prepared.plan.base_snapshot_ref,
        )
        authority_identity = {
            "channel_ref": authority.channel_ref,
            "invocation_ref": authority.invocation_ref,
            "process_ref": authority.process_ref,
            "principal_ref": authority.principal_ref,
            "episode_ref": authority.episode_ref,
            "capability_ref": authority.capability_ref,
            "scope_ref": authority.scope_ref,
            "policy_ref": authority.policy_ref,
            "policy_fingerprint": authority.policy_fingerprint,
            "maximum_calls": authority.maximum_calls,
            "maximum_delivery_attempts": authority.maximum_delivery_attempts,
            "maximum_content_bytes": authority.maximum_content_bytes,
            "maximum_parent_refs": authority.maximum_parent_refs,
        }
        authority_identity_sha256 = hashlib.sha256(
            _AUTHORITY_IDENTITY_DOMAIN + canonical_json(authority_identity)
        ).hexdigest()
        identity = PublicationIdentity(
            mode="PMW_BOUND",
            protocol=PMW_PUBLICATION_PROTOCOL,
            public_config={
                "world_id": prepared.plan.world_id,
                "world_ref": prepared.plan.world_ref,
                "base_snapshot_ref": prepared.plan.base_snapshot_ref,
                "principal_ref": authority.principal_ref,
                "scope_ref": authority.scope_ref,
                "policy_ref": authority.policy_ref,
                "policy_fingerprint": authority.policy_fingerprint,
                "authority_identity_sha256": authority_identity_sha256,
                "maximum_calls": authority.maximum_calls,
                "maximum_delivery_attempts": authority.maximum_delivery_attempts,
                "maximum_content_bytes": authority.maximum_content_bytes,
                "maximum_parent_refs": authority.maximum_parent_refs,
            },
        )
        return cls(prepared=prepared, world=world, identity=identity)

    def __call__(
        self,
        spec: SessionSpec,
        contribution: ResearchContribution,
    ) -> dict[str, object]:
        if spec not in self.prepared.plan.sessions:
            raise ValueError("session is not present in the authenticated plan")
        bound = self.world.bind_session(
            spec,
            artifact_exists=self.prepared.artifact_store.exists,
        )
        return bound.publish(contribution).to_value()
