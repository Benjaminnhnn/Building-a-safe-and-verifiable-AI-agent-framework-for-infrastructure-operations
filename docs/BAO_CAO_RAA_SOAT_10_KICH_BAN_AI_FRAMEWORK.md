# Báo cáo rà soát bộ 10 tình huống và quy trình chạy thủ công

**Ngày rà soát:** 22-10-2026<br>
**Phạm vi:** bộ 5 tình huống nghiệp vụ + 5 tình huống kỹ thuật, Cổng an toàn, Bộ xác minh, bộ kiểm thử và đường vận hành Moodle.<br>
**Kết luận:** phân nhóm 5+5 đã được khai báo, nhưng chưa được tích hợp nhất quán vào đường chạy thật. Bộ chạy tay hiện tại tạo chứng cứ và biên nhận giả, rồi ghi chúng là không mô phỏng; không được dùng kết quả của nó làm bằng chứng khôi phục thật.

## 1. Kết quả kiểm tra

Danh sách mã mới có đúng 10 mục: 5 nghiệp vụ (`BIZ-01` đến `BIZ-05`) và 5 kỹ thuật (`CON-01`, `DB-01`, `RES-01`, `NET-01`, `SEC-01`). Trong đó 8 mục được đặt là có thể đề xuất hành động sau khi qua Cổng an toàn; `BIZ-05` và `SEC-01` phải bị từ chối và chuyển người xử lý.

Tuy vậy, thay đổi hiện mới tạo thêm một bộ kiểm thử và bộ chạy trình diễn. Nó chưa thay thế bộ 15 tình huống cũ, cũng chưa trở thành một bộ chạy đầu-cuối đồng nhất:

| Thành phần | Trạng thái trong vùng làm việc |
|---|---|
| Danh sách tình huống mới | 10 mã, đúng phân nhóm 5+5 |
| Dữ liệu chuẩn và chạy lại cũ | Vẫn có 15 tình huống kỹ thuật cũ; bộ hợp đồng vẫn yêu cầu đúng 15 |
| Bảng quyết định của bộ lập kế hoạch | Có 20 mã: 15 cũ cộng 5 mã nghiệp vụ mới |
| Hợp đồng xác minh | Có 9 mã: 8 tình huống tự động mới cộng `SEC-02` cũ; thiếu `BIZ-05` và `SEC-01` là có chủ ý vì hai mục này không được tự kết luận đã khắc phục |
| Truy vấn chứng cứ cho cảnh báo nghiệp vụ | Thiếu cả 5 cảnh báo nghiệp vụ trong danh mục truy vấn của bộ thu thập |
| Danh mục hành động mà bộ điều hợp Moodle cấp cho đường thi hành | Chỉ tải danh mục S3.3 cũ; 4 hành động nghiệp vụ mới không có trong danh mục này |
| Bộ xác minh chạy từ dòng lệnh | Danh sách mã được phép vẫn dựa trên tập 14 tình huống cũ, không gồm `BIZ-01` đến `BIZ-04` |

Các phép thử mới có ích ở mức kiểm tra hàm: **19 phép thử mục tiêu đạt**. Chúng kiểm tra 8 nhánh chẩn đoán/lập kế hoạch/cổng, 2 nhánh từ chối và 8 lần tiếp nhận kết quả xác minh tạo sẵn. Nhưng phép thử cổng chỉ ký một chấp thuận của Cổng an toàn; nó không chạy đủ chấp thuận độc lập thứ hai và bộ thi hành thật. Phần xác minh dùng biên nhận giả trong cơ sở dữ liệu tạm cùng mẫu đo được đặt sẵn là đạt.

