# Cấu hình và kho tri thức (`agent_src/config/`)

Thư mục này chứa các hợp đồng và danh mục tài nguyên mà Agent đọc lúc chạy, cùng các runbook được dùng làm ngữ cảnh RAG.

## Danh mục tài nguyên và hợp đồng

- **`moodle_resource_inventory.json`**: danh mục tài nguyên Moodle (`moodle-app`, `postgres-db`, `moodle-rds-security-group`, `moodledata-volume`, `moodle-web-endpoint`, giám sát và chính Agent) cùng quan hệ phụ thuộc. Planner chỉ nhắm vào tài nguyên có trong danh mục; dependency graph dùng file này để tính phạm vi ảnh hưởng.
- **`moodle_capability_contract.json`**: hợp đồng bằng chứng, năng lực, phạm vi đích và năng lực theo kịch bản. `core/moodle/contract.py` kiểm tra hợp đồng này khớp với action catalog.
- **`erpnext_resource_inventory.json`**: danh mục tài nguyên ERPNext cho `ERPNextPlatformAdapter` (kiểm chứng khả năng áp dụng cho nền tảng thứ hai).
- **`contracts/`**: communication contract ở dạng YAML. `moodle_communication_contract.yaml` dùng cho Independent Verifier trong replay; `demo_web_communication_contract.yaml` chỉ là fixture kiểm thử của verifier.

## Kho tri thức runbook (`knowledge_base/moodle/`)

Runbook Markdown do con người soạn và review. `core/rag/runbook_chunks.py` tách runbook theo heading thành các chunk có `chunk_id` và SHA-256. ChromaDB (`standard_runbooks`) và retriever tất định đều dùng cùng các chunk này. `RUNBOOK_ALERT_NAMES` gán runbook cho từng alert.

| Runbook | Alert áp dụng |
|---|---|
| `runbook_moodle_connectivity.md` | Kết nối Moodle → RDS (TCP, độ trễ, synthetic transaction) |
| `runbook_moodle_service.md` | Web container, ALB, Apache router, trusted proxy |
| `runbook_moodle_storage_resources.md` | EFS, scratch ENOSPC, CPU, bộ nhớ |
| `runbook_moodle_monitoring.md` | Chất lượng tín hiệu giám sát (exporter, probe, audit) |
| `runbook_moodle_security.md` | Phơi bày cổng RDS, trusted proxy |
| `runbook_moodle_platform_operations.md` | Maintenance mode bị kẹt, hàng đợi cron ad-hoc |

Runbook chỉ là ngữ cảnh cho chẩn đoán LLM. Chúng không cấp quyền hành động: mọi hành động vẫn phải nằm trong action catalog và đi qua Safety Gate.
