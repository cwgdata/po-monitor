"""Statement Execution API wrapper.

Uses the Databricks SDK's statement_execution surface. Honors user OBO
(pass a user-scoped WorkspaceClient) so all data reads happen as the
logged-in user — no service principal bypasses UC ACLs.
"""
import re
from typing import Any, Optional
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import (
    StatementState,
    StatementParameterListItem,
    ExecuteStatementRequestOnWaitTimeout,
)
from .config import get_warehouse_id


# UC-safe identifier pattern. Reject anything that could break out of a
# backtick-quoted identifier — most importantly the backtick itself, plus
# control characters and SQL metacharacters. UC permits a wide character
# set in identifiers, but we choose to be conservative since every legitimate
# Databricks deployment we've seen uses [A-Za-z0-9_-] names.
_IDENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]{0,254}$")


class InvalidIdentifier(ValueError):
    """Raised when a catalog/schema/table identifier fails validation."""


def validate_ident(name: str, kind: str = "identifier") -> str:
    """Validate a catalog/schema/table identifier; raise on hostile input.

    Use at every route boundary BEFORE the name is interpolated into SQL.
    Names returned from `SHOW TABLES` / `SHOW SCHEMAS` should also pass
    through `escape_ident` rather than this — those are trusted to exist
    but may contain odd characters that we still need to safely quote.
    """
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise InvalidIdentifier(f"invalid {kind}: {name!r}")
    return name


def escape_ident(name: str) -> str:
    """Defensively escape an identifier for backtick-quoted interpolation.

    Used for names that come from SHOW TABLES / SHOW SCHEMAS where we trust
    they exist (UC returned them) but cannot trust their content. Doubles
    any embedded backtick, the canonical SQL escape for backtick quoting.
    Caller must still wrap the return value in backticks.
    """
    if not isinstance(name, str):
        raise InvalidIdentifier(f"non-string identifier: {name!r}")
    if "\x00" in name or "\n" in name or "\r" in name:
        raise InvalidIdentifier(f"control chars in identifier: {name!r}")
    return name.replace("`", "``")


def _coerce_params(parameters):
    """Accept raw dicts or SDK objects; return a list of StatementParameterListItem.

    The SDK serializes parameters via `.as_dict()` — passing raw dicts breaks with
    `'dict' object has no attribute 'as_dict'`. This normalizes at the boundary.
    """
    if not parameters:
        return None
    out = []
    for p in parameters:
        if isinstance(p, StatementParameterListItem):
            out.append(p)
        elif isinstance(p, dict):
            out.append(StatementParameterListItem(
                name=p.get("name"),
                value=p.get("value"),
                type=p.get("type"),
            ))
        else:
            out.append(p)
    return out


def execute_sql(
    client: WorkspaceClient,
    sql: str,
    warehouse_id: Optional[str] = None,
    parameters: Optional[list] = None,
    wait_timeout: str = "30s",
    fire_and_forget: bool = False,
) -> dict[str, Any]:
    """Execute SQL and return {columns, rows} or raise on error.

    parameters: list of {"name": str, "value": str, "type": str} for safe params.
    """
    wh = warehouse_id or get_warehouse_id()
    if not wh:
        raise RuntimeError(
            "No warehouse_id. Set DATABRICKS_WAREHOUSE_ID env or bind a sql-warehouse resource."
        )

    resp = client.statement_execution.execute_statement(
        statement=sql,
        warehouse_id=wh,
        wait_timeout=wait_timeout,
        on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CONTINUE,
        parameters=_coerce_params(parameters),
    )
    if fire_and_forget:
        state = resp.status.state if resp.status else None
        # Surface immediate failures (e.g. warehouse stopped, permission denied).
        if state in (StatementState.FAILED, StatementState.CLOSED, StatementState.CANCELED):
            err = resp.status.error if resp.status else None
            msg = err.message if err else f"submit state={state}"
            raise RuntimeError(f"SQL submit failed: {msg}\nSQL: {sql}")
        # Small/no-op ops can reach SUCCEEDED inside the wait window; flag so
        # the client knows it's already done and won't wait for a running entry.
        done = state == StatementState.SUCCEEDED
        return {
            "columns": [],
            "rows": [],
            "statement_id": resp.statement_id,
            "state": state.value if state else None,
            "done": done,
        }

    # Poll if needed
    statement_id = resp.statement_id
    while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        resp = client.statement_execution.get_statement(statement_id)

    if resp.status.state != StatementState.SUCCEEDED:
        err = resp.status.error
        msg = err.message if err else f"state={resp.status.state}"
        raise RuntimeError(f"SQL failed: {msg}\nSQL: {sql}")

    manifest = resp.manifest
    result = resp.result
    cols = [c.name for c in manifest.schema.columns] if manifest and manifest.schema else []
    rows = result.data_array if result and result.data_array else []
    return {"columns": cols, "rows": rows, "statement_id": statement_id}


def rows_as_dicts(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert {columns, rows} to list of dicts."""
    cols = result["columns"]
    return [dict(zip(cols, row)) for row in result["rows"]]