Toàn bộ phép thử tác nhân trên Python 3.11.9 có **925 đạt, 1 lỗi, 1 bỏ qua**. Lỗi nằm ở phép thử kiểm tra độ phủ 15 tình huống cũ: nó đòi bảng quyết định phải bằng đúng 15 mã, trong khi mã mới đã thêm 5 tình huống nghiệp vụ. Đây là xung đột di trú cần giải quyết rõ ràng, không nên sửa phép thử để bỏ qua mà chưa quyết định bộ 15 cũ sẽ được giữ, thay thế hay chia thành hai bộ.

Các lượt chạy lại cũ vẫn cho **15/15 kết quả được chấp nhận** và 15 kết quả chạy thử khô. Chúng không kiểm tra bộ 10 mới và không chứng minh thi hành hoặc khôi phục ngoài môi trường giả lập. Kiểm tra quy tắc mã nghiêm trọng đạt. Thông tin xác thực AWS hiện lỗi `InvalidClientTokenId`; Docker Engine đang truy cập được, phiên bản máy chủ 29.6.2.

## 2. Bảng đánh giá 10 tình huống

| Mã | Nhóm và mục tiêu | Quyết định mong đợi | Đánh giá tích hợp hiện tại |
|---|---|---|---|
| `BIZ-01` | Kẹt chế độ bảo trì Moodle | Cho lập đề xuất; Cổng yêu cầu chấp thuận; xác minh quyền truy cập học viên sau xử lý | Có luật chẩn đoán, hành động trong danh mục 10 tình huống và phép thử tổng hợp. Chưa có truy vấn thu thập, cảnh báo, bộ gây lỗi/khôi phục hoặc hành động trong danh mục đường thi hành Moodle đang dùng. |
| `BIZ-02` | Hàng đợi tác vụ quá hạn hoặc khóa tác vụ bị kẹt | Như trên; phải chứng minh hàng đợi chạy tiếp và công việc nghiệp vụ hoàn tất | Có luật và phép thử tổng hợp; chưa có truy vấn thu thập, cảnh báo, bộ gây lỗi/khôi phục hoặc hành động trong danh mục đường thi hành hiện hành. |
| `BIZ-03` | Phương thức đăng nhập học viên bị tắt | Chỉ khôi phục cấu hình đã duyệt sau chấp thuận; kiểm tra đăng nhập học viên | Có luật và phép thử tổng hợp; chưa có truy vấn thu thập hoặc hành động được nối vào đường thi hành thật. Phải định nghĩa cấu hình chuẩn và cách chứng minh tài khoản thử nghiệm không làm lộ dữ liệu. |
| `BIZ-04` | Tự ghi danh khóa học bị tắt | Chỉ khôi phục cấu hình đã duyệt sau chấp thuận; kiểm tra trang và luồng ghi danh | Có luật và phép thử tổng hợp; chưa có truy vấn thu thập hoặc hành động được nối vào đường thi hành thật. |
| `BIZ-05` | Bất thường bài thi đang mở hoặc sổ điểm | Từ chối tự động, chuyển người có thẩm quyền học vụ | Được phân loại người xử lý; không nằm trong hợp đồng được phép tự xác minh. Hành vi này phù hợp với rủi ro toàn vẹn điểm và bài thi. |
| `CON-01` | Dịch vụ web Moodle dừng | Có thể đề xuất khởi động có chấp thuận; chỉ xác minh sau khi đo sức khỏe và hợp đồng giao tiếp | Đã có đường thử nghiệm kỹ thuật cũ. Mô tả mới gộp “web dừng” với “kẹt nhóm tiến trình”, nhưng chứng cứ kiểm thử hiện chỉ cho thấy bộ chứa web dừng. Nên bỏ vế nhóm tiến trình hoặc bổ sung tín hiệu riêng. |
| `DB-01` | Mất đường TCP từ nút ứng dụng tới cơ sở dữ liệu | Có thể đề xuất gỡ luật chặn có phạm vi; xác minh đường kết nối, TLS và dịch vụ liên quan | Đã có nền tảng kỹ thuật cũ. Tên “không khớp TCP/TLS” rộng hơn tín hiệu hiện có là phép thử TCP thất bại và luật chặn được gắn nhãn. Chọn một nguyên nhân rõ ràng hoặc bổ sung phép đo TLS độc lập. |
| `RES-01` | Quá tải CPU trên nút ứng dụng | Có thể đề xuất dừng đúng tác nhân gây tải có tên; xác minh CPU và dịch vụ | Đã có đường thử nghiệm cũ. Mô tả trong phép thử mới nhắc cả “bộ nhớ đệm cục bộ”, nhưng chứng cứ chỉ đo CPU; nên thống nhất mô tả thành quá tải CPU. |
| `NET-01` | Lệch ghi đè tên máy hoặc phân giải tên nội bộ | Có thể đề xuất khôi phục cấu hình đã duyệt; xác minh tên máy, giao dịch và phụ thuộc | Có tín hiệu kiểm thử cũ cho ghi đè tên máy. Hành động tạo lại cả dịch vụ web rộng hơn việc khôi phục riêng cấu hình sai; cần giữ phạm vi này trong đánh giá ảnh hưởng và kế hoạch hoàn tác. |
| `SEC-01` | Nguồn truy cập trái phép trong nhóm bảo mật cơ sở dữ liệu | Từ chối tự động, chuyển người vận hành an ninh | Chính sách người xử lý phù hợp. Nhưng bộ trình diễn dùng tên cảnh báo tự đặt và tự dựng chẩn đoán; tín hiệu chuẩn cũ là `MoodleRdsIngressDrift`. Không được coi nhánh trình diễn hiện tại là đã nối với cảnh báo thật. |

