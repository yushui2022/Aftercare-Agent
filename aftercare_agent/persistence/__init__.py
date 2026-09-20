"""PostgreSQL persistence primitives for the durable Aftercare runtime."""

from .actions import ActionRepository
from .admission import (
    MAX_PAGE_SIZE,
    AdmissionRepository,
    AdmissionResult,
    CaseListEntry,
    CaseRepository,
    RetryReservation,
    ScheduledClaim,
    SlotReservation,
)
from .approvals import ApprovalRepository
from .assessments import InvestigationAssessmentRepository
from .case_grants import CaseGrantRepository
from .db import Database, PoolStats, assert_schema_current, migrate, set_transaction_context
from .events import MAX_OUTBOX_BATCH, EventRepository, OutboxDelivery
from .investigations import (
    BuyerHistoryCursorRepository,
    InvestigationEgmBindingRepository,
    InvestigationEgmProjectionRepository,
    InvestigationEgmRevocationRepository,
    InvestigationObservationRepository,
)
from .pool_metrics import PoolStatsSampler, report_pool_stats, sampler_from_environment
from .projection import ProjectionIngestResult, ProjectionRepository
from .queue_metrics import (
    QueueStats,
    QueueStatsSampler,
    queue_sampler_from_environment,
    report_queue_stats,
)
from .repositories import (
    AttemptRepository,
    CheckpointRepository,
    RunRepository,
    SessionMessageRepository,
    SessionRepository,
    StepRepository,
)
from .reviews import ReviewRepository
from .strategy_migrations import StrategyMigrationRepository
from .waits import WaitRepository

__all__ = [
    "MAX_PAGE_SIZE",
    "MAX_OUTBOX_BATCH",
    "AdmissionRepository",
    "AdmissionResult",
    "ActionRepository",
    "CaseGrantRepository",
    "InvestigationAssessmentRepository",
    "ApprovalRepository",
    "AttemptRepository",
    "CaseListEntry",
    "CaseRepository",
    "BuyerHistoryCursorRepository",
    "RetryReservation",
    "ScheduledClaim",
    "SlotReservation",
    "CheckpointRepository",
    "Database",
    "EventRepository",
    "OutboxDelivery",
    "InvestigationObservationRepository",
    "InvestigationEgmBindingRepository",
    "InvestigationEgmProjectionRepository",
    "InvestigationEgmRevocationRepository",
    "PoolStats",
    "set_transaction_context",
    "PoolStatsSampler",
    "QueueStats",
    "QueueStatsSampler",
    "ProjectionIngestResult",
    "ProjectionRepository",
    "report_pool_stats",
    "sampler_from_environment",
    "queue_sampler_from_environment",
    "report_queue_stats",
    "ReviewRepository",
    "StrategyMigrationRepository",
    "WaitRepository",
    "RunRepository",
    "SessionRepository",
    "SessionMessageRepository",
    "StepRepository",
    "assert_schema_current",
    "migrate",
]
