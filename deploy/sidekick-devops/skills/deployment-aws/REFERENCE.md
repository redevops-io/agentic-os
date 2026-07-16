# deployment-aws — REFERENCE

Level-3 detail for the `deployment-aws` skill. Loaded when the runbook needs exact tiers, resource
names, backend/IAM setup, or region caveats. Grounded in current (2024–2026) AWS + Terraform
primitives. Where an exact flag is version-specific, the resource/step is described rather than a
guessed flag.

## §A — Managed-vs-self-hosted tiers on AWS

| Tier | Service | Terraform resources | When it fits |
|---|---|---|---|
| Serverless containers | **ECS on Fargate** | `aws_ecs_cluster`, `aws_ecs_service` (launch_type `FARGATE`), `aws_ecs_task_definition` | Few services, spiky/low duty cycle, no node ops; per-task billing |
| Managed k8s | **EKS** (managed node group) | `aws_eks_cluster`, `aws_eks_node_group`, `aws_iam_openid_connect_provider` (IRSA) | ≥ ~10–20 services, k8s ecosystem, steady utilization |
| Managed k8s (nodeless) | **EKS on Fargate** | `aws_eks_fargate_profile` | k8s API but no nodes to size; higher per-pod cost |
| VMs | **EC2** | `aws_instance`, `aws_launch_template`, `aws_autoscaling_group` | Lift-and-shift, GPU/bespoke AMIs, full control |
| Ingress | **ALB** | `aws_lb` (type `application`), `aws_lb_target_group`, `aws_lb_listener` | HTTP(S) L7 routing; on EKS via AWS Load Balancer Controller |
| Registry | **ECR** | `aws_ecr_repository`, `aws_ecr_lifecycle_policy` | OCI images; per-region; login token ~12 h |
| DNS | **Route 53** | `aws_route53_zone`, `aws_route53_record` (alias to ALB) | Managed DNS + health checks |

Rule of thumb: **ECS/Fargate until node/k8s features are actually needed**; EKS earns its control-plane
cost (~$0.10/hr/cluster) at scale, multi-region, or regulated. GPU inference → EC2 GPU node group or
EKS GPU node group (see §B).

## §B — Instance / GPU sizing reference

| Need | Instance family | Notes |
|---|---|---|
| General web/API | `t3`/`t4g` (burstable), `m6i`/`m7g` (steady) | Graviton (`*g`) ~20% cheaper for ARM-clean images |
| CPU-bound | `c6i`/`c7g` | compute-optimized |
| Memory-bound | `r6i`/`r7g` | caches, in-memory DBs |
| GPU inference | `g5` (A10G), `g6` (L4) | cost-effective serving |
| GPU training | `p4d`/`p4de` (A100), `p5` (H100) | reserve/Capacity Blocks for large runs |

- **Fargate task sizing** is vCPU/memory pairs (0.25 vCPU/0.5 GB up to 16 vCPU/120 GB), not instances.
- **Reserved/Savings Plans** only after rightsizing (see `deployment-audit`); target ~70–80% of the
  stable baseline, not 100%. ~40% off on 1-yr, more on 3-yr all-upfront.
- **Spot** for interruptible batch/GPU (`aws_ec2_spot_*` / node group `capacity_type = "SPOT"`).

## §C — Cost / egress break-evens (AWS-specific)

- Internet egress: **$0.09/GB** (first ~10TB, tiers down above); first 100 GB/mo free.
- **NAT Gateway** data processing: **$0.045/GB** + hourly — a common hidden bill; use VPC endpoints
  for S3/ECR/DynamoDB to avoid NAT for those.
- **Cross-AZ** traffic: **$0.01/GB each way** — chatty microservices across AZs add up.
- ECR pull traffic to in-region compute is free over a VPC endpoint; cross-region replication bills.
- EKS control plane: ~$73/mo/cluster flat, independent of node count.

## §D — Terraform provider + S3/DynamoDB state backend

Provider: **`hashicorp/aws`** (`provider "aws" { region = var.region }`).

Backend (in `deploy/terraform/envs/aws/backend.tf`):
```hcl
terraform {
  backend "s3" {
    bucket         = "<org>-tfstate"
    key            = "envs/aws/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "<org>-tflock"   # state locking
    encrypt        = true
  }
}
```
Bootstrap once (chicken-and-egg — create the backend before using it):
```bash
aws s3api create-bucket --bucket <org>-tfstate --region us-east-1
aws s3api put-bucket-versioning --bucket <org>-tfstate \
  --versioning-configuration Status=Enabled
aws dynamodb create-table --table-name <org>-tflock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH --billing-mode PAY_PER_REQUEST
```
Note: Terraform ≥1.10 also supports S3-native lockfile locking (`use_lockfile = true`) as an
alternative to the DynamoDB table; the DynamoDB table remains the widely-deployed default.

## §E — Least-privilege IAM scopes

Grant only what the mission touches; prefer a dedicated deploy role assumed for the run.

- **Plan (read):** `ec2:Describe*`, `eks:Describe*`/`eks:List*`, `ecs:Describe*`/`ecs:List*`,
  `ecr:Describe*`, `elasticloadbalancing:Describe*`, `route53:List*`/`Get*`, plus S3 `Get/Put` on the
  state bucket and DynamoDB `Get/Put/Delete` on the lock table.
- **Provision (write):** the create/update/delete verbs for exactly the resource types in §A the env
  provisions (e.g. `eks:CreateCluster`, `ecs:CreateService`, `ecr:CreateRepository`,
  `elasticloadbalancing:Create*`, `route53:ChangeResourceRecordSets`), plus `iam:PassRole` scoped to
  the specific execution/node roles. Avoid `*:*`.
- **Workloads (pods):** use **IRSA** (`aws_iam_openid_connect_provider` + per-service-account roles),
  not node instance-profile creds, so pods get scoped, not broad, permissions.
- ECR push/pull: `ecr:GetAuthorizationToken` + `ecr:BatchGetImage`/`PutImage`/`Upload*` on the repo.

## §F — Compliance / region caveats

- **Residency/compliance is per-region, not per-provider** — pick the region first; it is a human
  gate. PHI → run only on **BAA-eligible** services under a signed AWS BAA (HIPAA imposes no region
  requirement itself). **FedRAMP High → AWS GovCloud (us-gov-*)**; commercial regions cap at Moderate.
- Some newer regions are opt-in and lack a given instance family or EKS add-on — check availability in
  the target region before planning GPU or a specific service.
- Public exposure (ALB internet-facing, public subnets, `0.0.0.0/0` security groups) is a
  security-posture gate — see `deployment-security`.

## §G — Sources

AWS docs: eks user guide (managed node groups, Fargate profiles, IRSA, ALB controller), ECS
developer guide (Fargate launch type, task definitions), ECR (auth token lifetime), ELB (ALB target
groups/listeners), Route 53 (alias records), VPC pricing (NAT/cross-AZ/egress), GovCloud + HIPAA BAA
eligibility. Terraform Registry: `registry.terraform.io/providers/hashicorp/aws` (resource
reference), Terraform S3 backend docs (DynamoDB locking, ≥1.10 lockfile). Verify $/GB and instance
prices against live AWS pricing before a binding cost claim.
