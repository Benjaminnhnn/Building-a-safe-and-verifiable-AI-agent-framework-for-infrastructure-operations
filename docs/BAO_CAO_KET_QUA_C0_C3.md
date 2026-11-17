# Kết quả so sánh C0–C3: AIHS LDA/GBDT có và không có Gate, Executor, Verifier trên Moodle

**Cập nhật:** 17/11/2026 · **Phạm vi:** Bước 6 của [PHAN_CONG](PHAN_CONG_LDA_GBDT.md). Báo cáo này gom kết quả đã đo; không chứa số liệu mới ngoài các file nguồn ở mục 7.

Câu hỏi nghiên cứu: *khi thêm Safety Gate, Safe Executor và Independent Verifier vào AIHS LDA/GBDT, hệ thống có an toàn và đáng tin cậy hơn AIHS cơ sở trên Moodle không?* Mô hình C0 dùng ở mọi phép đo là **`aihs-paper-repro-moodle-v1`** (`LOCKED_C0`, K = 20, `model_content_sha256` `0da82b58…`). Model, proposal và mapping giữ nguyên giữa các cấu hình; chỉ lớp bảo vệ phía sau model thay đổi.

## 1. Tóm tắt

| Câu hỏi | Kết quả | Nguồn bằng chứng |
| --- | --- | --- |
| Gate có chặn action sai không? | Trên tập test lab: C0 cho chạy 4/24 action có nhãn "không an toàn"; C1 chỉ còn 1/24. Nhưng mẫu số action không an toàn chỉ là 4 (< 5), nên theo protocol **không được** kết luận "Gate giảm hành động nguy hiểm". | offline, mục 2 |
| Gate có chặn nhầm action đúng không? | Có: 2/20 = 10 % (`scope_mismatch`), arm độ nhạy 2/21. Tỷ lệ chuyển người tăng từ 8,5 % lên 30,5 %. | offline, mục 2 |
| Executor có chạy đúng phạm vi không? | 13/13 action được duyệt chạy đúng action/máy đích/phạm vi; 0 lần thực thi khi chưa có chữ ký; 0 mutation ngoài phạm vi quan sát được. | live, mục 3 |
| Verifier có tránh kết luận hồi phục sai không? | 0/11 RESOLVED sai (9 chính + 2 bổ sung). Health-only (C2) báo hồi phục sai 2/2 lần ở lượt có lỗi phụ có chủ đích; Verifier chặn cả 2. Mẫu chính không có ca hồi phục giả tự nhiên nào (0/9). | live, mục 3–4 |
| Hệ thống có còn xử lý được sự cố không? | Hoàn tất an toàn 9/18 (50 %) ở mẫu chính: 4 lượt Operator từ chối có chủ ý, 4 lượt model chọn `call_human`, 1 lượt Gate chặn. Mọi lượt đã thực thi đều hồi phục thật (9/9). | live, mục 3 |
| Thêm lớp bảo vệ tốn bao nhiêu thời gian? | Trung vị detect→hồi phục thật 145 s (69 s nếu bỏ thời gian chờ người duyệt). Verifier thêm khoảng 128 s để ký `RESOLVED`. Không có RTO C0 live để so (mục 5). | live, mục 3 |

**Kết luận được phép nói:** trên Moodle staging và lab, với AIHS tái hiện đã khóa, chuỗi Gate → Operator → Executor → Verifier không tạo kết luận `RESOLVED` sai nào và không thực thi action nào ngoài phạm vi đã ký. Verifier phát hiện được hồi phục giả mà health-only bỏ sót trong các ca cấy lỗi có kiểm soát. Cái giá là tăng chuyển người (Gate chặn nhầm khoảng 10 % action đúng trên lab), thời gian xác minh thêm khoảng 2 phút và phụ thuộc vào Operator.

**Không được nói:** Gate giảm hành động nguy hiểm có ý nghĩa thống kê; tỷ lệ hồi phục giả của health-only trong vận hành tự nhiên; C3 nhanh hơn hay chậm hơn C0 live; bất kỳ so sánh trực tiếp nào với số liệu Alibaba (F1 0,97, −51 %).

## 2. Offline: C0 so với C1 trên tập test lab

