from __future__ import annotations

import pytest

from sdai.architecture_data_observer import (
    _config_hits,
    _python_orm_hits,
    _sql_hits,
)
from sdai.architecture_drift import ArchitectureDriftError, ArchitectureFactKind


def test_quoted_sql_identifiers_are_canonical_and_supported() -> None:
    hits = _sql_hits(
        'CREATE TABLE "Public"."Customers" (id bigint); SELECT * FROM "Public"."Customers";',
        source="migrations/quoted.sql",
    )

    assert [(item.kind, item.target, dict(item.attributes).get("access")) for item in hits] == [
        (ArchitectureFactKind.DATA_OWNERSHIP, "data:resource:public.customers", None),
        (ArchitectureFactKind.DATA_ACCESS, "data:resource:public.customers", "admin"),
        (ArchitectureFactKind.DATA_ACCESS, "data:resource:public.customers", "read"),
    ]


def test_cte_alias_and_dollar_quoted_literals_do_not_become_resources() -> None:
    hits = _sql_hits(
        """WITH recent AS (
  SELECT * FROM public.customers
)
SELECT * FROM recent;
SELECT $$FROM private.fake$$ AS value;
""",
        source="queries/report.sql",
    )

    reads = [item for item in hits if dict(item.attributes).get("access") == "read"]
    assert [item.target for item in reads] == ["data:resource:public.customers"]


def test_python_orm_parser_uses_ast_and_ignores_text_lookalikes() -> None:
    text = '''fake = "__tablename__ = \'not_a_table\'"\nclass Customer:\n    __tablename__ = "customers"\n'''
    hits = _python_orm_hits(text, source="models.py")

    assert len(hits) == 2
    assert {item.target for item in hits} == {"data:resource:customers"}
    assert {dict(item.attributes)["access"] for item in hits} == {"read", "write"}


def test_dynamic_python_orm_table_mapping_fails_closed() -> None:
    with pytest.raises(ArchitectureDriftError, match="dynamic Python ORM table mapping"):
        _python_orm_hits(
            "class Customer:\n    __tablename__ = table_name()\n",
            source="models.py",
        )


def test_malformed_database_port_fails_closed_without_echoing_connection_value() -> None:
    secret = "DO_NOT_ECHO"
    config = f"database.url=postgresql://user:{secret}@db.internal:notaport/orders\n"

    with pytest.raises(ArchitectureDriftError) as exc_info:
        _config_hits(config, source="application.properties")

    message = str(exc_info.value)
    assert "SDAI-ARCH-DATA-004" in message
    assert secret not in message
    assert "notaport" not in message
