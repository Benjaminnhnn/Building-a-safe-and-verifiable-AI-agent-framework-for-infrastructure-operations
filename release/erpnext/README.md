# ERPNext staging runtime

This is the isolated, single-host generalization environment for ERP-01/02/03. It is separate from the Moodle Compose project and has no public IP. The Terraform host is optional and disabled by default.

Create `secrets/db_root_password` and `secrets/erpnext_admin_password` on the host with root-only permissions, then place `.env` from `.env.example` beside this file. Initialize once with `docker compose --profile initialize run --rm create-site`, then start with `docker compose up -d`. Do not commit secret files or use the demo `admin/admin` credentials.

The stack follows Frappe's production Compose service layout and pins ERPNext v16.51.0, MariaDB 11.8, and Redis 8.6. Test image compatibility and restore a known-good database backup before any major-version upgrade.
