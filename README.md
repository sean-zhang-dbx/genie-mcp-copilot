# Genie MCP Server for Copilot Studio & Teams (Databricks Apps)

[![GitHub](https://img.shields.io/badge/GitHub-Repository-blue?logo=github)](https://github.com/sean-zhang-dbx/genie-mcp-copilot)

> **This is the Databricks Apps branch.** For the Azure Web Apps version, see the [`main`](https://github.com/sean-zhang-dbx/genie-mcp-copilot/tree/main) branch.

An MCP (Model Context Protocol) server that connects **Databricks Genie** to **Microsoft Copilot Studio** and **Microsoft Teams**. Deployed as a **Databricks App** with optional **Entra ID OAuth 2.0** authentication.

Users ask natural-language questions in Teams, Copilot Studio routes them to the MCP server, which forwards them to a Databricks Genie Space and returns the answer.

## Architecture

```
Teams / Copilot Studio User
    |
    v
Copilot Studio Agent
    |  MCP Streamable HTTP + Entra ID OAuth 2.0 Bearer token
    v
Databricks App  (FastAPI + FastMCP)
    |  Auto-injected Service Principal credentials
    v
Databricks Genie Space
    |
    v
Unity Catalog Tables
```

**Authentication chain (two hops):**

| Hop | From | To | Method |
|-----|------|----|--------|
| 1 | Copilot Studio | MCP Server | Entra ID OAuth 2.0 -- Copilot Studio obtains a Bearer token and sends it with each request; the MCP server validates it against Microsoft's JWKS |
| 2 | MCP Server | Databricks | Auto-injected Service Principal -- Databricks Apps automatically provisions an SP and injects `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` into the app environment |

---

## Prerequisites

- **Databricks CLI** (`databricks`) -- [Install](https://docs.databricks.com/dev-tools/cli/install.html)
- A **Databricks workspace** (Azure) with at least one Genie Space configured
- **Workspace admin** access (to grant the app's Service Principal access to the Genie Space)
- A **Microsoft Copilot Studio** license (for the agent)
- An **Azure subscription** with permissions to create Entra ID App Registrations (for Copilot Studio OAuth)

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

## Step 2 -- Create Entra ID App Registrations

> This step is only required if you're connecting to **Copilot Studio**. If you're using the MCP server with a client that authenticates via Databricks directly, you can skip this.

You need **two** app registrations: a **Server** app (the MCP API) and a **Client** app (Copilot Studio's connector).

> **Single-tenant vs Multi-tenant:** If Copilot Studio runs in the **same** Entra ID tenant as the app registrations, use single-tenant. If they are in **different** tenants (common in enterprise environments), use multi-tenant. Instructions for both are provided below.

### 2a. Server App Registration (MCP Server API)

1. Go to **Azure Portal > Microsoft Entra ID > App registrations > New registration**
2. Fill in:
   - **Name**: A descriptive name (e.g. `Genie MCP Server`)
   - **Supported account types**:
     - **Single-tenant**: *Accounts in this organizational directory only* -- choose this if Copilot Studio is in the same tenant
     - **Multi-tenant**: *Accounts in any organizational directory* -- choose this if Copilot Studio is in a different tenant
3. Click **Register**
4. Note these values from the **Overview** page:
   - **Application (client) ID** -- this becomes your `AZURE_CLIENT_ID` env var
   - **Directory (tenant) ID** -- this becomes your `AZURE_TENANT_ID` env var

**Expose an API (create a permission scope):**

5. Go to **Expose an API**
6. Click **Set** next to Application ID URI -- accept the default `api://{client-id}` or enter a custom URI
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
10. Select **Application permissions** (for client_credentials flow) -- check `MCP.Tools.ReadWrite`
11. Click **Add permissions**
12. Click **Grant admin consent for [your org]** (requires admin role)

### 2c. Pre-authorize the Client App (recommended)

1. Go back to the **Server App Registration > Expose an API**
2. Under **Authorized client applications**, click **Add a client application**
3. Enter the **Client App Registration's Application (client) ID**
4. Check the `MCP.Tools.ReadWrite` scope
5. Click **Add application**

### 2d. Set the Entra ID env vars on the Databricks App

After creating the app registrations, update the app's environment variables. Edit `app.yaml` and set:

```yaml
  - name: AZURE_TENANT_ID
    value: "your-entra-tenant-id"
  - name: AZURE_CLIENT_ID
    value: "your-server-app-client-id"
```

Then redeploy:

```bash
databricks apps deploy YOUR-APP-NAME --source-code-path .
```

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

## Step 3 -- Connect to Copilot Studio

### 3a. Create the MCP tool connection

1. Go to [Copilot Studio](https://copilotstudio.microsoft.com)
2. Open your agent (or create a new one)
3. Go to **Tools > Add a tool > New tool > Model Context Protocol**
4. Fill in:

   | Field | Value |
   |-------|-------|
   | Server name | Your preferred display name |
   | Server description | A description of what the tool does |
   | Server URL | `https://YOUR-WORKSPACE.azuredatabricks.net/apps/YOUR-APP-NAME/mcp` |

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

### 3b. Add the callback URL

After creating the connection, Copilot Studio shows a **Callback URL** (redirect URI).

1. Copy this URL
2. Go to **Azure Portal > Client App Registration > Authentication**
3. Under **Web > Redirect URIs**, click **Add URI**
4. Paste the callback URL
5. Click **Save**

### 3c. Verify and add the tool

1. Back in Copilot Studio, click **Next** on the tool configuration
2. Verify the `query_genie` tool appears in the list
3. Click **Add to agent**

---

## Step 4 -- Configure the Agent

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

---

## Step 5 -- Publish to Microsoft Teams

1. In Copilot Studio, click **Publish** in the left navigation
2. Confirm the publish action (takes 1-2 minutes)
3. Go to **Channels > Microsoft Teams**
4. Click **Turn on Teams**
5. Options:
   - **Open in Teams** -- for personal testing
   - **Make available to others** -- share within your org
   - **Submit to admin** -- submit to the Teams App Store for org-wide availability

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
| `GENIE_SPACE_ID` | Yes | Genie Space ID. Resolved via `valueFrom` resource in `app.yaml` |
| `AZURE_TENANT_ID` | For Copilot Studio | Entra ID tenant ID. Auth is **disabled** when this is unset |
| `AZURE_CLIENT_ID` | For Copilot Studio | Server App Registration's client ID (used as the token audience) |
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

Check the app logs:

```bash
databricks apps logs YOUR-APP-NAME
```

Common causes:
- **Missing Genie Space resource**: Ensure you added the Genie Space as a managed resource (Step 1d)
- **Dependency install failure**: Check the build logs for pip errors
- **Port mismatch**: Databricks Apps expects port 8080; verify `app.yaml` uses `--port 8080`

### "Error: GENIE_SPACE_ID not configured"

The Genie Space resource is not attached or the key doesn't match. Verify:
- The resource key in the Apps UI is `genie-space` (matching `app.yaml`)
- The resource status shows as connected

### "Invalid token audience" (HTTP 401)

The OAuth token's `aud` claim does not match the server's expected audience. Verify:
- `AZURE_CLIENT_ID` is set to the **Server** App Registration's client ID (not the Client app)
- The Copilot Studio scope is `api://SERVER-APP-CLIENT-ID/.default`

### "Token signing key not found" (HTTP 401)

The token's `kid` was not found in Microsoft's JWKS. This can happen if:
- The token is from a different identity provider (not Entra ID)
- There is a network issue reaching `login.microsoftonline.com` from the Databricks Apps network

### AADSTS700016: Application not found in directory

Copilot Studio is in a **different tenant** than the app registrations. Fix:
1. Change both app registrations to **multi-tenant** (Accounts in any organizational directory)
2. Update the Copilot Studio OAuth URLs to use `/common/` instead of a specific tenant ID
3. Ensure the code uses the `/common` JWKS endpoint (the default)

### AADSTS900144: Missing 'scope' parameter

Copilot Studio is not sending the scope. In the tool OAuth config, set:
- **Scope**: `api://SERVER-APP-CLIENT-ID/.default`

### Genie returns "Model registry functionality is unavailable"

The `MLFLOW_TRACKING_URI` env var is not set. Verify it's set to `databricks` in `app.yaml`.

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

**Entra ID auth is optional**: When `AZURE_TENANT_ID` is unset, the middleware passes all requests through without validation. This is the default for Databricks Apps. Enable it only when connecting Copilot Studio.

**Multi-tenant by default**: The code uses the `/common` JWKS endpoint and skips issuer validation, allowing tokens from any Entra ID tenant. For stricter security, switch to single-tenant mode (see [Step 2](#single-tenant-vs-multi-tenant-code-configuration)).

**Streamable HTTP transport**: Copilot Studio requires MCP servers to use Streamable HTTP. FastMCP's `http_app()` provides this on the `/mcp` endpoint.