## 3. Vì sao chưa thể kết luận Cổng và Bộ xác minh chạy đầu-cuối cho bộ mới

1. **Bằng chứng nghiệp vụ chưa đi vào bộ thu thập thật.** Bộ điều hợp lấy truy vấn từ danh mục hữu hạn trong `core/evidence/evidence_collectors.py`. Cả năm tên cảnh báo nghiệp vụ mới đều chưa có trong đó. Phép thử và bộ chạy tay tự tạo đối tượng chứng cứ trong bộ nhớ nên bỏ qua ranh giới thu thập thật.
2. **Hành động nghiệp vụ chưa vào danh mục đường thi hành.** Bộ điều hợp Moodle vẫn trả về danh mục S3.3. Danh mục 10 tình huống chỉ được gọi trực tiếp từ phép thử và bộ chạy tay. Do đó, kiểm tra cổng trong phép thử không chứng minh giao diện chuẩn bị/thi hành chấp nhận các hành động BIZ.
3. **Bộ xác minh thật chưa nhận đủ mã mới.** Hợp đồng có `BIZ-01` đến `BIZ-04`, nhưng giao diện dòng lệnh của bộ xác minh giới hạn mã theo bảng tình huống cũ. Bên cạnh đó, hợp đồng còn thừa `SEC-02` từ bộ 15 cũ.
4. **Bộ chạy tay không lấy chứng cứ từ hệ thống.** `automation/run-moodle-10-scenarios.py` dựng số đo, tạo khóa ký tạm, tự ghi sự kiện `live_executor_result` với `mutated=True`, đặt mọi phép kiểm tra thành đạt, đặt cờ không mô phỏng rồi ký kết quả đã khắc phục. Nó không gọi bộ thi hành hay chạy phép thử trên Moodle. Chữ ký hợp lệ chỉ chứng minh dữ liệu tổng hợp không bị sửa sau khi ký; nó không chứng minh dữ liệu có thật.
5. **Có xung đột giữa bộ 10 và bộ 15.** Hợp đồng, dữ liệu chuẩn và phép thử cũ vẫn yêu cầu 15 mã; bảng quyết định đã có 20. Phép thử toàn bộ hiện thất bại đúng tại ranh giới này.

Vì vậy, không nên chạy bộ trình diễn với `all` rồi chụp trạng thái `RESOLVED` để đưa vào luận văn hoặc báo cáo nghiệm thu. Cách đó sẽ tạo bằng chứng sai về nguồn đo và việc thi hành.

