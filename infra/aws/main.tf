locals {
  name = "agenteval-${var.environment}"
  services = {
    proxy     = { image = "proxy", port = 8000, cpu = 512, memory = 1024, command = "uvicorn main:app --host 0.0.0.0 --port 8000 --no-access-log" }
    ingest    = { image = "proxy", port = 0, cpu = 256, memory = 512, command = "python ingest_worker.py" }
    eval      = { image = "eval", port = 0, cpu = 256, memory = 512, command = "python eval_engine.py" }
    dashboard = { image = "dashboard", port = 8501, cpu = 512, memory = 1024, command = "streamlit run app.py --server.port=8501 --server.address=0.0.0.0 --server.headless=true" }
    monitor   = { image = "dashboard", port = 0, cpu = 256, memory = 512, command = "python monitor.py" }
  }
  health_commands = {
    proxy     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3)\""]
    dashboard = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3)\""]
    ingest    = ["CMD", "python", "ingest_worker.py", "--health"]
    eval      = ["CMD", "python", "eval_engine.py", "--health"]
    monitor   = ["CMD", "python", "monitor.py", "--health"]
  }
  secret_keys = {
    proxy     = ["PROXY_API_KEY", "UPSTREAM_API_KEY", "RDS_CA_PEM"]
    ingest    = ["DATABASE_URL", "RDS_CA_PEM"]
    eval      = ["DATABASE_URL", "JUDGE_API_KEY", "RDS_CA_PEM"]
    dashboard = ["DATABASE_URL", "DASHBOARD_PASSWORD", "RDS_CA_PEM"]
    monitor   = ["DATABASE_URL", "RDS_CA_PEM"]
    migrate   = ["INGEST_DB_PASSWORD", "EVAL_DB_PASSWORD", "DASHBOARD_DB_PASSWORD", "MONITOR_DB_PASSWORD", "RDS_CA_PEM"]
  }
  environment = {
    TRACE_QUEUE_BACKEND   = "sqs", TRACE_SQS_QUEUE_URL = aws_sqs_queue.traces.url,
    TRACE_INGEST_EMBEDDED = "false", TRACE_CONTENT_MODE = var.trace_content_mode,
    ALLOW_INSECURE_LOCAL  = "false", UPSTREAM_BASE = var.upstream_base,
    PGSSLMODE             = "verify-full", PGSSLROOTCERT = "/tmp/rds-ca.pem",
    AWS_REGION            = var.region, STREAMLIT_BROWSER_GATHER_USAGE_STATS = "false"
  }
}
resource "aws_ecr_repository" "images" {
  for_each             = toset(["proxy", "eval", "dashboard", "migrate"])
  name                 = "${local.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_cloudwatch_log_group" "services" {
  for_each          = toset(concat(keys(local.services), ["migrate"]))
  name              = "/ecs/${local.name}/${each.key}"
  retention_in_days = var.log_retention_days
}
resource "aws_sqs_queue" "dead" {
  name                      = "${local.name}-trace-dead"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}
resource "aws_sqs_queue" "traces" {
  name                       = "${local.name}-traces"
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 120
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true
  redrive_policy             = jsonencode({ deadLetterTargetArn = aws_sqs_queue.dead.arn, maxReceiveCount = 5 })
}
resource "aws_sqs_queue_policy" "tls" {
  for_each  = { traces = aws_sqs_queue.traces, dead = aws_sqs_queue.dead }
  queue_url = each.value.url
  policy    = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Deny", Principal = "*", Action = "sqs:*", Resource = each.value.arn, Condition = { Bool = { "aws:SecureTransport" = "false" } } }] })
}
resource "aws_security_group" "alb" {
  name_prefix = "${local.name}-alb-"
  vpc_id      = var.vpc_id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.private_client_cidrs
  }

}

resource "aws_security_group" "tasks" {
  name_prefix = "${local.name}-tasks-"
  vpc_id      = var.vpc_id
  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    from_port       = 8501
    to_port         = 8501
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  # Route tables must constrain egress through approved NAT/proxy or VPC endpoints.
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.database.id]
  }
}

resource "aws_vpc_security_group_egress_rule" "alb_services" {
  for_each                     = toset(["8000", "8501"])
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.tasks.id
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.key)
  to_port                      = tonumber(each.key)
}
resource "aws_security_group" "database" {
  name_prefix = "${local.name}-db-"
  vpc_id      = var.vpc_id
}
resource "aws_vpc_security_group_ingress_rule" "database" {
  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = aws_security_group.tasks.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}
resource "aws_db_subnet_group" "database" {
  name       = local.name
  subnet_ids = var.private_subnet_ids
}
resource "aws_db_parameter_group" "database" {
  name_prefix = "${local.name}-"
  family      = "postgres16"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
}

