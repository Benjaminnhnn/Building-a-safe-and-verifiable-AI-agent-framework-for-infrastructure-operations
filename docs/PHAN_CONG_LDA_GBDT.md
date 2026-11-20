# Hướng dẫn hoàn thiện phép so sánh với AIHS

Báo cáo này hướng dẫn **những việc còn phải làm** để trả lời câu hỏi: *Khi thêm Safety Gate, Safe Executor và Independent Verifier vào AIHS LDA/GBDT, hệ thống có an toàn và đáng tin cậy hơn AIHS cơ sở trên Moodle không?* Kết quả chỉ được công bố sau khi đo trên dữ liệu đủ điều kiện. Phần đã thực hiện và bằng chứng gốc nằm ở [PLANNING](PLANNING_UNG_DUNG_LDA_GBDT.md) và [BAO_CAO_AIHS](BAO_CAO_AIHS.md).

## Cần so sánh những gì?

Giữ **cùng model AIHS, cùng dữ liệu đầu vào và cùng action được đề xuất**. Chỉ thay các lớp bảo vệ phía sau model:

| Cấu hình | Luồng xử lý | Dùng để xem |
| --- | --- | --- |
| **C0** | AIHS LDA/GBDT đề xuất action | AIHS cơ sở sẽ chọn gì. Đây là bản tái hiện trong nghiên cứu, không phải hệ thống production Alibaba. |
| **C1** | C0 → Safety Gate | Gate chặn đúng action nguy hiểm và có chặn nhầm action an toàn không. |
| **C2** | C1 → Operator duyệt → Safe Executor | Action được thực thi có đúng phạm vi đã duyệt không. |
| **C3** | C2 → Independent Verifier | Hệ thống đã phục hồi thật chưa, có hỏng thành phần liên quan không. |

**Action nguy hiểm ở C0 chỉ được đánh dấu “sẽ thực thi nếu không có Gate” trên bản replay; không chạy thật.** Nếu cần so thời gian phục hồi C0/C3, chỉ chạy action an toàn đã review trên **cùng lab cô lập**. Trên staging vẫn giữ các lớp bảo vệ bắt buộc. Độ chính xác của model LDA/GBDT được báo riêng; thêm Gate/Executor/Verifier không tự làm model dự đoán chính xác hơn.

## Hiện còn thiếu gì?

**Cập nhật 17/11/2026: Bước 1–6 đã chạy xong.** Kết quả tổng hợp ở [BAO_CAO_KET_QUA_C0_C3](BAO_CAO_KET_QUA_C0_C3.md).

| Điều kiện | Ngày 14/11/2026 | Ngày 17/11/2026 |
| --- | --- | --- |
| Dữ liệu Moodle đủ huấn luyện | **0/10** bản ghi đạt | Lab `campaign-1`; freeze `17efc7b6…`: train 277 / validation 57 / test 59 |
| Tập test độc lập | **0** bản ghi | 59 dòng, 8 nhóm; mở **một lần** sau khi khóa protocol `c0-c3-offline-v1` |
| Action AIHS được duyệt để chạy trên Moodle | **0** mapping `live_reviewed` | `aihs-c0-moodle-reviewed-v1`, `reviewed_for_live` |
| Lượt AIHS chạy trên staging | **0** lượt | 18 lượt C3 chính + 4 lượt bổ sung đăng ký trước |

**Còn mở (cần con người):** nhãn do một người gán, chưa có người gán thứ hai độc lập; lý do REJECT của seq 2 và seq 13 chưa ghi; ERPNext (PLANNING 9.3) chưa làm. Các chỉ số vẫn `NOT_MEASURABLE` được liệt kê ở mục 5 của báo cáo kết quả.

## Bước 1 — Thu dữ liệu đúng thời điểm và gán nhãn độc lập

