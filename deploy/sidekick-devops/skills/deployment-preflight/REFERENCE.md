# deployment-preflight — REFERENCE

## A. Cross-platform install (what the user actually installs)

**Required everywhere: a container runtime.** Everything else runs inside the operator container.

| Tool | macOS | Windows | Linux |
|---|---|---|---|
| **Docker** *(required)* | `brew install --cask docker` · or OrbStack/Colima | `winget install Docker.DockerDesktop` (WSL2) | `curl -fsSL https://get.docker.com \| sh` · or Podman |
| aws CLI *(optional)* | `brew install awscli` | `winget install Amazon.AWSCLI` | `curl … awscli-exe-linux…` |
| gcloud *(optional)* | `brew install --cask google-cloud-sdk` | `winget install Google.CloudSDK` | `curl https://sdk.cloud.google.com \| bash` |
| az *(optional)* | `brew install azure-cli` | `winget install Microsoft.AzureCLI` | `curl -sL https://aka.ms/InstallAzureCLIDeb \| sudo bash` |
| doctl *(optional)* | `brew install doctl` | `winget install DigitalOcean.Doctl` | `snap install doctl` |

"Optional" = only if the user wants to run commands by hand; the mission never requires them locally.

## B. Executable binding

The skill's checklist is executed by a deterministic checker, not by the model guessing:
- **AWS reference implementation:** `redevops-aws-demo` → `aws_demo/preflight.py` (`check_local`,
  `check_aws`, `render`) + `python -m aws_demo.doctor`. Returns the `{ready, checks[], blockers[]}`
  contract. It runs in **either** credential mode — Vault STS assume-role, or the ambient CLI profile
  (so solo users need no Vault).
- Other clouds implement the same contract with the CLI/probes from §C; the SKILL body is identical.
- Expose the checker as an MCP tool (`preflight_check(cloud)`) so any Sidekick agent can call it; the
  skill is the *procedure*, the tool is the *execution*.

## C. Per-role permission probes (least-privilege, read-only calls)

| Role | AWS | GCP | Azure | DigitalOcean |
|---|---|---|---|---|
| **deploy** *(blocker)* | `eks:ListClusters` / `ec2:Describe*` | `container.clusters.list` | `Microsoft.ContainerService/*/read` | `kubernetes cluster list` |
| **readonly** *(warn)* | `ce:GetCostAndUsage`, `cloudwatch:List*` | `billing`, `monitoring.read` | Cost Mgmt reader | `balance get`, `monitoring` |
| **agent** *(warn)* | `bedrock:ListFoundationModels` | Vertex AI user | Azure OpenAI reader | n/a (external model plane) |

## D. The Bedrock-style account gate (AWS-specific but generalizes)

IAM + model access can both be green while the **account** still blocks model invocation on new /
under-review accounts (`"access ... restricted to ensure compliance with AWS Customer Agreement"`).
The checker classifies this as a **warning**, not a blocker, and tells the user to open a Support case
+ confirm a payment method — the deploy proceeds on the existing model plane meanwhile. GCP/Azure have
analogous "enable the API / accept terms" gates; treat them the same way.

## E. Mission integration

Preflight is node 0 of the deploy mission (see SKILL.md). On `BLOCKED`, park a HumanTask carrying the
checklist; do not compile `terraform plan`. The demo wires this in `missions/deploy_operate.py`.
