"""Deterministic end-to-end Aftercare slice used by integration tests and demos.

This module intentionally stops at the provider boundary.  It demonstrates
the durable shape of a case: admission, a bounded Harness slice, a customer
input wait, atomic Inbox wake-up, and a second Worker resuming from the
checkpoint.  Real model calls, connectors, approvals, and provider effects
remain separate adapters.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from aftercare_agent.actions import ActionIntent
from aftercare_agent.domain.approvals import ApprovalRecord, ApprovalRequest
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.investigation import (
    BuyerAssertion,
    BuyerStatement,
    ClaimProposal,
    DeliveryStatus,
    FreshnessPolicy,
    InvestigationAssessment,
    InvestigationClaim,
    InvestigationEvidence,
    InvestigationProposal,
    InvestigationScope,
    LogisticsObservation,
    OrderSnapshot,
    OriginalReference,
    SourceGrant,
    SourceKind,
    assess_investigation,
    ingest_observation,
)
from aftercare_agent.domain.protocol import ArtifactReference, Checkpoint
from aftercare_agent.domain.runtime import (
    AdmissionKey,
    CaseRecord,
    OpenCaseInput,
    RunRecord,
    SessionRecord,
)
from aftercare_agent.domain.waits import InboxSignal, WaitRecord
from aftercare_agent.persistence import (
    ActionRepository,
    AdmissionRepository,
    ApprovalRepository,
    CheckpointRepository,
    Database,
    InvestigationAssessmentRepository,
    InvestigationObservationRepository,
    RunRepository,
    WaitRepository,
)

from .harness import run_fake_harness
from .worker import WorkerResult, run_next


@dataclass(frozen=True)
class SyntheticCase:
    """Stable identifiers for one reproducible aftercare demonstration."""

    tenant_id: str
    case_id: str
    order_id: str
    session_id: str
    run_id: str
    wait_id: str = "customer-input-1"
    wait_generation: int = 1
    approval_run_id: str = "approval-run-1"
    approval_wait_id: str = "approval-wait-1"
    approval_id: str = "approval-1"
    action_id: str = "refund-action-1"


@dataclass(frozen=True)
class WaitingSlice:
    case: SyntheticCase
    wait: WaitRecord
    checkpoint: Checkpoint


@dataclass(frozen=True)
class ApprovalSlice:
    run_id: str
    action_id: str
    approval_id: str
    wait_id: str
    approval: ApprovalRecord


class SyntheticAftercareFlow:
    """Small application service proving the runtime's durable boundaries.

    Every method opens short transactions and can therefore be called from
    separate processes.  The ``reply`` method is the same trusted boundary a
    webhook/ACP consumer would use; the model never supplies its scope.
    """

    def __init__(self, database: Database, case: SyntheticCase) -> None:
        self.database = database
        self.case = case

    @property
    def investigation_scope(self) -> InvestigationScope:
        return InvestigationScope(
            tenant_id=self.case.tenant_id,
            case_id=self.case.case_id,
            order_id=self.case.order_id,
        )

    @staticmethod
    def _registered_sources() -> dict[str, SourceKind]:
        return {
            "synthetic-erp": SourceKind.ORDER_LEDGER,
            "synthetic-carrier": SourceKind.CARRIER,
            "synthetic-buyer": SourceKind.BUYER_CHANNEL,
        }

    def seed_investigation_observations(
        self, *, now: datetime, delivery_status: DeliveryStatus = DeliveryStatus.IN_TRANSIT
    ) -> None:
        """Insert trusted synthetic order and carrier observations.

        The source registry and grants are application-owned.  A model can
        later propose claims, but it cannot choose these source identities.
        """
        scope = self.investigation_scope
        observations: tuple[InvestigationEvidence, ...] = (
            OrderSnapshot(
                **scope.model_dump(),
                evidence_id="order-observation-1",
                source_id="synthetic-erp",
                source_event_id="erp-event-1",
                observed_at=now - timedelta(minutes=2),
                received_at=now - timedelta(minutes=1),
                original=OriginalReference(
                    artifact_id="erp-artifact-1",
                    sha256="a" * 64,
                    excerpt="order paid and dispatched",
                ),
            ),
            LogisticsObservation(
                **scope.model_dump(),
                evidence_id="carrier-observation-1",
                source_id="synthetic-carrier",
                source_event_id="carrier-event-1",
                observed_at=now - timedelta(minutes=1),
                received_at=now - timedelta(seconds=30),
                original=OriginalReference(
                    artifact_id="carrier-artifact-1",
                    sha256="b" * 64,
                    excerpt="parcel remains in transit",
                ),
                delivery_status=delivery_status,
            ),
        )
        with self.database.transaction() as conn:
            for evidence in observations:
                accepted = ingest_observation(
                    scope,
                    SourceGrant(
                        **scope.model_dump(),
                        subject_id="synthetic-connector",
                        source_id=evidence.source_id,
                    ),
                    self._registered_sources(),
                    evidence,
                    now=now,
                )
                InvestigationObservationRepository().put(conn, accepted, scope=scope)

    def admit(self, *, message: str = "buyer reports non-receipt") -> None:
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        body = OpenCaseInput(
            order_id=self.case.order_id,
            channel="synthetic",
            message_ref=f"message-{self.case.case_id}",
            message_sha256=digest,
        )
        with self.database.transaction() as conn:
            AdmissionRepository().open(
                conn,
                AdmissionKey(tenant_id=self.case.tenant_id, idempotency_key=self.case.case_id),
                body,
                case=CaseRecord(
                    tenant_id=self.case.tenant_id,
                    case_id=self.case.case_id,
                    order_id=self.case.order_id,
                    version=1,
                ),
                session=SessionRecord(
                    tenant_id=self.case.tenant_id,
                    case_id=self.case.case_id,
                    session_id=self.case.session_id,
                    channel="synthetic",
                ),
                run=RunRecord(
                    tenant_id=self.case.tenant_id,
                    case_id=self.case.case_id,
                    run_id=self.case.run_id,
                    session_id=self.case.session_id,
                    definition_version="synthetic-aftercare-v1",
                    input_version=1,
                ),
            )

    def pause_for_customer(self, *, now: datetime) -> WaitingSlice:
        """Run one bounded slice and durably park it for customer material."""
        runs = RunRepository()
        waits = WaitRepository()
        checkpoints = CheckpointRepository()
        with self.database.transaction() as conn:
            claim = runs.claim(
                conn,
                self.case.tenant_id,
                self.case.run_id,
                "synthetic-worker-a",
                timedelta(seconds=30),
            )
            slot = AdmissionRepository().acquire_slot(conn, claim, timedelta(seconds=30))
            previous = checkpoints.get_latest(conn, self.case.tenant_id, self.case.run_id)
        # Model/tool work is deliberately outside the database transaction.
        result = run_fake_harness(
            tenant_id=self.case.tenant_id,
            case_id=self.case.case_id,
            run_id=self.case.run_id,
            checkpoint=previous,
            now=now,
            max_steps=2,
        )
        with self.database.transaction() as conn:
            wait = WaitRecord(
                tenant_id=self.case.tenant_id,
                case_id=self.case.case_id,
                run_id=self.case.run_id,
                wait_id=self.case.wait_id,
                generation=self.case.wait_generation,
                kind="input",
                correlation_key=f"customer:{self.case.case_id}",
                condition_version="synthetic-input-v1",
                created_at=now,
                deadline=now + timedelta(hours=1),
                state="PENDING",
            )
            checkpoint = result.checkpoint.model_copy(
                update={
                    "checkpoint_version": result.checkpoint.checkpoint_version + 1,
                    "saved_fencing_token": claim.fencing_token,
                    "next_step": "wait",
                    # Persist the exact continuation phase.  The resumed
                    # Worker can therefore be selected by a generic daemon,
                    # without a global ``resume_next_step`` setting.
                    "resume_next_step": "tool",
                    "wait_id": wait.wait_id,
                    "wait_generation": wait.generation,
                }
            )
            checkpoints.save(conn, checkpoint, claim)
            waits.register(conn, wait, claim)
            runs.transition(
                conn,
                claim,
                "WAITING_INPUT",
                wait_id=wait.wait_id,
                wait_generation=wait.generation,
            )
            active = waits.activate(conn, wait)
            if slot is not None:
                AdmissionRepository().release_slot(conn, claim)
        return WaitingSlice(self.case, active, checkpoint)

    def reply(self, *, now: datetime, event_id: str = "customer-reply-1") -> WaitRecord:
        """Insert a scoped customer reply and atomically wake the Run."""
        signal = InboxSignal(
            tenant_id=self.case.tenant_id,
            case_id=self.case.case_id,
            run_id=self.case.run_id,
            event_id=event_id,
            source_id="synthetic-customer-channel",
            source_event_id=event_id,
            wait_id=self.case.wait_id,
            generation=self.case.wait_generation,
            kind="input",
            correlation_key=f"customer:{self.case.case_id}",
            condition_version="synthetic-input-v1",
            received_at=now,
            payload=ArtifactReference(
                tenant_id=self.case.tenant_id,
                case_id=self.case.case_id,
                reference_id=f"artifact-{event_id}",
                sha256=hashlib.sha256(event_id.encode("utf-8")).hexdigest(),
            ),
        )
        with self.database.transaction() as conn:
            resolved = WaitRepository().receive_and_resolve(conn, signal)
            if resolved is None:
                raise RuntimeError("synthetic wait was not found")
            # A real channel connector would normalize the signed payload
            # before this write.  The assertion is fixed here deliberately:
            # the model never gets to manufacture a buyer fact.
            scope = self.investigation_scope
            buyer = BuyerStatement(
                **scope.model_dump(),
                evidence_id=f"buyer-observation-{event_id}",
                source_id="synthetic-buyer",
                source_event_id=event_id,
                observed_at=now,
                received_at=now,
                original=OriginalReference(
                    artifact_id=f"artifact-{event_id}",
                    sha256=hashlib.sha256(event_id.encode("utf-8")).hexdigest(),
                    excerpt="buyer reports parcel not received",
                ),
                assertion=BuyerAssertion.NOT_RECEIVED,
            )
            accepted = ingest_observation(
                scope,
                SourceGrant(
                    **scope.model_dump(),
                    subject_id="synthetic-customer-channel",
                    source_id=buyer.source_id,
                ),
                self._registered_sources(),
                buyer,
                now=now,
            )
            InvestigationObservationRepository().put(conn, accepted, scope=scope)
            return resolved

    def assess(
        self,
        *,
        now: datetime,
        proposal: InvestigationProposal | None = None,
    ) -> InvestigationAssessment:
        """Evaluate the complete trusted observation ledger deterministically."""
        scope = self.investigation_scope
        policy = FreshnessPolicy(
            policy_id="synthetic-freshness",
            policy_version=1,
            order_max_age_seconds=86_400,
            carrier_max_age_seconds=86_400,
            buyer_max_age_seconds=86_400,
        )
        with self.database.transaction() as conn:
            observations = InvestigationObservationRepository().list_case(conn, scope=scope)
        if proposal is None:
            # Bind the default demo proposal to the actual ledger IDs.  A
            # channel event may legitimately use any stable event ID; a
            # hard-coded buyer reference would turn that into a false
            # unknown-reference review.
            claims: list[ClaimProposal] = []
            for evidence_type, claim in (
                (OrderSnapshot, InvestigationClaim.ORDER_RECORDED),
                (LogisticsObservation, None),
                (BuyerStatement, None),
            ):
                matches = [item for item in observations if isinstance(item, evidence_type)]
                if not matches:
                    continue
                evidence = matches[-1]
                selected_claim = claim
                if isinstance(evidence, LogisticsObservation):
                    selected_claim = {
                        DeliveryStatus.DELIVERED: InvestigationClaim.CARRIER_REPORTED_DELIVERED,
                        DeliveryStatus.IN_TRANSIT: InvestigationClaim.CARRIER_REPORTED_IN_TRANSIT,
                    }.get(evidence.delivery_status)
                elif isinstance(evidence, BuyerStatement):
                    selected_claim = {
                        BuyerAssertion.NOT_RECEIVED: InvestigationClaim.BUYER_REPORTED_NOT_RECEIVED,
                        BuyerAssertion.RECEIVED: InvestigationClaim.BUYER_REPORTED_RECEIVED,
                    }.get(evidence.assertion)
                if selected_claim is not None:
                    claims.append(
                        ClaimProposal(
                            claim=selected_claim,
                            evidence_refs=(evidence.evidence_id,),
                        )
                    )
            if not claims:
                raise RuntimeError("synthetic investigation has no supported observations")
            selected = InvestigationProposal(claims=tuple(claims))
        else:
            selected = proposal
        result = assess_investigation(
            scope,
            selected,
            observations,
            self._registered_sources(),
            policy,
            now=now,
        )
        with self.database.transaction() as conn:
            InvestigationAssessmentRepository().put(
                conn,
                tenant_id=self.case.tenant_id,
                case_id=self.case.case_id,
                run_id=self.case.run_id,
                assessment=result,
            )
        return result

    def request_refund_approval(self, *, now: datetime) -> ApprovalSlice:
        """Create a trusted refund obligation and park its approval Wait.

        Amount, currency and provider idempotency are fixed by this trusted
        demo host; no model output participates in the Action identity.
        """
        intent = ActionIntent(
            tenant_id=self.case.tenant_id,
            case_id=self.case.case_id,
            order_id=self.case.order_id,
            action_id=self.case.action_id,
            action_type="refund",
            business_key=f"payment:{self.case.order_id}:refund",
            idempotency_key=f"refund-request:{self.case.case_id}",
            parameters_sha256="c" * 64,
            amount_minor="2500",
            currency="USD",
            provider_idempotency_key=f"refund:{self.case.order_id}",
        )
        request_run = RunRecord(
            tenant_id=self.case.tenant_id,
            case_id=self.case.case_id,
            run_id=self.case.approval_run_id,
            session_id=self.case.session_id,
            predecessor_run_id=self.case.run_id,
            definition_version="synthetic-aftercare-v1",
            input_version=1,
        )
        wait = WaitRecord(
            tenant_id=self.case.tenant_id,
            case_id=self.case.case_id,
            run_id=self.case.approval_run_id,
            wait_id=self.case.approval_wait_id,
            generation=1,
            kind="approval",
            correlation_key=self.case.approval_id,
            condition_version=intent.parameters_sha256,
            created_at=now,
            deadline=now + timedelta(hours=1),
            state="PENDING",
        )
        request = ApprovalRequest(
            tenant_id=self.case.tenant_id,
            case_id=self.case.case_id,
            approval_id=self.case.approval_id,
            action_id=intent.action_id,
            action_parameters_sha256=intent.parameters_sha256,
            policy_version="refund-v1",
            requested_by="synthetic-agent",
            expires_at=now + timedelta(hours=1),
            run_id=request_run.run_id,
            wait_id=wait.wait_id,
            wait_generation=wait.generation,
        )
        runs = RunRepository()
        waits = WaitRepository()
        approvals = ApprovalRepository()
        with self.database.transaction() as conn:
            assessment = InvestigationAssessmentRepository().get_latest(
                conn,
                tenant_id=self.case.tenant_id,
                case_id=self.case.case_id,
                run_id=self.case.run_id,
            )
            if assessment is None or assessment.disposition != "recommendation_ready":
                raise ContractViolation(
                    ErrorCode.FORBIDDEN,
                    "only a recommendation-ready assessment may create a refund action",
                )
            existing_approval = approvals.get(conn, self.case.tenant_id, self.case.approval_id)
            if existing_approval is not None:
                if existing_approval.run_id != self.case.approval_run_id:
                    raise ContractViolation(ErrorCode.CONFLICT, "approval replay changed run")
                return ApprovalSlice(
                    run_id=self.case.approval_run_id,
                    action_id=self.case.action_id,
                    approval_id=existing_approval.approval_id,
                    wait_id=self.case.approval_wait_id,
                    approval=existing_approval,
                )
            runs.create_run(conn, request_run)
            claim = runs.claim(
                conn,
                self.case.tenant_id,
                request_run.run_id,
                "synthetic-worker-approval",
                timedelta(seconds=30),
            )
            slot = AdmissionRepository().acquire_slot(conn, claim, timedelta(seconds=30))
            ActionRepository().reserve(conn, intent, claim=claim)
            waits.register(conn, wait, claim)
            runs.transition(
                conn,
                claim,
                "WAITING_APPROVAL",
                wait_id=wait.wait_id,
                wait_generation=wait.generation,
            )
            waits.activate(conn, wait)
            approval, _ = approvals.request(conn, request)
            if slot is not None:
                AdmissionRepository().release_slot(conn, claim)
        return ApprovalSlice(
            run_id=request_run.run_id,
            action_id=intent.action_id,
            approval_id=approval.approval_id,
            wait_id=wait.wait_id,
            approval=approval,
        )

    def approve_and_confirm_refund(
        self, approval: ApprovalSlice, *, now: datetime, decision_key: str = "decision-1"
    ) -> str:
        """Approve, atomically wake, then dispatch a fake provider once."""
        approvals = ApprovalRepository()
        runs = RunRepository()
        actions = ActionRepository()
        provider_reference = f"synthetic-refund:{approval.action_id}"
        result_sha = hashlib.sha256(provider_reference.encode("utf-8")).hexdigest()
        with self.database.transaction() as conn:
            decided = approvals.decide(
                conn,
                self.case.tenant_id,
                approval.approval_id,
                approver="synthetic-ops",
                decision="APPROVED",
                decision_idempotency_key=decision_key,
                decision_reason="synthetic policy matched",
            )
            assert decided.decision == "APPROVED"
            existing = actions.get(conn, self.case.tenant_id, approval.action_id)
            if existing is not None and existing.state == "CONFIRMED":
                if (
                    existing.provider_reference != provider_reference
                    or existing.result_sha256 != result_sha
                ):
                    raise ContractViolation(ErrorCode.CONFLICT, "confirmed provider result changed")
                return provider_reference
        # The provider call is outside the transaction in production.  This
        # deterministic connector returns a stable reference and is safe to
        # replay only through the same Action idempotency key.
        with self.database.transaction() as conn:
            claim = runs.claim(
                conn,
                self.case.tenant_id,
                approval.run_id,
                "synthetic-provider-worker",
                timedelta(seconds=30),
            )
            actions.mark_requested(
                conn,
                approval.action_id,
                claim,
                approval_id=approval.approval_id,
                policy_version="refund-v1",
            )
        with self.database.transaction() as conn:
            actions.mark_result(
                conn,
                approval.action_id,
                claim,
                state="CONFIRMED",
                provider_reference=provider_reference,
                result_sha256=result_sha,
            )
            runs.transition(conn, claim, "COMPLETED")
        return provider_reference

    def resume(self, *, now: datetime) -> WorkerResult:
        """Let a new Worker claim the woken Run and finish the Harness slice."""
        result = run_next(
            self.database,
            tenant_id=self.case.tenant_id,
            owner="synthetic-worker-b",
            now=now,
        )
        if result is None:
            raise RuntimeError("synthetic run was not available after wake-up")
        return result


__all__ = ["ApprovalSlice", "SyntheticAftercareFlow", "SyntheticCase", "WaitingSlice"]