Protocol `c0-c3-offline-v1` ([BAO_CAO_PROTOCOL_C0_C3_OFFLINE](BAO_CAO_PROTOCOL_C0_C3_OFFLINE.md)) được khóa bằng sha256 rồi mới mở tập test **một lần** ngày 15/11/2026 (lock `adb0d194…`). Dữ liệu là lab Moodle cục bộ, freeze `17efc7b6…`: train 277 / validation 57 / test 59, chia theo thời gian và theo nhóm incident/phiên. Test có 59 dòng trong 8 nhóm. Khoảng tin cậy là bootstrap 95 % theo nhóm (B = 2000, seed `20261116`). Với 8 nhóm, khoảng tin cậy rộng và chỉ mang tính mô tả.

C2 và C3 offline bằng C1 theo cấu trúc (`OFFLINE_EQUALS_C1_BY_CONSTRUCTION`): offline không có Operator, Executor hay probe phục hồi.

### 2.1. Arm chính (nhãn "Một phần" → `failure`)

| Chỉ số | C0 (chỉ model) | C1 (+ Gate) | C1 − C0 [CI 95 %] |
| --- | --- | --- | --- |
| HitRate | 19/21 = 0,905 [0,80; 1,00] | 17/21 = 0,810 [0,72; 0,93] | −0,095 [−0,20; 0,00] |
| ReverseRate | 26/30 = 0,867 [0,78; 0,96] | 29/30 = 0,967 [0,89; 1,00] | +0,100 [0,025; 0,188] |
| OverallRate | 0,885 [0,82; 0,96] | 0,881 [0,82; 0,95] | −0,004 [−0,066; 0,056] |
| Tỷ lệ chuyển người | 5/59 = 0,085 | 18/59 = 0,305 | +0,220 [0,085; 0,375] |
| Action không an toàn được đi tiếp | 4/24 = 0,167 | 1/24 = 0,042 | −0,125 [−0,222; −0,030] |
| Action sai bị chặn | – | 3/4 = 0,75 | **`INSUFFICIENT_DENOMINATOR`** (mẫu số < 5) |
| Chặn nhầm action an toàn | – | 2/20 = 0,10 [0,00; 0,20] | lý do: `scope_mismatch` ×2 |
| Action mutate được Gate cho đi tiếp | – | 41/54 = 0,759 [0,60; 0,91] | còn lại: 11 `scope_mismatch`, 2 `ambiguous_context` |

Nhãn an toàn (Q1) chỉ có khi proposal trùng action lab đã chạy: 24 dòng có nhãn, 30 dòng proposal khác action lab nên không có nhãn (8 bị chặn, 22 được đi tiếp). Không suy ra đúng/sai cho 30 dòng đó.

### 2.2. Arm độ nhạy (loại nhãn "Một phần"; freeze, model và split riêng)

| Chỉ số | C0 | C1 | C1 − C0 |
| --- | --- | --- | --- |
| HitRate | 20/21 = 0,952 | 18/21 = 0,857 | −0,095 [−0,20; 0,00] |
| ReverseRate | 14/14 = 1,000 | 14/14 = 1,000 | 0 |
| OverallRate | 0,976 | 0,923 | −0,053 [−0,118; 0,00] |
| Tỷ lệ chuyển người | 0/43 = 0 | 5/43 = 0,116 | +0,116 [0,026; 0,205] |
| Action không an toàn được đi tiếp | 0/21 | 0/21 | 0 |
| Chặn nhầm action an toàn | – | 2/21 = 0,095 [0,00; 0,20] | `scope_mismatch` ×2 |

**Đọc hai arm cùng nhau (D4):** cùng chiều ở HitRate (giảm khoảng 0,1), chặn nhầm (khoảng 10 %) và chuyển người (tăng). ReverseRate tăng ở arm chính nhưng không đổi ở arm độ nhạy, và arm độ nhạy không có action không an toàn nào, nên lợi ích "chặn action sai" **không vững** theo luật D4. Kết luận vững duy nhất về Gate offline là **chi phí** của nó: khoảng 1/10 action đúng bị chuyển người do suy ngữ cảnh fail-closed.

**Đối chứng đồng hồ batch:** nếu dùng thời điểm proposal được sinh hàng loạt thay cho thời điểm quyết định, mọi proposal tới Gate đều bị `stale_evidence` (C1 HitRate = 0). Đây là kiểm tra Gate thực sự áp dụng độ tươi bằng chứng, không phải kết quả chính. Quy tắc dừng (≥ 50 % `stale_evidence` ở đồng hồ chính) không bị kích hoạt (0 %).

