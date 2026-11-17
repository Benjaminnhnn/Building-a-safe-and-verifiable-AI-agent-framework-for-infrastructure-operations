# Tóm tắt

**Đề tài:** Xây dựng khung AI Agent an toàn và kiểm chứng được cho vận hành hạ tầng

Các hệ thống tự chữa lành dựa trên học máy, như AIHS (LDA → GBDT) của Alibaba, có thể đề xuất cách xử lý sự cố nhanh và chính xác, nhưng việc để model trực tiếp thi hành hành động trên hạ tầng thật đặt ra hai rủi ro: hành động sai hoặc vượt phạm vi, và kết luận "đã phục hồi" khi hệ thống chưa thật sự khoẻ. Khoá luận này đề xuất và hiện thực một khung an toàn đặt sau đề xuất của model, gồm bốn lớp: **Safety Gate** kiểm tra chính sách, phạm vi và độ tươi bằng chứng; **chữ ký của Operator** gắn đúng action đã chuẩn bị, tách phê duyệt khỏi thi hành; **Safe Executor** chỉ chạy action trong allowlist và ghi receipt; **Independent Verifier** đo lại các probe allowed/forbidden/related trong cửa sổ ổn định và là thành phần duy nhất được xác nhận `RESOLVED`.

Khung được triển khai trên môi trường Moodle trên AWS (ALB, hai node ứng dụng, RDS PostgreSQL, EFS, Prometheus/Alertmanager), dựng bằng Terraform và Ansible. Khoá luận tái hiện phương pháp AIHS trên dữ liệu Moodle thu từ lab cục bộ, khoá model, rồi so sánh bốn cấu hình C0–C3 (model đơn thuần, thêm Gate, thêm Operator và Executor với kết luận health-only, thêm Verifier) theo protocol được đăng ký và khoá trước khi xem kết quả.

Trên tập test offline (59 dòng, 8 nhóm, mở một lần), Safety Gate làm HitRate giảm từ 0,905 xuống 0,810, ReverseRate tăng từ 0,867 lên 0,967 và chặn nhầm khoảng 10 % action đúng; mẫu action không an toàn quá nhỏ để khẳng định Gate giảm hành động nguy hiểm. Trên AWS staging, qua 18 lượt cấy lỗi chính và 4 lượt bổ sung đăng ký trước, hệ thống không có kết luận `RESOLVED` sai nào (0/11), không thực thi ngoài phạm vi đã ký (13/13 action được duyệt chạy đúng), giữ mọi bất biến an toàn, và Verifier chặn được cả 4 ca có ảnh hưởng phụ mà kiểm tra health-only đã báo nhầm là đã phục hồi. Cái giá là một nửa số sự cố dừng ở các điểm kiểm soát của con người và mốc xác nhận phục hồi chậm hơn khoảng 2 phút. Hướng phát triển chính là mở rộng sang ERPNext, bổ sung người gán nhãn độc lập và tăng cỡ mẫu.

**Từ khoá:** AIOps, tự chữa lành, AI Agent, Safety Gate, xác minh độc lập, hồi phục giả, Moodle, AWS.

---

# Abstract

**Title:** Building a safe and verifiable AI agent framework for infrastructure operations

Machine-learning self-healing systems such as Alibaba's AIHS (LDA → GBDT) can propose incident repairs quickly and accurately, but letting a model act directly on production infrastructure creates two risks: wrong or out-of-scope actions, and declaring recovery while the system is not actually healthy. This thesis designs and implements a safety framework placed behind the model's proposal, with four layers: a **Safety Gate** that checks policy, scope and evidence freshness; an **Operator signature** bound to the exact prepared action, separating approval from execution; a **Safe Executor** that runs only allowlisted actions and records receipts; and an **Independent Verifier** that re-measures allowed/forbidden/related probes over a stability window and is the only component allowed to mark an incident `RESOLVED`.

The framework runs on a Moodle deployment on AWS (ALB, two application nodes, RDS PostgreSQL, EFS, Prometheus/Alertmanager) provisioned with Terraform and Ansible. The thesis reproduces the AIHS method on Moodle data collected in a local lab, locks the model, and compares four configurations C0–C3 (model only, plus Gate, plus Operator and Executor with health-only recovery, plus Verifier) under protocols registered and locked before results were seen.

On the offline test set (59 rows, 8 groups, opened once), the Safety Gate lowered HitRate from 0.905 to 0.810, raised ReverseRate from 0.867 to 0.967 and wrongly blocked about 10 % of correct actions; too few unsafe actions were observed to claim that the Gate reduces dangerous actions. On AWS staging, across 18 main fault-injection runs and 4 pre-registered supplementary runs, the system produced no false `RESOLVED` (0/11), executed nothing outside the signed scope (13/13 approved actions ran correctly), kept every safety invariant, and the Verifier caught all 4 cases with collateral damage that the health-only check had reported as recovered. The cost is that half of the incidents stopped at a human control point and confirmed recovery came about 2 minutes later than actual recovery. Future work extends the framework to ERPNext, adds an independent second labeller and enlarges the sample.

**Keywords:** AIOps, self-healing, AI agent, safety gate, independent verification, false recovery, Moodle, AWS.
