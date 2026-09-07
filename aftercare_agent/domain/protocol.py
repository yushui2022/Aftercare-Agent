"""Application-owned checkpoints and complete local tool intents, provider-neutral."""

import json
import math
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from .common import (
    CaseScope,
    ContractModel,
    ContractViolation,
    ErrorCode,
    Identifier,
    NonNegativeInt,
    PositiveInt,
    RunScope,
    SchemaVersion,
    Sha256,
    UtcDatetime,
    require_same_case,
    utc,
)


class ArtifactReference(CaseScope):
    reference_id: Identifier
    sha256: Sha256


class RemainingBudget(ContractModel):
    model_calls: NonNegativeInt
    tool_calls: NonNegativeInt
    cost_microusd: NonNegativeInt
    deadline: UtcDatetime


class ToolResultReference(ContractModel):
    call_id: Identifier
    step_id: Identifier
    attempt_id: Identifier
    outcome: Literal["succeeded", "error", "unknown"]
    artifact: ArtifactReference


class Checkpoint(RunScope):
    schema_version: SchemaVersion = 1
    checkpoint_version: PositiveInt
    input_version: PositiveInt
    case_version: PositiveInt
    saved_fencing_token: PositiveInt
    step_id: Identifier | None = None
    attempt_id: Identifier | None = None
    definition_version: Identifier
    policy_version: Identifier
    tool_schema_version: Identifier
    model_config_version: Identifier
    protocol_version: Identifier
    protocol_ref: ArtifactReference | None = None
    tool_results: tuple[ToolResultReference, ...] = ()
    evidence_refs: tuple[ArtifactReference, ...] = ()
    action_ids: tuple[Identifier, ...] = ()
    remaining_budget: RemainingBudget
    next_step: Literal["model", "tool", "evaluate", "wait", "retry", "review", "complete"]
    wait_id: Identifier | None = None
    wait_generation: PositiveInt | None = None
    available_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def coherent_checkpoint(self) -> Self:
        if self.attempt_id is not None and self.step_id is None:
            raise ValueError("attempt requires its step")
        if self.next_step == "wait":
            if self.wait_id is None or self.wait_generation is None:
                raise ValueError("wait checkpoint needs an exact wait generation")
        elif self.wait_id is not None or self.wait_generation is not None:
            raise ValueError("non-wait checkpoint must not contain an active wait")
        if (self.next_step == "retry") != (self.available_at is not None):
            raise ValueError("retry checkpoint needs available_at, other kinds must omit it")
        refs = [r.artifact for r in self.tool_results] + list(self.evidence_refs)
        if self.protocol_ref is not None:
            refs.append(self.protocol_ref)
        for ref in refs:
            require_same_case(self, ref)
        if len({r.call_id for r in self.tool_results}) != len(self.tool_results):
            raise ValueError("tool result call IDs must be unique in a checkpoint")
        return self


def strict_json_object(payload: str) -> dict[str, object]:
    """Reject ambiguous duplicate keys, non-JSON constants and oversized wire input."""
    try:
        payload_bytes = payload.encode("utf-8")
    except UnicodeError as exc:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "JSON input must encode as UTF-8") from exc
    if len(payload_bytes) > 65_536:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "JSON input exceeds 65536 bytes")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(value: str) -> object:
        raise ValueError(f"non-JSON constant: {value}")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number")
        return parsed

    try:
        decoded: object = json.loads(
            payload, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float
        )
    except (ValueError, RecursionError) as exc:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "malformed or ambiguous JSON") from exc
    if not isinstance(decoded, dict):
        raise ContractViolation(ErrorCode.INVALID_INPUT, "JSON object required")
    return decoded


def decode_checkpoint(payload: str) -> Checkpoint:
    value = strict_json_object(payload)
    version = value.get("schema_version")
    if type(version) is not int:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "explicit integer schema_version required")
    if version != 1:
        raise ContractViolation(ErrorCode.UNSUPPORTED_VERSION, "checkpoint version unsupported")
    try:
        # JSON mode permits ISO timestamps/arrays while retaining strict integer validation.
        return Checkpoint.model_validate_json(payload)
    except ValidationError as exc:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "invalid checkpoint") from exc


def assert_resume_compatible(
    checkpoint: Checkpoint,
    scope: RunScope,
    *,
    expected_input_version: int,
    expected_case_version: int,
    protocol_version: str,
) -> None:
    require_same_case(scope, checkpoint)
    if checkpoint.run_id != scope.run_id:
        raise ContractViolation(ErrorCode.FORBIDDEN, "checkpoint belongs to another run")
    if checkpoint.protocol_version != protocol_version:
        raise ContractViolation(ErrorCode.UNSUPPORTED_VERSION, "protocol decoder unavailable")
    if (
        type(expected_input_version) is not int
        or type(expected_case_version) is not int
        or checkpoint.input_version != expected_input_version
        or checkpoint.case_version != expected_case_version
    ):
        raise ContractViolation(
            ErrorCode.CONFLICT, "checkpoint inputs need explicit reconciliation"
        )
    # saved_fencing_token is provenance, never the new execution claim.


class ToolRequest(ContractModel):
    schema_version: SchemaVersion = 1
    call_id: Identifier
    name: Identifier
    arguments_json: str = Field(max_length=65_536)


class BoundLookupArguments(ContractModel):
    """No model-supplied tenant, case or order: trusted tool closure binds those."""


class MaterialDraftArguments(ContractModel):
    questions: Annotated[
        tuple[Literal["confirm_address", "check_delivery_location", "provide_delivery_photo"], ...],
        Field(min_length=1, max_length=3),
    ]

    @model_validator(mode="after")
    def no_duplicates(self) -> Self:
        if len(set(self.questions)) != len(self.questions):
            raise ValueError("duplicate material question")
        return self


def validate_tool_request(
    request: ToolRequest,
    *,
    stream_complete: bool,
    allowed_tools: frozenset[str],
    budget: RemainingBudget,
    now: datetime,
) -> BoundLookupArguments | MaterialDraftArguments:
    """Produce validated arguments only. This function never invokes a tool."""
    if stream_complete is not True:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "partial tool call cannot execute")
    known_tools = {"lookup_order", "lookup_tracking", "request_material_draft"}
    if request.name not in known_tools or request.name not in allowed_tools:
        raise ContractViolation(ErrorCode.FORBIDDEN, "tool unavailable in this execution scope")
    if budget.tool_calls == 0 or utc(now) >= budget.deadline:
        raise ContractViolation(ErrorCode.BUDGET_EXHAUSTED, "tool budget exhausted")
    strict_json_object(request.arguments_json)
    try:
        if request.name == "request_material_draft":
            return MaterialDraftArguments.model_validate_json(request.arguments_json)
        return BoundLookupArguments.model_validate_json(request.arguments_json)
    except ValidationError as exc:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "tool arguments violate schema") from exc