**Kiểm tra âm tính (C0 shadow replay, 15/11/2026):** action lạ (`DROP_DATABASE`), action chỉ dùng để reset và action ngoài không gian C0 đều bị adapter từ chối trước Gate (`unknown_aihs_action`), 0 lần gọi Executor, chuỗi audit hợp lệ.

## 3. Live staging: C2 so với C3 trên cùng lượt thực thi

Thiết kế: [BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3](BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md) mục 1–8. Mỗi lượt live là C3; C2 được lấy từ `health_only_view` của chính lượt đó (cùng action, cùng thời điểm). C0 và C1 không thực thi live theo thiết kế. Mapping dùng ở live là `a3-c0-action-mapping-reviewed.json`.

### 3.1. Mẫu chính: 18 lượt (6 kịch bản × 3), 16–17/11/2026

| Chỉ số | Giá trị | Wilson 95 % |
| --- | --- | --- |
| Model đề xuất action sửa đúng kịch bản | 14/14 (4 lượt khác model chọn `call_human`) | [0,78; 1,00] |
| Gate: `REQUIRE_APPROVAL` / `scope_mismatch` / không qua Gate (`call_human`) | 13 / 1 / 4 | – |
| Operator: duyệt / từ chối có chủ ý | 9 / 4 | – |
| Executor chạy đúng action, máy đích, phạm vi | 9/9; 0 lần chạy khi chưa ký | – |
| Hồi phục thật trong các lượt đã thực thi | 9/9 | [0,70; 1,00] |
| C2 (health-only) tuyên bố hồi phục, trong đó sai | 9, sai 0 | sai: [0; 0,30] |
| C3 `RESOLVED`, trong đó sai | 9, sai 0 | sai: [0; 0,30] |
| C3 bỏ sót hồi phục thật | 0 | – |
| Hoàn tất an toàn (RESOLVED / mọi sự cố được giao) | 9/18 = 0,50 | [0,29; 0,71] |
| Bất biến an toàn; reset đã kiểm tra | 18/18; 18/18 | – |
| RPO (6 lượt đo được) | 0 s, 0 ghi nhận bị mất | – |

Trung vị thời gian (giây): inject→detect 58,0; detect→proposal 26,1; chờ duyệt 40,9; thực thi 2,0; detect→health-only tuyên bố 135,5; **detect→hồi phục thật 145,0 (69,1 nếu bỏ chờ duyệt)**; xác minh 128,1; detect→`RESOLVED` 274,7. Phân theo kịch bản ở mục 15 của báo cáo kế hoạch live.

### 3.2. Lượt bổ sung đăng ký trước: 4 lượt, 17/11/2026

Chi tiết ở mục 16–17 của báo cáo kế hoạch live. Không gộp với mẫu chính vì Operator không mù và hai lượt có lỗi phụ cấy có chủ đích.

| Nhóm | Kết quả |
| --- | --- |
| DB-01 ×2 (không lỗi phụ) | Đề xuất đúng 2/2, Operator duyệt, thực thi đúng, hồi phục thật 2/2, `RESOLVED` 2/2, sai 0. Trung vị detect→hồi phục thật 620,2 s (121,6 s nếu bỏ chờ duyệt). |
| CON-01 ×2 + `web_a_stop` sau khi health-only kết luận | C2 tuyên bố hồi phục **sai 2/2**; C3 không `RESOLVED` 2/2 (`not_resolved`, `collateral_damage`). Ở seq 103, health-only vẫn xanh 20 s sau lỗi phụ; chỉ probe related của Verifier phát hiện. |
| An toàn | 4/4 đạt bất biến; 4/4 reset đã kiểm tra (gồm reset lỗi phụ). |

## 4. Khôi phục giả: gộp các ca ghép cặp health-only/Verifier

| Nguồn | Health-only tuyên bố | Health-only sai | Verifier `RESOLVED` | Verifier sai |
| --- | --- | --- | --- | --- |
| Mẫu chính (tự nhiên, không cấy lỗi phụ) | 9 | 0 | 9 | 0 |
| Bổ sung DB-01 | 2 | 0 | 2 | 0 |
| Bổ sung CON-01 + lỗi phụ (cấy có chủ đích) | 2 | 2 | 0 | 0 |
| Lượt seq 2 bị huỷ (mục 13 báo cáo live; ngoài mẫu) | 1 | 1 | 0 | 0 |
| NET-01 lịch sử 08/11/2026 (`b2b3-measurement-2026-11-14.json`) | 1 | 1 | 0 | 0 |

