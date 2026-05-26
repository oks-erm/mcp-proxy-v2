import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Optional

from firestore_safety import (
    collection_access_error,
    filter_visible_collection_ids,
    json_sanitize,
    redact_document,
)
from google.cloud.firestore import Client as FirestoreDBClient
from limits import max_query_limit
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from mcp.types import CallToolResult
from mcp_platform.detail_level import parse_detail_level
from mcp_platform.envelope import tool_error
from mcp_platform.meta import with_response_meta
from mcp_platform.transport import structured_result

logger = logging.getLogger(__name__)


def _shape_document(
    doc_data: dict[str, Any],
    *,
    detail_level: str,
) -> dict[str, Any]:
    redacted = redact_document(doc_data)
    dl = parse_detail_level(detail_level, default="compact")
    if dl == "full":
        return redacted
    if dl == "summary":
        return _compact_firestore_doc(redacted, max_scalar_keys=48, max_string_len=800)
    return _compact_firestore_doc(redacted, max_scalar_keys=24, max_string_len=400)


def _compact_firestore_doc(d: dict[str, Any], *, max_scalar_keys: int, max_string_len: int) -> dict[str, Any]:
    """Keep _id and top-level scalar fields only (no nested dict/list values)."""
    out: dict[str, Any] = {}
    if "_id" in d:
        out["_id"] = d["_id"]
    keys = sorted(k for k in d if k != "_id")
    n = 0
    for k in keys:
        if n >= max_scalar_keys:
            break
        v = d[k]
        if isinstance(v, (str, int, float, bool)) or v is None:
            if isinstance(v, str) and len(v) > max_string_len:
                out[k] = v[:max_string_len] + "…"
            else:
                out[k] = v
            n += 1
    return out


@dataclass
class AppContext:
    db: FirestoreDBClient


@asynccontextmanager
async def mcp_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    from main import firestore_client

    if firestore_client is None:
        raise RuntimeError("Firestore client not initialised — MCP lifespan started before REST startup?")
    yield AppContext(db=firestore_client.db)


mcp = FastMCP(
    "Firestore Gateway MCP",
    instructions=(
        "You have access to a Firestore database (data-warehouse-firestore). "
        "Call list_collections to discover top-level collection names, then use count_documents, query_documents, "
        "or get_document. Deny-listed names are omitted from list_collections; policy is "
        "FIRESTORE_GATEWAY_DENIED_COLLECTIONS (default blocks e.g. tokens, n8n-tokens) and optional "
        "FIRESTORE_GATEWAY_DENIED_COLLECTION_PATTERNS (fnmatch globs). Values in sensitive field names "
        "are redacted in responses (<redacted>), not removed. "
        "Use detail_level=compact (default) for list/query/get: top-level scalars only, truncated strings. "
        "Use detail_level=full only when you need nested fields after confirming access."
    ),
    stateless_http=True,
    json_response=True,
    lifespan=mcp_lifespan,
    host="0.0.0.0",
)


@mcp.tool(structured_output=False)
def list_collections(
    detail_level: str = "compact",
    ctx: Context[ServerSession, AppContext] = None,
) -> CallToolResult:
    """List top-level Firestore collections.

    **Use when:**
        You need collection discovery before querying.

    **Args:**
        detail_level: ``compact`` (default) or ``full`` (same data; echoed for platform consistency).

    **Returns:**
        ``{"data": [string, ...], "detail_level", "meta": {...}}`` on success.
        ``data`` is the authoritative list field.

    **Notes:**
        Collection ids denied by ``FIRESTORE_GATEWAY_DENIED_COLLECTIONS`` or matching
        ``FIRESTORE_GATEWAY_DENIED_COLLECTION_PATTERNS`` are **omitted** from this list (they cannot be read anyway).

    **Errors:**
        ``{"error": string, "details": string}`` if listing fails.

    **Example:**
        Call with default args after initialize; then use query_documents on an allowed collection name.
    """
    db = ctx.request_context.lifespan_context.db
    dl = parse_detail_level(detail_level, default="compact")
    try:
        collections = filter_visible_collection_ids([col.id for col in db.collections()])
        payload = with_response_meta(
            {"data": collections, "detail_level": dl},
            tool="firestore_gateway_list_collections",
        )
        return structured_result(payload, content_sanitize=json_sanitize)
    except Exception as e:
        logger.error(f"MCP list_collections failed: {e}")
        return structured_result(
            tool_error(
                "list_failed",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or check Firestore availability.",
            ),
            content_sanitize=json_sanitize,
        )