resource "aws_db_instance" "database" {
  identifier                      = local.name
  engine                          = "postgres"
  engine_version                  = "16"
  instance_class                  = "db.t4g.small"
  allocated_storage               = 20
  max_allocated_storage           = 100
  storage_type                    = "gp3"
  storage_encrypted               = true
  db_name                         = "agenteval"
  username                        = "agenteval_admin"
  manage_master_user_password     = true
  db_subnet_group_name            = aws_db_subnet_group.database.name
  vpc_security_group_ids          = [aws_security_group.database.id]
  parameter_group_name            = aws_db_parameter_group.database.name
  publicly_accessible             = false
  multi_az                        = var.environment == "production"
  backup_retention_period         = var.environment == "production" ? 35 : 7
  deletion_protection             = true
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${local.name}-final"
  auto_minor_version_upgrade      = true
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  copy_tags_to_snapshot           = true
}
resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_iam_role" "execution" {
  for_each           = var.service_secret_arns
  name               = "${local.name}-${each.key}-execution"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role_policy_attachment" "execution" {
  for_each   = aws_iam_role.execution
  role       = each.value.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy" "secrets" {
  for_each = aws_iam_role.execution
  role     = each.value.id
  policy   = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = each.key == "migrate" ? [var.service_secret_arns[each.key], aws_db_instance.database.master_user_secret[0].secret_arn] : [var.service_secret_arns[each.key]] }] })
}
resource "aws_iam_role" "task" {
  for_each           = local.services
  name               = "${local.name}-${each.key}-task"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role_policy" "producer" {
  role   = aws_iam_role.task["proxy"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["sqs:SendMessage", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.traces.arn }] })
}
resource "aws_iam_role_policy" "consumer" {
  role   = aws_iam_role.task["ingest"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.traces.arn }] })
}
resource "aws_lb" "main" {
  name                       = local.name
  internal                   = true
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = var.private_subnet_ids
  drop_invalid_header_fields = true
}
resource "aws_lb_target_group" "services" {
  for_each             = { for key, service in local.services : key => service if service.port != 0 }
  name                 = "${local.name}-${each.key}"
  port                 = each.value.port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30
  health_check {
    path     = each.key == "proxy" ? "/ready" : "/_stcore/health"
    matcher  = "200"
    interval = 15
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  certificate_arn   = var.certificate_arn
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "Not found"
      status_code  = "404"
    }
  }

}

resource "aws_lb_listener_rule" "proxy" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 10
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.services["proxy"].arn
  }

  condition {
    path_pattern {
      values = ["/v1/*", "/health", "/ready", "/metrics"]
    }
  }

}

resource "aws_lb_listener_rule" "dashboard" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 20
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.services["dashboard"].arn
  }

  condition {
    path_pattern {
      values = ["/*"]
    }
  }

}

resource "aws_ecs_task_definition" "services" {
  for_each                 = local.services
  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution[each.key].arn
  task_role_arn            = aws_iam_role.task[each.key].arn
  container_definitions = jsonencode([{
    name             = each.key, image = var.image_digests[each.value.image], essential = true,
    user             = "10001:10001", readonlyRootFilesystem = true,
    linuxParameters  = { capabilities = { drop = ["ALL"] } },
    command          = ["sh", "-ec", "umask 077; printf '%s\\n' \"$RDS_CA_PEM\" > /tmp/rds-ca.pem; exec ${each.value.command}"],
    environment      = [for name, value in local.environment : { name = name, value = value }],
    secrets          = [for key in local.secret_keys[each.key] : { name = key, valueFrom = "${var.service_secret_arns[each.key]}:${key}::" }],
    mountPoints      = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }],
    portMappings     = each.value.port == 0 ? [] : [{ containerPort = each.value.port, protocol = "tcp" }],
    logConfiguration = { logDriver = "awslogs", options = { "awslogs-group" = aws_cloudwatch_log_group.services[each.key].name, "awslogs-region" = var.region, "awslogs-stream-prefix" = "ecs" } },
    healthCheck = {
      command  = local.health_commands[each.key]
      interval = 30, timeout = 10, retries = 3, startPeriod = 60
    },
    stopTimeout = 30
  }])
  volume {
    name = "tmp"
  }
}

resource "aws_ecs_service" "services" {
  for_each                           = local.services
  name                               = each.key
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.services[each.key].arn
  desired_count                      = var.start_services ? (var.environment == "production" && contains(["proxy", "ingest", "eval"], each.key) ? 2 : 1) : 0
  launch_type                        = "FARGATE"
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = each.value.port == 0 ? [] : [each.value.port]
    content {
      target_group_arn = aws_lb_target_group.services[each.key].arn
      container_name   = each.key
      container_port   = load_balancer.value
    }
  }

  depends_on = [aws_lb_listener_rule.proxy, aws_lb_listener_rule.dashboard]
}
resource "aws_cloudwatch_metric_alarm" "queue_age" {
  alarm_name          = "${local.name}-trace-age"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.traces.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 120
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = [var.alert_topic_arn]
}
resource "aws_cloudwatch_metric_alarm" "dead" {
  alarm_name          = "${local.name}-trace-dead"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dead.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = [var.alert_topic_arn]
}
resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Project$agenteval"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution["migrate"].arn
  container_definitions = jsonencode([{
    name                   = "migrate", image = var.image_digests["migrate"], essential = true,
    readonlyRootFilesystem = true,
    command                = ["sh", "-ec", "umask 077; printf '%s\\n' \"$RDS_CA_PEM\" > /tmp/rds-ca.pem; psql -v ON_ERROR_STOP=1 -q -f /sql/init.sql && psql -v ON_ERROR_STOP=1 -q -f /sql/runtime_roles.sql"],
    environment            = [for name, value in merge(local.environment, { PGHOST = aws_db_instance.database.address, PGUSER = "agenteval_admin", PGDATABASE = "agenteval" }) : { name = name, value = value }],
    secrets                = concat([for key in local.secret_keys["migrate"] : { name = key, valueFrom = "${var.service_secret_arns["migrate"]}:${key}::" }], [{ name = "PGPASSWORD", valueFrom = "${aws_db_instance.database.master_user_secret[0].secret_arn}:password::" }]),
    mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }],
    logConfiguration       = { logDriver = "awslogs", options = { "awslogs-group" = aws_cloudwatch_log_group.services["migrate"].name, "awslogs-region" = var.region, "awslogs-stream-prefix" = "ecs" } }
  }])
  volume {
    name = "tmp"
  }
}

resource "aws_route53_record" "application" {
  zone_id = var.private_hosted_zone_id
  name    = var.private_dns_name
  type    = "A"
  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
