import logging
import os
from typing import Annotated, Any, Optional

from databricks.sdk import WorkspaceClient
from databricks_ai_bridge.genie import Genie
from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

log_level = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=getattr(logging, log_level, logging.INFO))
logger = logging.getLogger(__name__)

DATABRICKS_HOST = os.getenv("DATABRICKS_HOST", "")

_app_client: WorkspaceClient | None = None


def _get_app_client() -> WorkspaceClient:
    """Return a WorkspaceClient using the app's auto-injected SP credentials."""
    global _app_client
    if _app_client is None:
        _app_client = WorkspaceClient()
    return _app_client


def get_workspace_client(headers: dict[str, str] | None = None) -> WorkspaceClient:
    """Return a WorkspaceClient, preferring the user's OBO token when available.

    Databricks Apps pass the authenticated user's token via HTTP headers.
    When present, we create a per-request client so Genie queries run
    as the user (respecting their permissions). Otherwise we fall back
    to the app's Service Principal.
    """
    if headers:
        token = (
            headers.get("x-forwarded-access-token")
            or headers.get("x-user-token")
            or _extract_bearer(headers.get("authorization", ""))
        )
        if token:
            logger.debug("Using OBO user token for WorkspaceClient")
            return WorkspaceClient(
                host=DATABRICKS_HOST, token=token, auth_type="pat"
            )

    logger.debug("Using app SP credentials for WorkspaceClient")
    return _get_app_client()


def _extract_bearer(auth_header: str) -> str | None:
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:]
    return None


def format_genie_response(
    description: str | None,
    query: str | None,
    result: Any,
    conversation_id: str | None,
) -> str:
    parts = []
    if description:
        parts.append(f"**Description:** {description}")
    if query:
        parts.append(f"**SQL Query:**\n```sql\n{query}\n```")
    if result:
        result_str = result if isinstance(result, str) else result.to_markdown()
        parts.append(f"**Results:**\n{result_str}")
    if conversation_id:
        parts.append(f"\n*Conversation ID: {conversation_id}*")
    return "\n\n".join(parts) if parts else "No results."


# ==================== FastMCP Server ====================

mcp_server = FastMCP(
    "CTRP MCP Server",
    instructions=(
        "CTRP (Conformance to Release Plan) data analysis tools.\n\n"
        "Available Tables:\n"
        "- fact_ctrp_week_site: Weekly KPI by site\n"
        "- fact_ctrp_week_site_sku: Weekly KPI by site and SKU\n"
        "- fact_quality_management_flow: Root cause events\n\n"
        "Use query_genie for complex exploratory analysis or ad-hoc questions."
    ),
)


@mcp_server.tool()
def query_genie(
    query: Annotated[
        str,
        "Natural language question about CTRP data. Be specific with dates and metrics.",
    ],
    conversation_id: Annotated[
        Optional[str],
        "Continue a previous conversation by passing its ID. Omit for new query.",
    ] = None,
) -> str:
    """Query Databricks Genie for CTRP analysis using natural language.

    Best for: complex exploratory analysis, YTD trends, multi-table queries,
    comparisons, or questions not covered by the direct SQL tools.

    Example: query_genie(query="Compare CTRP performance Q3 vs Q4 2025 by site")
    """
    space_id = os.getenv("GENIE_SPACE_ID")
    if not space_id:
        return "Error: GENIE_SPACE_ID not configured"

    logger.info("Genie query: '%s' (conv=%s)", query[:100], conversation_id)

    headers = get_http_headers()
    client = get_workspace_client(headers)

    try:
        genie = Genie(
            space_id=space_id,
            client=client,
            truncate_results=True,
            return_pandas=False,
        )
        response = genie.ask_question(query, conversation_id=conversation_id)
        return format_genie_response(
            response.description,
            response.query,
            response.result,
            response.conversation_id,
        )
    except Exception as e:
        logger.error("Genie error: %s", e)
        return f"Error querying Genie: {str(e)}"


# ==================== FastAPI App ====================

mcp_app = mcp_server.http_app()

app = FastAPI(
    title="CTRP MCP Server",
    routes=[*mcp_app.routes],
    lifespan=mcp_app.lifespan,
)
