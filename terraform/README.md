# Terraform — CMK Control Tower Azure Container App

Deploys the containerized CMK Control Tower app (built from
`control-tower-agent/Dockerfile`, published to
`ghcr.io/scallighan/cmk-control-tower`) as an **Azure Container App**.

Modeled on
[ai-tabletop-co/terraform](https://github.com/scallighan/ai-tabletop-co/tree/main/terraform),
but deploys into **existing** infrastructure rather than creating it:

- Resource group: **`cmk-settlement-rg`** (existing)
- Virtual network: **`core-dev-vnet`** (existing)
- A **new** `containerapp-subnet` (`10.0.10.0/23`) is created in that vnet and
  delegated to `Microsoft.App/environments` for the Container App environment.

## What it creates

| Resource | Purpose |
| --- | --- |
| `azurerm_subnet.containerapp` | New delegated subnet in `core-dev-vnet` |
| `azurerm_log_analytics_workspace.this` | Container App logs |
| `azurerm_user_assigned_identity.app` | App identity (Foundry + SQL auth) |
| `azurerm_role_assignment.app_foundry` | `Cognitive Services User` on the Foundry account |
| `azurerm_container_app_environment.this` | VNet-integrated Consumption environment |
| `azurerm_container_app.this` | The app (external ingress on port 8000) |

The container authenticates to Azure SQL and AI Foundry with
`DefaultAzureCredential`; `AZURE_CLIENT_ID` is set to the user-assigned
identity so managed identity is used at runtime.

## Usage

```bash
cp env.sample .env && edit .env      # set TF_VAR_subscription_id
source .env

terraform init
terraform plan
terraform apply
```

`terraform output app_url` prints the public URL.

## Required manual step: grant the identity read access to Azure SQL

Managed-identity access to `cmk-sqldb-ledger` requires a contained database
user (this cannot be expressed in `azurerm`). After `apply`, connect to the DB
as an Entra admin and run — using the identity name `uai-cmkct<suffix>` shown in
`terraform output`:

```sql
CREATE USER [uai-cmkct<suffix>] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [uai-cmkct<suffix>];
```

The app only issues `SELECT` statements, so `db_datareader` is sufficient.

## Notes

- The image is built and pushed to GHCR by the
  `.github/workflows/build-and-push.yml` GitHub Actions workflow (on push to
  `main` under `control-tower-agent/**`, or via **Run workflow**).
- The GHCR package is created **private** by default. Make it **public** once
  (repo → Packages → `cmk-control-tower` → Package settings → Change visibility)
  so the Container App can pull it without registry credentials.
- `foundry_project_endpoint` / `foundry_model` / `sql_server` / `sql_database`
  default to the current environment; override via `TF_VAR_*` if needed.
