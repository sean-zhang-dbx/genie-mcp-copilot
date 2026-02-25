# Genie MCP Server (Databricks Apps)

[![GitHub](https://img.shields.io/badge/GitHub-Repository-blue?logo=github)](https://github.com/sean-zhang-dbx/genie-mcp-copilot)

> **This is the Databricks Apps branch.** For the Azure Web Apps version (required for Copilot Studio / Teams integration), see the [`main`](https://github.com/sean-zhang-dbx/genie-mcp-copilot/tree/main) branch.

An MCP (Model Context Protocol) server that connects **Databricks Genie** to any MCP-compatible client. Deployed as a **Databricks App** with Databricks-native authentication.

Users or MCP clients send natural-language questions to the server, which forwards them to a Databricks Genie Space and returns the answer.

> **Important: Databricks Apps vs Azure Web Apps**
>
> Databricks Apps are behind Databricks' own session authentication. This means **only clients that can authenticate to Databricks** (e.g. users in a browser, Claude Desktop with Databricks auth, Cursor) can reach this endpoint. **External services like Copilot Studio cannot directly call Databricks Apps** because they cannot complete Databricks' OAuth login flow.
>
> - **For Copilot Studio / Teams**: Use the [`main` branch](https://github.com/sean-zhang-dbx/genie-mcp-copilot/tree/main) (Azure Web Apps deployment)
> - **For MCP clients with Databricks auth**: Use this branch (Databricks Apps deployment)

## Architecture

```
MCP Client (Claude Desktop, Cursor, browser, etc.)
    |  Databricks session auth (browser) or Databricks OAuth
    v
Databricks App  (FastAPI + FastMCP)
    |  Auto-injected Service Principal credentials
    v
Databricks Genie Space
    |
    v
Unity Catalog Tables
```

**Authentication:** Databricks Apps automatically provisions a Service Principal for each app and injects `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, and `DATABRICKS_CLIENT_SECRET` into the environment. Users authenticate to the app via Databricks' own session management (browser login).

---

## Prerequisites

- **Databricks CLI** (`databricks`) -- [Install](https://docs.databricks.com/dev-tools/cli/install.html)
- A **Databricks workspace** (Azure) with at least one Genie Space configured
- **Workspace admin** access (to grant the app's Service Principal access to the Genie Space)

---

## Customization

Before deploying, customize the server metadata and tool descriptions in `app.py` to match your use case. These values are exposed to Copilot Studio and affect how the agent selects and describes tools.

### Server name and instructions

Edit the `FastMCP(...)` constructor (around line 137):

```python
mcp_server = FastMCP(
    "YOUR SERVER NAME",               # Visible in Copilot Studio tool list
    instructions=(
        "Description of what this server does.\n\n"
        "Available data:\n"
        "- table_a: description\n"
        "- table_b: description\n\n"
        "Use query_genie for exploratory analysis."
    ),
)
```

### Tool description and parameter help text

Edit the `@mcp_server.tool()` function (around line 150):

```python
@mcp_server.tool()
def query_genie(
    query: Annotated[
        str,
        "Natural language question about YOUR DATA. Be specific with dates and metrics.",
    ],
    conversation_id: Annotated[
        Optional[str],
        "Continue a previous conversation by passing its ID. Omit for new query.",
    ] = None,
) -> str:
    """Query Databricks Genie for YOUR DOMAIN analysis using natural language.

    Best for: complex exploratory analysis, trend comparisons, multi-table queries.

    Example: query_genie(query="Show revenue by region for Q3 2025")
    """
```

### Adding more tools

You can add additional MCP tools beyond `query_genie`. Each `@mcp_server.tool()` function becomes a separate tool in Copilot Studio:

```python
@mcp_server.tool()
def get_summary_report(
    period: Annotated[str, "Time period, e.g. '2025-Q3' or 'last 30 days'"],
) -> str:
    """Return a pre-built summary report for the given period."""
    # Your implementation here
    ...
```

---

## Step 1 -- Deploy to Databricks Apps

### 1a. Authenticate the Databricks CLI

```bash
databricks configure --host https://YOUR-WORKSPACE.azuredatabricks.net
```

### 1b. Create the app

```bash
databricks apps create --name YOUR-APP-NAME --description "Genie MCP Server for Copilot Studio"
```

### 1c. Deploy the code

From the project root directory:

```bash
databricks apps deploy YOUR-APP-NAME --source-code-path .
```

This uploads the code and starts the app. The first deployment takes 2-5 minutes.

### 1d. Add the Genie Space resource

After the app is created, you need to attach your Genie Space as a managed resource:

1. In the Databricks workspace, go to **Compute > Apps**
2. Click on your app name
3. Go to the **Resources** tab
4. Click **Add resource**
5. Select **Genie space** as the resource type
6. Select your Genie Space
7. Set the key to `genie-space` (must match the `app.yaml` resource name)
8. Set permission to **Can Run**
9. Click **Save** and redeploy if prompted

### 1e. Grant the app's Service Principal access

Databricks Apps auto-creates a Service Principal for each app. You need to grant it access to the Genie Space's underlying data:

1. Find the app's SP name: go to **Compute > Apps > YOUR-APP-NAME > Settings** and note the Service Principal name
2. Open the Genie Space and click **Share** -- add the SP with **Can Run** permission
3. Grant the SP access to the underlying tables:

```sql
-- Replace with your catalog/schema names and the app's SP name
GRANT USE CATALOG ON CATALOG your_catalog TO `YOUR-APP-NAME`;
GRANT USE SCHEMA ON SCHEMA your_catalog.your_schema TO `YOUR-APP-NAME`;
GRANT SELECT ON SCHEMA your_catalog.your_schema TO `YOUR-APP-NAME`;
```

### 1f. Verify the deployment

```bash
# Check app status
databricks apps get YOUR-APP-NAME

# Get the app URL
databricks apps get YOUR-APP-NAME --output json | python3 -c "import sys,json; print(json.load(sys.stdin).get('url',''))"
```

Test the health endpoint:

```bash
curl https://YOUR-WORKSPACE.azuredatabricks.net/apps/YOUR-APP-NAME/health
```

Expected response:

```json
{
  "status": "healthy",
  "genie_configured": true,
  "databricks_configured": true,
  "entra_auth_enabled": false
}
```

> `entra_auth_enabled` is `false` until you configure the Entra ID env vars (Step 2).

The MCP endpoint is at: `https://YOUR-WORKSPACE.azuredatabricks.net/apps/YOUR-APP-NAME/mcp`

---

## Step 2 -- Using the MCP Server

Once deployed, the MCP endpoint is available at:

```
https://YOUR-APP-NAME-WORKSPACE_ID.N.azure.databricksapps.com/mcp
```

Any MCP client that supports **Streamable HTTP** transport and can authenticate to Databricks can connect.

### Browser access

Navigate to the app URL in a browser while logged into the Databricks workspace. The `/health` endpoint shows server status:

```
https://YOUR-APP-NAME-WORKSPACE_ID.N.azure.databricksapps.com/health
```

### Copilot Studio / Teams integration

Databricks Apps cannot be reached by external services like Copilot Studio. For that integration, use the **Azure Web Apps** version on the [`main` branch](https://github.com/sean-zhang-dbx/genie-mcp-copilot/tree/main).

---

## Local Development

For local development, Entra ID auth is automatically disabled when `AZURE_TENANT_ID` is not set.

```bash
# Install dependencies
pip install -r requirements.txt

# Set Databricks credentials
export DATABRICKS_HOST="https://YOUR-WORKSPACE.azuredatabricks.net"
export DATABRICKS_CLIENT_ID="your-sp-client-id"
export DATABRICKS_CLIENT_SECRET="your-sp-client-secret"
export GENIE_SPACE_ID="your-genie-space-id"
export MLFLOW_TRACKING_URI="databricks"

# Start the server
uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

Test the endpoints:

```bash
# Health check
curl http://localhost:8080/health

# MCP initialize (no auth when Entra is disabled)
curl -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {"name": "test", "version": "1.0"}
    }
  }'
```

---

## Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABRICKS_HOST` | Auto-injected | Databricks workspace URL. Auto-injected by Databricks Apps; set manually for local dev |
| `DATABRICKS_CLIENT_ID` | Auto-injected | Service Principal client ID. Auto-injected by Databricks Apps; set manually for local dev |
| `DATABRICKS_CLIENT_SECRET` | Auto-injected | Service Principal secret. Auto-injected by Databricks Apps; set manually for local dev |
| `GENIE_SPACE_ID` | Yes | Genie Space ID. Set in `app.yaml` or resolved from a managed resource |
| `MLFLOW_TRACKING_URI` | Yes | Set to `databricks` -- required by the `databricks-ai-bridge` dependency |
| `LOG_LEVEL` | No | Python logging level. Default: `INFO`. Options: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Project Structure

```
app.py              Main application -- FastAPI + FastMCP server with Entra ID middleware
app.yaml            Databricks Apps configuration (command, env, resources)
requirements.txt    Python dependencies
images/             Architecture diagrams
README.md           This file
```

---

## Troubleshooting

### App fails to start

Check the app logs in the Databricks workspace: go to **Compute > Apps > YOUR-APP-NAME > Logs**.

Common causes:
- **Missing Genie Space ID**: Ensure `GENIE_SPACE_ID` is set in `app.yaml`
- **Dependency install failure**: Check the build logs for pip errors
- **Port mismatch**: Databricks Apps expects port 8080; verify `app.yaml` uses `--port 8080`

### "Error: GENIE_SPACE_ID not configured"

The `GENIE_SPACE_ID` env var is missing or empty. Set it in `app.yaml` and redeploy. You can find the Genie Space ID in the URL:

```
https://YOUR-WORKSPACE.azuredatabricks.net/genie/rooms/THIS-IS-THE-ID?o=...
```

### Genie returns "Model registry functionality is unavailable"

The `MLFLOW_TRACKING_URI` env var is not set. Verify it's set to `databricks` in `app.yaml`.

### Genie returns permission errors

The app's auto-provisioned Service Principal needs access to the Genie Space and underlying tables. See Step 1e.

---

## Comparison: Databricks Apps vs Azure Web Apps

| Feature | Databricks Apps (this branch) | Azure Web Apps (`main` branch) |
|---------|-------------------------------|-------------------------------|
| Deployment | `databricks apps deploy` | `az webapp up` |
| Service Principal | Auto-provisioned | Manual creation required |
| Genie Space access | Managed resource (`valueFrom`) | Manual env var |
| Port | 8080 | 8000 |
| Startup script | Not needed (`app.yaml` command) | `startup.sh` |
| Entra ID auth | Optional (for Copilot Studio) | Optional (for Copilot Studio) |
| Network | Databricks workspace network | Azure App Service network |
| Cost | Included in Databricks compute | Separate Azure App Service Plan |

---

## Design Decisions

**Auto-injected Service Principal**: Databricks Apps automatically provisions an SP for each app and injects credentials. You only need to grant this SP access to the Genie Space and underlying tables -- no manual secret management.

**Databricks-native auth**: Requests are authenticated via Databricks' session management. The Entra ID middleware in `app.py` is effectively unused in this deployment since Databricks handles auth before the request reaches the app.

**Streamable HTTP transport**: The MCP server uses FastMCP's `http_app()` which provides Streamable HTTP on the `/mcp` endpoint, compatible with any MCP client that supports this transport.