**Bạn cần chạy thêm các lượt sự cố có kiểm soát trong lab Moodle** (hoặc thu sự cố thật khi có) vì artifact cũ không có snapshot đủ điều kiện. Trước tiên bổ sung collector để tự lưu metric/log; sau đó chạy nhiều lượt với lỗi, tải và action an toàn đã review. Bạn kiểm tra collector có chụp bằng chứng **trước lúc planner quyết định** và lưu artifact từng lượt. Không cần tự chụp metric bằng tay, không cần cố tạo hành động nguy hiểm và **chưa cần model AIHS ở bước này**: dữ liệu huấn luyện có thể đến từ action catalog đã được thực thi và quan sát kết quả. Chỉ sau bước 2 mới sinh proposal C0 cho tập test.

**Làm gì**

1. Với mỗi sự cố, collector lưu metric/log, thời điểm chụp và mã băm bằng chứng **trước khi planner quyết định**. Sau đó lưu action catalog được đề xuất, quyết định Gate, phê duyệt, action thực chạy, receipt và probe sau sửa. Nếu lượt đó có AIHS shadow thì lưu proposal riêng; không coi shadow là điều kiện bắt buộc để thu dữ liệu huấn luyện.
2. Lưu cả lượt thành công, thất bại, Gate từ chối, Operator từ chối, timeout và lượt chưa thực thi. Không gán nhãn “sửa thất bại” cho action chưa chạy.
3. Bổ sung [bộ xuất dữ liệu A2](../evaluation/aihs/a2_export.py) để đọc artifact mới có `proposal.source=aihs` và snapshot trước quyết định. Bộ xuất hiện chỉ đọc một loại artifact staging cũ nên chưa đủ cho nghiên cứu này. Giữ dữ liệu nhạy cảm trong vùng hạn chế; bản xuất phải lọc token, chữ ký, ID và thông tin cá nhân.
4. Nhờ người đánh giá độc lập xem bằng chứng gốc để gán **hai loại nhãn**: action đề xuất có an toàn/đúng phạm vi không; sau khi action thật chạy, hệ thống có phục hồi thật và có ảnh hưởng phụ không. Người đánh giá không dùng verdict của Gate hoặc dự đoán của model làm đáp án.

**Hoàn thành khi:** có bộ bản ghi theo [schema A2](../evaluation/aihs/a2-record.schema.json), manifest/checksum, nhãn độc lập và bảng số mẫu theo loại lỗi, action, môi trường, kết quả. Tách dữ liệu lab, staging, replay và tổng hợp; không cộng chúng thành một tỷ lệ.

## Bước 2 — Tạo tập train/test và khóa model AIHS cơ sở

**Làm gì**

1. **Kiểm tra dữ liệu từ bước 1:** phải có feature trước quyết định và action **đã chạy** với kết quả quan sát được; cần cả ca thành công và thất bại cho từng action muốn học. Proposal bị Gate từ chối vẫn lưu để đo Gate ở bước 4, nhưng không dùng làm nhãn “action sửa thất bại” khi huấn luyện.
2. **Chọn bản C0:** nên tái hiện phương pháp trong bài báo AIHS làm baseline chính; giữ prototype công khai `as-is` để đối chiếu riêng. Ghi rõ bản nào được dùng, vì prototype có khác biệt về LDA, cách chọn mẫu huấn luyện Predictor và fallback Controller. Không trộn hai bản trong một kết quả.
3. **Đóng băng dữ liệu:** chạy công cụ freeze bên dưới. Nó chia train/validation/test theo thời gian và nhóm sự cố/phiên. Nếu trả `BLOCKED`, đọc lý do thiếu dữ liệu/nhãn rồi quay lại bước 1; không hạ ngưỡng cho qua.
4. **Xây phần huấn luyện Moodle còn thiếu:** hiện repo có script huấn luyện prototype trên **dữ liệu tổng hợp**, chưa có lệnh huấn luyện AIHS trên dữ liệu Moodle. Cần viết pipeline biến snapshot Moodle thành thuộc tính/log đầu vào LDA, huấn luyện Predictor GBDT trên ca sửa thành công và Controller GBDT trên cặp action đã chạy có cả kết quả thành công/thất bại. Fit từ vựng/LDA **chỉ trên train**; chọn tham số trên validation; không học từ test.
5. **Khóa và chạy model:** lưu phiên bản code, seed, phụ thuộc, model hash, cách tạo feature và thời điểm AI chốt action. Sau khi khóa model, chạy C0 trên từng snapshot của test để lưu proposal; chỉ lúc đó mới mở nhãn test để chấm, không chỉnh model theo kết quả test.

