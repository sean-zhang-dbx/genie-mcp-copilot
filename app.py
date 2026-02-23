import logging
import os
import time
from typing import Annotated, Any, Optional

import httpx
import jwt
from databricks.sdk import WorkspaceClient
from databricks_ai_bridge.genie import Genie
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastmcp import FastMCP

log_level = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=getattr(logging, log_level, logging.INFO))
logger = logging.getLogger(__name__)

# ==================== Entra ID Token Validation ====================

AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID")
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
ENTRA_ENABLED = bool(AZURE_TENANT_ID and AZURE_CLIENT_ID)

_jwks_cache: dict = {"keys": [], "fetched_at": 0}
JWKS_CACHE_TTL = 3600  # Re-fetch signing keys every hour


def _get_jwks_uri() -> str:
    # Use "common" endpoint to accept tokens from any Entra ID tenant
    # (needed when Copilot Studio is in a different tenant than the app registration)
    return "https://login.microsoftonline.com/common/discovery/v2.0/keys"


def _get_signing_keys() -> list[dict]:
    now = time.time()
    if _jwks_cache["keys"] and (now - _jwks_cache["fetched_at"]) < JWKS_CACHE_TTL:
        return _jwks_cache["keys"]

    resp = httpx.get(_get_jwks_uri(), timeout=10)
    resp.raise_for_status()
    keys = resp.json().get("keys", [])
    _jwks_cache["keys"] = keys
    _jwks_cache["fetched_at"] = now
    logger.info("Refreshed Entra ID JWKS (%d keys)", len(keys))
    return keys


def _find_rsa_key(token: str) -> dict | None:
    unverified_header = jwt.get_unverified_header(token)
    kid = unverified_header.get("kid")
    if not kid:
        return None
    for key in _get_signing_keys():
        if key.get("kid") == kid:
            return key
    # Key not found -- force refresh in case of key rotation
    _jwks_cache["fetched_at"] = 0
    for key in _get_signing_keys():
        if key.get("kid") == kid:
            return key
    return None


def validate_entra_token(token: str) -> dict:
    """Validate a Bearer token issued by Microsoft Entra ID.

    Returns the decoded JWT claims on success, raises HTTPException on failure.
    """
    rsa_key = _find_rsa_key(token)
    if not rsa_key:
        raise HTTPException(401, "Token signing key not found")

    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(rsa_key)
    try:
        # Multi-tenant: skip issuer validation since tokens may come from
        # a different tenant than the app registration (e.g. Copilot Studio
        # in Databricks corp tenant, app registered in dbdevfieldeng tenant).
        # Audience check is sufficient to ensure the token is meant for us.
        valid_audiences = [AZURE_CLIENT_ID, f"api://{AZURE_CLIENT_ID}"]
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=valid_audiences,
            options={"verify_exp": True, "verify_iss": False},
        )
        return claims
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidAudienceError:
        raise HTTPException(401, "Invalid token audience")
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Token validation failed: {e}")


# ==================== Databricks Client ====================

_workspace_client: WorkspaceClient | None = None


def get_workspace_client() -> WorkspaceClient:
    """Return a WorkspaceClient using Service Principal credentials from env vars.

    Expects DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET
    to be set. The SDK discovers them automatically.
    """
    global _workspace_client
    if _workspace_client is None:
        _workspace_client = WorkspaceClient()
    return _workspace_client


# ==================== Response Formatting ====================


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

    try:
        genie = Genie(
            space_id=space_id,
            client=get_workspace_client(),
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


@app.middleware("http")
async def entra_auth_middleware(request: Request, call_next) -> Response:
    """Validate Entra ID tokens on the /mcp endpoint.

    Skips validation for /health (used by Azure health probes) and when
    Entra ID is not configured (local development).
    """
    if request.url.path == "/health":
        return await call_next(request)

    if not ENTRA_ENABLED:
        return await call_next(request)

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"detail": "Missing Bearer token"})

    token = auth_header[len("Bearer "):]
    try:
        claims = validate_entra_token(token)
        logger.info(
            "Authenticated: sub=%s, appid=%s",
            claims.get("sub", "?"),
            claims.get("appid", claims.get("azp", "?")),
        )
    except HTTPException as exc:
        logger.warning("Auth failed: %s", exc.detail)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return await call_next(request)


@app.get("/health")
async def health():
    """Health check for Azure Web App probes."""
    genie_configured = bool(os.getenv("GENIE_SPACE_ID"))
    databricks_configured = bool(os.getenv("DATABRICKS_HOST"))
    return {
        "status": "healthy",
        "genie_configured": genie_configured,
        "databricks_configured": databricks_configured,
        "entra_auth_enabled": ENTRA_ENABLED,
    }
