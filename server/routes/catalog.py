"""Catalog / schema / table selector endpoints.

All routes accept the user's X-Forwarded-Access-Token via dependency so that
UC ACLs are enforced on-behalf-of the user, not the app service principal.
"""
from fastapi import APIRouter, Depends, Header, HTTPException
from typing import Optional

from ..config import get_user_client
from ..sql_client import (
    InvalidIdentifier,
    escape_ident,
    execute_sql,
    rows_as_dicts,
    validate_ident,
)


def _validate_loc(catalog: str, schema: Optional[str] = None) -> None:
    try:
        validate_ident(catalog, "catalog")
        if schema is not None:
            validate_ident(schema, "schema")
    except InvalidIdentifier as e:
        raise HTTPException(status_code=400, detail=str(e))

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


def _user_client(x_forwarded_access_token: Optional[str] = Header(default=None)):
    return get_user_client(x_forwarded_access_token)


@router.get("/catalogs")
def list_catalogs(client=Depends(_user_client)):
    """List catalogs the caller can USE, via SHOW CATALOGS on the bound warehouse.

    We use SQL (not the SDK's catalogs.list) because SHOW CATALOGS reflects UC
    grants uniformly for OBO callers — the SDK path has been observed to omit
    workspace catalogs under OBO tokens.
    """
    try:
        result = execute_sql(client, "SHOW CATALOGS")
        rows = rows_as_dicts(result)
        names = sorted({r.get("catalog") or r.get("catalog_name") or "" for r in rows})
        names = [n for n in names if n]
        return {"catalogs": names}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/schemas")
def list_schemas(catalog: str, client=Depends(_user_client)):
    _validate_loc(catalog)
    try:
        result = execute_sql(client, f"SHOW SCHEMAS IN `{escape_ident(catalog)}`")
        rows = rows_as_dicts(result)
        names = sorted({r.get("databaseName") or r.get("namespace") or r.get("database") or "" for r in rows})
        names = [n for n in names if n]
        return {"schemas": names}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tables")
def list_tables(
    catalog: str,
    schema: str,
    managed_only: bool = True,
    client=Depends(_user_client),
):
    """List managed Iceberg + Delta tables in a schema.

    SHOW TABLES + DESCRIBE EXTENDED on each to resolve provider + table type.
    Avoids system.information_schema.tables (not populated for Delta Sharing catalogs).
    """
    _validate_loc(catalog, schema)
    cat_q = escape_ident(catalog)
    sch_q = escape_ident(schema)
    try:
        show = execute_sql(client, f"SHOW TABLES IN `{cat_q}`.`{sch_q}`")
        show_rows = rows_as_dicts(show)
        names = [r.get("tableName") or r.get("table_name") for r in show_rows]
        names = [n for n in names if n]

        if not managed_only:
            return {"tables": [{"table_name": n, "data_source_format": None} for n in names]}

        out = []
        for n in names:
            try:
                n_q = escape_ident(n)
            except InvalidIdentifier:
                continue
            try:
                d = execute_sql(client, f"DESCRIBE EXTENDED `{cat_q}`.`{sch_q}`.`{n_q}`")
                fmt = None
                table_type = None
                for row in d["rows"]:
                    if not row or not row[0] or len(row) < 2:
                        continue
                    key = str(row[0]).strip().lower()
                    val = (row[1] or "").upper() if row[1] else None
                    if key == "provider":
                        fmt = val
                    elif key == "type":
                        table_type = val
                # Include both managed Iceberg and Delta — PO supports both
                if table_type == "MANAGED" and fmt in ("ICEBERG", "DELTA"):
                    out.append({"table_name": n, "data_source_format": fmt, "table_type": table_type})
            except Exception:
                continue
        return {"tables": out}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/whoami")
def whoami(
    x_forwarded_email: Optional[str] = Header(default=None),
    x_forwarded_user: Optional[str] = Header(default=None),
    x_forwarded_preferred_username: Optional[str] = Header(default=None),
):
    """Return the logged-in user identity.

    Databricks Apps injects these headers when the app is behind OAuth.
    """
    email = x_forwarded_email or x_forwarded_preferred_username or x_forwarded_user
    return {
        "email": email,
        "name": email.split("@")[0] if email and "@" in email else email,
    }


@router.get("/diag")
def diag(
    x_forwarded_email: Optional[str] = Header(default=None),
    x_forwarded_access_token: Optional[str] = Header(default=None),
    client=Depends(_user_client),
):
    """Diagnostic: who is actually running SQL?

    Runs SELECT current_user() via the OBO client and returns the result
    alongside header presence info. Used to verify OBO flow end-to-end.
    """
    # Hash the token rather than echoing a prefix — the prefix is enough to
    # fingerprint a session, but a SHA256 hash truncated to 8 chars lets us
    # confirm "same token across requests" without leaking material.
    import hashlib
    token_fp = (
        hashlib.sha256(x_forwarded_access_token.encode("utf-8")).hexdigest()[:8]
        if x_forwarded_access_token else None
    )
    out = {
        "x_forwarded_email_present": bool(x_forwarded_email),
        "x_forwarded_email": x_forwarded_email,
        "x_forwarded_access_token_present": bool(x_forwarded_access_token),
        "token_fingerprint_sha256_8": token_fp,
        "sql_current_user": None,
        "sql_error": None,
    }
    try:
        r = execute_sql(client, "SELECT current_user() AS me, is_account_group_member('admins') AS is_admin")
        rows = rows_as_dicts(r)
        if rows:
            out["sql_current_user"] = rows[0].get("me")
            out["is_admin"] = rows[0].get("is_admin")
    except Exception as e:
        out["sql_error"] = str(e)
    return out