@mcp.tool(structured_output=False)
def query_documents(
    collection: str,
    filters: Optional[list[dict[str, Any]]] = None,
    order_by: Optional[str] = None,
    order_direction: str = "ASCENDING",
    limit: int = 20,
    cursor_document_id: Optional[str] = None,
    detail_level: str = "compact",
    ctx: Context[ServerSession, AppContext] = None,
) -> CallToolResult:
    """Query documents from a Firestore collection with optional filters, ordering, and pagination.

    **Use when:**
        You need to read multiple documents from one known collection.

    **Args:**
        collection: Firestore collection name. Denied names are in FIRESTORE_GATEWAY_DENIED_COLLECTIONS.
        filters: Optional list of ``{field, operator, value}`` filter objects.
        order_by: Optional field name to sort by.
        order_direction: ASCENDING or DESCENDING.
        limit: Max documents (clamped to policy max).
        cursor_document_id: Start-after cursor from a previous response's ``next_cursor_document_id``.
        detail_level: ``compact`` (default: top-level scalars only), ``summary`` (more scalar keys), ``full`` (full redacted documents).

    **Returns:**
        ``{"documents": [...], "total_returned", "has_more", "next_cursor_document_id", "detail_level"}``.

    **Notes:**
        Empty ``documents`` is success, not an error. Sensitive field values appear as ``<redacted>``.

    **Errors:**
        ``{"error", "details"}`` when the collection is denied or the query fails.

    **Example:**
        ``query_documents(collection="projects", limit=5)``
    """
    from google.cloud import firestore

    denied = collection_access_error(collection)
    if denied:
        return structured_result(
            tool_error(
                "access_denied",
                details=denied,
                cause="permission",
                retryable=False,
                suggested_fix="Use an allowed collection or adjust FIRESTORE_GATEWAY_DENIED_* configuration.",
            ),
            content_sanitize=json_sanitize,
        )

    dl = parse_detail_level(detail_level, default="compact")
    limit = max(1, min(limit, max_query_limit()))
    db = ctx.request_context.lifespan_context.db

    try:
        query = db.collection(collection)

        if filters:
            for f in filters:
                field = f.get("field")
                operator = f.get("operator", "==")
                value = f.get("value")
                query = query.where(field, operator, value)

        if order_by:
            direction = firestore.Query.DESCENDING if order_direction == "DESCENDING" else firestore.Query.ASCENDING
            query = query.order_by(order_by, direction=direction)

        if cursor_document_id:
            doc_ref = db.collection(collection).document(cursor_document_id)
            doc_snapshot = doc_ref.get()
            if doc_snapshot.exists:
                query = query.start_after(doc_snapshot)

        query = query.limit(limit + 1)
        docs = list(query.stream())
        has_more = len(docs) > limit

        results = []
        last_doc_id = None
        for doc in docs[:limit]:
            doc_data = doc.to_dict()
            doc_data["_id"] = doc.id
            results.append(_shape_document(doc_data, detail_level=dl))
            last_doc_id = doc.id

        payload = with_response_meta(
            {
                "documents": results,
                "total_returned": len(results),
                "has_more": has_more,
                "next_cursor_document_id": last_doc_id if has_more else None,
                "detail_level": dl,
            },
            tool="firestore_gateway_query_documents",
            pagination={
                "limit": limit,
                "has_more": has_more,
                "next_cursor_document_id": last_doc_id if has_more else None,
            },
            data_from="documents",
        )
        return structured_result(payload, content_sanitize=json_sanitize)
    except Exception as e:
        logger.error(f"MCP query_documents failed: {e}")
        return structured_result(
            tool_error(
                "query_failed",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Fix filters or retry; check Firestore index requirements for compound queries.",
            ),
            content_sanitize=json_sanitize,
        )


