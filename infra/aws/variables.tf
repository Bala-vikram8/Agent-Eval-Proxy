variable "region" { type = string }
variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the approved 12-digit AWS account ID."
  }
}
variable "environment" {
  type = string
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "Use separate staging or production state and accounts."
  }
}
variable "vpc_id" { type = string }
variable "private_subnet_ids" {
  type = list(string)
  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "Provide private subnets in at least two availability zones."
  }
}
variable "private_client_cidrs" {
  type        = list(string)
  description = "Private VPN/corporate CIDRs allowed to reach the internal HTTPS load balancer."
  validation {
    condition = length(var.private_client_cidrs) > 0 && alltrue([for cidr in var.private_client_cidrs :
      can(cidrnetmask(cidr)) && (
        (can(regex("^10\\.", cidrhost(cidr, 0))) && try(tonumber(split("/", cidr)[1]), 0) >= 8) ||
        (can(regex("^192\\.168\\.", cidrhost(cidr, 0))) && try(tonumber(split("/", cidr)[1]), 0) >= 16) ||
        (can(regex("^172\\.(1[6-9]|2[0-9]|3[01])\\.", cidrhost(cidr, 0))) && try(tonumber(split("/", cidr)[1]), 0) >= 12)
      )
    ])
    error_message = "Supply explicit private IPv4 client CIDRs; open Internet access is forbidden."
  }
}
variable "certificate_arn" {
  type        = string
  description = "An existing ACM certificate for the private DNS names used by approved clients."
}
variable "image_digests" {
  type        = map(string)
  description = "Approved ECR image URIs pinned with @sha256 for proxy, eval, dashboard and migrate."
  validation {
    condition     = alltrue([for key in ["proxy", "eval", "dashboard", "migrate"] : can(regex("@sha256:[a-f0-9]{64}$", lookup(var.image_digests, key, "")))])
    error_message = "Every required image must be pinned to an immutable digest."
  }
}
variable "service_secret_arns" {
  type        = map(string)
  description = "Existing distinct Secrets Manager JSON secrets for proxy, ingest, eval, dashboard, monitor, migrate. Supply values securely outside Terraform."
  validation {
    condition     = alltrue([for key in ["proxy", "ingest", "eval", "dashboard", "monitor", "migrate"] : can(regex("^arn:aws:secretsmanager:", lookup(var.service_secret_arns, key, "")))]) && length(distinct(values(var.service_secret_arns))) == length(values(var.service_secret_arns))
    error_message = "Supply distinct pre-populated secrets for each service."
  }
}
variable "alert_topic_arn" {
  type        = string
  description = "Existing SNS topic with confirmed operations subscriptions."
}
variable "budget_email" { type = string }
variable "monthly_budget_usd" {
  type = number
  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "An explicit positive monthly alert budget is required."
  }
}
variable "trace_content_mode" {
  type    = string
  default = "metadata"
  validation {
    condition     = contains(["metadata", "redacted"], var.trace_content_mode)
    error_message = "Cloud trace storage supports metadata or redacted; never raw."
  }
}
variable "upstream_base" {
  type    = string
  default = "https://api.openai.com"
  validation {
    condition     = startswith(var.upstream_base, "https://")
    error_message = "Provider requests require HTTPS."
  }
}
variable "log_retention_days" {
  type    = number
  default = 30
}
variable "start_services" {
  type        = bool
  default     = false
  description = "Keep tasks stopped until schema migration succeeds; explicit enable after approved setup."
}

variable "private_hosted_zone_id" {
  type        = string
  description = "Existing private Route 53 zone associated with the approved VPC."
}
variable "private_dns_name" {
  type        = string
  description = "Private application DNS name covered by the supplied ACM certificate."
}
