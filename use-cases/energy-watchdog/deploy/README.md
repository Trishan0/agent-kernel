# AWS serverless deployment

Follows `examples/aws-serverless/schedule-openai/deploy`. **Scaffold only — not run against a
live AWS account.** The local build (`app.py`) is the verified path; this directory is the
deployment target the SPEC calls for.

## Topology

`queue_mode = true` (required by scheduling). Three Lambda functions behind one API Gateway:

| Function | Entry | Package | Job |
| --- | --- | --- | --- |
| request handler | `lambda.handler` | zip | chat ingress, `/telegram/webhook`, schedule management routes |
| agent runner | `lambda_agent_runner.handler` | **image** (ECR) | consumes the input queue, runs the four agents, fires sweep/chase occurrences, hosts `create_schedule` |
| response handler | `lambda_response_handler.handler` | zip | writes completed responses to the response store |

Backends (`deploy/config.yaml`, coordinates injected by Terraform):

- **session** → DynamoDB (`create_dynamodb_memory_table`)
- **schedule provider** → EventBridge Scheduler (`enable_scheduling`)
- **schedule store** → DynamoDB, partition key `task_id` (`create_dynamodb_schedule_table`)
- **response store** → DynamoDB (`create_dynamodb_response_store`)
- **site state store** (baselines, dismissals, cases) → DynamoDB `aws_dynamodb_table.site_state`,
  **partition key `site_id`**, selected in code by `AK_STATE__BACKEND=dynamodb`
  (`state_dynamodb.py`)
- **sandbox** → `local_subprocess` in the agent-runner Lambda's `/tmp` (no isolation; a real
  deployment would move this to a `docker`/`e2b`/`daytona` provider)

## Deploy

```bash
cd deploy
cp terraform.tfvars.example terraform.tfvars   # fill in vpc_id, subnets, secrets
./deploy.sh                                     # ./deploy.sh local  -> use ../../../ak-py/dist
```

`deploy.sh` builds the packages (Docker must be running for the agent-runner image), removes the
generated `requirements.txt`, then `terraform init && terraform apply`.

After apply, register the Telegram webhook against the `telegram_webhook_url` output, then
register the recurring sweeps by POSTing a `schedule` block to `<agent_invoke_url>/api/v1/chat`
for each site (as `scripts/register_sweeps.py` does locally, pointed at the deployed URL).

## What the operator must still supply

1. **IAM for the site state table.** The request-handler and agent-runner execution roles need
   `dynamodb:GetItem` / `PutItem` / `UpdateItem` on `aws_dynamodb_table.site_state`. The role
   identifiers the `ak-serverless` module exposes vary by version — attach
   `data.aws_iam_policy_document.site_state_rw` (already defined in `main.tf`) to them with an
   `aws_iam_role_policy` once you know the output names.
2. **VPC** with private subnets that have outbound internet (NAT) for the Gemini and Telegram
   APIs.
3. **The framework module version.** `main.tf` pins `yaalalabs/ak-serverless/aws` `0.8.1`; match
   it to your installed `agentkernel`.

## Not committed

`.terraform/`, `*.tfstate*`, `terraform.tfvars`, generated `requirements.txt`, `dist_*` — see
`deploy/.gitignore` and the project `.gitignore`.
