# Genie MCP Server for Copilot Studio -- Private Link Edition

[![GitHub](https://img.shields.io/badge/GitHub-Repository-blue?logo=github)](https://github.com/sean-zhang-dbx/genie-mcp-copilot)

An MCP server deployed as a **Databricks App** on a **VNet-injected, Private Link-enabled workspace** that connects **Microsoft Copilot Studio** to **Databricks Genie**.

This branch demonstrates the full end-to-end setup with Azure Private Link, including VNet injection, Private Endpoints, Private DNS, and hybrid public/private access for Copilot Studio compatibility.

> For a version without Private Link, see the [`databricks-apps`](https://github.com/sean-zhang-dbx/genie-mcp-copilot/tree/databricks-apps) branch.

## Architecture

```
Teams User
    |  Chat message
    v
Microsoft Copilot Studio (Microsoft cloud)
    |  MCP tool call (Streamable HTTP)
    |  Databricks OIDC OAuth (user token)
    |  --- public internet (IP ACL restricted) --->
    v
Azure Databricks Workspace (VNet-injected + Private Link)
    |  Hybrid mode: public access ON + IP ACLs
    |  Private Endpoint: 10.0.1.4 (databricks_ui_api)
    v
Databricks App  (FastAPI + FastMCP)
    |  OBO: user token from x-forwarded-access-token header
    v
Databricks Genie Space --> Unity Catalog Tables
```

**Key design:** The workspace runs in hybrid mode -- Private Link is enabled for internal users via the Private Endpoint, while Copilot Studio accesses the workspace publicly through IP-restricted access (Power Platform IPs only). The Databricks App URL (`*.databricksapps.com`) is inherently public-facing, so Copilot Studio can reach it. The OIDC endpoints (`/oidc/v1/authorize`, `/oidc/v1/token`) also need public access for the OAuth flow.

---

## Prerequisites

- **Azure CLI** -- [Install](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli)
- **Databricks CLI** -- [Install](https://docs.databricks.com/dev-tools/cli/install.html)
- An **Azure subscription** with permissions to create VNets, NSGs, Private Endpoints, DNS zones, and Databricks workspaces
- **Databricks Account Console** access (to create an OAuth App Connection)
- **Microsoft Copilot Studio** access ([copilotstudio.microsoft.com](https://copilotstudio.microsoft.com))

---

## Part 1 -- Azure Infrastructure

### 1.1 Create VNet and subnets

```bash
RESOURCE_GROUP="<your-resource-group>"
LOCATION="<your-region>"    # e.g. eastus2
VNET_NAME="vnet-databricks"

# VNet with a subnet for Private Endpoints
az network vnet create \
  --name $VNET_NAME \
  --resource-group $RESOURCE_GROUP \
  --location $LOCATION \
  --address-prefix 10.0.0.0/16 \
  --subnet-name snet-private-endpoints \
  --subnet-prefix 10.0.1.0/24

# Disable PE network policies
az network vnet subnet update \
  --name snet-private-endpoints \
  --resource-group $RESOURCE_GROUP \
  --vnet-name $VNET_NAME \
  --private-endpoint-network-policies Disabled

# Databricks public subnet (delegated)
az network vnet subnet create \
  --name snet-databricks-public \
  --resource-group $RESOURCE_GROUP \
  --vnet-name $VNET_NAME \
  --address-prefix 10.0.2.0/24 \
  --delegations Microsoft.Databricks/workspaces

# Databricks private subnet (delegated)
az network vnet subnet create \
  --name snet-databricks-private \
  --resource-group $RESOURCE_GROUP \
  --vnet-name $VNET_NAME \
  --address-prefix 10.0.3.0/24 \
  --delegations Microsoft.Databricks/workspaces
```

### 1.2 Create NSG and attach

```bash
az network nsg create \
  --name nsg-databricks \
  --resource-group $RESOURCE_GROUP \
  --location $LOCATION

az network vnet subnet update \
  --name snet-databricks-public \
  --resource-group $RESOURCE_GROUP \
  --vnet-name $VNET_NAME \
  --network-security-group nsg-databricks

az network vnet subnet update \
  --name snet-databricks-private \
  --resource-group $RESOURCE_GROUP \
  --vnet-name $VNET_NAME \
  --network-security-group nsg-databricks
```

### 1.3 Create VNet-injected Databricks workspace

```bash
WORKSPACE_NAME="<your-workspace-name>"
VNET_ID=$(az network vnet show --name $VNET_NAME --resource-group $RESOURCE_GROUP --query id -o tsv)

az databricks workspace create \
  --name $WORKSPACE_NAME \
  --resource-group $RESOURCE_GROUP \
  --location $LOCATION \
  --sku premium \
  --public-network-access Enabled \
  --required-nsg-rules AllRules \
  --vnet "$VNET_ID" \
  --public-subnet snet-databricks-public \
  --private-subnet snet-databricks-private \
  --enable-no-public-ip
```

> **`--public-network-access Enabled`** is critical. This creates hybrid mode: internal users use Private Link, Copilot Studio uses IP-restricted public access.

This takes ~4 minutes. Note the workspace URL from the output (e.g. `adb-XXXXXXXXX.X.azuredatabricks.net`).

### 1.4 Create Private DNS zones and link to VNet

```bash
# DNS zone for workspace
az network private-dns zone create \
  --name "privatelink.azuredatabricks.net" \
  --resource-group $RESOURCE_GROUP

# DNS zone for Databricks Apps
az network private-dns zone create \
  --name "privatelink.azure.databricksapps.com" \
  --resource-group $RESOURCE_GROUP

# Link both to VNet
az network private-dns link vnet create \
  --name link-dbx \
  --resource-group $RESOURCE_GROUP \
  --zone-name "privatelink.azuredatabricks.net" \
  --virtual-network $VNET_NAME \
  --registration-enabled false

az network private-dns link vnet create \
  --name link-apps \
  --resource-group $RESOURCE_GROUP \
  --zone-name "privatelink.azure.databricksapps.com" \
  --virtual-network $VNET_NAME \
  --registration-enabled false
```

### 1.5 Create Private Endpoint

```bash
WORKSPACE_RESOURCE_ID=$(az databricks workspace show \
  --name $WORKSPACE_NAME \
  --resource-group $RESOURCE_GROUP \
  --query id -o tsv)

az network private-endpoint create \
  --name pe-databricks-frontend \
  --resource-group $RESOURCE_GROUP \
  --location $LOCATION \
  --vnet-name $VNET_NAME \
  --subnet snet-private-endpoints \
  --private-connection-resource-id "$WORKSPACE_RESOURCE_ID" \
  --group-id "databricks_ui_api" \
  --connection-name "pe-conn-databricks"

# Auto-register DNS records
az network private-endpoint dns-zone-group create \
  --endpoint-name pe-databricks-frontend \
  --resource-group $RESOURCE_GROUP \
  --name dbx-dns-group \
  --zone-name privatelink-azuredatabricks-net \
  --private-dns-zone "$(az network private-dns zone show \
    --name privatelink.azuredatabricks.net \
    --resource-group $RESOURCE_GROUP --query id -o tsv)"
```

### 1.6 Verify Private Link

```bash
# Confirm PE is approved
az databricks workspace show \
  --name $WORKSPACE_NAME \
  --resource-group $RESOURCE_GROUP \
  --query "privateEndpointConnections[].{name:name, status:properties.privateLinkServiceConnectionState.status}" \
  -o table
```

Expected output:

```
Name                         Status
---------------------------  --------
pe-conn-databricks           Approved
```

---

## Part 2 -- Deploy the Databricks App

### 2.1 Authenticate to the workspace

```bash
WORKSPACE_URL="https://<your-workspace>.azuredatabricks.net"
databricks auth login --host $WORKSPACE_URL
```

### 2.2 Update `app.yaml`

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

### 2.3 Upload and deploy

```bash
APP_NAME="genie-mcp-copilot"
WS_PATH="/Workspace/Users/<your-email>/$APP_NAME"

databricks workspace mkdirs "$WS_PATH"
for f in app.py app.yaml pyproject.toml requirements.txt; do
  databricks workspace import "$WS_PATH/$f" --file "$f" --format AUTO --overwrite
done

databricks apps create $APP_NAME
databricks apps deploy $APP_NAME --source-code-path "$WS_PATH"
```

Wait for `app_status.state = RUNNING` (~2-5 minutes):

```bash
databricks apps get $APP_NAME
```

### 2.4 Grant the app's SP permissions

Find the SP Application ID from the app details, then:

```bash
TOKEN=$(databricks auth token --host $WORKSPACE_URL | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
SP_APP_ID="<sp-application-id-from-app-details>"

# CAN_RUN on Genie space
curl -X PUT "$WORKSPACE_URL/api/2.0/permissions/genie/<space-id>" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"access_control_list\": [{\"service_principal_name\": \"$SP_APP_ID\", \"permission_level\": \"CAN_RUN\"}]}"

# CAN_USE on SQL warehouse
curl -X PUT "$WORKSPACE_URL/api/2.0/permissions/sql/warehouses/<warehouse-id>" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"access_control_list\": [{\"service_principal_name\": \"$SP_APP_ID\", \"permission_level\": \"CAN_USE\"}]}"
```

```sql
-- Unity Catalog grants (run via SQL editor or API)
GRANT USE CATALOG ON CATALOG <catalog> TO `<sp-application-id>`;
GRANT USE SCHEMA ON SCHEMA <catalog>.<schema> TO `<sp-application-id>`;
GRANT SELECT ON SCHEMA <catalog>.<schema> TO `<sp-application-id>`;
```

### 2.5 Verify MCP endpoint

```bash
MCP_URL="https://$APP_NAME-<workspace-id>.<N>.azure.databricksapps.com/mcp"

INIT=$(curl -sS -D - -X POST "$MCP_URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}')

SESSION=$(echo "$INIT" | grep -i "mcp-session-id" | head -1 | sed 's/.*: *//;s/\r//')

curl -sS -X POST "$MCP_URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"query_genie","arguments":{"query":"How many customers are there?"}}}'
```

---

## Part 3 -- Databricks OAuth App Connection

1. Go to [Databricks Account Console](https://accounts.azuredatabricks.net) > **Settings** > **App connections**
2. Click **Add connection**
3. Configure:
   - **Name**: `copilot-studio-mcp`
   - **Redirect URLs** (add both):
     - `https://token.botframework.com/.auth/web/redirect`
     - `https://global.consent.azure-apim.net/redirect/<your-copilot-path>` *(get this from step 4.3)*
   - **Access scopes**: `all-apis offline_access`
4. **Generate a client secret** -- save the **Client ID** and **Client Secret** (shown only once)

---

## Part 4 -- Copilot Studio Configuration

### 4.1 Create an agent

1. Go to [copilotstudio.microsoft.com](https://copilotstudio.microsoft.com)
2. **Create** > **New agent** > name it (e.g. `Genie Data Assistant`)

### 4.2 Configure agent authentication

**Settings** > **Security** > **Authentication** > **Authenticate manually** > **Require users to sign in**: ON

Set **Service provider** to **Generic OAuth 2** and fill in every field:

| Field | Value |
|-------|-------|
| Client ID | `<from Part 3>` |
| Client secret | `<from Part 3>` |
| Scope list delimiter | ` ` *(single space -- not blank)* |
| Authorization URL template | `https://<workspace>.azuredatabricks.net/oidc/v1/authorize` |
| Authorization URL query string template | `?client_id={ClientId}&response_type=code&redirect_uri={RedirectUrl}&scope={Scopes}` |
| Token URL template | `https://<workspace>.azuredatabricks.net/oidc/v1/token` |
| Token URL query string template | *(leave blank)* |
| Token body template | `code={Code}&grant_type=authorization_code&redirect_uri={RedirectUrl}&client_id={ClientId}&client_secret={ClientSecret}` |
| Refresh URL template | `https://<workspace>.azuredatabricks.net/oidc/v1/token` |
| Refresh URL query string template | *(leave blank)* |
| Refresh body template | `refresh_token={RefreshToken}&grant_type=refresh_token&client_id={ClientId}&client_secret={ClientSecret}` |
| Scopes | `all-apis offline_access` |

Click **Save**.

### 4.3 Add the MCP Server action

**Actions** > **Add an action** > **Model Context Protocol**

| Field | Value |
|-------|-------|
| Server name | `genie-mcp` |
| Server description | `Databricks Genie data analysis` |
| Server URL | `https://<app-name>-<workspace-id>.<N>.azure.databricksapps.com/mcp` |
| Authentication | **OAuth 2.0** |

Fill in the **same OAuth fields** as step 4.2 (Client ID, Client secret, all URL templates, body templates, scopes).

Click **Create**. Copilot Studio connects to the MCP server and auto-discovers the `query_genie` tool.

> **Important:** After creating the action, check the error log or redirect URL. If you see a new redirect URL like `https://global.consent.azure-apim.net/redirect/...`, go back to the Databricks Account Console and add it to the App Connection's redirect URLs.

### 4.4 Test

1. Open the **Test** panel in Copilot Studio
2. The agent prompts you to **Login** (first time)
3. Authenticate with your Databricks / Entra ID credentials
4. Ask: `How many customers are there?`
5. The agent calls `query_genie` and returns SQL + results

---

## Part 5 -- IP Access Lists (Securing Hybrid Mode)

With Private Link + hybrid mode, the workspace is publicly accessible. To lock it down so only your IPs and Copilot Studio can access it:

### 5.1 Important: Test first without IP ACLs

IP ACLs are the #1 cause of `403 Forbidden` errors from Copilot Studio. Confirm everything works **before** enabling them.

### 5.2 Enable IP access lists

```bash
# Create an allow list with your IP
curl -X POST "$WORKSPACE_URL/api/2.0/ip-access-lists" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"label": "Admin IPs", "list_type": "ALLOW", "ip_addresses": ["<your-ip>"]}'

# Add Power Platform IPs (required for Copilot Studio)
# Get current IP ranges for PowerPlatformInfra and PowerPlatformPlex:
# https://www.microsoft.com/en-us/download/details.aspx?id=56519
# Add the relevant regional ranges to the allow list.

# Enable IP access lists
curl -X PATCH "$WORKSPACE_URL/api/2.0/workspace-conf" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enableIpAccessLists": "true"}'
```

> IP ACLs only apply to **public** traffic. Private Link traffic bypasses them entirely.

### 5.3 If Copilot Studio gets 403 after enabling IP ACLs

Temporarily disable to confirm it's an IP issue:

```bash
curl -X PATCH "$WORKSPACE_URL/api/2.0/workspace-conf" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enableIpAccessLists": "false"}'
```

Then add the missing Power Platform IP ranges and re-enable.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `NonVNetInjectedWorkspaceNotSupported` | Workspace was not created with VNet injection | VNet injection is a creation-time setting. Create a new workspace with `--vnet`, `--public-subnet`, `--private-subnet` flags |
| `Connector request failed / forbidden` | IP ACLs blocking Copilot Studio | Disable IP ACLs or add Power Platform IPs (see Part 5) |
| `redirect_uri not registered` | Copilot Studio's redirect URL is not in the Databricks OAuth App Connection | Add the exact URL from the error message to the App Connection's redirect URLs |
| `PERMISSION_DENIED: Failed to fetch tables` | App SP lacks table access | Grant `USE CATALOG`, `USE SCHEMA`, `SELECT` to the SP's Application ID (UUID) |
| `Node with resource name ... does not exist` | App SP lacks Genie Space access | Grant `CAN_RUN` on the Genie Space to the SP's Application ID |
| `Model registry functionality is unavailable` | Missing `MLFLOW_TRACKING_URI` | Add `MLFLOW_TRACKING_URI: databricks` to `app.yaml` and redeploy |
| Scope delimiter error in Copilot Studio | `all-apis offline_access` has a space | Set **Scope list delimiter** to a single space character, not blank |
| OAuth login loops | Wrong OIDC URLs | URLs must point to the **workspace** (`*.azuredatabricks.net`), not the app (`*.databricksapps.com`) |

---

## Azure Resources Created

| Resource | Name | Details |
|----------|------|---------|
| VNet | `vnet-databricks` | `10.0.0.0/16` |
| Subnet | `snet-private-endpoints` | `10.0.1.0/24` -- for Private Endpoints |
| Subnet | `snet-databricks-public` | `10.0.2.0/24` -- delegated to Databricks |
| Subnet | `snet-databricks-private` | `10.0.3.0/24` -- delegated to Databricks |
| NSG | `nsg-databricks` | Attached to Databricks subnets |
| Private DNS Zone | `privatelink.azuredatabricks.net` | Linked to VNet |
| Private DNS Zone | `privatelink.azure.databricksapps.com` | Linked to VNet |
| Databricks Workspace | `<workspace-name>` | VNet-injected, Premium, NPIP, hybrid mode |
| Private Endpoint | `pe-databricks-frontend` | `databricks_ui_api`, auto-approved |

---

## Files

```
app.py              FastAPI + FastMCP server with OBO token passthrough
app.yaml            Databricks App config (points to PL workspace)
pyproject.toml      Python dependencies (managed by uv)
requirements.txt    Bootstrap dependency (uv) for Databricks Apps
README.md           This file (Private Link edition)
```
