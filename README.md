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

**Authentication flow:** Copilot Studio authenticates the user via Databricks OIDC (`/oidc/v1/authorize`). The user's token is passed through to the MCP server via HTTP headers. The app creates a per-user `WorkspaceClient` with that token, so Genie queries respect the user's Unity Catalog permissions (OBO pattern). If no user token is present, the app falls back to its auto-injected Service Principal credentials.

---

## Prerequisites

- **Databricks CLI** -- [Install](https://docs.databricks.com/dev-tools/cli/install.html)
- A **Databricks workspace** (Azure) with at least one Genie Space configured
- **Workspace admin** access (to grant the app's SP access to the Genie Space)
- **Databricks Account Console** access (to create an OAuth App Connection)
- **Microsoft Copilot Studio** access ([copilotstudio.microsoft.com](https://copilotstudio.microsoft.com))

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
databricks auth login --host https://<your-workspace>.azuredatabricks.net
```

### 1.2 Update `app.yaml`

Set `DATABRICKS_HOST` and `GENIE_SPACE_ID` for your workspace:

```yaml
command:
  - "uv"
  - "run"
  - "uvicorn"
  - "app:app"
  - "--host"
  - "0.0.0.0"
  - "--port"
  - "8000"

env:
  - name: DATABRICKS_HOST
    value: "https://<your-workspace>.azuredatabricks.net/"
  - name: GENIE_SPACE_ID
    value: "<your-genie-space-id>"
  - name: MLFLOW_TRACKING_URI
    value: "databricks"
  - name: LOG_LEVEL
    value: "INFO"
```

Find your Genie Space ID in the URL: `https://<workspace>/genie/rooms/<THIS-IS-THE-ID>?o=...`

### 1.3 Upload the code to the workspace

```bash
WS_PATH="/Workspace/Users/<your-email>/genie-mcp-copilot"
databricks workspace mkdirs "$WS_PATH"
for f in app.py app.yaml pyproject.toml requirements.txt; do
  databricks workspace import "$WS_PATH/$f" --file "$f" --format AUTO --overwrite
done
```

### 1.4 Create and deploy the app

```bash
# Create the app
databricks apps create <your-app-name>

# Deploy the code
databricks apps deploy <your-app-name> --source-code-path "$WS_PATH"
```

First deployment takes 2-5 minutes. Monitor with:

```bash
databricks apps get <your-app-name>
```

The app URL will be:

```
https://<your-app-name>-<workspace-id>.<N>.azure.databricksapps.com
```

### 1.5 Grant the app's Service Principal access

Databricks Apps auto-creates a Service Principal. You need to grant it access to the Genie Space and the underlying data.

**Find the SP's Application ID:**

Go to **Compute > Apps > your-app** and note the Service Principal name (e.g. `app-xxxxx your-app-name`). Then find its Application ID (UUID) via:

```bash
databricks api get /api/2.0/preview/scim/v2/ServicePrincipals \
  | python3 -c "
import sys,json
for sp in json.load(sys.stdin).get('Resources',[]):
    if 'your-app-name' in sp.get('displayName',''):
        print('App ID:', sp.get('applicationId'))
"
```

**Grant CAN_RUN on the Genie Space** (use the Application ID, not the display name):

```bash
curl -X PUT "https://<workspace>/api/2.0/permissions/genie/<space-id>" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"access_control_list": [
    {"service_principal_name": "<sp-application-id>", "permission_level": "CAN_RUN"}
  ]}'
```

**Grant CAN_USE on the SQL warehouse:**

```bash
curl -X PUT "https://<workspace>/api/2.0/permissions/sql/warehouses/<warehouse-id>" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"access_control_list": [
    {"service_principal_name": "<sp-application-id>", "permission_level": "CAN_USE"}
  ]}'
```

**Grant Unity Catalog table access:**

```sql
GRANT USE CATALOG ON CATALOG <your_catalog> TO `<sp-application-id>`;
GRANT USE SCHEMA ON SCHEMA <your_catalog>.<your_schema> TO `<sp-application-id>`;
GRANT SELECT ON SCHEMA <your_catalog>.<your_schema> TO `<sp-application-id>`;
```

### 1.6 Verify the MCP endpoint

Test the app directly with `curl`:

```bash
# Get a Databricks token
TOKEN=$(databricks auth token --host https://<your-workspace>.azuredatabricks.net | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

MCP_URL="https://<your-app-name>-<workspace-id>.<N>.azure.databricksapps.com/mcp"

# Initialize an MCP session
INIT=$(curl -sS -D - -X POST "$MCP_URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}')

SESSION_ID=$(echo "$INIT" | grep -i "mcp-session-id" | head -1 | sed 's/.*: *//;s/\r//')

# Query Genie
curl -sS -X POST "$MCP_URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"query_genie","arguments":{"query":"How many customers are there?"}}}'
```

You should see a JSON-RPC response with SQL, results, and a conversation ID.

---

## Part 2 -- Create OAuth App Connection in Databricks

### 2.1 Create the App Connection

1. Go to [Databricks Account Console](https://accounts.azuredatabricks.net) > **Settings** > **App connections**
2. Click **Add connection**
3. Fill in:
   - **Name**: `copilot-studio-mcp` (or any descriptive name)
   - **Redirect URLs**: Add both of these:
     - `https://token.botframework.com/.auth/web/redirect`
     - `https://global.consent.azure-apim.net/redirect/<your-copilot-redirect-path>` (you'll get this from Copilot Studio later -- see step 3.3)
   - **Access scopes**: `all-apis offline_access`
4. Click **Generate a client secret**
5. **Save the Client ID and Client Secret** -- the secret is only shown once

### 2.2 Identify your OIDC endpoints

Your workspace OIDC endpoints follow this pattern:

| Endpoint | URL |
|----------|-----|
| Authorization | `https://<your-workspace>.azuredatabricks.net/oidc/v1/authorize` |
| Token | `https://<your-workspace>.azuredatabricks.net/oidc/v1/token` |
| Refresh | `https://<your-workspace>.azuredatabricks.net/oidc/v1/token` |
| Scopes | `all-apis offline_access` |

---

## Part 3 -- Connect to Copilot Studio

### 3.1 Create an agent

1. Go to [copilotstudio.microsoft.com](https://copilotstudio.microsoft.com)
2. Click **Create** > **New agent**
3. Name it (e.g. `Genie Data Assistant`)
4. Click **Create**

### 3.2 Configure agent-level authentication

1. In your agent, click **Settings** (gear icon, top-right) > **Security** > **Authentication**
2. Select **Authenticate manually**
3. Toggle **Require users to sign in** to ON
4. Set **Service provider** to **Generic OAuth 2**
5. Fill in:

| Field | Value |
|-------|-------|
| **Client ID** | `<client-id from step 2.1>` |
| **Client secret** | `<client-secret from step 2.1>` |
| **Scope list delimiter** | ` ` (a single space character) |
| **Authorization URL template** | `https://<your-workspace>.azuredatabricks.net/oidc/v1/authorize` |
| **Authorization URL query string template** | `?client_id={ClientId}&response_type=code&redirect_uri={RedirectUrl}&scope={Scopes}` |
| **Token URL template** | `https://<your-workspace>.azuredatabricks.net/oidc/v1/token` |
| **Token URL query string template** | *(leave blank)* |
| **Token body template** | `code={Code}&grant_type=authorization_code&redirect_uri={RedirectUrl}&client_id={ClientId}&client_secret={ClientSecret}` |
| **Refresh URL template** | `https://<your-workspace>.azuredatabricks.net/oidc/v1/token` |
| **Refresh URL query string template** | *(leave blank)* |
| **Refresh body template** | `refresh_token={RefreshToken}&grant_type=refresh_token&client_id={ClientId}&client_secret={ClientSecret}` |
| **Scopes** | `all-apis offline_access` |

6. Click **Save**

> **Important:** Note the **Redirect URL** shown at the top of the Authentication page (e.g. `https://token.botframework.com/.auth/web/redirect`). Make sure this URL is registered in your Databricks OAuth App Connection (step 2.1).

### 3.3 Add the MCP Server action

1. In your agent, go to **Actions** (left sidebar) > **Add an action**
2. Search for or select **Model Context Protocol** (MCP Server)
3. Fill in:

| Field | Value |
|-------|-------|
| **Server name** | `genie-mcp` (or any name) |
| **Server description** | `Databricks Genie data analysis` |
| **Server URL** | `https://<your-app-name>-<workspace-id>.<N>.azure.databricksapps.com/mcp` |
| **Authentication** | **OAuth 2.0** |

4. When OAuth 2.0 is selected, fill in the same OAuth fields:

| Field | Value |
|-------|-------|
| **Client ID** | `<client-id from step 2.1>` |
| **Client secret** | `<client-secret from step 2.1>` |
| **Scope list delimiter** | ` ` (a single space character) |
| **Authorization URL template** | `https://<your-workspace>.azuredatabricks.net/oidc/v1/authorize` |
| **Authorization URL query string template** | `?client_id={ClientId}&response_type=code&redirect_uri={RedirectUrl}&scope={Scopes}` |
| **Token URL template** | `https://<your-workspace>.azuredatabricks.net/oidc/v1/token` |
| **Token URL query string template** | *(leave blank)* |
| **Token body template** | `code={Code}&grant_type=authorization_code&redirect_uri={RedirectUrl}&client_id={ClientId}&client_secret={ClientSecret}` |
| **Refresh URL template** | `https://<your-workspace>.azuredatabricks.net/oidc/v1/token` |
| **Refresh URL query string template** | *(leave blank)* |
| **Refresh body template** | `refresh_token={RefreshToken}&grant_type=refresh_token&client_id={ClientId}&client_secret={ClientSecret}` |
| **Scopes** | `all-apis offline_access` |

5. Click **Create**
6. Copilot Studio will connect to the MCP server and auto-discover the `query_genie` tool
7. **Check the redirect URL** shown on this page. If it differs from step 3.2 (e.g. `https://global.consent.azure-apim.net/redirect/...`), go back to the Databricks Account Console and add it to the OAuth App Connection's redirect URLs.

### 3.4 Test in Copilot Studio

1. Open the **Test** panel (bottom-left)
2. The agent should prompt you to **Login** (first time only)
3. Click **Login** and authenticate with your Databricks/Entra ID credentials
4. Ask a question: `How many customers are there?`
5. You should see Copilot Studio call the `query_genie` tool and return a response with SQL and results

### 3.5 Publish to Teams (optional)

1. Go to **Channels** > **Microsoft Teams**
2. Click **Turn on Teams**
3. Open the agent in Teams and start chatting

---

## Part 4 -- Private Link Configuration (Optional)

If your Databricks workspace uses Azure Private Link, Copilot Studio (running in Microsoft's cloud) cannot reach the workspace OIDC endpoints or the app URL by default.

### Prerequisites for Private Link

Azure Databricks Private Link requires **VNet injection** -- the workspace must be deployed into a customer-managed VNet. This is a **creation-time setting** and cannot be changed after the workspace is created.

### Azure infrastructure setup

If your workspace is not VNet-injected, you need to create a new one:

```bash
# 1. Create a VNet
az network vnet create \
  --name vnet-databricks \
  --resource-group <your-rg> \
  --location <your-region> \
  --address-prefix 10.0.0.0/16 \
  --subnet-name snet-private-endpoints \
  --subnet-prefix 10.0.1.0/24

# 2. Disable PE network policies on the PE subnet
az network vnet subnet update \
  --name snet-private-endpoints \
  --resource-group <your-rg> \
  --vnet-name vnet-databricks \
  --private-endpoint-network-policies Disabled

# 3. Create Databricks subnets (with delegation)
az network vnet subnet create \
  --name snet-databricks-public \
  --resource-group <your-rg> \
  --vnet-name vnet-databricks \
  --address-prefix 10.0.2.0/24 \
  --delegations Microsoft.Databricks/workspaces

az network vnet subnet create \
  --name snet-databricks-private \
  --resource-group <your-rg> \
  --vnet-name vnet-databricks \
  --address-prefix 10.0.3.0/24 \
  --delegations Microsoft.Databricks/workspaces

# 4. Create NSG and attach to subnets
az network nsg create --name nsg-databricks --resource-group <your-rg> --location <your-region>
az network vnet subnet update --name snet-databricks-public --resource-group <your-rg> --vnet-name vnet-databricks --network-security-group nsg-databricks
az network vnet subnet update --name snet-databricks-private --resource-group <your-rg> --vnet-name vnet-databricks --network-security-group nsg-databricks

# 5. Create VNet-injected workspace with public access enabled (hybrid mode)
az databricks workspace create \
  --name <workspace-name> \
  --resource-group <your-rg> \
  --location <your-region> \
  --sku premium \
  --public-network-access Enabled \
  --required-nsg-rules AllRules \
  --vnet "/subscriptions/<sub-id>/resourceGroups/<your-rg>/providers/Microsoft.Network/virtualNetworks/vnet-databricks" \
  --public-subnet snet-databricks-public \
  --private-subnet snet-databricks-private \
  --enable-no-public-ip

# 6. Create Private DNS zones
az network private-dns zone create --name "privatelink.azuredatabricks.net" --resource-group <your-rg>
az network private-dns zone create --name "privatelink.azure.databricksapps.com" --resource-group <your-rg>

# 7. Link DNS zones to VNet
az network private-dns link vnet create --name link-dbx --resource-group <your-rg> --zone-name "privatelink.azuredatabricks.net" --virtual-network vnet-databricks --registration-enabled false
az network private-dns link vnet create --name link-apps --resource-group <your-rg> --zone-name "privatelink.azure.databricksapps.com" --virtual-network vnet-databricks --registration-enabled false

# 8. Create Private Endpoint
az network private-endpoint create \
  --name pe-databricks-frontend \
  --resource-group <your-rg> \
  --location <your-region> \
  --vnet-name vnet-databricks \
  --subnet snet-private-endpoints \
  --private-connection-resource-id "/subscriptions/<sub-id>/resourceGroups/<your-rg>/providers/Microsoft.Databricks/workspaces/<workspace-name>" \
  --group-id "databricks_ui_api" \
  --connection-name "pe-conn-databricks"

# 9. Create DNS zone group for automatic records
az network private-endpoint dns-zone-group create \
  --endpoint-name pe-databricks-frontend \
  --resource-group <your-rg> \
  --name dbx-dns-group \
  --zone-name privatelink-azuredatabricks-net \
  --private-dns-zone "/subscriptions/<sub-id>/resourceGroups/<your-rg>/providers/Microsoft.Network/privateDnsZones/privatelink.azuredatabricks.net"
```

### Copilot Studio + Private Link options

Since Copilot Studio runs in Microsoft's cloud, it needs public access to reach the workspace OIDC endpoints and the app URL. Three approaches:

#### Option A: Hybrid Mode (Recommended)

Keep public network access enabled alongside Private Link. Use IP access lists to restrict public access to Power Platform IPs. **Important:** Copilot Studio IP ranges must be whitelisted, otherwise the OAuth token exchange and MCP calls will get `403 Forbidden`.

1. Enable IP access lists in Databricks: **Settings > Security > IP Access Lists**
2. Add `PowerPlatformInfra` and `PowerPlatformPlex` Azure service tag IP ranges
3. Internal users continue using Private Link; only whitelisted Power Platform IPs can access publicly

> **Gotcha we hit:** If IP access lists are enabled but Power Platform IPs are not whitelisted, Copilot Studio gets `403 Forbidden` errors when trying to authenticate or call the MCP endpoint. When testing, disable IP ACLs first, confirm everything works, then add the Power Platform IPs.

#### Option B: Power Platform VNet Integration

Route Copilot Studio traffic through a VNet with a Private Endpoint:
1. Configure VNet support for the Power Platform environment (requires a Managed Environment)
2. Copilot Studio agent traffic routes through the VNet's private connection

#### Option C: Azure API Management (APIM) Reverse Proxy

Deploy APIM with a public frontend that proxies to Databricks via Private Endpoint.

| Network Config | Recommended Approach |
|---|---|
| Public workspace (default) | No extra config needed |
| IP ACLs only | Whitelist Power Platform IPs |
| Private Link (hybrid OK) | **Option A** -- enable public + IP ACLs |
| Private Link (strict, no public) | **Option B** first, fall back to **Option C** |

---

## Local Development

```bash
pip install uv
uv sync

export DATABRICKS_HOST="https://<your-workspace>.azuredatabricks.net"
export DATABRICKS_CLIENT_ID="<your-sp-client-id>"
export DATABRICKS_CLIENT_SECRET="<your-sp-client-secret>"
export GENIE_SPACE_ID="<your-genie-space-id>"

uv run uvicorn app:app --host 0.0.0.0 --port 8000 --reload
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
- Missing `GENIE_SPACE_ID` in `app.yaml`
- Dependency install failure -- check build logs for `uv` errors
- Port mismatch -- `app.yaml` must use `--port 8000`

### "GENIE_SPACE_ID not configured"

Set `GENIE_SPACE_ID` in `app.yaml` and redeploy.

### "Model registry functionality is unavailable"

`MLFLOW_TRACKING_URI` is not set to `databricks`. Add it to `app.yaml` and redeploy.

### "Node with resource name ... does not exist"

The app's SP doesn't have `CAN_RUN` on the Genie Space. Grant it via the permissions API using the SP's **Application ID** (UUID), not its display name.

### "PERMISSION_DENIED: Failed to fetch tables" or "No access to table"

The app's SP needs `USE CATALOG`, `USE SCHEMA`, and `SELECT` on the underlying tables. See Part 1, step 1.5.

### "redirect_uri not registered for OAuth application"

The redirect URL used by Copilot Studio is not in the Databricks OAuth App Connection. Copilot Studio uses **two different redirect URLs**:

1. Agent authentication: `https://token.botframework.com/.auth/web/redirect`
2. MCP action OAuth: `https://global.consent.azure-apim.net/redirect/<dynamic-path>`

Add **both** to the Databricks Account Console > App connections > Redirect URLs. The MCP action redirect URL appears after you create the action -- check the error message for the exact URL.

### "Connector request failed / HttpStatusCode: forbidden"

This is usually caused by **IP access lists** blocking Copilot Studio's requests. Copilot Studio calls from Microsoft's Power Platform infrastructure IPs, which must be whitelisted. To diagnose:

1. Temporarily disable IP access lists: **Settings > Security > IP Access Lists > Disable**
2. Test the agent again
3. If it works, re-enable IP ACLs and add `PowerPlatformInfra` / `PowerPlatformPlex` service tag IPs

### Copilot Studio "Scope list delimiter" error

The scope `all-apis offline_access` contains a space. Set the **Scope list delimiter** to a single space character (` `). Do not leave it blank.

### Copilot Studio OAuth login loops or fails silently

- Verify the Authorization URL, Token URL, and Refresh URL all point to the **workspace URL** (not the app URL)
- Confirm Client ID and Client Secret match the Databricks Account Console App Connection
- Check that `all-apis offline_access` is in both the App Connection scopes and the Copilot Studio scopes
