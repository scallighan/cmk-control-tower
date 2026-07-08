terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "=4.67.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "=3.1.0"
    }
  }
}

provider "azurerm" {
  features {
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
  }

  resource_provider_registrations = "none"

  subscription_id = var.subscription_id
}

resource "random_string" "unique" {
  length  = 8
  special = false
  upper   = false
}

data "azurerm_client_config" "current" {}

# --- Existing resources ----------------------------------------------------

data "azurerm_resource_group" "this" {
  name = var.resource_group_name
}

data "azurerm_virtual_network" "this" {
  name                = var.vnet_name
  resource_group_name = data.azurerm_resource_group.this.name
}

data "azurerm_cognitive_account" "foundry" {
  name                = var.foundry_account_name
  resource_group_name = data.azurerm_resource_group.this.name
}

# --- New subnet for the Container App environment --------------------------
# Created inside the existing vnet and delegated to Microsoft.App/environments.

resource "azurerm_subnet" "containerapp" {
  name                 = var.containerapp_subnet_name
  resource_group_name  = data.azurerm_resource_group.this.name
  virtual_network_name = data.azurerm_virtual_network.this.name
  address_prefixes     = [var.containerapp_subnet_prefix]

  delegation {
    name = "Microsoft.App/environments"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

# --- Observability ---------------------------------------------------------

resource "azurerm_log_analytics_workspace" "this" {
  name                = "log-${local.func_name}"
  location            = data.azurerm_resource_group.this.location
  resource_group_name = data.azurerm_resource_group.this.name
  sku                 = "PerGB2018"
  retention_in_days   = 30

  tags = local.tags
}

# --- Application managed identity ------------------------------------------

resource "azurerm_user_assigned_identity" "app" {
  name                = "uai-${local.func_name}"
  resource_group_name = data.azurerm_resource_group.this.name
  location            = data.azurerm_resource_group.this.location

  tags = local.tags
}

# Read access to the AI Foundry project (chat model).
resource "azurerm_role_assignment" "app_foundry" {
  scope                = data.azurerm_cognitive_account.foundry.id
  role_definition_name = "Cognitive Services User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# --- Container App environment + app ---------------------------------------

resource "azurerm_container_app_environment" "this" {
  name                       = "ace-${local.func_name}"
  location                   = data.azurerm_resource_group.this.location
  resource_group_name        = data.azurerm_resource_group.this.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  infrastructure_subnet_id = azurerm_subnet.containerapp.id

  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }

  tags = local.tags

  lifecycle {
    ignore_changes = [infrastructure_resource_group_name]
  }
}

resource "azurerm_container_app" "this" {
  name                         = "aca-${local.func_name}"
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = data.azurerm_resource_group.this.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  template {
    container {
      name   = "control-tower"
      image  = local.image
      cpu    = 1.0
      memory = "2Gi"

      env {
        name  = "FOUNDRY_PROJECT_ENDPOINT"
        value = var.foundry_project_endpoint
      }
      env {
        name  = "FOUNDRY_MODEL"
        value = var.foundry_model
      }
      env {
        name  = "SQL_SERVER"
        value = var.sql_server
      }
      env {
        name  = "SQL_DATABASE"
        value = var.sql_database
      }
      # Bind DefaultAzureCredential to the user-assigned identity.
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.app.client_id
      }
    }

    http_scale_rule {
      name                = "http-1"
      concurrent_requests = "100"
    }

    min_replicas = 1
    max_replicas = 1
  }

  ingress {
    allow_insecure_connections = false
    external_enabled           = true
    target_port                = 8000
    transport                  = "auto"
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  tags = local.tags
}

# --- Outputs ---------------------------------------------------------------

output "app_url" {
  value = "https://${azurerm_container_app.this.ingress[0].fqdn}"
}

output "identity_client_id" {
  value = azurerm_user_assigned_identity.app.client_id
}

output "identity_principal_id" {
  value = azurerm_user_assigned_identity.app.principal_id
}
