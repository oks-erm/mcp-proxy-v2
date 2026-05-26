import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from firestore_safety import collection_access_error, redact_document, redact_documents
from google.cloud import firestore, secretmanager
from limits import max_query_limit
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)

firestore_client: Optional["FirestoreClient"] = None
API_KEY: Optional[str] = None


def get_api_key_from_secret_manager() -> str:
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "it-team-hw-project")
    secret_name = "firestore-gateway-api-key"
    secret_version = f"projects/{project_id}/secrets/{secret_name}/versions/latest"

    client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(name=secret_version)
    return response.payload.data.decode("UTF-8").strip()


async def verify_api_key(api_key: str = Security(API_KEY_HEADER)) -> str:
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key is missing. Please provide X-API-Key header.",
        )
    if api_key != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return api_key


class FirestoreClient:
    def __init__(self):
        self.project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "it-team-hw-project")
        self.database = os.getenv("GOOGLE_CLOUD_DATABASE", "data-warehouse-firestore")
        self.db = firestore.Client(project=self.project_id, database=self.database)

    def query_documents(
        self,
        collection: str,
        filters: Optional[List[Dict[str, Any]]] = None,
        order_by: Optional[str] = None,
        order_direction: str = "ASCENDING",
        limit: Optional[int] = None,
        start_after: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """
        Query documents from a collection with optional filters, ordering, limit, and pagination.

        Args:
            collection: Collection name
            filters: List of filter dicts with 'field', 'operator', and 'value' keys
            order_by: Field name to order by (optional)
            order_direction: "ASCENDING" or "DESCENDING" (default: "ASCENDING")
            limit: Maximum number of documents to return (optional)
            start_after: Document snapshot to start after for pagination

        Returns:
            Tuple of (list of documents, last document snapshot for pagination)
        """
        try:
            query = self.db.collection(collection)

            # Apply filters
            if filters:
                for filter_dict in filters:
                    field = filter_dict.get("field")
                    operator = filter_dict.get("operator", "==")
                    value = filter_dict.get("value")
                    query = query.where(field, operator, value)

            # Apply ordering
            if order_by:
                direction = firestore.Query.DESCENDING if order_direction == "DESCENDING" else firestore.Query.ASCENDING
                query = query.order_by(order_by, direction=direction)

            # Apply pagination cursor
            if start_after:
                # Reconstruct the document reference from the cursor
                doc_ref = self.db.collection(collection).document(start_after["id"])
                doc_snapshot = doc_ref.get()
                if doc_snapshot.exists:
                    query = query.start_after(doc_snapshot)

            # Apply limit (add 1 to check if there are more results)
            query_limit = (limit or 20) + 1 if limit else 21
            query = query.limit(query_limit)

            # Execute query and convert to list
            docs = list(query.stream())
            has_more = len(docs) > (limit or 20)

            # Get the actual limit of results
            actual_limit = limit or 20
            results = []
            last_doc = None

            for _, doc in enumerate(docs[:actual_limit]):
                doc_data = doc.to_dict()
                doc_data["_id"] = doc.id
                results.append(doc_data)
                last_doc = doc

            # Prepare next cursor
            next_cursor = None
            if has_more and last_doc:
                next_cursor = {
                    "id": last_doc.id,
                    "order_by_value": (last_doc.to_dict().get(order_by) if order_by else None),
                }

            logger.info(f"Query returned {len(results)} documents from collection {collection}")
            return results, next_cursor

        except Exception as e:
            logger.error(f"Error querying collection {collection}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error querying collection: {str(e)}",
            )

    def get_document(self, collection: str, document_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a single document by its ID from a collection.

        Args:
            collection: Collection name
            document_id: Document ID

        Returns:
            Document as dictionary, or None if not found
        """
        try:
            doc_ref = self.db.collection(collection).document(document_id)
            doc = doc_ref.get()
            if doc.exists:
                doc_data = doc.to_dict()
                doc_data["_id"] = doc.id
                return doc_data
            return None
        except Exception as e:
            logger.error(f"Error getting document {document_id} from collection {collection}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error getting document: {str(e)}",
            )


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    global firestore_client, API_KEY

    logger.info("Initialising Firestore client and API key...")

    if os.getenv("ENV") == "local":
        API_KEY = os.getenv("API_KEY", "local-dev-key")
        logger.info("Using local API Key")
    else:
        API_KEY = get_api_key_from_secret_manager()
        logger.info("Fetched API Key from Secret Manager")

    firestore_client = FirestoreClient()
    logger.info("Firestore client initialised")

    from mcp_server import mcp

    async with mcp.session_manager.run():
        yield

    logger.info("Firestore gateway shutdown complete")


app = FastAPI(
    title="Firestore Gateway API",
    description="API Gateway for querying Firestore collections with API key authentication",
    version="1.0.0",
    lifespan=lifespan,
)

from mcp_server import mcp  # noqa: E402

mcp_app = mcp.streamable_http_app()


@app.middleware("http")
async def mcp_api_key_guard(request: Request, call_next):
    """Enforce API key auth on the MCP endpoint."""
    if request.url.path.startswith("/mcp-server"):
        key = request.headers.get("X-API-Key")
        if key != API_KEY:
            return JSONResponse(status_code=401, content={"detail": "Invalid API Key"})
    return await call_next(request)


app.mount("/mcp-server", mcp_app)


# Request/Response Models
class FilterModel(BaseModel):
    field: str = Field(..., description="Field name to filter on")
    operator: str = Field(
        default="==",
        description="Filter operator: ==, !=, <, <=, >, >=, in, not-in, array-contains",
    )
    value: Any = Field(..., description="Value to filter by")


class QueryRequest(BaseModel):
    collection: str = Field(..., description="Firestore collection name")
    filters: Optional[List[FilterModel]] = Field(default=None, description="List of filters to apply")
    order_by: Optional[str] = Field(default=None, description="Field name to order by")
    order_direction: str = Field(
        default="ASCENDING",
        description="Order direction: ASCENDING or DESCENDING",
    )
    limit: Optional[int] = Field(
        default=20,
        ge=1,
        description="Maximum number of documents to return (capped by FIRESTORE_GATEWAY_MAX_QUERY_LIMIT, default 100)",
    )
    cursor: Optional[Dict[str, Any]] = Field(default=None, description="Pagination cursor from previous response")


class QueryResponse(BaseModel):
    documents: List[Dict[str, Any]] = Field(..., description="List of documents")
    total_returned: int = Field(..., description="Number of documents returned")
    has_more: bool = Field(..., description="Whether there are more documents")
    next_cursor: Optional[Dict[str, Any]] = Field(default=None, description="Cursor for next page")


# Endpoints
@app.get("/health")
def health_check():
    """Health check endpoint (no authentication required)"""
    return {"status": "healthy"}


@app.post(
    "/query",
    response_model=QueryResponse,
    summary="Query Firestore Collection",
    description="Query documents from a Firestore collection with filters, ordering, and pagination",
    tags=["Firestore"],
)
def query_firestore(
    request: QueryRequest,
    api_key: str = Depends(verify_api_key),
) -> QueryResponse:
    """
    Query Firestore collection with optional filters, ordering, and pagination.

    - **collection**: The Firestore collection name
    - **filters**: Optional list of filters (field, operator, value)
    - **order_by**: Optional field name to order by
    - **order_direction**: ASCENDING or DESCENDING (default: ASCENDING)
    - **limit**: Maximum number of documents per page (default: 20, max: FIRESTORE_GATEWAY_MAX_QUERY_LIMIT)
    - **cursor**: Pagination cursor from previous response

    Returns a paginated list of documents.
    """
    try:
        denied = collection_access_error(request.collection)
        if denied:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=denied)

        # Convert filters to dict format
        filters = None
        if request.filters:
            filters = [f.dict() for f in request.filters]

        cap = max_query_limit()
        req_limit = request.limit if request.limit is not None else 20
        effective_limit = min(req_limit, cap)

        # Query documents
        documents, next_cursor = firestore_client.query_documents(
            collection=request.collection,
            filters=filters,
            order_by=request.order_by,
            order_direction=request.order_direction,
            limit=effective_limit,
            start_after=request.cursor,
        )

        documents = redact_documents(documents)

        return QueryResponse(
            documents=documents,
            total_returned=len(documents),
            has_more=next_cursor is not None,
            next_cursor=next_cursor,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in query endpoint: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {str(e)}",
        )


@app.get(
    "/documents/{collection}/{document_id}",
    summary="Get Document by ID",
    description="Retrieve a single document from a Firestore collection by its document ID",
    tags=["Firestore"],
)
def get_document(
    collection: str,
    document_id: str,
    api_key: str = Depends(verify_api_key),
) -> Dict[str, Any]:
    """
    Get a document by its ID from a Firestore collection.

    - **collection**: The Firestore collection name
    - **document_id**: The document ID

    Returns the document if found, or 404 if not found.
    """
    try:
        denied = collection_access_error(collection)
        if denied:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=denied)

        document = firestore_client.get_document(collection, document_id)
        if document is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Document '{document_id}' not found in collection '{collection}'",
            )
        return redact_document(document)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in get_document endpoint: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {str(e)}",
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