@mcp.tool(structured_output=False)
def count_documents(
    collection: str,
    filters: Optional[list[dict[str, Any]]] = None,
    detail_level: str = "compact",
    ctx: Context[ServerSession, AppContext] = None,
) -> CallToolResult:
    """Count documents in a Firestore collection.

    **Use when:**
        You need the number of matching documents without fetching rows.

    **Args:**
        collection: Collection name (same allow/deny rules as query_documents).
        filters: Optional filters (same shape as query_documents).
        detail_level: Echoed as ``compact`` default.

    **Returns:**
        ``{"count": int, "detail_level": string}``.

    **Notes:**
        Empty matches yield ``count: 0``.

    **Errors:**
        ``{"error", "details"}`` when denied or aggregation fails.

    **Example:**
        ``count_documents(collection="projects")``
    """
    denied = collection_access_error(collection)
    if denied:
        return structured_result(
            tool_error(
                "access_denied",
                details=denied,
                cause="permission",
                retryable=False,
                suggested_fix="Use an allowed collection or adjust FIRESTORE_GATEWAY_DENIED_* configuration.",
            ),
            content_sanitize=json_sanitize,
        )

    dl = parse_detail_level(detail_level, default="compact")
    db = ctx.request_context.lifespan_context.db
    try:
        query = db.collection(collection)
        if filters:
            for f in filters:
                field = f.get("field")
                operator = f.get("operator", "==")
                value = f.get("value")
                query = query.where(field, operator, value)
        agg_query = query.count(alias="count")
        count = 0
        for result_group in agg_query.get():
            items = result_group if isinstance(result_group, (list, tuple)) else [result_group]
            for r in items:
                if getattr(r, "alias", None) == "count" and getattr(r, "value", None) is not None:
                    count = int(r.value)
                    break
            if count:
                break
        return structured_result({"count": count, "detail_level": dl}, content_sanitize=json_sanitize)
    except Exception as e:
        logger.error(f"MCP count_documents failed: {e}")
        return structured_result(
            tool_error(
                "count_failed",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Fix filters or retry; check Firestore index requirements.",
            ),
            content_sanitize=json_sanitize,
        )