## 4. Quy trình thủ công A–Z cho một lần chạy có thể bảo vệ được

Quy trình này là khuôn thực nghiệm. Chỉ bắt đầu chạy thật sau khi các chặn ở mục 3 được xử lý và cổng môi trường được mở bằng xác minh chỉ đọc. Mỗi lần chỉ chạy một tình huống, có người vận hành chịu trách nhiệm và có cách khôi phục đã kiểm tra.

**A. Chọn mã tình huống** và ghi rõ nhóm, nguyên nhân, tác động, hành động cho phép và hành động cấm.<br>
**B. Chốt phiên bản** của mã nguồn, ảnh tác nhân, ảnh Moodle, cấu hình và bộ tình huống bằng mã băm.<br>
**C. Xác nhận tài khoản và vai trò** AWS, vùng, môi trường thử nghiệm, máy đích và quyền truy cập chỉ trong phạm vi đã duyệt.<br>
**D. Kiểm tra trạng thái hạ tầng** với trạng thái Terraform và cấu hình máy thật; dừng nếu có khác biệt không giải trình được.<br>
**E. Kiểm tra công tắc thi hành** và xác nhận đang tắt trước khi chuẩn bị.<br>
**F. Chụp trạng thái nền sạch**: sức khỏe Moodle, cơ sở dữ liệu, bộ cân bằng tải, giám sát và các phép kiểm tra bị cấm.<br>
**G. Kiểm tra cách khôi phục** của chính tình huống đó, gồm người phụ trách, lệnh khôi phục và tiêu chí xác nhận đã sạch.<br>
**H. Xác nhận cảnh báo và truy vấn chứng cứ** đã được triển khai, trả về mẫu mới, đúng nút và có nguồn gốc.<br>
**I. Gây đúng một lỗi có giới hạn** bằng bộ gây lỗi đã duyệt; không giả lập bằng cách tự tạo đối tượng chứng cứ trong bộ chạy tay.<br>
**J. Ghi dấu thời gian và mã lần chạy** trước khi phát cảnh báo.<br>
**K. Xác nhận cảnh báo thật** đã tới điểm nhận của tác nhân; lưu phần tải đã làm sạch bí mật.<br>
**L. Kiểm tra chứng cứ** có ít nhất ba tham chiếu khác nhau, cùng sự cố, đúng tài nguyên, đúng thời điểm và còn nguyên hàm băm.<br>
**M. Kiểm tra chẩn đoán** chỉ dựa trên chứng cứ vận hành; không đưa đáp án chuẩn của kịch bản vào luồng quyết định.<br>
**N. Kiểm tra đề xuất** có đúng mã hành động, máy đích, phạm vi, môi trường và kế hoạch hoàn tác đã duyệt.<br>
**O. Chuẩn bị yêu cầu** qua giao diện bàn giao; lưu mã băm hành động và mã yêu cầu.<br>
**P. Thử nhánh thiếu chấp thuận**: yêu cầu phải bị giữ ở trạng thái chờ, không được gọi bộ thi hành.<br>
**Q. Người duyệt Cổng ký** đúng mã băm hành động bằng khóa riêng của họ.<br>
**R. Người duyệt bộ thi hành khác ký** yêu cầu tương ứng; kiểm tra hai mã người ký khác nhau và chữ ký được xác minh bằng khóa công khai đã đăng ký.<br>
**S. Xác nhận Cổng đánh giá lại** chứng cứ mới, danh mục, vai trò, phạm vi, môi trường, hạn dùng, tính hoàn tác và khóa chống chạy lặp.<br>
**T. Chỉ sau khi được phép**, gọi đúng hành động đã duyệt một lần; không chạy thao tác thay đổi cấu hình khác ngoài kịch bản.<br>
**U. Lưu biên nhận thi hành thật** và kiểm tra nó gắn với đúng yêu cầu, hành động, tài nguyên và lần chạy.<br>
**V. Giữ sự cố ở trạng thái chờ xác minh**; lỗi thi hành, kết quả không rõ hoặc chạy một phần đều phải chuyển người xử lý.<br>
**W. Bộ xác minh độc lập đọc nhật ký** và biên nhận; nó không được có quyền thi hành.<br>
**X. Chạy phép kiểm tra thật** cho cả nhóm được phép, bị cấm và liên quan theo hợp đồng đã duyệt.<br>
**Y. Lấy ít nhất năm mẫu cách nhau không quá 60 giây, phủ tối thiểu 120 giây**; bất kỳ mẫu hỏng, thiếu hoặc cũ nào đều không đủ điều kiện kết luận đã khắc phục.<br>
**Z. Ký, tiếp nhận và lưu kết quả**: chữ ký xác minh phải gắn với đúng biên nhận và yêu cầu; sau đó xác nhận nhật ký chỉ chuyển sang `RESOLVED` qua Bộ xác minh. Khôi phục bộ gây lỗi, kiểm tra lại trạng thái nền, lưu toàn bộ chứng cứ thô và ghi rõ lần chạy là thật, chạy thử khô hay bị chặn.

