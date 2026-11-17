# Danh mục từ viết tắt và thuật ngữ

| Viết tắt / thuật ngữ | Tiếng Anh | Nghĩa trong khoá luận |
| --- | --- | --- |
| AIHS | Automated Intelligent Healing System | Hệ thống tự chữa lành của Alibaba (Mapper LDA → Predictor GBDT → Controller GBDT), dùng làm model cơ sở C0 |
| AIOps | Artificial Intelligence for IT Operations | Ứng dụng AI vào vận hành hệ thống CNTT |
| ALB | Application Load Balancer | Bộ cân bằng tải tầng ứng dụng của AWS, đứng trước hai node Moodle |
| AWS | Amazon Web Services | Nền tảng điện toán đám mây dùng cho staging |
| C0–C3 | Configurations 0–3 | Bốn cấu hình so sánh: model đơn thuần; + Safety Gate; + Operator và Safe Executor (kết luận health-only); + Independent Verifier |
| CI/CD | Continuous Integration / Continuous Delivery | Tích hợp và triển khai liên tục (GitHub Actions) |
| CI 95 % | Confidence Interval | Khoảng tin cậy 95 % |
| EC2 | Elastic Compute Cloud | Máy ảo của AWS |
| EFS | Elastic File System | Hệ thống tệp chia sẻ của AWS, chứa `moodledata` |
| GBDT | Gradient Boosted Decision Trees | Cây quyết định tăng cường theo gradient, dùng cho Predictor và Controller |
| GHCR | GitHub Container Registry | Kho image container |
| health-only | – | Cách kết luận hồi phục chỉ dựa vào healthz 200, giao dịch synthetic đạt và không còn alert (cấu hình C2) |
| HitRate | – | Tỷ lệ ca sửa thành công mà hệ thống chọn đúng action đã sửa được |
| IaC | Infrastructure as Code | Hạ tầng dưới dạng mã (Terraform, Ansible) |
| Independent Verifier | – | Thành phần xác minh độc lập; duy nhất được phép xác nhận `RESOLVED` |
| LDA | Latent Dirichlet Allocation | Mô hình chủ đề, dùng để nén bằng chứng thành vector chủ đề |
| LLM | Large Language Model | Mô hình ngôn ngữ lớn |
| MTTD / MTTR | Mean Time To Detect / Repair | Thời gian trung bình để phát hiện / sửa sự cố |
| NAT | Network Address Translation | Cổng NAT cho subnet riêng trên AWS |
| `NOT_MEASURABLE` | – | Chỉ số chưa đo được do thiếu mẫu số, nhãn hoặc đối chứng; không đồng nghĩa với 0 % |
| Operator | – | Người vận hành có quyền duyệt và ký action |
| OverallRate | – | Trung bình điều hoà của HitRate và ReverseRate |
| Q1 / Q2 | – | Nhãn an toàn và đúng phạm vi của action / nhãn hồi phục sau action |
| RDS | Relational Database Service | Dịch vụ PostgreSQL được quản lý của AWS |
| `RESOLVED` | – | Trạng thái sự cố đã được Verifier xác nhận hồi phục |
| ReverseRate | – | Tỷ lệ ca sửa thất bại mà hệ thống tránh lặp lại action đã thất bại |
| RPO | Recovery Point Objective | Lượng dữ liệu tối đa có thể mất, tính bằng thời gian |
| RTO | Recovery Time Objective | Thời gian hồi phục; khoá luận đo detect→hồi phục thật |
| Safe Executor | – | Bộ thực thi chỉ chạy action trong allowlist khi có chữ ký hợp lệ, ghi receipt |
| Safety Gate | – | Lớp kiểm tra chính sách, catalog, phạm vi và độ tươi bằng chứng trước khi action được đưa đi duyệt |
| SRE | Site Reliability Engineering | Kỹ thuật độ tin cậy hệ thống |
| κ | Cohen's kappa | Hệ số đồng thuận giữa hai người gán nhãn |
