# infra — deployment as a governed mission (Terraform + Ansible)

The `infra` operator turns a deploy into a Mission Runtime mission (`deploy_app` template):

    edge-sentinel.supply_chain_scan → infra.plan → [approval] infra.provision → infra.configure → infra.verify

- **infra.plan / provision / destroy_delta** → `terraform -chdir=deploy/terraform/envs/<cloud> plan|apply|destroy`
  (clouds: `aws`, `gcp`, `azure`, `digitalocean`). `provision` is approval-gated; `destroy_delta` is its saga undo.
- **infra.configure / rollback_release** → `ansible-playbook deploy/ansible/playbooks/{deploy-app,rollback}.yml`.
- **infra.verify** → `/health` + `/capabilities` smoke against the deployed host.

Credentials come from the environment (never HCL): AWS `AWS_*`, DigitalOcean `TF_VAR_do_token`
(from `DO_TOKEN`), GCP ADC + a service account with `roles/compute.admin`, Azure `ARM_*` (a service
principal — not yet exported here). `INFRA_DEPLOY_ROOT` overrides the deploy-tree location.