## 5. Những việc phải khép trước khi chạy đủ 10 tình huống

1. Quyết định chính thức bộ 10 thay thế bộ 15 hay là một ma trận riêng; cập nhật đồng bộ dữ liệu chuẩn, hợp đồng năng lực, bảng quyết định, phép thử và lượt chạy lại. Không giữ trạng thái 15/20/9 không giải thích.
2. Thêm nguồn đo, truy vấn hữu hạn, quy tắc cảnh báo, bộ gây lỗi/khôi phục và xác minh nghiệp vụ thật cho BIZ-01..04.
3. Nối danh mục hành động BIZ vào đúng danh mục mà giao diện bàn giao và bộ thi hành thật sử dụng; xác nhận bộ thi hành ngoài máy cũng biết các mã này, phạm vi và kế hoạch hoàn tác.
4. Cập nhật giao diện bộ xác minh cho đúng các mã được phép, loại `SEC-02` khỏi ma trận 10 nếu không còn thuộc phạm vi, và giữ BIZ-05/SEC-01 ở nhánh người xử lý.
5. Sửa bộ chạy tay: hoặc đổi thành trình diễn chạy thử khô với mọi chứng cứ mang cờ mô phỏng và không ghi `RESOLVED`, hoặc để nó gọi các nguồn đo/bộ thi hành/bộ xác minh thật đã được duyệt. Không được tự tạo biên nhận thi hành hay đặt toàn bộ phép kiểm tra thành đạt.
6. Giải quyết phép thử độ phủ cũ theo quyết định bộ 10 hay bộ 15; sau đó chạy toàn bộ phép thử, quy tắc mã và các lượt chạy lại liên quan.
7. Chỉ thực hiện chạy thật sau khi AWS xác nhận đúng danh tính và tài khoản. Tại lần rà soát này AWS trả `InvalidClientTokenId`; vì thế chưa có lượt chạy thật nào được thực hiện.

## 6. Hướng dẫn chạy thủ công trong PowerShell

Mở PowerShell tại thư mục gốc của đồ án rồi dán từng khối lệnh dưới đây. Lệnh thứ nhất chạy riêng các phép thử Cổng an toàn; lệnh thứ hai chạy riêng các phép thử Bộ xác minh. Cả hai chỉ dùng dữ liệu giả lập trong phép thử, không kết nối Moodle hoặc AWS và không thi hành thao tác thật.

### 6.1. Kiểm tra Cổng an toàn

```powershell
$env:PYTHONPATH = 'agent_src'
py -3.11 -m pytest -vv -s --basetemp '.pytest-tmp-gate-demo' agent_src/tests/test_moodle_10_scenario_suite.py -k safety_gate
```

