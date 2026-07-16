---
name: deployment-aws
description: Deploy a workload to AWS as a governed mission — EKS or ECS/Fargate (or EC2) behind an ALB, image from ECR, DNS in Route 53, Terraform state in S3+DynamoDB. Use when the target stack is AWS and you need the exact plan→approval→apply→configure→verify runbook, mapped to the infra operator, with real aws/terraform/kubectl commands and the saga rollback.
license: Apache-2.0
version: 0.1.0
---

# deployment-aws

The provider runbook for deploying to **AWS** through the `deploy_app` mission. It maps each mission
step to an infra-operator capability over Terraform + Ansible, names the real AWS resources, and
gives the exact commands. It is written to be followed **literally** by a non-frontier driver
(KIMI/Qwen) — no tool-calling or JSON-mode required; emit each command verbatim and **stop at the
approval gate**.

## When to use

The deploy spec's target stack is AWS: EKS (managed k8s) or ECS/Fargate (serverless containers) or
EC2 (VMs), image in **ECR**, ingress via an **ALB**, DNS in **Route 53**. If the stack is unset or
looks wrong, run `deployment-stack-selection` first — this skill executes the *how*, not the *where*.

## Prerequisites (deterministic checklist — all must exist before step 1)

1. **Credentials** — an IAM role/user assumable by the runner (`aws sts get-caller-identity` returns
   an ARN). Prefer a short-lived role over long-lived keys. Least-privilege scopes in `REFERENCE.md` §E.
2. **Region** decided and exported (`AWS_REGION`, e.g. `us-east-1`). Compliance/residency is a human gate.
3. **Terraform state backend** — an **S3 bucket** (versioned, encrypted) + a **DynamoDB table** for
   state locking, already created. Backend block + bootstrap in `REFERENCE.md` §D.
4. **ECR repository** for the app image (`aws ecr describe-repositories --repository-names <app>`), or
   plan to create it in the same Terraform.
5. **Terraform layout** present at `deploy/terraform/envs/aws/` and the Ansible tree at
   `deploy/ansible/` (this is the path `infra.plan/provision/configure` drive).
6. **kubectl / helm** on PATH for an EKS target; the app image built and pushed (or buildable) to ECR.

## The deployment procedure (a gated mission)

Run in this exact order. Each step is one infra-operator capability.

1. **Preflight / stack-selection sanity.** Confirm the target is AWS and the tier fits (EKS vs
   ECS/Fargate vs EC2 — see `REFERENCE.md` §A). Confirm region, ECR repo, and that the app image is
   scanned (the mission's `image_scanned` step — Trivy/SBOM). Do not proceed on unknowns.
2. **`infra.plan` → `terraform plan`.** Read-only. **The diff is the evidence.**
   ```bash
   aws sts get-caller-identity
   terraform -chdir=deploy/terraform/envs/aws init -input=false
   terraform -chdir=deploy/terraform/envs/aws plan -input=false -no-color \
     -var region=us-east-1 -var app=<app> -out=aws.plan
   ```
   Attach the plan output to the mission as `infra_planned`.
3. **APPROVAL GATE (MANDATORY).** `infra.provision` is `approval_required=True` — the
   highest-consequence capability in the fleet. **STOP.** Present the plan diff to a human and wait
   for explicit sign-off in the cockpit inbox. **Never auto-approve.** A rejection here unwinds the
   mission via the saga (nothing was applied yet, so this is a clean stop).
4. **`infra.provision` → `terraform apply`** (only after approval). Provisions the AWS resources:
   `aws_eks_cluster` + `aws_eks_node_group` (or `aws_ecs_cluster`/`aws_ecs_service`/
   `aws_ecs_task_definition`, or `aws_instance`), `aws_ecr_repository`, `aws_lb` (ALB) +
   `aws_lb_target_group`/`aws_lb_listener`, `aws_route53_record`.
   ```bash
   terraform -chdir=deploy/terraform/envs/aws apply -input=false -no-color aws.plan
   ```
   Undo = `infra.destroy_delta` (`terraform destroy`).
5. **`infra.configure` → `ansible-playbook`.** Pull the image from ECR, render manifests/compose,
   seed + migrate, roll out.
   ```bash
   aws ecr get-login-password --region us-east-1 \
     | docker login --username AWS --password-stdin <acct_id>.dkr.ecr.us-east-1.amazonaws.com
   aws eks update-kubeconfig --name <cluster> --region us-east-1   # EKS target
   ansible-playbook deploy/ansible/playbooks/deploy-app.yml -i deploy/ansible/inventory/aws.ini \
     -e app=<app> -e image=<acct_id>.dkr.ecr.us-east-1.amazonaws.com/<app>:<tag>
   ```
   Undo = `infra.rollback_release` (redeploy the prior release).
6. **`infra.verify` → health + smoke.** Probe `/health` and `/capabilities` on the ALB DNS name
   (or the service host), plus one smoke request.
   ```bash
   curl -fsS http://<alb-dns-name>/health
   curl -fsS http://<alb-dns-name>/capabilities
   ```
   **A verify failure (or the gate rejection above) unwinds via the saga:** `infra.rollback_release`
   rolls back the Ansible release, then `infra.destroy_delta` tears down the just-applied Terraform
   delta — you never leave a half-applied stack.

## Provider-specific notes

- **Registry login token expires (~12 h).** Re-run `aws ecr get-login-password | docker login …`
  before a fresh push/pull.
- **EKS auth is IAM-mapped.** `aws eks update-kubeconfig` writes an exec-based kubeconfig; the caller
  must be in the cluster's `aws-auth`/access-entry list or `kubectl` returns `Unauthorized`.
- **ALB needs the AWS Load Balancer Controller** on EKS (Ingress → ALB). ECS attaches the service to
  a target group directly. Public exposure is a security-posture decision (`deployment-security`).
- **Egress + cross-AZ cost.** Internet egress ~$0.09/GB; NAT Gateway data $0.045/GB; cross-AZ
  $0.01/GB each way. Keep chatty pods and their data in one AZ where the SLO allows.
- **Fargate vs node group.** Fargate = no nodes to size (per-task pricing); managed node group =
  cheaper at steady high utilization. See `REFERENCE.md` §B.

## KIMI-reliability note (read this if you are the driving model)

- **Follow the steps in order, literally.** Do not reorder, merge, or skip.
- **Emit every command verbatim**, one per action, and wait for its result before the next.
- **STOP at step 3 (the approval gate).** Do **not** run `terraform apply` / `infra.provision`
  until a human has approved. You have no authority to auto-approve `infra.provision`.
- If a step fails, **do not improvise** — report the failure and let the saga compensation
  (`infra.rollback_release` → `infra.destroy_delta`) run. Do not invent flags or resource names;
  if an exact detail is unknown, say so rather than guess.

## See also

- `REFERENCE.md` — EKS/ECS/EC2 tiers, instance/GPU sizing, egress break-evens, the full Terraform
  resource + provider reference, S3+DynamoDB backend bootstrap, least-privilege IAM, region caveats.
- Companion skills: `deployment-stack-selection` (choose the tier), `deployment-security` (exposure +
  secrets), `deployment-observability` (wire monitoring), `deployment-audit` (audit what's running).
- Mission: `deploy_app` (this runbook is its AWS arm). Operator: `apps/infra` (plan · provision ·
  configure · verify · destroy_delta · rollback_release).
