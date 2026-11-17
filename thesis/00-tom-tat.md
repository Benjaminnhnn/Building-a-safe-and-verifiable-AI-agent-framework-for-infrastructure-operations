# Tóm tắt

**Đề tài:** Xây dựng khung AI Agent an toàn và kiểm chứng được cho vận hành hạ tầng

Các hệ thống tự chữa lành dựa trên học máy, như AIHS (LDA → GBDT) của Alibaba, có thể đề xuất cách xử lý sự cố nhanh và chính xác, nhưng việc để model trực tiếp thi hành hành động trên hạ tầng thật đặt ra hai rủi ro: hành động sai hoặc vượt phạm vi, và kết luận "đã hồi phục" khi hệ thống chưa thật sự khoẻ. Khoá luận này đề xuất và hiện thực một khung an toàn đặt sau đề xuất của model, gồm bốn lớp: **Safety Gate** kiểm tra chính sách, phạm vi và độ tươi bằng chứng; **chữ ký của Operator** gắn đúng action đã chuẩn bị, tách phê duyệt khỏi thi hành; **Safe Executor** chỉ chạy action trong allowlist và ghi receipt; **Independent Verifier** đo lại các probe allowed/forbidden/related trong cửa sổ ổn định và là thành phần duy nhất được xác nhận `RESOLVED`.

Khung được triển khai trên môi trường Moodle trên AWS (ALB, hai node ứng dụng, RDS PostgreSQL, EFS, Prometheus/Alertmanager), dựng bằng Terraform và Ansible. Khoá luận tái hiện phương pháp AIHS trên dữ liệu Moodle thu từ lab cục bộ, khoá model, rồi so sánh bốn cấu hình C0–C3 (model đơn thuần, thêm Gate, thêm Operator và Executor với kết luận health-only, thêm Verifier) theo protocol được đăng ký và khoá trước khi xem kết quả.

Trên tập test offline (59 dòng, 8 nhóm, mở một lần), Safety Gate làm HitRate giảm từ 0,905 xuống 0,810, ReverseRate tăng từ 0,867 lên 0,967, chặn nhầm khoảng 10 % action đúng (2/20) và tăng tỷ lệ chuyển người từ 8,5 % lên 30,5 %; mẫu action không an toàn quá nhỏ (4) để khẳng định Gate giảm hành động nguy hiểm. Trên AWS staging, qua 18 lượt cấy lỗi chính và 4 lượt bổ sung đăng ký trước, không quan sát thấy kết luận `RESOLVED` sai nào (0/11 lần Verifier ghi `RESOLVED`) và không có thực thi ngoài phạm vi đã ký (13/13 action được duyệt chạy đúng); mọi bất biến an toàn được giữ. Ở 4 ca ghép cặp có ảnh hưởng phụ (hai lượt cấy có chủ đích, một lượt phát sinh ngoài mẫu và một ca lịch sử), kiểm tra health-only đều báo nhầm là đã hồi phục, còn Verifier chặn cả 4. Cái giá là chỉ 9/18 sự cố ở mẫu chính hoàn tất an toàn (phần còn lại dừng ở điểm kiểm soát của Operator, của model hoặc của Gate) và mốc xác nhận hồi phục chậm hơn khoảng 2 phút. Do mẫu nhỏ, kết quả live chỉ mang tính mô tả, và khoá luận không so sánh được thời gian hồi phục với C0 trên live. Hướng phát triển chính là mở rộng sang ERPNext, bổ sung người gán nhãn độc lập và tăng cỡ mẫu.

<!-- nguồn: Chương 5; docs/BAO_CAO_KET_QUA_C0_C3.md §1–§4; evaluation/aihs/live-main-results/aggregate.json; evaluation/aihs/live-supplementary-results/aggregate.json -->

**Từ khoá:** AIOps, tự chữa lành, AI Agent, Safety Gate, xác minh độc lập, hồi phục giả, Moodle, AWS.

---

# Abstract

**Title:** Building a safe and verifiable AI agent framework for infrastructure operations

Machine-learning self-healing systems such as Alibaba's AIHS (LDA → GBDT) can propose incident repairs quickly and accurately, but letting a model act directly on production infrastructure creates two risks: wrong or out-of-scope actions, and declaring recovery while the system is not actually healthy. This thesis designs and implements a safety framework placed behind the model's proposal, with four layers: a **Safety Gate** that checks policy, scope and evidence freshness; an **Operator signature** bound to the exact prepared action, separating approval from execution; a **Safe Executor** that runs only allowlisted actions and records receipts; and an **Independent Verifier** that re-measures allowed/forbidden/related probes over a stability window and is the only component allowed to mark an incident `RESOLVED`.

The framework runs on a Moodle deployment on AWS (ALB, two application nodes, RDS PostgreSQL, EFS, Prometheus/Alertmanager) provisioned with Terraform and Ansible. The thesis reproduces the AIHS method on Moodle data collected in a local lab, locks the model, and compares four configurations C0–C3 (model only, plus Gate, plus Operator and Executor with health-only recovery, plus Verifier) under protocols registered and locked before results were seen.

On the offline test set (59 rows, 8 groups, opened once), the Safety Gate lowered HitRate from 0.905 to 0.810, raised ReverseRate from 0.867 to 0.967, wrongly blocked about 10 % of correct actions (2/20) and raised the human-handoff rate from 8.5 % to 30.5 %; too few unsafe actions were observed (4) to claim that the Gate reduces dangerous actions. On AWS staging, across 18 main fault-injection runs and 4 pre-registered supplementary runs, no false `RESOLVED` was observed (0/11 `RESOLVED` conclusions) and nothing was executed outside the signed scope (13/13 approved actions ran correctly); every safety invariant held. In the 4 paired cases with collateral damage (two deliberately injected runs, one run outside the sample where the damage arose spontaneously, and one historical case), the health-only check wrongly reported recovery every time, while the Verifier blocked all 4. The cost is that only 9 of 18 incidents in the main sample completed safely (the rest stopped at the Operator, the model or the Gate) and that confirmed recovery came about 2 minutes later than actual recovery. Because the samples are small, the live results are descriptive only, and the thesis cannot compare recovery time with C0 on live infrastructure. Future work extends the framework to ERPNext, adds an independent second labeller and enlarges the sample.

<!-- nguồn: Chương 5; docs/BAO_CAO_KET_QUA_C0_C3.md §1–§4; evaluation/aihs/live-main-results/aggregate.json; evaluation/aihs/live-supplementary-results/aggregate.json -->

**Keywords:** AIOps, self-healing, AI agent, safety gate, independent verification, false recovery, Moodle, AWS.
