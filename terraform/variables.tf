variable "subscription_id" {
  type      = string
  sensitive = true
}

variable "location" {
  type    = string
  default = "westus"
}

variable "gh_repo" {
  type    = string
}

variable "image_tag" {
  type    = string
  default = "latest"
}

# --- Existing resources the Container App is deployed into ------------------

variable "resource_group_name" {
  type    = string
}

variable "vnet_name" {
  type    = string
}

# New subnet created in the existing vnet for the Container App environment.
# Must be delegated to Microsoft.App/environments (done below) and unused.
variable "containerapp_subnet_name" {
  type    = string
  default = "containerapp-subnet"
}

variable "containerapp_subnet_prefix" {
  type    = string
  default = "10.0.10.0/23"
}

# --- Application configuration (surfaced as container env vars) -------------

variable "foundry_project_endpoint" {
  type    = string
  
}

variable "foundry_model" {
  type    = string
  default = "gpt-5.4-mini"
}

# Cognitive Services (AI Foundry) account name, used to grant the app identity
# read access to the Foundry project.
variable "foundry_account_name" {
  type    = string
}

variable "sql_server" {
  type    = string
}

variable "sql_database" {
  type    = string
}
