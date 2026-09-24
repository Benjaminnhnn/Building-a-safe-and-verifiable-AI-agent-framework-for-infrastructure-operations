# Prompt hoàn thiện và nghiệm thu Sprint 1–2

Làm việc trong repo hiện tại, dựa trên `docs/PLANNING.md`, `docs/SPRINT_STATUS.md` và `docs/MOODLE_SPRINT_RUNBOOK.md`. Hoàn thiện Moodle foundation và MVP vertical slice. Không dùng kết quả lịch sử làm bằng chứng hiện tại.

## Phạm vi

- Sprint 1: Moodle staging trên Terraform/Ansible/Compose, PostgreSQL/EFS và secrets, health/login, synthetic read/write, backup/restore/reset, monitoring Prometheus/Alertmanager/blackbox/exporters, DB-01 và bốn scenario đại diện với ground truth, injector và reset.
- Sprint 2: alert → incident → evidence → diagnosis → plan → gate → execute → verify; dedup, exact staging allowlist, dry-run, audit/evidence, independent verifier, năm scenario DB-01/RES-01/NET-01/CON-01/SEC-02.
- Tái sử dụng tài nguyên hiện có. Ghi `git status`, diff trước và sau; không ghi đè thay đổi có sẵn. Không commit/push hoặc đưa secret, key, state, plan hay dữ liệu nhạy cảm vào Git.

## Điều kiện an toàn

1. Chạy offline trước: Ruff critical, test liên quan, `terraform fmt -check -recursive`, `validate`, `test`, Compose `config -q`, Python compile, Bash `-n`, replay. Không cài/nâng dependency tùy tiện.
2. Trước AWS: xác nhận profile/account, region, environment, Docker, Ansible, baseline, Terraform state/plan hiện thời và ước tính chi phí. Không dùng plan cũ nếu input/code/state đã đổi; chỉ apply plan cụ thể đã được duyệt. Không inject live khi Moodle chưa healthy.
3. Khi live: baseline healthy → fault có phạm vi → alert mới và delivery vào pipeline → allowlisted reset → health, toàn bộ allowed/forbidden/related communication probes và ít nhất 120 giây quan sát ổn định → baseline/checksum. Verifier thiếu bằng chứng phải fail closed; không tự đặt `RESOLVED`.
4. Replay chỉ được dùng fixture và dry-run; `mutated=false`, forbidden executions = 0, verifier `resolution_eligible=false`, incident không `RESOLVED`. Tách static/config, offline replay, shadow và live.

## QA/QC và báo cáo

- Đối chiếu từng deliverable với source, fixture, injector/reset, CI, test, tài liệu và evidence; ghi `PASS`, `FAIL`, `BLOCKED` hoặc `NOT RUN` kèm đường dẫn bằng chứng.
- DB-01 ít nhất một lần ở mức khả dụng: fixture hợp lệ, ≥3 evidence refs, dedup cùng incident, exact allowlist, hành động cấm bị từ chối, dry-run không gọi hạ tầng, replay reset/recovery chỉ là hợp đồng; live chỉ khi đủ preflight.
- Lưu timestamp UTC, lệnh, exit code, môi trường, scenario, kết quả và hạn chế ở `.artifacts/` đã ignore. Không sửa số liệu lịch sử.
- Kết luận Sprint 1 và 2 riêng: `COMPLETE`, `PARTIAL` hoặc `BLOCKED`; liệt kê acceptance còn thiếu và bước kiểm chứng tiếp theo. Kết thúc bằng `git diff --check`, `git status`, review diff và kiểm tra secret/state/artifact.
