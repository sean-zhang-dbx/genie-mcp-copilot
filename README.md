# Genie MCP Server for Microsoft Copilot Studio

[![GitHub](https://img.shields.io/badge/GitHub-Repository-blue?logo=github)](https://github.com/sean-zhang-dbx/genie-mcp-copilot)

An MCP (Model Context Protocol) server deployed as a **Databricks App** that connects **Microsoft Copilot Studio** to **Databricks Genie** using Databricks-native OAuth (OIDC).

Users ask natural-language questions in Teams (via Copilot Studio), which are forwarded to a Genie Space and return structured answers with SQL and results.

## Architecture

```
Teams User
    |  Chat message
    v
Microsoft Copilot Studio
    |  MCP tool call (Streamable HTTP)
    |  Databricks OIDC OAuth (user token)
    v
Databricks App  (FastAPI + FastMCP)
    |  On-Behalf-Of: user token from headers
    v
Databricks Genie Space
    |
    v
Unity Catalog Tables
```

**Authentication flow:** Copilot Studio authenticates the user via Databricks OIDC (`/oidc/v1/authorize`). Databricks passes the user's token to the app through HTTP headers (`x-forwarded-access-token`). The app creates a per-user `WorkspaceClient` with that token, so Genie queries respect the user's permissions (OBO pattern). If no user token is present, the app falls back to its auto-injected Service Principal credentials.

---

## Prerequisites

- **Databricks CLI** -- [Install](https://docs.databricks.com/dev-tools/cli/install.html)
- A **Databricks workspace** (Azure) with at least one Genie Space configured
- **Workspace admin** access (to grant the app's SP access to the Genie Space)
- **Databricks Account Console** access (to create an OAuth App Connection)
- **Microsoft Copilot Studio** access

---

## Project Structure

```
app.py              FastAPI + FastMCP server with OBO token passthrough
app.yaml            Databricks App config (uv runner, env vars)
pyproject.toml      Python dependencies (managed by uv)
requirements.txt    Bootstrap dependency (uv) for Databricks Apps
README.md           This file
```

---

## Customization

Before deploying, edit `app.py` to match your domain:

### Server name and instructions

```python
mcp_server = FastMCP(
    "YOUR SERVER NAME",
    instructions=(
        "Description of your data domain.\n\n"
        "Available Tables:\n"
        "- table_a: description\n"
        "- table_b: description\n"
    ),
)
```

### Tool description

```python
@mcp_server.tool()
def query_genie(
    query: Annotated[str, "Natural language question about YOUR DATA."],
    ...
) -> str:
    """Query Databricks Genie for YOUR DOMAIN analysis."""
```

---

## Part 1 -- Deploy to Databricks Apps

### 1.1 Authenticate the Databricks CLI

```bash
databricks configure --host https://<your-workspace>.azuredatabricks.net
```

### 1.2 Update `app.yaml`

Set `DATABRICKS_HOST` and `GENIE_SPACE_ID` for your workspace:

```yaml
env:
  - name: DATABRICKS_HOST
    value: "https://<your-workspace>.azuredatabricks.net/"
  - name: GENIE_SPACE_ID
    value: "<your-genie-space-id>"
  - name: MLFLOW_TRACKING_URI
    value: "databricks"
```

Find your Genie Space ID in the URL: `https://<workspace>/genie/rooms/<THIS-IS-THE-ID>?o=...`

### 1.3 Create the app

```bash
databricks apps create <your-app-name> --description "Genie MCP Server for Copilot Studio"
```

### 1.4 Deploy the code

```bash
databricks apps deploy <your-app-name> --source-code-path .
```

First deployment takes 2-5 minutes. Monitor with:

```bash
databricks apps get <your-app-name>
```

### 1.5 Grant the app's Service Principal access

Databricks Apps auto-creates a Service Principal. Grant it access to your Genie Space and data:

1. Go to **Compute > Apps > your-app > Settings** and note the SP name
2. Open the Genie Space > **Share** > add the SP with **Can Run** permission
3. Grant table access:

```sql
GRANT USE CATALOG ON CATALOG <your_catalog> TO `<your-app-name>`;
GRANT USE SCHEMA ON SCHEMA <your_catalog>.<your_schema> TO `<your-app-name>`;
GRANT SELECT ON SCHEMA <your_catalog>.<your_schema> TO `<your-app-name>`;
```

### 1.6 Verify

The app URL follows this pattern:

```
https://<your-app-name>-<workspace-id>.<N>.azure.databricksapps.com
```

Open it in a browser while logged into the workspace. If you see the FastAPI docs page or an MCP response, the app is running.

---

## Part 2 -- Connect to Copilot Studio

### 2.1 Create an OAuth App Connection in Databricks

1. Go to the **Databricks Account Console** > **Settings** > **App connections**
2. Click **Add connection**
3. Fill in:
   - **Name**: `copilot-studio-mcp` (or any descriptive name)
   - **Redirect URLs**: Add the redirect URL from Copilot Studio (you'll get this in step 2.3)
   - **Access scopes**: `all-apis offline_access`
   - **Generate a client secret** and save it securely
4. Note the **Client ID** and **Client Secret**

### 2.2 Identify your OIDC endpoints

Your workspace OIDC endpoints are:

| Endpoint | URL |
|----------|-----|
| Authorization | `https://<your-workspace>.azuredatabricks.net/oidc/v1/authorize` |
| Token | `https://<your-workspace>.azuredatabricks.net/oidc/v1/token` |
| Scopes | `all-apis offline_access` |

### 2.3 Configure Copilot Studio

1. In Copilot Studio, create or open your agent
2. Go to **Settings** > **Security** > **Authentication**
3. Select **Authenticate with Microsoft** or **OAuth 2.0** (manual)
4. Configure:
   - **Authorization URL**: `https://<your-workspace>.azuredatabricks.net/oidc/v1/authorize`
   - **Token URL**: `https://<your-workspace>.azuredatabricks.net/oidc/v1/token`
   - **Client ID**: from step 2.1
   - **Client Secret**: from step 2.1
   - **Scope**: `all-apis offline_access`
5. Copy the **Redirect URL** shown by Copilot Studio
6. Go back to the Databricks Account Console and add this redirect URL to your app connection (step 2.1)

### 2.4 Add the MCP tool action

1. In Copilot Studio, go to **Actions** > **Add action**
2. Select **MCP Server (Streamable HTTP)**
3. Enter the MCP endpoint URL:
   ```
   https://<your-app-name>-<workspace-id>.<N>.azure.databricksapps.com/mcp
   ```
4. Copilot Studio discovers the `query_genie` tool automatically
5. Configure the tool description if needed and publish

### 2.5 Test in Copilot Studio

Use the **Test** panel to ask a question like:

> "What is the CTRP performance for last quarter?"

You should see:
- Copilot Studio prompts the user to authenticate (first time)
- After login, the query is sent to Genie via the MCP tool
- The response includes SQL, description, and results

---

## Part 3 -- Private Link Configuration

If your Databricks workspace uses Azure Private Link, Copilot Studio (running in Microsoft's cloud) cannot reach the workspace URLs by default. Here are three approaches, from simplest to most secure.

### Option A: Hybrid Mode (Public Access + Private Link + IP ACLs)

Keep Private Link for internal users while allowing Copilot Studio through restricted public access.

1. **Enable public network access** alongside Private Link in the workspace network settings (Azure Portal > Databricks workspace > Networking)
2. **Add IP access lists** to restrict public access to Power Platform IPs only:
   - Use the `PowerPlatformInfra` and `PowerPlatformPlex` Azure service tags
   - In Databricks: **Settings** > **Security** > **IP Access Lists**
   - IP ACLs only apply to public traffic; Private Link traffic bypasses them

This is the simplest option. Internal users continue using Private Link. Only whitelisted Power Platform IPs can access the workspace publicly.

### Option B: Power Platform VNet Integration

Route Copilot Studio traffic through a VNet that has a Private Endpoint to Databricks.

1. Configure **VNet support for the Power Platform environment** (requires a Managed Environment)
2. Create an **Azure Private Endpoint for Databricks** in the VNet
3. Set up **conditional DNS forwarding** for `*.azuredatabricks.net` and `*.databricksapps.com`
4. Copilot Studio agent traffic routes through the VNet's private connection

**Caveat:** VNet integration covers HTTP Request nodes and supported connectors. MCP tool connections may take a different code path -- test to confirm MCP calls route through the VNet.

### Option C: Azure API Management (APIM) Reverse Proxy

Deploy APIM as a bridge between public Copilot Studio and private Databricks.

1. Deploy **APIM in the customer's VNet** with a public frontend
2. APIM accesses Databricks via the **Private Endpoint** in the VNet
3. Configure APIM to proxy:
   - `/oidc/v1/authorize` and `/oidc/v1/token` (OAuth endpoints)
   - `/apps/<your-app-name>/*` (the MCP server)
4. Point Copilot Studio to the APIM URL instead of the Databricks URL

Most complex but works when public access is completely disabled.

### Recommendation

| Network Config | Recommended Approach |
|---|---|
| Public workspace (default) | No extra config needed |
| IP ACLs only | Whitelist Power Platform IPs (PDF section 3.5) |
| Private Link (hybrid OK) | **Option A** -- simplest, enable public + IP ACLs |
| Private Link (strict, no public) | **Option B** first, fall back to **Option C** |

---

## Local Development

```bash
# Install dependencies
pip install uv
uv sync

# Set credentials
export DATABRICKS_HOST="https://<your-workspace>.azuredatabricks.net"
export DATABRICKS_CLIENT_ID="<your-sp-client-id>"
export DATABRICKS_CLIENT_SECRET="<your-sp-client-secret>"
export GENIE_SPACE_ID="<your-genie-space-id>"

# Start the server
uv run uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Test the MCP endpoint:

```bash
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

## Environment Variables

| Variable | Source | Description |
|----------|--------|-------------|
| `DATABRICKS_HOST` | `app.yaml` | Workspace URL |
| `DATABRICKS_CLIENT_ID` | Auto-injected | App SP client ID (set automatically by Databricks Apps) |
| `DATABRICKS_CLIENT_SECRET` | Auto-injected | App SP secret (set automatically by Databricks Apps) |
| `GENIE_SPACE_ID` | `app.yaml` | Genie Space ID |
| `MLFLOW_TRACKING_URI` | `app.yaml` | Must be `databricks` (required by `databricks-ai-bridge`) |
| `LOG_LEVEL` | `app.yaml` | Python log level (default: `INFO`) |

---

## Troubleshooting

### App fails to start

Check logs: **Compute > Apps > your-app > Logs**. Common causes:

- **Missing `GENIE_SPACE_ID`** in `app.yaml`
- **Dependency install failure** -- check build logs for `uv` errors
- **Port mismatch** -- `app.yaml` must use `--port 8000`

### "Error: GENIE_SPACE_ID not configured"

Set `GENIE_SPACE_ID` in `app.yaml` and redeploy. Find it in the Genie Space URL:
`https://<workspace>/genie/rooms/<THIS-IS-THE-ID>?o=...`

### Genie returns "Model registry functionality is unavailable"

The `MLFLOW_TRACKING_URI` env var is not set or not set to `databricks`. Verify it's in `app.yaml` and redeploy.

### Genie returns "Node with resource name ... does not exist"

The app's SP doesn't have `CAN_RUN` permission on the Genie Space. Grant it via the permissions API using the SP's application ID (not display name):

```bash
curl -X PUT "https://<workspace>/api/2.0/permissions/genie/<space-id>" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"access_control_list": [{"service_principal_name": "<sp-application-id>", "permission_level": "CAN_RUN"}]}'
```

### Genie returns other permission errors

The app's SP needs access to the underlying tables. See Part 1, step 1.5.

### Copilot Studio authentication fails

- Verify the **redirect URL** in the Databricks App Connection matches what Copilot Studio provides
- Confirm **scopes** are set to `all-apis offline_access`
- Check that the OIDC endpoints match your workspace URL (not the app URL)

### IP access list blocks Copilot Studio

If using IP ACLs, ensure the `PowerPlatformInfra` and `PowerPlatformPlex` service tag IPs are whitelisted. See Part 3, Option A.