Sau khi bước 1 có dữ liệu và file review nhãn, chạy freeze bằng chính đường dẫn của chiến dịch:

```bash
python3 evaluation/aihs/b2_freeze_dataset.py \
  --records evaluation/aihs/.artifacts/new-a2-export/audit-records.jsonl \
  --source-manifest evaluation/aihs/.artifacts/new-a2-export/manifest.json \
  --label-review evaluation/aihs/.artifacts/independent-label-review.jsonl \
  --allowed-mode lab \
  --manifest-out evaluation/aihs/.artifacts/new-freeze-manifest.json
```

**Hoàn thành khi:** freeze báo `READY`, train/validation/test không trùng nhóm; pipeline Moodle huấn luyện lại được cùng model C0 và tạo lại cùng proposal trên test. Ngưỡng tối thiểu 10 nhóm của công cụ chỉ là kiểm tra kỹ thuật; cỡ mẫu để kết luận phải quyết định bằng pilot. Nếu thiếu ca thất bại hoặc một action quá ít mẫu để học Controller, ghi rõ giới hạn và thu thêm dữ liệu, không suy diễn kết quả cho action đó.

## Bước 3 — Nối đề xuất AIHS với hệ thống an toàn

**Bạn dùng đầu ra nào?** Model C0 đã khóa và các proposal trên tập test từ bước 2. Bước này kiểm tra **AIHS đề xuất gì và hệ thống hiểu đề xuất đó ra sao**; chưa chạy lỗi live.

1. Mở [mapping A3](../evaluation/aihs/a3-action-mapping.json). Với từng mã action AIHS, cùng người phụ trách Moodle xác định có action catalog tương đương không. Chỉ đánh dấu `live_reviewed` khi đã review action ID, kịch bản, máy đích, phạm vi, quyền, điều kiện trước khi chạy, rollback và probe xác minh. Mapping live còn cần `review_reference`; mapping tổng thể cần `review_status=reviewed_for_live`. Mã không tương đương giữ `unmapped` hoặc `human_only`.
2. Cho **proposal thật của C0** qua [contract](../agent_src/core/moodle/aihs_contract.py) và [shadow bridge](../agent_src/core/moodle/aihs_shadow.py). Kiểm tra action lạ, bằng chứng cũ, sai máy đích và sai phạm vi đều bị chặn. Gemini shadow và fixture test không thay proposal AIHS.
3. Chạy [staging handoff](../agent_src/core/moodle/aihs_staging_handoff.py) đến trạng thái **chờ Operator**. Bridge hiện chỉ chuẩn bị yêu cầu; nó không ký, không gọi Executor và không chạy Verifier. Toàn bộ chuỗi thực thi sẽ được kiểm tra ở bước 5.

**Lệnh kiểm tra code hiện có:**

```bash
PYTHONPATH=agent_src python3 -m unittest discover -s agent_src/tests -p test_aihs_contract.py -v
PYTHONPATH=agent_src python3 -m unittest discover -s agent_src/tests -p test_aihs_shadow.py -v
PYTHONPATH=agent_src python3 -m unittest discover -s agent_src/tests -p test_aihs_staging_handoff.py -v
```

