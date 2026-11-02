# Choosing a deployment platform and budget

No cloud account is required for local development. No cloud resources were created by this implementation. Choose a provider only after the local failure/recovery tests pass and the expected traffic, access boundary and operating budget are known.

## Practical options

| Platform | Suitable mapping | Tradeoff for this project |
|---|---|---|
| AWS | ECS Fargate services, RDS PostgreSQL, SQS + DLQ, ECR, Secrets Manager, CloudWatch | The repository includes an optional Terraform starting point. It provides clear service/permission boundaries, but private connectivity, load balancing and an always-on database have fixed costs. |
| Google Cloud | Cloud Run services/jobs or worker pools, Cloud SQL PostgreSQL, Pub/Sub, Secret Manager, Artifact Registry, Cloud Monitoring | Request services can scale down. Continuous consumers need an appropriate always-running worker configuration or push delivery; do not deploy the polling workers as request-only services that suspend CPU. The implemented SQS adapter would need a tested Pub/Sub adapter. |
| Microsoft Azure | Container Apps, Azure Database for PostgreSQL, Service Bus, Key Vault, Container Registry, Azure Monitor | Similar separation is possible. Configure long-running worker replicas and managed identity. A tested Service Bus adapter would be needed; the repository does not currently implement one. |
| Render or Railway | Separate Docker web/worker services, managed Postgres, persistent disk for a single-host spool | Often simpler for an initial portfolio staging demonstration. Verify private networking, backups, disk durability, worker uptime and sleep policies. A local SQLite spool cannot be shared safely between independently scheduled hosts; colocate it with its consumer or implement a supported durable broker adapter. |
| One small VM | Docker Compose on one host plus managed Postgres or a private local database | Lower architectural complexity, but the operator owns patching, TLS, disks, backup, monitoring and host failure. A single disk is not replicated message durability. |

These are deployment choices, not interchangeable queue APIs. The application supports a persistent single-host SQLite queue and an AWS SQS adapter. GCP/Azure broker adapters must be implemented and tested before claiming those managed queues work.

## Initial budget recommendation

Use local synthetic mode while finishing the engineering work: no model-provider spend and no cloud infrastructure charge. Your computer/cloud development environment itself may have a separate cost.

For a small **temporary demonstration**, set an initial planning allowance of **US$50–100/month** on a simple platform where its current limits support this topology. For an **always-on managed AWS staging topology**, reserve **US$150–250/month as an initial planning range**. These are rough allowances, not verified quotes, promises, spending caps, or estimates for production capacity. The included private AWS topology can exceed that range once its required networking, database, logs, data transfer, availability and access services are included.

Before deployment, enter the actual region, service sizes, replica counts, operating hours, storage, backups, NAT/VPC endpoints, load balancer and expected traffic into the chosen provider's calculator:

- [AWS Pricing Calculator](https://calculator.aws/)
- [Google Cloud Pricing Calculator](https://cloud.google.com/products/calculator)
- [Azure Pricing Calculator](https://azure.microsoft.com/pricing/calculator/)

Model and judge costs are additional: `requests × evaluated fraction × judge tokens × current model prices`. Use measured token counts, verify the selected price/date and include retries. Mock tokens/scores are synthetic and must not be used as a paid-model cost forecast.

Set a budget notification well below the approved monthly amount, configure a model-provider spending limit if offered, and review actual bills daily during staging. AWS Budgets alerts are delayed notifications; they do not stop resources or guarantee a hard cap. Shut down temporary services when finished using the provider's reviewed teardown procedure, retaining only backups you intentionally need.

## Recommendation for the current project

Finish and demonstrate the local system first. Then choose a single-organization staging environment using synthetic data. A simpler Docker platform is reasonable if it supports the separate long-running workers and durable storage. AWS is a useful infrastructure portfolio path when you are willing to set up an account, private access, identity and the larger fixed-cost baseline. Do not deploy a public service simply to obtain an easy demonstration link.
