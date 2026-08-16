"""Failure-isolated asynchronous cohort execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import math
from typing import Awaitable, Callable, Generic, TypeVar

from .model import CohortPlan, SessionSpec


ResultT = TypeVar("ResultT")
SessionWorker = Callable[[SessionSpec], Awaitable[ResultT]]
ReceiptSink = Callable[["SessionReceipt[ResultT]"], Awaitable[None]]
MAXIMUM_ERROR_MESSAGE_BYTES = 2_048


class SessionStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SessionReceipt(Generic[ResultT]):
    """The settlement of exactly one session, independent of its peers."""

    cohort_id: str
    session_id: str
    plan_sha256: str
    world_id: str
    world_ref: str
    base_snapshot_ref: str
    safety_profile: str
    safety_profile_sha256: str
    core_lock_sha256: str
    briefing_sha256: str
    status: SessionStatus
    result: ResultT | None = None
    error_type: str | None = None
    error_message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is SessionStatus.SUCCEEDED


@dataclass(frozen=True, slots=True)
class CohortReceipt(Generic[ResultT]):
    """An ordered index over independent session receipts."""

    cohort_id: str
    plan_sha256: str
    world_id: str
    world_ref: str
    base_snapshot_ref: str
    safety_profile: str
    safety_profile_sha256: str
    core_lock_sha256: str
    briefing_sha256: str
    receipts: tuple[SessionReceipt[ResultT], ...]

    @property
    def succeeded(self) -> tuple[SessionReceipt[ResultT], ...]:
        return tuple(receipt for receipt in self.receipts if receipt.succeeded)

    @property
    def failed(self) -> tuple[SessionReceipt[ResultT], ...]:
        return tuple(receipt for receipt in self.receipts if not receipt.succeeded)

    def for_session(self, session_id: str) -> SessionReceipt[ResultT]:
        for receipt in self.receipts:
            if receipt.session_id == session_id:
                return receipt
        raise KeyError(session_id)


def _safe_error_message(error: BaseException) -> str:
    try:
        value = str(error)
    except BaseException:
        value = "<exception message unavailable>"
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= MAXIMUM_ERROR_MESSAGE_BYTES:
        return encoded.decode("utf-8", errors="replace")
    suffix = b"...[truncated]"
    return (
        encoded[: MAXIMUM_ERROR_MESSAGE_BYTES - len(suffix)] + suffix
    ).decode("utf-8", errors="replace")


async def run_cohort(
    plan: CohortPlan,
    worker: SessionWorker[ResultT],
    *,
    receipt_sink: ReceiptSink[ResultT] | None = None,
    cancellation_grace_seconds: float = 30.0,
) -> CohortReceipt[ResultT]:
    """Run every frozen spec with bounded concurrency and local failures.

    Ordinary ``Exception`` instances raised by a worker become that session's
    failed receipt.  They never cancel sibling tasks.  External cancellation
    remains a cohort-control signal and is deliberately not swallowed.
    """

    if (
        isinstance(cancellation_grace_seconds, bool)
        or not isinstance(cancellation_grace_seconds, (int, float))
        or not math.isfinite(float(cancellation_grace_seconds))
        or cancellation_grace_seconds <= 0
    ):
        raise ValueError("cancellation_grace_seconds must be positive and finite")

    plan_digest = plan.sha256

    async def sink(receipt: SessionReceipt[ResultT]) -> None:
        if receipt_sink is not None:
            await receipt_sink(receipt)

    async def persist(receipt: SessionReceipt[ResultT]) -> None:
        """Finish an already-started settlement before propagating cancellation."""

        if receipt_sink is None:
            return
        sink_task = asyncio.create_task(sink(receipt))
        try:
            await asyncio.shield(sink_task)
        except asyncio.CancelledError as cancellation:
            current = asyncio.current_task()
            if current is None or not current.cancelling():
                raise
            # Once a receipt exists, its sink is the settlement boundary.  A
            # cohort cancellation may stop new work, but must not tear a
            # receipt write in half.  Repeated cancellation requests are
            # remembered by asyncio and propagated after the sink settles.
            while not sink_task.done():
                try:
                    await asyncio.shield(sink_task)
                except asyncio.CancelledError:
                    continue
            if not sink_task.cancelled():
                sink_task.result()
            raise cancellation

    async def run_one(spec: SessionSpec) -> SessionReceipt[ResultT]:
        async def invoke_worker() -> ResultT:
            # Normalize every Awaitable (including Future and custom
            # awaitables) into the coroutine required by create_task.  Calling
            # an ordinary callable happens inside this local task, so a
            # synchronous exception cannot escape and cancel sibling sessions.
            return await worker(spec)

        try:
            worker_task = asyncio.create_task(invoke_worker())
        except asyncio.CancelledError:
            receipt = _receipt(
                spec,
                plan_sha256=plan_digest,
                status=SessionStatus.CANCELLED,
                error_type="asyncio.CancelledError",
                error_message="worker cancelled itself before starting",
            )
            await persist(receipt)
            return receipt
        except Exception as error:
            # Event-loop task construction failures remain local to this
            # session.  Worker invocation failures are caught below after the
            # normalized task starts.
            receipt = _failure_receipt(
                spec, error, plan_sha256=plan_digest
            )
            await persist(receipt)
            return receipt
        try:
            result = await asyncio.shield(worker_task)
        except asyncio.CancelledError as error:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                # Ask the worker to stop, then persist what is actually known.
                # A real process adapter must make this cancellation killable;
                # a non-cooperative callable settles as UNKNOWN.
                cancel_requested = worker_task.cancel()
                done, _pending = await asyncio.wait(
                    {worker_task}, timeout=float(cancellation_grace_seconds)
                )
                if not done:
                    receipt = _receipt(
                        spec,
                        plan_sha256=plan_digest,
                        status=SessionStatus.UNKNOWN,
                        error_type="pmw_platform.UnsettledWorker",
                        error_message="worker did not settle during cancellation grace",
                    )
                else:
                    try:
                        result = worker_task.result()
                    except asyncio.CancelledError:
                        receipt = _receipt(
                            spec,
                            plan_sha256=plan_digest,
                            status=(
                                SessionStatus.UNKNOWN
                                if cancel_requested
                                else SessionStatus.CANCELLED
                            ),
                            error_type=(
                                "pmw_platform.UnsettledWorker"
                                if cancel_requested
                                else "asyncio.CancelledError"
                            ),
                            error_message=(
                                "async task stopped; underlying side effects are not proven stopped"
                                if cancel_requested
                                else "worker had already cancelled itself"
                            ),
                        )
                    except Exception as worker_error:
                        receipt = _failure_receipt(
                            spec, worker_error, plan_sha256=plan_digest
                        )
                    else:
                        receipt = _success_receipt(
                            spec, result, plan_sha256=plan_digest
                        )
                await persist(receipt)
                raise error
            receipt = _receipt(
                spec,
                plan_sha256=plan_digest,
                status=SessionStatus.CANCELLED,
                error_type="asyncio.CancelledError",
                error_message="worker cancelled itself",
            )
        except Exception as error:
            receipt = _failure_receipt(
                spec, error, plan_sha256=plan_digest
            )
        else:
            receipt = _success_receipt(
                spec, result, plan_sha256=plan_digest
            )
        await persist(receipt)
        return receipt

    receipts: list[SessionReceipt[ResultT] | None] = [None] * plan.count
    queue: asyncio.Queue[tuple[int, SessionSpec]] = asyncio.Queue()
    for index, session in enumerate(plan.sessions):
        queue.put_nowait((index, session))

    async def consume() -> None:
        while True:
            try:
                index, spec = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            receipts[index] = await run_one(spec)

    # The number of asyncio tasks is bounded by actual concurrency, not N.
    await asyncio.gather(*(consume() for _ in range(plan.concurrency)))
    if any(receipt is None for receipt in receipts):
        raise AssertionError("cohort worker pool left an unsettled session")
    settled = tuple(receipt for receipt in receipts if receipt is not None)
    return CohortReceipt(
        cohort_id=plan.cohort_id,
        plan_sha256=plan_digest,
        world_id=plan.world_id,
        world_ref=plan.world_ref,
        base_snapshot_ref=plan.base_snapshot_ref,
        safety_profile=plan.safety_profile,
        safety_profile_sha256=plan.safety_profile_sha256,
        core_lock_sha256=plan.core_lock_sha256,
        briefing_sha256=plan.briefing_sha256,
        receipts=settled,
    )


def _receipt(
    spec: SessionSpec,
    *,
    plan_sha256: str,
    status: SessionStatus,
    result: ResultT | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> SessionReceipt[ResultT]:
    return SessionReceipt(
        cohort_id=spec.cohort_id,
        session_id=spec.session_id,
        plan_sha256=plan_sha256,
        world_id=spec.world_id,
        world_ref=spec.world_ref,
        base_snapshot_ref=spec.base_snapshot_ref,
        safety_profile=spec.safety_profile,
        safety_profile_sha256=spec.safety_profile_sha256,
        core_lock_sha256=spec.core_lock_sha256,
        briefing_sha256=spec.briefing_sha256,
        status=status,
        result=result,
        error_type=error_type,
        error_message=error_message,
    )


def _success_receipt(
    spec: SessionSpec, result: ResultT, *, plan_sha256: str
) -> SessionReceipt[ResultT]:
    return _receipt(
        spec,
        plan_sha256=plan_sha256,
        status=SessionStatus.SUCCEEDED,
        result=result,
    )


def _failure_receipt(
    spec: SessionSpec, error: Exception, *, plan_sha256: str
) -> SessionReceipt[ResultT]:
    error_type = f"{type(error).__module__}.{type(error).__qualname__}"
    return _receipt(
        spec,
        plan_sha256=plan_sha256,
        status=SessionStatus.FAILED,
        error_type=error_type[:512],
        error_message=_safe_error_message(error),
    )
