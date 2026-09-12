import pytest

from aftercare_agent.observability import InMemoryTracer


def test_in_memory_tracer_records_bounded_success_and_error() -> None:
    tracer = InMemoryTracer()
    with tracer.span("aftercare.run", {"tenant_id": "tenant-1"}):
        pass
    with pytest.raises(RuntimeError):
        with tracer.span("aftercare.tool", {"case_id": "case-1"}):
            raise RuntimeError("synthetic")
    assert tracer.spans[0].status == "ok"
    assert tracer.spans[1].status == "error"
    assert tracer.spans[1].error_type == "RuntimeError"
    assert "synthetic" not in str(tracer.spans[1].__dict__)
