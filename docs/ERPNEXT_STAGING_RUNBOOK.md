# ERPNext staging validation runbook

ERPNext is the required generalization environment in the thesis proposal. This runbook deploys it to a dedicated private EC2 host, separate from Moodle. Terraform creates no ERPNext resources unless `enable_erpnext=true`; it does not create or apply AWS changes by itself.

## Deploy

1. Review the Terraform plan with the correct AWS account, backend and `terraform.tfvars`; the proposed host defaults to `t3.xlarge` with an encrypted 80 GiB root volume and incurs AWS charges when running. Set `enable_erpnext=true` only for a reviewed staging window.
2. Apply only after reviewing the plan. Export `terraform output -raw erpnext_ansible_inventory` to a protected temporary inventory file; it includes the monitor as an SSH jump host. Do not edit the shared real-address `ansible/inventory.ini`.
3. Bootstrap Docker/Compose on the host with the existing `ansible/playbooks/bootstrap.yml` using that inventory. Create `/opt/erpnext/secrets` as a root-only directory and provision `db_root_password` and `erpnext_admin_password` from an approved secret store (root-owned, mode 0600); never pass values on the command line or commit them.
4. Run `ansible-playbook -i <protected-inventory> ansible/playbooks/deploy-erpnext-staging.yml`. The play installs Node Exporter and ERPNext container metrics, installs bounded drill commands in `/usr/local/sbin`, starts dependencies, and creates the `erpnext-staging.local` site once. Verify `http://<private-ip>:8080/api/method/frappe.auth.get_logged_user` through the monitor host.
5. Add the ERPNext group to the protected combined Moodle/monitoring inventory, then rerun `ansible/playbooks/configure-moodle-monitoring.yml` to include the private Node Exporter scrape target and ERPNext alerts. The alert path currently observes and reports; it does not execute ERPNext remediation.
6. Provision a dedicated least-privilege Frappe API token to a protected token file on the monitor host. Check the authenticated, read-only transaction with `python automation/erpnext-synthetic-transaction.py --base-url http://<private-ip>:8080 --token-file <protected-token-file>`.

The Compose definition follows Frappe's production service layout and currently pins ERPNext `v16.51.0`, MariaDB `11.8`, and Redis `8.6-alpine`. Verify release compatibility and backup/restore before upgrading. Frappe documents the multi-service production layout and its DB/Redis configuration in the [official Compose file](https://github.com/frappe/frappe_docker/blob/main/compose.yaml), [MariaDB override](https://github.com/frappe/frappe_docker/blob/main/overrides/compose.mariadb.yaml), and [Redis override](https://github.com/frappe/frappe_docker/blob/main/overrides/compose.redis.yaml). API token authentication uses the `Authorization: token ...` header per [Frappe REST authentication docs](https://docs.frappe.io/framework/user/en/guides/integration/rest_api/token_based_authentication).

## Fault drills

Only use the scripts on the isolated host, against disposable staging data, after establishing a restorable baseline and collecting normal-state probes. They require the host marker installed by Ansible and `ERPNEXT_FAULT_CONFIRM=staging`.

```bash
ERPNEXT_FAULT_CONFIRM=staging /usr/local/sbin/erpnext-fault-inject.sh ERP-01 # stop MariaDB
ERPNEXT_FAULT_CONFIRM=staging /usr/local/sbin/erpnext-fault-reset.sh ERP-01  # restart MariaDB
```

Replace `ERP-01` with `ERP-02` to stop/restart Redis or `ERP-03` to point Nginx at a deliberately invalid upstream and restore the saved `.env` baseline. Capture allowed, forbidden and related communication probes, logs/metrics, authenticated synthetic-transaction outcomes, action/audit trail, and stability observations before declaring recovery. Reset the fault before ending each drill. Never use the scripts against Moodle or production.

## Evidence boundary

The three files under `evaluation/ground_truth/erpnext/` define expected outcomes for offline evaluation; they are not empirical results. Terraform, a healthy Compose stack, replay output, and a passing synthetic probe do not prove the agent diagnosed or recovered a fault. Record each live trial with timestamps, exact image digests, fault/reset logs, independent verifier observations, and preserved artifacts. Existing benchmark report remains at 0/225 until those trials are actually collected.
