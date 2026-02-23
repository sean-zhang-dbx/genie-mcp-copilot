# Genie MCP Server for Copilot Studio & Teams

An MCP (Model Context Protocol) server that connects **Databricks Genie** to **Microsoft Copilot Studio** and **Microsoft Teams**. Deployed on **Azure Web Apps** with optional **Entra ID OAuth 2.0** authentication.

Users ask natural-language questions in Teams, Copilot Studio routes them to the MCP server, which forwards them to a Databricks Genie Space and returns the answer.

## Architecture

```
Teams / Copilot Studio User
    │
    ▼
Copilot Studio Agent
    │  MCP Streamable HTTP + Entra ID OAuth 2.0 Bearer token
    ▼
Azure Web App  (FastAPI + FastMCP)
    │  Databricks Service Principal OAuth (client credentials)
    ▼
Databricks Genie Space
    │
    ▼
Unity Catalog Tables
```

**Authentication chain (two hops):**

| Hop | From | To | Method |
|-----|------|----|--------|
| 1 | Copilot Studio | MCP Server | Entra ID OAuth 2.0 — Copilot Studio obtains a Bearer token and sends it with each request; the MCP server validates it against Microsoft's JWKS |
| 2 | MCP Server | Databricks | Service Principal client credentials — the Databricks SDK auto-discovers `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, and `DATABRICKS_CLIENT_SECRET` from env vars |

---

## Prerequisites

- **Python 3.11+**
- **Azure CLI** (`az`) — [Install](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli)
- **Databricks CLI** (`databricks`) — [Install](https://docs.databricks.com/dev-tools/cli/install.html)
- An **Azure subscription** with permissions to create Web Apps and Entra ID App Registrations
- A **Databricks workspace** (Azure) with at least one Genie Space configured
- A **Microsoft Copilot Studio** license (for the agent)

---

## Customization

Before deploying, customize the server metadata and tool descriptions in `app.py` to match your use case. These values are exposed to Copilot Studio and affect how the agent selects and describes tools.

### Server name and instructions

Edit the `FastMCP(...)` constructor (around line 137):

```python
mcp_server = FastMCP(
    "YOUR SERVER NAME",               # ← Visible in Copilot Studio tool list
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

## Step 1 — Create a Databricks Service Principal

The MCP server authenticates to Databricks using a Service Principal (SP). You can create one via the Databricks CLI or the workspace UI.

### Option A: Databricks CLI

```bash
# Authenticate the CLI to your workspace
databricks configure --host https://YOUR-WORKSPACE.azuredatabricks.net

# Create the Service Principal
databricks service-principals create \
  --display-name "YOUR-SP-NAME" \
  --json '{"active": true}'
```

Note the `application_id` from the output — this is your `DATABRICKS_CLIENT_ID`.

```bash
# Generate an OAuth secret
databricks service-principals secrets create \
  --service-principal-id APPLICATION_ID
```

Save the `secret` value — this is your `DATABRICKS_CLIENT_SECRET`. It is shown only once.

### Option B: Workspace UI

1. Go to **Settings > Identity and access > Service principals**
2. Click **Add service principal > Add new**
3. Enter a display name, click **Add**
4. Click into the SP, go to **Secrets > Generate secret**
5. Copy the **Client ID** and **Secret**

### Grant the SP access to the Genie Space

1. Open the Genie Space in the Databricks workspace
2. Click the **Share** button (or go to Genie Space settings)
3. Add the Service Principal with **Can Run** permission
4. Also ensure the SP has `USE CATALOG` and `SELECT` on the underlying tables

---

## Step 2 — Create Entra ID App Registrations

You need **two** app registrations: a **Server** app (the MCP API) and a **Client** app (Copilot Studio's connector).

> **Single-tenant vs Multi-tenant:** If Copilot Studio runs in the **same** Entra ID tenant as the app registrations, use single-tenant. If they are in **different** tenants (common in enterprise environments), use multi-tenant. Instructions for both are provided below.

### 2a. Server App Registration (MCP Server API)

1. Go to **Azure Portal > Microsoft Entra ID > App registrations > New registration**
2. Fill in:
   - **Name**: A descriptive name (e.g. `Genie MCP Server`)
   - **Supported account types**:
     - **Single-tenant**: *Accounts in this organizational directory only* — choose this if Copilot Studio is in the same tenant
     - **Multi-tenant**: *Accounts in any organizational directory* — choose this if Copilot Studio is in a different tenant
3. Click **Register**
4. Note these values from the **Overview** page:
   - **Application (client) ID** → this becomes your `AZURE_CLIENT_ID` env var
   - **Directory (tenant) ID** → this becomes your `AZURE_TENANT_ID` env var

**Expose an API (create a permission scope):**

5. Go to **Expose an API**
6. Click **Set** next to Application ID URI — accept the default `api://{client-id}` or enter a custom URI
7. Click **Add a scope**:
   | Field | Value |
   |-------|-------|
   | Scope name | `MCP.Tools.ReadWrite` |
   | Who can consent | Admins and users |
   | Admin consent display name | Read and write MCP tools |
   | Admin consent description | Allows the application to call MCP tools on this server |
   | State | Enabled |

### 2b. Client App Registration (Copilot Studio Connector)

1. Go to **App registrations > New registration**
2. Fill in:
   - **Name**: A descriptive name (e.g. `Genie Copilot Connector`)
   - **Supported account types**: Same choice as the server app (single-tenant or multi-tenant)
3. Click **Register**
4. Note the **Application (client) ID**

**Create a client secret:**

5. Go to **Certificates & secrets > Client secrets > New client secret**
6. Enter a description and expiry, click **Add**
7. Copy the **Value** immediately (shown only once)

**Add API permission:**

8. Go to **API permissions > Add a permission > My APIs**
9. Select the Server app (from step 2a)
10. Select **Application permissions** (for client_credentials flow) — check `MCP.Tools.ReadWrite`
11. Click **Add permissions**
12. Click **Grant admin consent for [your org]** (requires admin role)

### 2c. Pre-authorize the Client App (recommended)

This step lets the client app call the server without per-user consent prompts.

1. Go back to the **Server App Registration > Expose an API**
2. Under **Authorized client applications**, click **Add a client application**
3. Enter the **Client App Registration's Application (client) ID**
4. Check the `MCP.Tools.ReadWrite` scope
5. Click **Add application**

### Single-tenant vs Multi-tenant: code configuration

The `app.py` code supports both modes. The difference is in the JWKS endpoint and issuer validation:

| Setting | Single-tenant | Multi-tenant |
|---------|---------------|--------------|
| JWKS URI in `_get_jwks_uri()` | `https://login.microsoftonline.com/{AZURE_TENANT_ID}/discovery/v2.0/keys` | `https://login.microsoftonline.com/common/discovery/v2.0/keys` |
| Issuer validation in `jwt.decode()` | `"verify_iss": True` | `"verify_iss": False` |
| Copilot Studio OAuth URLs | Use `/{tenant-id}/` | Use `/common/` |

**For single-tenant**, edit `_get_jwks_uri()` in `app.py`:

```python
def _get_jwks_uri() -> str:
    return f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/discovery/v2.0/keys"
```

And set `"verify_iss": True` in the `jwt.decode()` options.

**The default code is configured for multi-tenant** (uses `/common` and skips issuer validation).

---

## Step 3 — Deploy to Azure Web App

### 3a. Create the resource group and deploy

```bash
# Log in to Azure
az login

# Set the correct subscription (if you have multiple)
az account set --subscription "YOUR-SUBSCRIPTION-NAME-OR-ID"

# Create a resource group (pick your preferred region)
az group create --name YOUR-RESOURCE-GROUP --location eastus2

# Deploy the app (creates the Web App + App Service Plan)
az webapp up \
  --name YOUR-APP-NAME \
  --resource-group YOUR-RESOURCE-GROUP \
  --runtime "PYTHON:3.11" \
  --sku B1
```

> `YOUR-APP-NAME` must be globally unique. The app URL will be `https://YOUR-APP-NAME.azurewebsites.net`.

### 3b. Configure the startup script

```bash
az webapp config set \
  --name YOUR-APP-NAME \
  --resource-group YOUR-RESOURCE-GROUP \
  --startup-file startup.sh
```

### 3c. Set environment variables

```bash
az webapp config appsettings set \
  --name YOUR-APP-NAME \
  --resource-group YOUR-RESOURCE-GROUP \
  --settings \
    DATABRICKS_HOST="https://YOUR-WORKSPACE.azuredatabricks.net" \
    DATABRICKS_CLIENT_ID="your-sp-client-id" \
    DATABRICKS_CLIENT_SECRET="your-sp-client-secret" \
    GENIE_SPACE_ID="your-genie-space-id" \
    AZURE_TENANT_ID="your-entra-tenant-id" \
    AZURE_CLIENT_ID="your-server-app-client-id" \
    MLFLOW_TRACKING_URI="databricks" \
    LOG_LEVEL="INFO"
```

> To **disable** Entra ID auth (e.g. for testing), omit `AZURE_TENANT_ID` and `AZURE_CLIENT_ID`.

### 3d. Enforce HTTPS

```bash
az webapp update \
  --name YOUR-APP-NAME \
  --resource-group YOUR-RESOURCE-GROUP \
  --https-only true
```

### 3e. Verify the deployment

```bash
curl https://YOUR-APP-NAME.azurewebsites.net/health
```

Expected response:

```json
{
  "status": "healthy",
  "genie_configured": true,
  "databricks_configured": true,
  "entra_auth_enabled": true
}
```

---

## Step 4 — Connect to Copilot Studio

### 4a. Create the MCP tool connection

1. Go to [Copilot Studio](https://copilotstudio.microsoft.com)
2. Open your agent (or create a new one)
3. Go to **Tools > Add a tool > New tool > Model Context Protocol**
4. Fill in:

   | Field | Value |
   |-------|-------|
   | Server name | Your preferred display name |
   | Server description | A description of what the tool does |
   | Server URL | `https://YOUR-APP-NAME.azurewebsites.net/mcp` |

5. Under **Authentication**, select **OAuth 2.0**, then **Manual**
6. Fill in the OAuth configuration:

   | Field | Value |
   |-------|-------|
   | Client ID | Client App Registration's Application (client) ID |
   | Client secret | Client App Registration's secret value |
   | Authorization URL | See table below |
   | Token URL | See table below |
   | Refresh URL | See table below |
   | Resource URL | `api://SERVER-APP-CLIENT-ID` |
   | Scope | `api://SERVER-APP-CLIENT-ID/.default` |

   **OAuth URLs by tenant mode:**

   | URL | Single-tenant | Multi-tenant |
   |-----|---------------|--------------|
   | Authorization URL | `https://login.microsoftonline.com/YOUR-TENANT-ID/oauth2/v2.0/authorize` | `https://login.microsoftonline.com/common/oauth2/v2.0/authorize` |
   | Token URL | `https://login.microsoftonline.com/YOUR-TENANT-ID/oauth2/v2.0/token` | `https://login.microsoftonline.com/common/oauth2/v2.0/token` |
   | Refresh URL | `https://login.microsoftonline.com/YOUR-TENANT-ID/oauth2/v2.0/token` | `https://login.microsoftonline.com/common/oauth2/v2.0/token` |

7. Click **Create**

### 4b. Add the callback URL

After creating the connection, Copilot Studio shows a **Callback URL** (also called redirect URI).

1. Copy this URL
2. Go to **Azure Portal > Client App Registration > Authentication**
3. Under **Web > Redirect URIs**, click **Add URI**
4. Paste the callback URL
5. Click **Save**

### 4c. Verify and add the tool

1. Back in Copilot Studio, click **Next** on the tool configuration
2. Verify the `query_genie` tool appears in the list
3. Click **Add to agent**

---

## Step 5 — Configure the Agent

### Agent instructions

In Copilot Studio, go to the agent's **Instructions** section and add guidance. Tailor this to your data domain:

```
You are a data analyst assistant. You help users explore and understand
data through natural language.

When users ask questions about the data, use the query_genie tool.

Guidelines:
- Be specific with dates and metric names when calling query_genie
- Present results clearly, using tables when appropriate
- If the Genie response includes SQL, you may show it to the user if they ask
- For follow-up questions, pass the conversation_id from the previous
  response to maintain context in the Genie conversation
```

### Topic configuration (optional)

You can create Copilot Studio **Topics** that route specific intents to the MCP tool:

1. Go to **Topics > Add a topic**
2. Add trigger phrases (e.g. "show me the data", "query the database")
3. Add an action node that calls the `query_genie` tool
4. Configure the response to display the result

---

## Step 6 — Publish to Microsoft Teams

1. In Copilot Studio, click **Publish** in the left navigation
2. Confirm the publish action (takes 1-2 minutes)
3. Go to **Channels > Microsoft Teams**
4. Click **Turn on Teams**
5. Options:
   - **Open in Teams** — for personal testing
   - **Make available to others** — share within your org
   - **Submit to admin** — submit to the Teams App Store for org-wide availability

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

# Optional: enable Entra ID auth locally
# export AZURE_TENANT_ID="your-tenant-id"
# export AZURE_CLIENT_ID="your-server-app-client-id"

# Start the server
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Test the endpoints:

```bash
# Health check
curl http://localhost:8000/health

# MCP initialize (no auth when Entra is disabled)
curl -X POST http://localhost:8000/mcp \
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
| `DATABRICKS_HOST` | Yes | Full URL of your Databricks workspace (e.g. `https://adb-xxxx.xx.azuredatabricks.net`) |
| `DATABRICKS_CLIENT_ID` | Yes | Databricks Service Principal application/client ID |
| `DATABRICKS_CLIENT_SECRET` | Yes | Databricks Service Principal OAuth secret |
| `GENIE_SPACE_ID` | Yes | ID of the Databricks Genie Space (from the Genie Space URL) |
| `AZURE_TENANT_ID` | For production | Entra ID directory/tenant ID. Auth is **disabled** when this is unset |
| `AZURE_CLIENT_ID` | For production | Server App Registration's application/client ID (used as the token audience) |
| `MLFLOW_TRACKING_URI` | Yes | Set to `databricks` — required by the `databricks-ai-bridge` dependency |
| `LOG_LEVEL` | No | Python logging level. Default: `INFO`. Options: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Project Structure

```
app.py              Main application — FastAPI + FastMCP server with Entra ID middleware
app.yaml            App configuration template (env var reference for Databricks Apps)
requirements.txt    Python dependencies
startup.sh          Azure Web App startup script (uvicorn command)
README.md           This file
```

---

## Troubleshooting

### "Application Error" / HTTP 503 from Azure

The app failed to start. Check the logs:

```bash
az webapp log tail --name YOUR-APP-NAME --resource-group YOUR-RESOURCE-GROUP
```

Common causes:
- **Missing env vars**: Ensure all required app settings are configured
- **Wrong Python version**: Verify with `az webapp config show --name YOUR-APP-NAME --resource-group YOUR-RESOURCE-GROUP --query linuxFxVersion`
- **Dependency install failure**: Check the Oryx build logs in the log stream

### "Invalid token audience" (HTTP 401)

The OAuth token's `aud` claim does not match the server's expected audience. Verify:
- `AZURE_CLIENT_ID` is set to the **Server** App Registration's client ID (not the Client app)
- The Copilot Studio scope is `api://SERVER-APP-CLIENT-ID/.default`

### "Token signing key not found" (HTTP 401)

The token's `kid` (key ID) was not found in Microsoft's JWKS. This can happen if:
- The token is from a different identity provider (not Entra ID)
- There is a network issue reaching `login.microsoftonline.com`

### AADSTS700016: Application not found in directory

This error from Entra ID means Copilot Studio is in a **different tenant** than the app registrations. Fix:
1. Change both app registrations to **multi-tenant** (Accounts in any organizational directory)
2. Update the Copilot Studio OAuth URLs to use `/common/` instead of a specific tenant ID
3. Ensure the code uses the `/common` JWKS endpoint (the default)

### AADSTS900144: Missing 'scope' parameter

Copilot Studio is not sending the scope. In the tool OAuth config, set:
- **Scope**: `api://SERVER-APP-CLIENT-ID/.default`

### Genie returns "Model registry functionality is unavailable"

Set the `MLFLOW_TRACKING_URI` environment variable to `databricks`:

```bash
az webapp config appsettings set \
  --name YOUR-APP-NAME \
  --resource-group YOUR-RESOURCE-GROUP \
  --settings MLFLOW_TRACKING_URI="databricks"
```

### Genie returns "Error: GENIE_SPACE_ID not configured"

The `GENIE_SPACE_ID` app setting is missing or empty. Set it to the Genie Space ID from your Databricks workspace. You can find it in the Genie Space URL:

```
https://YOUR-WORKSPACE.azuredatabricks.net/genie/rooms/THIS-IS-THE-ID?o=...
```

---

## Design Decisions

**Service Principal for Databricks access**: All Genie queries execute under a single SP identity. Unity Catalog governance (row-level security, column masking) applies to the SP, not the end-user. For per-user access control, you would need user-identity passthrough with token exchange.

**Entra ID auth is optional**: When `AZURE_TENANT_ID` is unset, the middleware passes all requests through without validation. This makes local development frictionless while production stays secured.

**Multi-tenant by default**: The code uses the `/common` JWKS endpoint and skips issuer validation, allowing tokens from any Entra ID tenant. For stricter security, switch to single-tenant mode (see [Step 2](#single-tenant-vs-multi-tenant-code-configuration)).

**Streamable HTTP transport**: Copilot Studio requires MCP servers to use Streamable HTTP. FastMCP's `http_app()` provides this on the `/mcp` endpoint.