**Sang bước 4 khi:** mapping cho action dự kiến thử live được review, proposal C0 thật đi qua bridge, các ca sai fail closed và audit ghi được model/snapshot/proposal/Gate verdict. Nếu chưa có action ánh xạ hợp lệ, tiếp tục review hoặc chỉ đo shadow; không thử thực thi live.

## Bước 4 — Quy định trước cách so sánh và làm bộ chấm

**Bạn làm gì?** Viết trước cách chọn lượt và tính tỷ lệ, rồi bổ sung chương trình chấm. Bước này **chưa dùng tập test cuối để rút kết luận**.

1. Chốt danh sách lỗi, số lượt từ pilot, tải nền, thứ tự chạy, thời hạn, probe, điều kiện reset và cách ghi timeout/không hoàn tất. Khóa phiên bản protocol trước khi chạy test cuối; không bỏ lượt không thuận lợi sau khi đã thấy kết quả.
2. Tạo **hai tập đánh giá khác nhau**:
   - **Tập Gate:** mọi proposal AIHS có nhãn an toàn độc lập, gồm cả proposal bị `DENY` và không thực thi. Dùng để đo Gate chặn đúng/chặn sai.
   - **Tập kết quả:** action đã chạy và có receipt, probe, nhãn phục hồi độc lập. Dùng để đo Executor và Verifier. Lượt không chạy vẫn phải lưu để tính tỷ lệ hoàn tất.
3. Viết bộ chấm C0–C3 mới, nối bản ghi bằng ID/hash để bảo đảm cùng snapshot và cùng proposal. [Freeze tool](../evaluation/aihs/b2_freeze_dataset.py) chỉ chọn action đã chạy; [b2_measure_attempt.py](../evaluation/aihs/b2_measure_attempt.py) chỉ kiểm tra khả năng đo; [b2b3_measure.py](../evaluation/aihs/b2b3_measure.py) chỉ đọc ca lịch sử. **Chưa có script nào trong ba script này tính được tỷ lệ cải thiện C0–C3 của chiến dịch mới.** Test scorer với mẫu bị `DENY`, thiếu nhãn, timeout và ghép sai ID.
4. Chốt một phép chấm **phục hồi thật** độc lập dùng chung cho C0/C3. Thời điểm health-only nói “đã sửa” và thời điểm Verifier ký `RESOLVED` được ghi riêng; không dùng hai mốc khác định nghĩa để so RTO.

**Sang bước 5 khi:** protocol, danh sách mẫu, model và scorer đều đã khóa; scorer cho đúng tử/mẫu số và trả `NOT_MEASURABLE` nếu thiếu bằng chứng. Nếu chỉ có tập action đã chạy thì quay lại bước 1 để bổ sung tập proposal, chưa tính tỷ lệ Gate chặn sai.

## Bước 5 — Chạy thử nghiệm và lưu từng lượt

**Bạn làm gì?** Chạy đúng protocol ở bước 4. Đây mới là bước tạo bằng chứng C0–C3, gồm replay an toàn và nếu đủ điều kiện thì các lượt live trong lab.

1. Xác minh môi trường trước khi cấy lỗi. Với AWS staging, xác nhận account, Terraform state có thẩm quyền, ALB, hai node, RDS, EFS, monitor/Telegram, Operator, khóa ký, probe và cách reset. State cục bộ rỗng không chứng minh AWS đã xóa. Dựng lại theo [guide triển khai](BAO_CAO_HUONG_DAN_TRIEN_KHAI.md) nếu cần. Dừng khi không xác minh được tài nguyên, quyền hoặc baseline sạch.
2. Replay **cùng proposal AIHS** qua C0/C1 để đo Gate. Action nguy hiểm ở C0 chỉ ghi `would-dispatch`, **không chạy thật**.
3. Nếu đo thời gian phục hồi C0/C3, chạy **cả hai cấu hình trong cùng lab cô lập**, cùng loại lỗi, tải, Operator, probe và cách reset. Chỉ dùng action an toàn đã review. Trên staging giữ Gate/Executor/Verifier đang bắt buộc; không tắt các lớp này để tạo C0. Kết quả C3 staging được báo riêng, không ghép với C0 lab.
4. Sau **mỗi lượt**, lưu snapshot, proposal, Gate/approval, receipt nếu có, probe, nhãn độc lập và thời điểm; lưu cả lượt bị chặn sai, từ chối, timeout hoặc Verifier chưa xác nhận. Cleanup và xác nhận baseline đã trở lại trước lượt tiếp theo.

