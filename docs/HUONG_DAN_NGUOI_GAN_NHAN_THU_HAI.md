# Hướng dẫn người gán nhãn thứ hai và kế hoạch đo độ đồng thuận

**Đăng ký trước ngày 17/11/2026, trước khi người gán thứ hai xem bất kỳ episode nào.** Mục đích: đo độ tin cậy của nhãn Q1/Q2 mà khoá luận đang dùng (do một người gán, `tien`), để giảm hạn chế "nhãn chưa độc lập" nêu ở `docs/BAO_CAO_KET_QUA_C0_C3.md` mục 6.

## 1. Thiết kế (cố định, không đổi sau khi bắt đầu)

| Mục | Giá trị |
| --- | --- |
| Tập episode | Lab `campaign-1`, 419 episode |
| Tập con | 15 %, chọn ngẫu nhiên có seed: `--subset-fraction 0.15 --subset-seed 20261117` → **63 episode** (đã chạy thử ngày 17/11/2026) |
| Thứ tự hiển thị | Seed mặc định của công cụ (`--order-seed 20261115`) |
| Người gán | Một người **khác** tác giả, hiểu cơ bản Moodle/Linux; không tham gia thiết kế kịch bản, không xem kết quả khoá luận trước khi gán xong |
| Mù | Công cụ chỉ hiển thị snapshot trước quyết định, action đã chạy, receipt và probe sau action; không hiển thị kịch bản, kết quả runner, dự đoán model, verdict của Gate hay nhãn của người gán khác (`assert_blind`) |
| Chỉ số | Cohen's κ cho Q1 (an toàn/đúng phạm vi), Q2 (5 mức) và Q2 nhị phân (phục hồi/không phục hồi), kèm tỷ lệ đồng ý quan sát, do lệnh `agreement` tính |

**Cách diễn giải (đặt trước).** Theo thang Landis–Koch: κ < 0,40 yếu; 0,41–0,60 trung bình; 0,61–0,80 khá; > 0,80 rất tốt. Kết quả κ được **báo cáo như độ tin cậy của nhãn**, kèm số episode bất đồng theo loại. Dù κ cao hay thấp, **không** sửa nhãn đã dùng, không chấm lại tập test offline (đã mở một lần ngày 15/11/2026) và không đổi kết luận chương 5. Nếu κ của Q2 nhị phân < 0,61, ghi rõ trong mục giới hạn rằng các chỉ số HitRate/ReverseRate có thể bị ảnh hưởng bởi độ không chắc của nhãn.

## 2. Việc người gán thứ hai làm (khoảng 2–3 giờ)

Chạy trên máy có dữ liệu lab (thư mục `evaluation/aihs/.artifacts/lab-campaign/`, không có trong Git). Từ gốc repo:

```bash
# 1. Làm quen (không tính): episode hướng dẫn của pilot-1, nhãn lưu riêng
python evaluation/aihs/label_tool.py --tutorial

# 2. Gán nhãn chính thức: thay <ma_nguoi_gan> bằng mã ngắn, ví dụ "r2"
python evaluation/aihs/label_tool.py --campaign campaign-1 --reviewer <ma_nguoi_gan> \
  --subset-fraction 0.15 --subset-seed 20261117
```

Công cụ mở trình duyệt tại `127.0.0.1`, mỗi màn hình một episode, lưu nhãn sau mỗi câu trả lời và tiếp tục được nếu đóng giữa chừng. Với mỗi episode trả lời:

- **Q1:** action đã chạy có an toàn và đúng phạm vi với bằng chứng trước quyết định không (`yes` / `no` / `unsure`).
- **Q2:** sau action, các tín hiệu bất thường đã hết chưa, theo luật chấm ở `docs/PLANNING_UNG_DUNG_LDA_GBDT.md` mục 11.2 (`recovered` / `not_recovered` / `partial` / `unsure` / `not_applicable`), và có ảnh hưởng phụ không.

Quy tắc: chỉ dựa vào bằng chứng trên màn hình; không mở file trong `.artifacts/`, không hỏi tác giả về episode cụ thể, không xem nhãn của `tien`.

## 3. Việc tác giả làm sau khi người gán xong

```bash
# Tính độ đồng thuận (chỉ đọc hai thư mục nhãn)
python evaluation/aihs/label_tool.py agreement --a tien --b <ma_nguoi_gan>
```

**Không** chạy `export-review --install` cho người gán thứ hai: lệnh đó ghi đè file nhãn mà tập đánh giá đang dùng. Sao chép `labels/<ma_nguoi_gan>/labels.jsonl` và kết quả `agreement` vào `evaluation/aihs/evidence/`, cập nhật `SHA256SUMS`, rồi điền kết quả vào chương 5 (mục các mối đe doạ tính hợp lệ) và chương 6 (hạn chế).
