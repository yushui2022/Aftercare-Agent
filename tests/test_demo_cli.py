"""Offline contract checks for the copyable synthetic demo entry point."""

import pytest

from aftercare_agent.demo import _new_case, build_parser, main


def test_demo_parser_defaults_are_explicit_and_case_children_are_scoped() -> None:
    args = build_parser().parse_args([])
    case = _new_case(tenant_id=args.tenant_id, case_id="case-demo", order_id=None)

    assert args.tenant_id == "demo-tenant"
    assert case.order_id == "case-demo-order"
    assert case.approval_id == "case-demo-approval"
    assert case.approval_run_id == "case-demo-approval-run"


def test_demo_requires_database_url(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as error:
        main([])

    assert error.value.code == 2
    assert "DATABASE_URL is required" in capsys.readouterr().err