Tách ca cấy lỗi khỏi sự cố tự nhiên theo PHAN_CONG Bước 6: trên sự cố tự nhiên, cả health-only và Verifier đều chưa sai lần nào (0/11). Bốn ca có ảnh hưởng phụ đều bị health-only bỏ sót và đều bị Verifier chặn. Đây là bằng chứng về **cơ chế**, không phải tỷ lệ quần thể.

## 5. Chỉ số chưa đo được (`NOT_MEASURABLE`)

| Chỉ số | Lý do |
| --- | --- |
| RTO C0 so với C3 trên cùng môi trường | C0 không thực thi live theo biên bản duyệt (action không qua Gate). Không có cặp live tương đương. Nhánh ablation Agent-only (commit `22ed0ba7`) là đối chứng phụ, không phải C0. |
| Gate giảm action nguy hiểm (có ý nghĩa) | Mẫu số action không an toàn trên test = 4 < 5 (arm chính), 0 (arm độ nhạy). |
| Tỷ lệ hồi phục giả tự nhiên của health-only | 0 sự kiện trong 11 lượt tự nhiên; cận trên Wilson 95 % ≈ 0,26. |
| NET-01 thực thi trong chiến dịch AIHS | `derive_context` (file đã khóa) suy NET-01 thành DB-01 nên Gate luôn chặn `scope_mismatch`. Hành động NET-01 chỉ có bằng chứng ở drill Operator và nhánh Agent-only. |
| Tỷ lệ chặn nhầm của Gate trên live | Live không có lượt Gate `DENY` một action đúng; lượt `scope_mismatch` NET-01 là hạn chế đã biết của suy ngữ cảnh, không có nhãn độc lập riêng cho live. |

## 6. Giới hạn

- **Nhãn chưa thật sự độc lập.** Nhãn Q1/Q2 trên lab do một người gán (`tien`). PLANNING 11.1 yêu cầu một người khác gán; chưa có đánh giá liên người gán.
- **Người duyệt, người thiết kế và người gán nhãn là cùng một người.** Bốn lượt REJECT có chủ ý ở mẫu chính phản ánh chính sách của người này.
- **Mẫu nhỏ.** 59 dòng/8 nhóm offline; 18 + 4 lượt live; 3 lặp mỗi kịch bản.
- **Lab khác staging.** Năm đặc trưng `lb_*` chỉ có ở HAProxy lab, không có trên ALB staging (cảnh báo preflight mọi lượt).
- **Mapping offline là bản nháp** (`draft_unreviewed`); mapping live là bản đã review.
- **Một môi trường.** Chỉ Moodle; ERPNext (PLANNING 9.3) chưa làm.

## 7. Artifact và lệnh tái lập

| Nội dung | Vị trí | Trong Git |
| --- | --- | --- |
| Kết quả test offline (2 arm) | `evaluation/aihs/.artifacts/phase4/test-{primary,sensitivity}/results.json`, `per-row.jsonl`; lock `protocol-lock-*.json` | Không (`.artifacts/`) |
| Tổng hợp live chính | `evaluation/aihs/live-main-results/aggregate.json` | Có |
| Tổng hợp live bổ sung | `evaluation/aihs/live-supplementary-results/aggregate.json` | Có |
| Run gốc + audit runtime, mẫu chính | `main-archive-20261117.tgz`, SHA256 `a46708e5…f730e`, 79 bản ghi audit | Không (bản sao ở `A:/KLTN/aihs-evidence/`) |
| Run gốc + audit runtime, bổ sung | `supplementary-archive-20261117.tgz`, SHA256 `fa0aa9a1d916dd271905aaf2d29afe961040a7f353e67e395dfa40d6b33a1e1e`, 22 bản ghi audit | Không (như trên) |

```bash
# Offline: chỉ đọc lại kết quả; KHÔNG chạy lại lock-protocol/test (marker test-opened chặn chạy lại)
python -c "import json;print(json.load(open('evaluation/aihs/.artifacts/phase4/test-primary/results.json'))['scoring']['metrics'])"

# Live: tổng hợp lại từ run gốc (sau khi giải nén archive)
python automation/aihs-live-aggregate.py ~/moodle-ops/aihs-live/main --json /tmp/main.json --md /tmp/main.md
python automation/aihs-live-aggregate.py ~/moodle-ops/aihs-live/supplementary --json /tmp/sup.json --md /tmp/sup.md
```
