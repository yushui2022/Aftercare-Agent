"""Transactional, explicit model-strategy migration control plane."""

import hashlib
import json
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.protocol import Checkpoint
from aftercare_agent.domain.strategy_migrations import (
    StrategyMigrationRecord,
    StrategyMigrationRequest,
)

from .gate_events import append_strategy_migrated

_FIELDS = (
    "tenant_id",
    "case_id",
    "run_id",
    "migration_id",
    "checkpoint_version_before",
    "checkpoint_version_after",
    "old_strategy_id",
    "old_model_config_version",
    "old_policy_version",
    "old_tool_schema_version",
    "new_strategy_id",
    "new_model_config_version",
    "new_policy_version",
    "new_tool_schema_version",
    "reason",
    "migrated_by",
    "idempotency_key",
    "created_at",
)


def _record(row: tuple[Any, ...]) -> StrategyMigrationRecord:
    return StrategyMigrationRecord.model_validate(dict(zip(_FIELDS, row, strict=False)))


class StrategyMigrationRepository:
    """Apply a strategy identity change while keeping the Run in REVIEW."""

    def get_by_key(
        self, conn: psycopg.Connection[Any], tenant_id: str, idempotency_key: str
    ) -> StrategyMigrationRecord | None:
        row = conn.execute(
            "SELECT " + ",".join(_FIELDS) + " FROM aftercare_strategy_migrations "
            "WHERE tenant_id=%s AND idempotency_key=%s",
            (tenant_id, idempotency_key),
        ).fetchone()
        return None if row is None else _record(row)

    @staticmethod
    def _same_request(record: StrategyMigrationRecord, request: StrategyMigrationRequest) -> bool:
        return (
            record.tenant_id == request.tenant_id
            and record.case_id == request.case_id
            and record.run_id == request.run_id
            and record.checkpoint_version_before == request.checkpoint_version
            and record.old_strategy_id == request.old_strategy_id
            and record.old_model_config_version == request.old_model_config_version
            and record.old_policy_version == request.old_policy_version
            and record.old_tool_schema_version == request.old_tool_schema_version
            and record.new_strategy_id == request.new_strategy_id
            and record.new_model_config_version == request.new_model_config_version
            and record.new_policy_version == request.new_policy_version
            and record.new_tool_schema_version == request.new_tool_schema_version
            and record.reason == request.reason
            and record.idempotency_key == request.idempotency_key
        )

    @staticmethod
    def _latest_checkpoint(
        conn: psycopg.Connection[Any], request: StrategyMigrationRequest
    ) -> Checkpoint:
        row = conn.execute(
            "SELECT checkpoint_version,payload FROM aftercare_checkpoints "
            "WHERE tenant_id=%s AND case_id=%s AND run_id=%s "
            "ORDER BY checkpoint_version DESC LIMIT 1 FOR UPDATE",
            (request.tenant_id, request.case_id, request.run_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.CONFLICT, "strategy migration needs a checkpoint")
        try:
            version = int(row[0])
            checkpoint = Checkpoint.model_validate_json(json.dumps(row[1], ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise ContractViolation(
                ErrorCode.RETRYABLE, "latest strategy checkpoint is invalid"
            ) from exc
        if version != checkpoint.checkpoint_version:
            raise ContractViolation(
                ErrorCode.RETRYABLE, "strategy checkpoint version is inconsistent"
            )
        if (checkpoint.tenant_id, checkpoint.case_id, checkpoint.run_id) != (
            request.tenant_id,
            request.case_id,
            request.run_id,
        ):
            raise ContractViolation(ErrorCode.FORBIDDEN, "strategy checkpoint scope mismatch")
        return checkpoint

    def migrate(
        self,
        conn: psycopg.Connection[Any],
        request: StrategyMigrationRequest,
        *,
        migrated_by: str,
    ) -> tuple[StrategyMigrationRecord, bool]:
        existing = self.get_by_key(conn, request.tenant_id, request.idempotency_key)
        if existing is not None:
            if not self._same_request(existing, request):
                raise ContractViolation(ErrorCode.CONFLICT, "strategy migration replay changed")
            return existing, True

        case = conn.execute(
            "SELECT 1 FROM aftercare_cases WHERE tenant_id=%s AND case_id=%s FOR UPDATE",
            (request.tenant_id, request.case_id),
        ).fetchone()
        if case is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "case not found")
        run = conn.execute(
            "SELECT state FROM aftercare_runs WHERE tenant_id=%s AND case_id=%s "
            "AND run_id=%s FOR UPDATE",
            (request.tenant_id, request.case_id, request.run_id),
        ).fetchone()
        if run is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "run not found")
        # The first lookup is a fast replay path.  Recheck after the Case/Run
        # locks because two callers with the same idempotency key can pass the
        # first lookup concurrently; the second caller must return the first
        # committed record instead of surfacing a unique-constraint error.
        existing = self.get_by_key(conn, request.tenant_id, request.idempotency_key)
        if existing is not None:
            if not self._same_request(existing, request):
                raise ContractViolation(ErrorCode.CONFLICT, "strategy migration replay changed")
            return existing, True
        if run[0] != "REVIEW":
            raise ContractViolation(ErrorCode.CONFLICT, "strategy migration requires REVIEW run")
        pending_review = conn.execute(
            "SELECT 1 FROM aftercare_reviews WHERE tenant_id=%s AND case_id=%s AND run_id=%s "
            "AND decision IS NULL ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
            (request.tenant_id, request.case_id, request.run_id),
        ).fetchone()
        if pending_review is None:
            raise ContractViolation(
                ErrorCode.CONFLICT, "strategy migration requires pending review"
            )
        checkpoint = self._latest_checkpoint(conn, request)
        if checkpoint.route_reason != "model_strategy_changed":
            raise ContractViolation(
                ErrorCode.CONFLICT, "strategy migration requires strategy-changed route"
            )
        if checkpoint.checkpoint_version != request.checkpoint_version:
            raise ContractViolation(ErrorCode.CONFLICT, "strategy checkpoint version changed")
        current = (
            checkpoint.strategy_id,
            checkpoint.model_config_version,
            checkpoint.policy_version,
            checkpoint.tool_schema_version,
        )
        expected = (
            request.old_strategy_id,
            request.old_model_config_version,
            request.old_policy_version,
            request.old_tool_schema_version,
        )
        if current != expected:
            raise ContractViolation(ErrorCode.CONFLICT, "strategy checkpoint identity changed")
        updated_checkpoint = checkpoint.model_copy(
            update={
                "checkpoint_version": checkpoint.checkpoint_version + 1,
                "strategy_id": request.new_strategy_id,
                "model_config_version": request.new_model_config_version,
                "policy_version": request.new_policy_version,
                "tool_schema_version": request.new_tool_schema_version,
                "route_reason": "strategy_migration_ready",
                "next_step": "review",
                "available_at": None,
            }
        )
        conn.execute(
            "INSERT INTO aftercare_checkpoints(tenant_id,case_id,run_id,checkpoint_version,"
            "saved_fencing_token,payload) VALUES (%s,%s,%s,%s,%s,%s)",
            (
                updated_checkpoint.tenant_id,
                updated_checkpoint.case_id,
                updated_checkpoint.run_id,
                updated_checkpoint.checkpoint_version,
                updated_checkpoint.saved_fencing_token,
                Jsonb(updated_checkpoint.model_dump(mode="json")),
            ),
        )
        migration_id = (
            "strategy-migration-"
            + hashlib.sha256(
                f"{request.tenant_id}:{request.case_id}:{request.run_id}:{request.idempotency_key}".encode()
            ).hexdigest()
        )
        row = conn.execute(
            "INSERT INTO aftercare_strategy_migrations(" + ",".join(_FIELDS[:-1]) + ") "
            "VALUES (" + ",".join(["%s"] * (len(_FIELDS) - 1)) + ") RETURNING " + ",".join(_FIELDS),
            (
                request.tenant_id,
                request.case_id,
                request.run_id,
                migration_id,
                checkpoint.checkpoint_version,
                updated_checkpoint.checkpoint_version,
                request.old_strategy_id,
                request.old_model_config_version,
                request.old_policy_version,
                request.old_tool_schema_version,
                request.new_strategy_id,
                request.new_model_config_version,
                request.new_policy_version,
                request.new_tool_schema_version,
                request.reason,
                migrated_by,
                request.idempotency_key,
            ),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.RETRYABLE, "strategy migration outcome is unknown")
        result = _record(row)
        append_strategy_migrated(conn, result)
        return result, False


__all__ = ["StrategyMigrationRepository"]