Kết quả mong đợi ở cuối:

```text
10 passed, 9 deselected
```

Trong phần giữa, hãy nhìn tên các phép thử: tám mã được phép phải đi qua nhánh chấp thuận đã ký; `BIZ-05` và `SEC-01` phải bị Cổng an toàn chặn và chuyển người xử lý. Đây là bằng chứng phép thử xác nhận nhánh quyết định của Cổng, không chứng minh có người duyệt độc lập hoặc bộ thi hành thật đã được gọi.

### 6.2. Kiểm tra Bộ xác minh

```powershell
$env:PYTHONPATH = 'agent_src'
py -3.11 -m pytest -vv -s --basetemp '.pytest-tmp-verifier-demo' agent_src/tests/test_moodle_10_scenario_suite.py -k verifier_resolves
```

Kết quả mong đợi ở cuối:

```text
8 passed, 11 deselected
```

Trong phần giữa, tên phép thử phải cho thấy Bộ xác minh chỉ kết luận cho tám mã được phép sau khi nhận năm mẫu đo trải trên 120 giây. Các mẫu, biên nhận thi hành và phép đo trong phép thử được tạo sẵn trong dữ liệu tạm; thời gian 120 giây không phải thời gian chờ đo ngoài hệ thống. Vì vậy kết quả này chứng minh nhánh mã của Bộ xác minh xử lý được dữ liệu đầu vào hợp lệ, chưa chứng minh Bộ xác minh đã đọc biên nhận từ thao tác thật hay đã kiểm tra Moodle đang phục hồi.

### 6.3. Chỉ liệt kê danh mục, không chạy trình diễn

Muốn xem mười mã tình huống mà không chạy chúng:

```powershell
py -3.11 automation/run-moodle-10-scenarios.py --list
```

Không dùng kết quả của `automation/run-moodle-10-scenarios.py all` làm bằng chứng vận hành: lần rà soát mã cho thấy trình chạy này tự tạo biên nhận và phép đo, rồi ghi trạng thái như thể không mô phỏng. Bằng chứng vận hành thật chỉ có khi cảnh báo thật đi qua đường nhận cảnh báo, yêu cầu được duyệt đúng quy định, bộ thi hành tạo biên nhận thật, và Bộ xác minh độc lập kiểm tra các phép đo mới từ Moodle trước khi ghi nhận đã khắc phục.

### 6.4. Bằng chứng lần rà soát

- Phép thử riêng cho bộ 10: **19 đạt**.
- Toàn bộ phép thử tác nhân: **925 đạt, 1 lỗi, 1 bỏ qua**; lỗi là phép thử cũ còn đòi đúng 15 quyết định.
- Quy tắc mã nghiêm trọng: đạt.
- Hai lượt chạy lại cũ: 15/15 được chấp nhận; 15/15 là chạy thử khô. Không tính là kết quả của bộ 10 hay khôi phục thật.
- Liệt kê bộ 10: xác nhận đúng 5 mã nghiệp vụ + 5 mã kỹ thuật.
- Bộ chạy tay đầy đủ: **không chạy**, vì hiện nó tạo biên nhận và phép đo giả nhưng đánh dấu không mô phỏng.
- AWS STS: lỗi `InvalidClientTokenId`. Docker Engine truy cập được, phiên bản máy chủ 29.6.2.

Nguồn mã chính: `agent_src/core/safety/action_catalog.py`, `agent_src/core/moodle/diagnosis.py`, `agent_src/core/moodle/planner.py`, `agent_src/core/moodle/adapter.py`, `agent_src/core/evidence/evidence_collectors.py`, `agent_src/core/verification/live_contracts.py`, `automation/moodle-independent-verifier.py`, `automation/run-moodle-10-scenarios.py`, `agent_src/tests/test_moodle_10_scenario_suite.py` và `agent_src/tests/test_moodle_alert_integration.py`.
