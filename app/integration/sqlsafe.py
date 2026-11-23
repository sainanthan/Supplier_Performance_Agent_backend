"""SELECT-only guard for custom source queries (DI Phase 2).

The query is PARSED (sqlglot, per dialect), not pattern-matched:
- exactly one statement, and it must be a query (SELECT / WITH ... SELECT / UNION / INTERSECT / EXCEPT)
- no data-modifying or DDL node anywhere (INSERT / UPDATE / DELETE / MERGE / CREATE / DROP / ALTER / TRUNCATE, incl. inside a CTE)
- no SELECT ... INTO (creates a table), no FOR UPDATE / row locks
- no dangerous functions (file access, sleeps, remote / dynamic SQL, sequence changes, admin procedures)
Defence in depth: connectors also run every statement in a READ ONLY transaction (PostgreSQL / Oracle), with a server-side
timeout, a row limit, autocommit off and an unconditional rollback, using a read-only account.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

DIALECTS = {"postgresql": "postgres", "oracle": "oracle", "sqlserver": "tsql"}
MAX_ROWS = 1000

DENY_FUNC = re.compile(r"^(pg_sleep\w*|pg_read_\w+|pg_ls_\w+|pg_stat_file|lo_\w+|dblink\w*|pg_terminate_backend|pg_cancel_backend|set_config|"
                       r"pg_advisory\w*|pg_try_advisory\w*|pg_reload_conf|pg_rotate_logfile|txid_\w+|nextval|setval|currval|query_to_xml\w*|"
                       r"xp_\w+|sp_\w+|openrowset|opendatasource|openquery|openxml|bulk\w*|"
                       r"dbms_\w+(\.\w+)?|utl_\w+(\.\w+)?|sys\.\w+|httpuritype\w*|ctxsys\.\w+|xdburitype\w*|sleep|benchmark)$", re.I)

_BLOCKED = tuple(t for t in (getattr(exp, n, None) for n in (
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterTable", "TruncateTable", "Command", "Into", "Lock",
    "Grant", "Revoke", "Copy", "Set", "Use", "Transaction", "Commit", "Rollback", "LoadData", "Execute", "ExecuteSql", "Pragma")) if t)
_QUERY = tuple(t for t in (getattr(exp, n, None) for n in ("Select", "Union", "Intersect", "Except", "SetOperation", "Subquery")) if t)


class UnsafeQuery(ValueError):
    pass


def _fname(node) -> str:
    if isinstance(node, exp.Anonymous):
        name = node.name
    else:
        name = node.sql_name() if hasattr(node, "sql_name") else type(node).__name__
    parent = node.parent
    if isinstance(parent, exp.Dot):                                  # pkg.function(...) e.g. dbms_lock.sleep
        name = f"{parent.this.sql()}.{name}" if parent.expression is node else name
    return name or ""


def validate_select(sql: str, connector_key: str) -> str:
    """Return the cleaned query or raise UnsafeQuery with a plain-language reason."""
    dialect = DIALECTS.get(connector_key)
    if not dialect:
        raise UnsafeQuery("custom queries are only supported for database connectors")
    s = (sql or "").strip()
    s = re.sub(r"^```(sql)?|```$", "", s, flags=re.I).strip().rstrip(";").strip()
    if not s:
        raise UnsafeQuery("the query is empty")
    if len(s) > 20000:
        raise UnsafeQuery("the query is too long (max 20,000 characters)")
    try:
        stmts = [x for x in sqlglot.parse(s, read=dialect) if x is not None]
    except sqlglot.errors.ParseError as e:
        raise UnsafeQuery(f"the query could not be parsed: {str(e).splitlines()[0][:200]}") from None
    if len(stmts) != 1:
        raise UnsafeQuery("only ONE statement is allowed")
    st = stmts[0]
    if not isinstance(st, _QUERY):
        raise UnsafeQuery("only SELECT queries are allowed")
    for node in st.find_all(*_BLOCKED):
        raise UnsafeQuery(f"'{type(node).__name__.upper()}' is not allowed - this is a read-only SELECT")
    for f in st.find_all(exp.Func):
        n = _fname(f)
        if DENY_FUNC.match(n) or DENY_FUNC.match(n.split(".")[-1]):
            raise UnsafeQuery(f"function '{n}' is not allowed in source queries")
    if st.args.get("locks"):
        raise UnsafeQuery("row locks (FOR UPDATE) are not allowed")
    return s


def wrap_limit(sql: str, connector_key: str, limit: int, probe: int = 0) -> str:
    """Server-side row cap (the cursor is also capped with fetchmany). `probe` extra row(s) tell the caller if more rows exist."""
    n = max(1, min(int(limit), MAX_ROWS)) + max(0, min(int(probe), 1))
    if connector_key == "postgresql":
        return f"SELECT * FROM ({sql}) AS q LIMIT {n}"
    if connector_key == "oracle":
        return f"SELECT * FROM ({sql}) WHERE ROWNUM <= {n}"
    return sql                                                         # T-SQL: TOP cannot wrap ORDER BY sub-queries; fetchmany caps


def tainted_outputs(sql: str, connector_key: str, is_sensitive) -> set[str]:
    """Output names (lower case) that may carry sensitive data, so masking cannot be bypassed by aliases or expressions.
    A name is tainted if it is a sensitive column, a whole-row / table reference (e.g. row_to_json(t)), or the alias of any
    projection (at any depth: CTEs, sub-queries) whose expression references a tainted name. Iterated to a fixed point."""
    st = sqlglot.parse_one(sql, read=DIALECTS[connector_key])
    tables = {(t.alias_or_name or "").lower() for t in st.find_all(exp.Table)} | {(t.name or "").lower() for t in st.find_all(exp.Table)}
    tables |= {(c.alias or "").lower() for c in st.find_all(exp.CTE)} | {(s.alias or "").lower() for s in st.find_all(exp.Subquery)}
    tables.discard("")
    tainted = {c.name.lower() for c in st.find_all(exp.Column) if c.name and (is_sensitive(c.name) or (not c.table and c.name.lower() in tables))}
    tainted |= {t for t in tables if any(not c.table and c.name.lower() == t for c in st.find_all(exp.Column))}
    sel = list(st.find_all(exp.Select))
    while True:
        before = len(tainted)
        for s in sel:
            for proj in s.expressions:
                refs = {c.name.lower() for c in proj.find_all(exp.Column)}
                if refs & tainted and proj.alias_or_name:
                    tainted.add(proj.alias_or_name.lower())
        if len(tainted) == before:
            break
    # positional taint for the OUTER select list (covers unaliased expressions such as `netpr + 0` -> "?column?")
    pos: set[int] = set()

    def branches(node):
        if isinstance(node, exp.Select):
            return [node]
        if isinstance(node, exp.Subquery):
            return branches(node.this)
        left, right = node.args.get("this"), node.args.get("expression")
        return (branches(left) if left is not None else []) + (branches(right) if right is not None else [])

    for b in branches(st):
        if any(isinstance(p, exp.Star) or p.find(exp.Star) for p in b.expressions):
            continue                                                   # `*` keeps real column names -> name rule applies
        for i, proj in enumerate(b.expressions):
            if {c.name.lower() for c in proj.find_all(exp.Column)} & tainted:
                pos.add(i)
    return tainted | {f"#{i}" for i in pos}