@mcp.tool(structured_output=False)
def get_document(
    collection: str,
    document_id: Optional[str] = None,
    id: Optional[str] = None,
    detail_level: str = "compact",
    ctx: Context[ServerSession, AppContext] = None,
) -> CallToolResult:
    """Get a single Firestore document by ID.

    **Use when:**
        You know collection and document id.

    **Args:**
        collection: Collection name.
        document_id: Document id (or use ``id`` alias).
        id: Alias for document_id.
        detail_level: ``compact`` (default), ``summary``, or ``full`` (see query_documents).

    **Returns:**
        On success, structuredContent is the document object (with ``detail_level`` echoed in a sibling key is omitted — only document fields for full backward shape). For compact, only scalar top-level fields after redaction.

    **Notes:**
        Prefer over query when you have the exact id.

    **Errors:**
        ``{"error", "details"}`` for missing id or denied collection; ``collection_not_found`` when the root
        collection id does not exist; ``document_not_found`` when the collection exists but the document id does not;
        ``read_failed`` on transport errors.

    **Example:**
        ``get_document(collection="projects", document_id="abc")``
    """
    doc_id = document_id or id
    if not doc_id or not str(doc_id).strip():
        return structured_result(
            tool_error(
                "validation_error",
                details="document_id (or id) is required and must be non-empty",
                cause="validation",
                retryable=False,
            ),
            content_sanitize=json_sanitize,
        )

    denied = collection_access_error(collection)
    if denied:
        return structured_result(
            tool_error(
                "access_denied",
                details=denied,
                cause="permission",
                retryable=False,
                suggested_fix="Use an allowed collection or adjust FIRESTORE_GATEWAY_DENIED_* configuration.",
            ),
            content_sanitize=json_sanitize,
        )

    dl = parse_detail_level(detail_level, default="compact")
    db = ctx.request_context.lifespan_context.db

    try:
        coll_key = (collection or "").strip()
        if coll_key and "/" not in coll_key:
            try:
                present = {c.id for c in db.collections()}
                if coll_key not in present:
                    return structured_result(
                        tool_error(
                            "collection_not_found",
                            details=(
                                f"Collection '{coll_key}' does not exist at the database root "
                                f"(Firestore only lists collections that contain at least one document). "
                                f"Use list_collections for names that exist in this project."
                            ),
                            cause="not_found",
                            retryable=False,
                            suggested_fix="Call firestore_gateway_list_collections, then use an existing collection id.",
                        ),
                        content_sanitize=json_sanitize,
                    )
            except Exception as list_exc:
                logger.warning("MCP get_document: could not verify collection %r: %s", coll_key, list_exc)

        doc_ref = db.collection(collection).document(doc_id)
        doc = doc_ref.get()
        if doc.exists:
            doc_data = doc.to_dict()
            doc_data["_id"] = doc.id
            shaped = _shape_document(doc_data, detail_level=dl)
            if dl != "full":
                shaped = {**shaped, "detail_level": dl}
            else:
                shaped = {**shaped, "detail_level": dl}
            return structured_result(shaped, content_sanitize=json_sanitize)
        return structured_result(
            tool_error(
                "document_not_found",
                details=f"Document '{doc_id}' not found in collection '{collection}'.",
                cause="not_found",
                retryable=False,
                suggested_fix="Verify document_id or query_documents with filters.",
            ),
            content_sanitize=json_sanitize,
        )
    except Exception as e:
        logger.error(f"MCP get_document failed: {e}")
        return structured_result(
            tool_error(
                "read_failed",
                details=str(e),
                cause="upstream_error",
                retryable=True,
                suggested_fix="Retry later or verify collection and document id.",
            ),
            content_sanitize=json_sanitize,
        )


@mcp.prompt()
def firestore_assistant(question: str) -> str:
    """Generate a Firestore query prompt to help answer a business question.

    **Args:**
        question: The business question to answer by querying Firestore.
    """
    return (
        "You are a Firestore query expert. You have access to a Firestore database "
        "(data-warehouse-firestore) through the following tools:\n\n"
        "- list_collections: Discover top-level collection names. Call this first when unsure.\n"
        "- count_documents: Return the number of documents (optional filters).\n"
        "- query_documents: Query with filters, ordering, and pagination.\n"
        "- get_document: Fetch one document by ID.\n\n"
        "Important: list_collections shows names that exist in Firestore; some may still be blocked "
        "for reads (FIRESTORE_GATEWAY_DENIED_COLLECTIONS). If a read returns an access error, the "
        "collection is not queryable through this gateway.\n\n"
        "Recommended flow: list_collections -> count_documents, query_documents, or get_document.\n\n"
        'Filters example: [{"field": "status", "operator": "==", "value": "active"}]. '
        "Operators: ==, !=, <, <=, >, >=, in, not-in, array-contains. "
        "For pagination, use cursor_document_id from next_cursor_document_id with the same "
        "collection, filters, and ordering as the prior page.\n\n"
        f"User question: {question}\n\n"
        "Start by listing collections if needed, then query the relevant collection "
        "with appropriate filters to answer the question."
    )
