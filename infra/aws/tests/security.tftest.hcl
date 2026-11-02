# Mock-provider plans cannot create resources or make AWS API calls.
mock_provider "aws" {}

override_resource {
  target = aws_db_instance.database
  values = {
    master_user_secret = [{
      secret_arn    = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-master"
      kms_key_id    = "test-key"
      secret_status = "active"
    }]
  }
}

variables {
  region                 = "us-east-1"
  account_id             = "123456789012"
  environment            = "staging"
  vpc_id                 = "vpc-0123456789abcdef0"
  private_subnet_ids     = ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"]
  private_client_cidrs   = ["10.20.0.0/16"]
  private_hosted_zone_id = "Z0123456789EXAMPLE"
  private_dns_name       = "agenteval.example.internal"
  certificate_arn        = "arn:aws:acm:us-east-1:123456789012:certificate/01234567-89ab-cdef-0123-456789abcdef"
  image_digests = {
    proxy     = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test/proxy@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    eval      = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test/eval@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    dashboard = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test/dashboard@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    migrate   = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test/migrate@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  service_secret_arns = {
    proxy     = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-proxy"
    ingest    = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-ingest"
    eval      = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-eval"
    dashboard = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-dashboard"
    monitor   = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-monitor"
    migrate   = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-migrate"
  }
  alert_topic_arn    = "arn:aws:sns:us-east-1:123456789012:synthetic-operations"
  budget_email       = "synthetic@example.invalid"
  monthly_budget_usd = 150
}

run "staging_is_private_and_stopped" {
  command = plan
  assert {
    condition     = aws_lb.main.internal && !aws_db_instance.database.publicly_accessible
    error_message = "Staging must not expose the load balancer or database publicly."
  }
  assert {
    condition     = alltrue([for service in aws_ecs_service.services : service.desired_count == 0])
    error_message = "Workloads must stay stopped until explicit migration approval."
  }
  assert {
    condition     = aws_db_instance.database.storage_encrypted && aws_db_instance.database.deletion_protection && aws_db_instance.database.backup_retention_period >= 7
    error_message = "Database storage, backup and deletion protections are required."
  }
  assert {
    condition     = aws_sqs_queue.traces.sqs_managed_sse_enabled && aws_sqs_queue.dead.sqs_managed_sse_enabled && aws_sqs_queue.traces.visibility_timeout_seconds >= 30
    error_message = "Queue encryption and recovery visibility are required."
  }
  assert {
    condition     = var.trace_content_mode == "metadata"
    error_message = "Content capture must remain opt-in."
  }
}

run "services_expose_real_health_and_private_metrics" {
  command = plan
  assert {
    condition     = strcontains(local.services.proxy.command, "--no-access-log")
    error_message = "Proxy must not log raw request-target query strings."
  }
  assert {
    condition     = alltrue([for worker in ["ingest", "eval", "monitor"] : contains(local.health_commands[worker], "--health")])
    error_message = "Background workers must have functional heartbeat checks."
  }
  assert {
    condition     = contains(one(one(aws_lb_listener_rule.proxy.condition).path_pattern).values, "/metrics")
    error_message = "Authenticated proxy metrics must not route to the dashboard."
  }
}

run "reject_open_client_network" {
  command = plan
  variables { private_client_cidrs = ["0.0.0.0/0"] }
  expect_failures = [var.private_client_cidrs]
}

run "reject_mutable_images" {
  command = plan
  variables {
    image_digests = { proxy = "example/proxy:latest" }
  }
  expect_failures = [var.image_digests]
}

run "reject_supernets_containing_public_addresses" {
  command = plan
  variables { private_client_cidrs = ["10.0.0.0/7", "192.168.0.0/15", "172.16.0.0/11"] }
  expect_failures = [var.private_client_cidrs]
}
