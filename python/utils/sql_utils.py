"""
sql_utils.py — parameterized query helpers.

Unchanged from the project-llm-wiki template.
Security hardening: never build SQL by string-concatenating user input.
Schema/table names below come from config.ProjectConfig (developer-
controlled, never user input); the values that vary by request always go
through bind params.
"""

from typing import Any, Iterable, List


class SQLBuilder:
    @staticmethod
    def build_merge_raw_document(qualified_schema: str) -> str:
        """MERGE for idempotent document upsert, keyed on SOURCE_HASH.

        DOCUMENT_DATE is wrapped in TRY_TO_DATE(?): CONFIRMED on a live
        account that binding a bare Python None to a bare `? AS
        DOCUMENT_DATE` (a DATE column) failed with "Date 'None' is not
        recognized" -- i.e. the bind somehow surfaced as the literal
        string 'None' rather than SQL NULL in this execution path.
        TRY_TO_DATE sidesteps the exact mechanism: NULL stays NULL, a real
        date string still parses, and 'None' (or anything else
        unparseable) becomes NULL instead of erroring."""
        return f"""
            MERGE INTO {qualified_schema}.RAW_DOCUMENTS AS tgt
            USING (SELECT ? AS FILE_NAME, ? AS STAGE_PATH, ? AS SOURCE_TYPE,
                          ? AS SOURCE_ITEM_ID, TRY_TO_DATE(?) AS DOCUMENT_DATE,
                          ? AS RAW_TEXT, ? AS SOURCE_HASH, ? AS SOURCE_URL) AS src
            ON tgt.SOURCE_HASH = src.SOURCE_HASH
            WHEN NOT MATCHED THEN INSERT
                (FILE_NAME, STAGE_PATH, SOURCE_TYPE, SOURCE_ITEM_ID,
                 DOCUMENT_DATE, RAW_TEXT, SOURCE_HASH, SOURCE_URL, PARSED_AT)
                VALUES (src.FILE_NAME, src.STAGE_PATH, src.SOURCE_TYPE,
                        src.SOURCE_ITEM_ID, src.DOCUMENT_DATE, src.RAW_TEXT,
                        src.SOURCE_HASH, src.SOURCE_URL, CURRENT_TIMESTAMP())
        """

    @staticmethod
    def build_merge_raw_document_by_source_item(qualified_schema: str) -> str:
        """
        MERGE for network-drive-sourced documents, keyed on SOURCE_ITEM_ID
        (the file's UNC path — a stable per-file identity) instead of
        SOURCE_HASH. When the same item's content has changed since it was
        last ingested, this updates the existing row in place — rather
        than build_merge_raw_document's SOURCE_HASH-keyed behavior, which
        would insert a second row and leave the old, now-stale one (and
        its old index entries) sitting alongside it.

        FILE_NAME/STAGE_PATH/SOURCE_TYPE are refreshed on EVERY matched
        row, not just when SOURCE_HASH differs — CONFIRMED on a live
        account as the actual root cause of a citation link that stayed
        permanently broken ("Argument 2 to function 'GET_PRESIGNED_URL'
        cannot be null or empty") no matter how many times extraction was
        re-run: this project's stage got dropped/recreated and files
        re-copied more than once during earlier debugging, and a
        document's STAGE_PATH baked in during an earlier, since-fixed
        ingestion bug was never revisited by later runs once its
        SOURCE_HASH stopped changing, because the old single WHEN MATCHED
        clause only touched STAGE_PATH alongside RAW_TEXT/SOURCE_HASH.
        Where a file currently lives is metadata about ITS OWN LOCATION
        THIS RUN, not something that should be conditional on whether its
        parsed content also changed — RAW_TEXT/SOURCE_HASH/PARSED_AT stay
        conditional (re-parsing/re-indexing is the expensive, worth-
        avoiding part; refreshing a stage path string is not).
        """
        return f"""
            MERGE INTO {qualified_schema}.RAW_DOCUMENTS AS tgt
            USING (SELECT ? AS FILE_NAME, ? AS STAGE_PATH, ? AS SOURCE_TYPE,
                          ? AS SOURCE_ITEM_ID, TRY_TO_DATE(?) AS DOCUMENT_DATE,
                          ? AS RAW_TEXT, ? AS SOURCE_HASH, ? AS SOURCE_URL) AS src
            ON tgt.SOURCE_ITEM_ID = src.SOURCE_ITEM_ID
            WHEN MATCHED AND tgt.SOURCE_HASH != src.SOURCE_HASH THEN UPDATE SET
                FILE_NAME = src.FILE_NAME, STAGE_PATH = src.STAGE_PATH,
                SOURCE_TYPE = src.SOURCE_TYPE, RAW_TEXT = src.RAW_TEXT,
                SOURCE_HASH = src.SOURCE_HASH, SOURCE_URL = src.SOURCE_URL,
                PARSED_AT = CURRENT_TIMESTAMP()
            WHEN MATCHED THEN UPDATE SET
                FILE_NAME = src.FILE_NAME, STAGE_PATH = src.STAGE_PATH,
                SOURCE_TYPE = src.SOURCE_TYPE
            WHEN NOT MATCHED THEN INSERT
                (FILE_NAME, STAGE_PATH, SOURCE_TYPE, SOURCE_ITEM_ID,
                 DOCUMENT_DATE, RAW_TEXT, SOURCE_HASH, SOURCE_URL, PARSED_AT)
                VALUES (src.FILE_NAME, src.STAGE_PATH, src.SOURCE_TYPE,
                        src.SOURCE_ITEM_ID, src.DOCUMENT_DATE, src.RAW_TEXT,
                        src.SOURCE_HASH, src.SOURCE_URL, CURRENT_TIMESTAMP())
        """

    @staticmethod
    def build_insert_index_node(qualified_schema: str) -> str:
        return f"""
            INSERT INTO {qualified_schema}.DOCUMENT_INDEX
                (DOC_ID, PARENT_NODE_ID, NODE_LEVEL, NODE_TITLE, NODE_SUMMARY, NODE_TEXT_REF)
            SELECT ?, ?, ?, ?, ?, ?
        """

    @staticmethod
    def in_clause(values: Iterable[Any]) -> tuple[str, List[Any]]:
        """Returns ('(?, ?, ?)', [v1, v2, v3]) for a safe dynamic IN clause."""
        values = list(values)
        placeholders = ", ".join(["?"] * len(values))
        return f"({placeholders})", values
