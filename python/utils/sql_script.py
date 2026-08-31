"""
sql_script.py — split a multi-statement .sql file into individual statements
without breaking on semicolons that appear inside comments, string literals,
or $$ ... $$ stored-procedure bodies.

Unchanged from the project-llm-wiki template (ds-madhavan-ramani/org_mm_chat).
Why this exists: a plain `text.split(';')` truncates any statement that
happens to contain a ';' inside a `--` comment or a dollar-quoted proc body —
exactly the shape of file this template ships (CREATE_PROJECT / TEARDOWN_
PROJECT are both $$ ... $$ blocks with free-text comments inside). Splitting
naively either cuts a CREATE TABLE/PROCEDURE statement in half (Snowflake
error: unexpected '<EOF>') or, worse, silently splits a procedure body into
two statements that individually don't parse.
"""

import re
from typing import List

_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def split_sql_statements(script_text: str) -> List[str]:
    """
    Returns a list of individual, executable SQL statements (semicolons and
    surrounding whitespace stripped, blank/comment-only statements dropped).
    Correctly treats:
      - '--' line comments (semicolons inside them are ignored)
      - '...' and "..." string literals (semicolons inside them are ignored)
      - $$ ... $$ dollar-quoted blocks, e.g. stored procedure bodies
        (never split inside one, regardless of what it contains)
    """
    statements: List[str] = []
    buf: List[str] = []

    in_line_comment = False
    in_single_quote = False
    in_double_quote = False
    in_dollar_quote = False

    i = 0
    n = len(script_text)
    while i < n:
        ch = script_text[i]
        nxt2 = script_text[i:i + 2]

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_dollar_quote:
            buf.append(ch)
            if nxt2 == "$$":
                buf.append(script_text[i + 1])
                in_dollar_quote = False
                i += 2
                continue
            i += 1
            continue

        if in_single_quote:
            buf.append(ch)
            if ch == "'":
                in_single_quote = False
            i += 1
            continue

        if in_double_quote:
            buf.append(ch)
            if ch == '"':
                in_double_quote = False
            i += 1
            continue

        # Not inside any quoted/comment region
        if nxt2 == "--":
            in_line_comment = True
            buf.append(nxt2)
            i += 2
            continue
        if nxt2 == "$$":
            in_dollar_quote = True
            buf.append(nxt2)
            i += 2
            continue
        if ch == "'":
            in_single_quote = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double_quote = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if _has_executable_content(stmt):
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if _has_executable_content(tail):
        statements.append(tail)

    return statements


def _has_executable_content(stmt: str) -> bool:
    """True if stmt has any non-comment, non-whitespace content."""
    without_comments = _LINE_COMMENT_RE.sub("", stmt)
    return bool(without_comments.strip())


def run_sql_file(session, path: str) -> int:
    """Convenience wrapper: split and execute every statement in a file. Returns count run."""
    with open(path) as f:
        text = f.read()
    statements = split_sql_statements(text)
    for stmt in statements:
        session.sql(stmt).collect()
    return len(statements)
