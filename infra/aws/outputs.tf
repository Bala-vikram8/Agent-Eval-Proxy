output "cluster_name" { value = aws_ecs_cluster.main.name }
output "migration_task_definition" { value = aws_ecs_task_definition.migrate.arn }
output "private_load_balancer_dns" { value = aws_lb.main.dns_name }
output "database_endpoint" { value = aws_db_instance.database.address }
output "database_master_secret_arn" { value = aws_db_instance.database.master_user_secret[0].secret_arn }
output "trace_queue_url" { value = aws_sqs_queue.traces.url }
output "dead_letter_queue_url" { value = aws_sqs_queue.dead.url }
output "repository_urls" { value = { for key, repo in aws_ecr_repository.images : key => repo.repository_url } }
output "task_security_group_id" { value = aws_security_group.tasks.id }
output "private_application_url" { value = "https://${var.private_dns_name}" }