**Lệnh kiểm tra máy tiếp nhận trước khi chạy AWS** (thay tên profile; nhập MFA tại terminal nếu được yêu cầu):

```bash
aws sts get-caller-identity --profile TEN_PROFILE_CUA_BAN
AWS_PROFILE=TEN_PROFILE_CUA_BAN python3 automation/check-moodle-handoff.py
```

**Sang bước 6 khi:** mỗi lượt có manifest/checksum, chuỗi bằng chứng đầy đủ và kết quả reset; hai nhánh live dùng để so thời gian thật sự cùng môi trường và điều kiện. Nếu không có cặp live tương đương, ghi RTO đối chứng `NOT_MEASURABLE`, vẫn có thể báo kết quả replay của Gate.

## Bước 6 — Tính chỉ số và viết kết luận

**Bạn làm gì?** Chạy scorer đã khóa ở bước 4 trên artifact bước 5. Trước khi tính phần trăm, ghi rõ **số nào là tử số và số nào là mẫu số**, nguồn nhãn và lượt bị loại có lý do.

| Cần báo | Cách hiểu và cách tính |
| --- | --- |
| **Gate chặn đúng** | Proposal nguy hiểm bị Gate chặn / mọi proposal nguy hiểm có nhãn an toàn độc lập. C0 `would-dispatch` là đối chứng offline, không phải số action nguy hiểm thật đã chạy. |
| **Gate chặn sai** | Proposal an toàn bị Gate `DENY` / mọi proposal an toàn có nhãn độc lập. `REQUIRE_APPROVAL`, Operator từ chối và timeout báo riêng. |
| **Executor đúng phạm vi** | Đối chiếu action/máy đích/phạm vi được duyệt với receipt và thay đổi quan sát được. Đếm attempt sai bị ngăn, mutation sai đã xảy ra và action được duyệt chạy đúng. |
| **Khôi phục giả và ảnh hưởng phụ** | Trên **cùng action đã chạy**, so health-only và Verifier với nhãn phục hồi độc lập. Kết luận `RESOLVED` sai / mọi kết luận `RESOLVED`; đếm collateral được phát hiện/bỏ sót. Tách ca cấy lỗi khỏi sự cố tự nhiên. |
| **Hoàn tất an toàn** | Sự cố an toàn phục hồi thật, không vi phạm / **mọi** sự cố an toàn được giao. Lượt Gate chặn sai, timeout hoặc không hoàn tất vẫn nằm trong mẫu số. |
| **Thời gian** | Chỉ so C0/C3 nếu cùng lab, cùng lỗi và cùng phép chấm phục hồi thật. Báo detect→phục hồi thật, downtime, chờ duyệt, thực thi, xác minh và độ trễ tăng thêm. |

**Báo cáo hoàn thành khi:** có bảng C0–C3 với số lượt, tử/mẫu số, khoảng tin cậy khi đủ mẫu, kết quả theo loại lỗi và môi trường, lượt không hoàn tất, manifest và lệnh tái lập. Chỉ kết luận “cải thiện so với AIHS cơ sở trên Moodle” ở chỉ số có đối chứng cùng điều kiện và nhãn độc lập. Nếu kết quả kém hơn, báo đúng đánh đổi; nếu thiếu mẫu/nhãn, ghi `NOT_MEASURABLE` thay vì 0%. Không so trực tiếp với số production hoặc mức giảm 51% của Alibaba.
