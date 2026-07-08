locals {
  func_name      = "cmkct${random_string.unique.result}"
  loc_for_naming = lower(replace(var.location, " ", ""))
  gh_repo        = split("/", var.gh_repo)[1]

  image = "ghcr.io/${var.gh_repo}:${var.image_tag}"

  tags = {
    "managed_by" = "terraform"
    "repo"       = local.gh_repo
  }
}
