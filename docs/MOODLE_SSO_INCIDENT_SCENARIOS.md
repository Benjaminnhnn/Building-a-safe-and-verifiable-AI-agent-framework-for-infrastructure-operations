# Kịch bản sự cố xác thực Moodle ← Okta ← Entra ID

**Phạm vi:** luồng đăng nhập OIDC Moodle ← Okta, và luồng đồng bộ danh tính Okta ← Entra ID, triển khai theo [MOODLE_ENTRA_OKTA_OIDC_IMPLEMENTATION.md](MOODLE_ENTRA_OKTA_OIDC_IMPLEMENTATION.md).
**Mã kịch bản:** `SSO-xx`. Mã `AUTH-xx` từng dùng cho lab OpenLDAP, đã gỡ khỏi repo ngày 14/10/2026; không tái sử dụng mã này để tránh nhầm với dữ liệu cũ.
**Mục tiêu:** (1) danh mục sự cố xếp theo mức độ, dùng cho diễn tập và làm ground truth cho AI agent; (2) **rèn tư duy chẩn đoán**: mỗi kịch bản đều đi theo cùng một phương pháp.

---

## Mục lục

1. [Phương pháp chẩn đoán dùng chung](#1-phương-pháp-chẩn-đoán-dùng-chung)
2. [Bản đồ luồng và nơi lấy bằng chứng](#2-bản-đồ-luồng-và-nơi-lấy-bằng-chứng)
3. [Thang mức độ](#3-thang-mức-độ)
4. [Danh mục kịch bản](#4-danh-mục-kịch-bản)
5. [Mức 1: một user](#5-mức-1-một-user)
6. [Mức 2: cấu hình ứng dụng, mọi user](#6-mức-2-cấu-hình-ứng-dụng-mọi-user)
7. [Mức 3: đồng bộ danh tính Entra → Okta](#7-mức-3-đồng-bộ-danh-tính-entra--okta)
8. [Mức 4: hạ tầng và phụ thuộc](#8-mức-4-hạ-tầng-và-phụ-thuộc)
9. [Mức 5: sự cố bảo mật](#9-mức-5-sự-cố-bảo-mật)
10. [Giả thuyết hay bị nghĩ tới nhưng không áp dụng](#10-giả-thuyết-hay-bị-nghĩ-tới-nhưng-không-áp-dụng)
11. [Diễn tập an toàn trên staging](#11-diễn-tập-an-toàn-trên-staging)
12. [Bài tập tự luyện](#12-bài-tập-tự-luyện)

---

## 1. Phương pháp chẩn đoán dùng chung

Mọi kịch bản bên dưới đều đi theo **7 bước** này. Mục tiêu là đọc kịch bản để luyện cách nghĩ, không phải để học thuộc đáp án.

| Bước | Câu hỏi tự đặt ra | Sai lầm thường gặp |
|---|---|---|
| **1. Mô tả triệu chứng chính xác** | Thông báo lỗi nguyên văn là gì? Hiện ở **domain nào** trên thanh địa chỉ? Mã HTTP bao nhiêu? Lúc mấy giờ? | Chỉ nói "không đăng nhập được". Câu này đúng với ít nhất 15 nguyên nhân khác nhau |
| **2. Khoanh phạm vi** | **Một user**, **một nhóm**, hay **tất cả**? Lỗi mới xảy ra hay đã có từ trước? Gần đây có ai thay đổi gì không? | Bỏ qua bước này. Phạm vi ảnh hưởng gần như quyết định ngay **mức độ** của sự cố (mục 3) |
| **3. Định vị trong luồng** | Lỗi xảy ra ở **bước nào trong 8 bước** của luồng OIDC (mục 2)? Trang lỗi thuộc về **Moodle hay Okta**? | Sửa ở Moodle trong khi lỗi nằm ở Okta, hoặc ngược lại |
| **4. Liệt kê giả thuyết** | Ở bước đó có thể hỏng những gì? Viết ra **ít nhất 2 giả thuyết** | Chốt ngay giả thuyết đầu tiên nghĩ ra |
| **5. Chọn phép thử phân biệt** | Phép thử **rẻ nhất** nào cho **kết quả khác nhau** tùy giả thuyết nào đúng? **Trước khi chạy, đoán trước** kết quả cho từng giả thuyết | Chạy phép thử mà giả thuyết nào đúng cũng cho cùng kết quả |
| **6. Xác nhận root cause rồi mới sửa** | Có **bằng chứng** (log, request ID, so sánh) không? Có phép **đối chứng** không: một trường hợp tương tự đang chạy đúng? | Sửa theo cảm giác, hoặc "làm lại từ đầu" (tạo org mới, redeploy) khi chưa biết nguyên nhân |
| **7. Kiểm chứng và rút kinh nghiệm** | Lỗi hết thật chưa, **kiểm tra bằng chính phép thử đã fail**? Cần thêm giám sát gì để lần sau phát hiện sớm hơn? | Tuyên bố "đã sửa" chỉ vì không còn thấy lỗi |

### Ví dụ thật: sự cố "org Okta bị hỏng" (10–11/10/2026)

| Bước | Đã làm | Lẽ ra nên làm |
|---|---|---|
| 1. Triệu chứng | API token `E0000011`, discovery 403, `/authorize` 400 trống | ✅ Ghi đủ |
| 2. Phạm vi | Mọi API, mọi client, mọi user | ⚠️ Lỗi xảy ra với **mọi thứ** trong khi Admin Console vẫn chạy bình thường. Đây là dấu hiệu nên nghi ngờ **chính cách ta gọi tới org**, trước khi nghi org |
| 4. Giả thuyết | Chỉ có một: "org trial bị lỗi" | ❌ Thiếu giả thuyết "đang gọi nhầm đối tượng" |
| 5. Phép thử | Thử nhiều tham số khác nhau, nhưng **đều trên cùng một domain** | ❌ Mọi biến thể đều chung một biến chưa kiểm chứng là **domain**. Phép thử phân biệt rẻ nhất: **so domain trên thanh địa chỉ của Admin Console** với domain đang gọi |
| 6. Root cause | Có lúc đã đề xuất tạo org mới | ❌ Đó là "làm lại" khi chưa có bằng chứng. Nếu làm, lỗi sẽ lặp lại y hệt |
| Kết quả | Thanh địa chỉ cho thấy `trial-3489054-admin.okta.com`, khác `galaxyfinx-trial-3489054` | ✅ Khi đổi đúng domain, mọi endpoint chạy |

**Bài học:** khi **mọi thứ** cùng hỏng một lúc, hãy nghi ngờ **thứ chung nhất mà mọi phép thử đều dùng**, ở đây là domain, rồi kiểm chứng nó trước tiên.

---

## 2. Bản đồ luồng và nơi lấy bằng chứng

```
 ①  Trình duyệt → Moodle /login/index.php            (trang của Moodle)
 ②  Bấm "Sign in by Okta" → 302 → Okta /oauth2/v1/authorize
 ③  Okta: mật khẩu + Okta Verify + kiểm tra assignment & policy   (trang của Okta)
 ④  Okta → 302 → Moodle /admin/oauth2callback.php?code=…&state=…
 ⑤  Moodle server → Okta /oauth2/v1/token     (server-to-server, qua NAT, cổng 443)
 ⑥  Moodle server → Okta /oauth2/v1/userinfo  (server-to-server)
 ⑦  Moodle: tìm/tạo account theo (issuer, sub), áp field mapping, login domain
 ⑧  Moodle: session + role/enrolment → user thấy course

 Nền (không có user): Entra ID ──Graph import (Full / Incremental)──► Okta user + group
```

| Bước | Trang lỗi thuộc về | Bằng chứng chính | Lệnh / nơi xem |
|---|---|---|---|
| ① ② | Moodle | Trạng thái OAuth 2 service (cột Login), log Apache | *Site admin → Server → OAuth 2 services*; `docker logs release-moodle-web-1` |
| ② → ③ | **Okta** | URL authorize Moodle sinh ra; trang lỗi Okta có hay không có mô tả | Copy URL trên thanh địa chỉ; `curl https://trial-3489054.okta.com/.well-known/openid-configuration` |
| ③ | **Okta** | **System Log** theo username | *Okta Admin → Reports → System Log*, lọc theo user và khoảng thời gian |
| ④ | Okta, rồi Moodle | Query `error=` / `error_description=` trên URL callback | Thanh địa chỉ lúc quay về Moodle |
| ⑤ ⑥ | Moodle (lỗi chung chung) | Log PHP và Apache; kết nối ra ngoài | `ssh moodle-app-a 'curl -s -o /dev/null -w "%{http_code}" -X POST https://trial-3489054.okta.com/oauth2/v1/token'` (kỳ vọng 400 hoặc 401, không phải timeout) |
| ⑦ | Moodle | Bảng `auth_oauth2_linked_login`, cấu hình issuer | PHP CLI đọc DB (xem phụ lục của tài liệu triển khai) |
| ⑧ | Moodle | Role và enrolment của user | *Course → Participants*; *Site admin → Users* |
| Nền | Okta | Lịch sử import, trạng thái user và group | *Office 365 app → Import*; *Directory → People / Groups* |

**Quy tắc vàng ở bước 3:** đọc **thanh địa chỉ**. Lỗi trên `trial-3489054.okta.com` là lỗi phía Okta. Lỗi trên `moodle-staging-…` là lỗi phía Moodle, hoặc ở các bước ⑤ đến ⑧.

---

## 3. Thang mức độ

| Mức | Tên | Phạm vi ảnh hưởng | Câu hỏi nhận diện | Ai xử lý | AI agent được làm gì |
|---|---|---|---|---|---|
| **1** | Một user | 1 người | "Người khác có đăng nhập được không?" → **Có** | Helpdesk / IAM admin | Chẩn đoán, đề xuất. **Không** tự reset mật khẩu hay MFA |
| **2** | Cấu hình ứng dụng | **Mọi** user của Moodle SSO | "Có ai đăng nhập được không?" → **Không**, và Okta Dashboard vẫn dùng được | Moodle admin / IAM admin | Chẩn đoán, so sánh với cấu hình chuẩn, đề xuất. **Không** tự sửa cấu hình IdP |
| **3** | Đồng bộ danh tính | Một **nhóm** user, hoặc sai lệch về **thời điểm** | "Trạng thái trên Okta có khớp Entra không?" → **Không** | IAM admin | Chẩn đoán độ trễ hoặc sai lệch, đề xuất chạy import |
| **4** | Hạ tầng và phụ thuộc | Mọi user, **kể cả** ngoài SSO | "Moodle hoặc Okta có sống không? Đường mạng có thông không?" | Infrastructure Engineer | Chẩn đoán; chỉ thực thi các hành động trong allowlist có sẵn (ví dụ gỡ một rule chặn do diễn tập tạo ra) |
| **5** | Bảo mật | Có thể là 1 user, nhưng **rủi ro cao** | "Có ai truy cập được thứ lẽ ra không được không? Có bí mật nào bị lộ không?" | Security / IAM admin | **HUMAN_ONLY**: chỉ cảnh báo và thu thập bằng chứng |

**Phạm vi (bước 2) gần như quyết định mức độ.** Vì vậy luôn hỏi *"người khác có bị không?"* trước khi đào sâu.

---

## 4. Danh mục kịch bản

| Mã | Tên | Mức | Bước lỗi | Diễn tập được trên staging? |
|---|---|---|---|---|
| SSO-01 | Sai mật khẩu Okta hoặc dùng mật khẩu Entra | 1 | ③ | ✅ |
| SSO-02 | User đã import nhưng chưa kích hoạt (Staged) | 1 | ③ | ✅ |
| SSO-03 | User bị khóa sau nhiều lần nhập sai | 1 | ③ | ✅ |
| SSO-04 | User không được assign app (không thuộc group) | 1 | ③ ④ | ✅ |
| SSO-05 | User chưa đăng ký hoặc mất thiết bị Okta Verify | 1 | ③ | ✅ (reset authenticator của user test) |
| SSO-10 | Sai domain Okta trong endpoint của Moodle | 2 | ② | ✅ (đã xảy ra thật) |
| SSO-11 | `redirect_uri` không khớp | 2 | ② ③ | ✅ |
| SSO-12 | Client secret sai hoặc đã xoay vòng | 2 | ⑤ | ✅ |
| SSO-13 | Plugin OAuth 2 hoặc service bị tắt | 2 | ① | ✅ |
| SSO-14 | Field mapping hoặc login domain sai, Moodle không tạo được account | 2 | ⑦ | ✅ |
| SSO-15 | Policy MFA chặn tất cả (Catch-all Deny, rule mất group) | 2 | ③ | ✅ |
| SSO-20 | Mất consent Microsoft Graph, import thất bại | 3 | Nền | ⚠️ Chỉ trên tenant demo |
| SSO-21 | Thiếu thuộc tính bắt buộc khi import | 3 | Nền | ✅ (đã xảy ra thật) |
| SSO-22 | User đã disable ở Entra nhưng vẫn đăng nhập được | 3 | Nền ③ | ✅ |
| SSO-23 | Đổi group ở Entra nhưng quyền trên Okta chưa đổi | 3 | Nền ③ | ✅ |
| SSO-30 | Moodle server không ra được Internet (egress 443) | 4 | ⑤ ⑥ | ✅ có giới hạn |
| SSO-31 | Moodle hoặc database lỗi, đăng nhập Okta xong vẫn hỏng | 4 | ⑦ ⑧ | ✅ (tái dùng DB-01) |
| SSO-32 | Okta không khả dụng | 4 | ② ③ | ❌ Chỉ mô phỏng (xem SSO-30) |
| SSO-40 | Client secret bị lộ | 5 | — | ⚠️ Chỉ diễn tập trên giấy (tabletop) |
| SSO-41 | User bị chặn lại truy cập được (assign nhầm, policy nới lỏng) | 5 | ③ | ✅ |
| SSO-42 | Chiếm account qua trùng email | 5 | ⑦ | ⚠️ Kiểm thử phòng ngừa |

Khoảng trống giữa các mã (06–09, 16–19…) được chừa sẵn để bổ sung kịch bản cùng mức sau này.

---

## 5. Mức 1: một user

### SSO-01. Sai mật khẩu Okta hoặc dùng mật khẩu Entra

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Trang Okta: *"Không thể đăng nhập"* / *"Unable to sign in"*. Các user khác vẫn vào được |
| **Định vị** | Bước ③, trên domain Okta |
| **Giả thuyết** | (a) gõ sai mật khẩu; (b) dùng **mật khẩu Entra**, vốn không được đồng bộ sang Okta; (c) user chưa Active (SSO-02); (d) đăng nhập nhầm org (SSO-10) |
| **Phép thử phân biệt** | System Log, lọc theo user: `INVALID_CREDENTIALS` → (a) hoặc (b); **không có event nào** → (d), request không tới đúng org; user ở trạng thái *Staged* → (c) |
| **Root cause** | Thiết kế import qua Graph **không chép mật khẩu**. User có mật khẩu Okta riêng |
| **Khắc phục được phép** | Hướng dẫn user dùng mật khẩu Okta; *Reset password* qua quy trình xác minh danh tính |
| **Bị cấm** | Đặt lại mật khẩu khi chưa xác minh người yêu cầu; tắt MFA để user "vào cho nhanh" |
| **Tiêu chí phục hồi** | Có event `user.session.start` SUCCESS trong System Log; user vào được Moodle |
| **Giám sát** | Cảnh báo khi một user có ≥ N lần `INVALID_CREDENTIALS` trong 10 phút (cũng phục vụ SSO-03) |

### SSO-02. User đã import nhưng chưa kích hoạt (Staged)

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Như SSO-01. Trên Admin Console, trang user có nút **Activate**, không có *Set password* |
| **Giả thuyết** | (a) Staged; (b) *Pending user action*: đã kích hoạt nhưng chưa đặt mật khẩu |
| **Phép thử** | Đọc trạng thái user ngay dưới tên trên trang user |
| **Root cause** | Import xong chỉ tạo user ở trạng thái Staged; còn bỏ tick auto-activate là **cố ý**, để kiểm soát ai được kích hoạt |
| **Khắc phục** | *Activate* (không gửi email nếu user không có hộp thư), rồi *Set Password & Activate* |
| **Bị cấm** | Bật **auto-activate cho mọi user import**. Cả tenant sẽ bị kích hoạt, vượt giới hạn 10 user của gói trial |
| **Tiêu chí phục hồi** | Trạng thái **Active**; đăng nhập thành công |

### SSO-03. User bị khóa sau nhiều lần nhập sai

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Okta báo tài khoản bị khóa, hoặc vẫn chỉ *"Không thể đăng nhập"* dù đã nhập đúng mật khẩu |
| **Giả thuyết** | (a) Locked out; (b) vẫn sai mật khẩu |
| **Phép thử** | Trạng thái user là **Locked out**; System Log có event khóa account |
| **Root cause** | Password policy khóa account sau N lần nhập sai. Đây là cơ chế bảo vệ, không phải lỗi |
| **Khắc phục** | *Unlock* sau khi xác minh chính chủ |
| **Câu hỏi tư duy** | Nếu **nhiều** user bị khóa cùng lúc thì sao? Đó không còn là Mức 1, mà có thể là **tấn công dò mật khẩu**. Nâng lên **Mức 5** |

### SSO-04. User không được assign app (không thuộc group)

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Đăng nhập Okta thành công, nhưng không về được Moodle: Okta báo user không được truy cập ứng dụng, hoặc URL callback có `error=access_denied` |
| **Định vị** | Cuối bước ③ hoặc bước ④ |
| **Giả thuyết** | (a) user không thuộc `moodle-students` / `moodle-teachers` trên **Okta**; (b) đã thêm vào group trên **Entra** nhưng **chưa import** (SSO-23); (c) policy MFA không chứa group của user (SSO-15) |
| **Phép thử** | Tab **Applications** của user trên Okta: có `Moodle` không? Tab **Groups** có group mong đợi không? So với group membership trên Entra |
| **Root cause** | Assignment theo group là **lớp kiểm soát truy cập thứ nhất**. Không thuộc group thì không được vào |
| **Khắc phục** | Thêm user vào group **trên Entra** (nguồn), rồi chạy import. **Không** assign trực tiếp trên Okta |
| **Bị cấm** | Assign app cho `Everyone`; assign riêng lẻ từng user để lách quy trình |
| **Ghi chú** | Đây là hành vi **đúng** với user trong `moodle-denied`. Khi diễn tập, kết quả mong đợi là **bị chặn** |

### SSO-05. User chưa đăng ký hoặc mất thiết bị Okta Verify

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Nhập mật khẩu xong thì bị yêu cầu đăng ký Okta Verify, hoặc push không về điện thoại |
| **Giả thuyết** | (a) lần đầu đăng nhập, chưa đăng ký; (b) đổi hoặc mất điện thoại; (c) giờ trên điện thoại lệch nhiều, khiến mã TOTP sai |
| **Phép thử** | Trang user → tab authenticators: Okta Verify đã đăng ký chưa? Thử nhập mã TOTP thay cho push |
| **Khắc phục** | (a) đăng ký theo hướng dẫn; (b) *Reset Authenticators* **sau khi xác minh chính chủ**; (c) bật đồng bộ giờ tự động trên điện thoại |
| **Bị cấm** | Gỡ yêu cầu MFA khỏi policy để user vào tạm |

---

## 6. Mức 2: cấu hình ứng dụng, mọi user

**Dấu hiệu chung:** **không ai** đăng nhập Moodle qua Okta được, nhưng mọi người vẫn đăng nhập **Okta Dashboard** bình thường. Phép thử phân biệt Mức 2 với Mức 4: Okta Dashboard và Moodle `/healthz.php` đều chạy thì lỗi nằm ở **kết nối giữa hai bên**, tức là cấu hình.

### SSO-10. Sai domain Okta trong endpoint của Moodle (đã xảy ra thật)

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Bấm *Sign in by Okta* thì hiện trang **400 không kèm mô tả**. Discovery trả 403. API token và service app đều báo invalid |
| **Định vị** | Bước ②: request **chưa tới đúng org** |
| **Giả thuyết** | (a) org Okta bị lỗi; (b) **gọi nhầm org**; (c) client không tồn tại hoặc không Active |
| **Phép thử phân biệt** | So **domain trên thanh địa chỉ Admin Console** (bỏ `-admin`) với domain trong endpoint. Đối chứng: `curl https://<domain>/.well-known/openid-configuration` phải trả **200** với `issuer` đúng domain |
| **Root cause** | Lấy **tên hiển thị** của org (`galaxyfinx-trial-3489054`) làm subdomain. Domain thật là `trial-3489054` |
| **Khắc phục** | Sửa 3 endpoint của issuer `Okta` trong Moodle, rồi purge cache |
| **Tiêu chí phục hồi** | Nút Okta dẫn tới `trial-3489054.okta.com` và hiện trang đăng nhập |
| **Giám sát** | Probe định kỳ: discovery trả 200 **và** `issuer` khớp giá trị mong đợi |

### SSO-11. `redirect_uri` không khớp

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Ngay sau khi bấm nút, Okta báo lỗi **có mô tả**: *"The 'redirect_uri' parameter must be a Login redirect URI in the client app settings"* |
| **Định vị** | Bước ②: request tới đúng org, đúng client, nhưng địa chỉ callback không nằm trong danh sách cho phép |
| **Giả thuyết** | (a) Moodle đổi URL (`wwwroot`), ví dụ chuyển từ HTTP sang HTTPS hoặc đổi hostname; (b) nhập redirect URI trong Okta sai ký tự (`http` / `https`, thừa `/`); (c) ALB DNS đổi sau khi tạo lại hạ tầng |
| **Phép thử** | Lấy `redirect_uri` trong URL authorize (giải mã URL), so **từng ký tự** với *Sign-in redirect URIs* của app trên Okta |
| **Root cause điển hình** | Đổi hạ tầng (HTTPS hoặc hostname mới) mà quên cập nhật Okta |
| **Khắc phục** | Cập nhật redirect URI trên Okta **trước** khi đổi `wwwroot` của Moodle |
| **Bị cấm** | Thêm redirect URI dạng wildcard, hoặc trỏ tới domain lạ |
| **Câu hỏi tư duy** | So với SSO-10: tại sao ở đây Okta **có** mô tả lỗi, còn SSO-10 thì không? (Gợi ý: Okta kiểm tra `client_id` trước `redirect_uri`) |

### SSO-12. Client secret sai hoặc đã xoay vòng

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Đăng nhập Okta và làm MFA **thành công**, quay về Moodle thì Moodle báo lỗi đăng nhập chung chung |
| **Định vị** | Bước ⑤: Okta đã cấp `code`, nhưng Moodle **không đổi được code lấy token** |
| **Giả thuyết** | (a) secret trong Moodle không khớp Okta (đã *Generate new secret* trên Okta, hoặc dán thiếu ký tự); (b) phương thức gửi secret lệch (`basicauth` của Moodle khác cấu hình của Okta); (c) Moodle không ra được Internet (SSO-30) |
| **Phép thử phân biệt** | Từ node Moodle, gọi thử token endpoint: **có phản hồi** thì loại (c). Xem System Log của Okta quanh thời điểm lỗi: có lỗi xác thực client thì nghiêng về (a) hoặc (b). Kiểm tra *Client secrets* trên Okta: secret nào đang **Active**, tạo lúc nào |
| **Khắc phục** | Copy secret Active vào Moodle (*Edit service*). Khi xoay vòng secret: tạo secret mới, cập nhật Moodle, kiểm tra, **rồi mới** vô hiệu hóa secret cũ |
| **Bị cấm** | Dán secret vào ticket, chat hay ảnh chụp |
| **Ghi chú tư duy** | Lỗi ở bước ⑤ **không hiện trên trang Okta**, vì Okta đã làm xong phần của mình. Người dùng chỉ thấy lỗi ở Moodle, nên dễ bị đổ cho Moodle |

### SSO-13. Plugin OAuth 2 hoặc service bị tắt

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Trang login Moodle **không có** nút *Sign in by Okta* |
| **Định vị** | Bước ①: thuần Moodle |
| **Giả thuyết** | (a) plugin OAuth 2 bị tắt trong *Manage authentication*; (b) issuer bị tắt (biểu tượng con mắt); (c) issuer thiếu endpoint, cột **Login** hiện ⚠️; (d) *This service will be used* không chứa Login page |
| **Phép thử** | Xem cột **Login** của service; `auth` trong config phải chứa `oauth2` |
| **Khắc phục** | Bật lại, hoặc bổ sung phần còn thiếu |
| **Giám sát** | Probe: HTML trang login phải chứa link `auth/oauth2/login.php` |

### SSO-14. Field mapping hoặc login domain sai, Moodle không tạo được account

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Okta thành công, nhưng Moodle báo không tạo được account hoặc không cho đăng nhập. Thường chỉ xảy ra với **user mới** |
| **Định vị** | Bước ⑦ |
| **Giả thuyết** | (a) email của user không thuộc *Login domains*; (b) thiếu mapping `email`, `given_name`, `family_name`; (c) bật *Prevent account creation when authenticating*; (d) bật *Require email verification* mà không có SMTP |
| **Phép thử phân biệt** | User **cũ** (đã có linked login) vẫn vào được, còn user **mới** thì không: lỗi ở khâu **tạo** account, tức (c) hoặc (d). Email khác domain: (a) |
| **Khắc phục** | Sửa đúng cấu hình bị lệch so với tài liệu triển khai (Bước 10) |
| **Bị cấm** | Xóa *Login domains* để "cho qua" (xem SSO-42) |

### SSO-15. Policy MFA chặn tất cả

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Mọi user hợp lệ đều bị chặn sau khi nhập mật khẩu |
| **Định vị** | Bước ③, phần đánh giá policy |
| **Giả thuyết** | (a) rule MFA bị sửa, mất group trong điều kiện IF, nên mọi người rơi xuống **Catch-all Deny**; (b) app bị gắn nhầm policy; (c) Okta Verify bị tắt ở *Security → Authenticators* |
| **Phép thử** | System Log có event đánh giá policy với kết quả **DENY**, chỉ rõ rule nào khớp |
| **Khắc phục** | Khôi phục rule theo cấu hình chuẩn |
| **Bị cấm** | Đổi Catch-all thành **Allow** để "mở tạm". Hành động này biến sự cố thành **SSO-41** |

---

## 7. Mức 3: đồng bộ danh tính Entra → Okta

**Dấu hiệu chung:** đăng nhập kỹ thuật vẫn chạy, nhưng **trạng thái trên Okta không khớp Entra**. Câu hỏi đầu tiên luôn là: *"Lần import gần nhất là khi nào, Full hay Incremental, và có thành công không?"*

### SSO-20. Mất consent Microsoft Graph, import thất bại

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Import báo lỗi xác thực. User hoặc group mới trên Entra không xuất hiện trên Okta |
| **Giả thuyết** | (a) Global Admin thu hồi consent của *Okta Microsoft Graph Client* trong Enterprise applications; (b) token tích hợp hết hạn; (c) lỗi cấu hình *Save* trước khi *Authenticate* (đã gặp khi triển khai) |
| **Phép thử** | Office 365 app → *Provisioning → API Integration → Test API Credentials*; trên Entra, xem *Enterprise applications → Okta Microsoft Graph Client → Permissions* |
| **Khắc phục** | *Authenticate with Microsoft Account* lại bằng Global Admin, Accept quyền |
| **Ảnh hưởng thật sự** | Đăng nhập **vẫn chạy**, nhưng vòng đời user ngừng cập nhật. Nghĩa là user nghỉ việc vẫn vào được (dẫn tới SSO-22). Đây là loại lỗi **im lặng** |

### SSO-21. Thiếu thuộc tính bắt buộc khi import (đã xảy ra thật)

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | *Confirm Imported User Assignments*: `firstName/lastName – The field cannot be left blank` |
| **Root cause** | User trên Entra thiếu *First name* / *Last name* |
| **Khắc phục** | **Sửa ở nguồn** (Entra), rồi Full import |
| **Câu hỏi tư duy** | Vì sao không sửa trực tiếp trên Okta? (Gợi ý: lần import sau sẽ ghi đè; và nguồn sự thật phải là Entra) |

### SSO-22. User đã disable ở Entra nhưng vẫn đăng nhập được

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Người đã nghỉ việc (đã disable trên Entra) vẫn vào Moodle |
| **Định vị** | Luồng nền, rồi tới bước ③ |
| **Giả thuyết** | (a) **chưa có Full import** từ lúc disable, mà **Incremental import không phát hiện user bị disable**; (b) import bị lỗi (SSO-20); (c) user vẫn còn **session Moodle cũ**, nên không cần đăng nhập lại |
| **Phép thử phân biệt** | Trạng thái user trên Okta: vẫn Active thì là (a) hoặc (b), xem lịch sử import. Đã Deactivated mà người đó vẫn đang dùng Moodle thì là (c) |
| **Khắc phục** | (a) chạy **Full import**; (c) suspend user trên Moodle hoặc hủy session |
| **Bài học thiết kế** | Thu hồi quyền truy cập có **độ trễ**: bằng khoảng thời gian tới lần Full import tiếp theo, cộng thời gian sống của session Moodle. Với người dùng thật, cần lịch Full import đều đặn và session timeout ngắn |
| **Bị cấm** | Xóa user hay dữ liệu học tập trên Moodle. Chỉ chặn truy cập, giữ lại hồ sơ để audit |

### SSO-23. Đổi group ở Entra nhưng quyền trên Okta chưa đổi

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Vừa thêm user vào `moodle-students` trên Entra mà vẫn bị chặn (SSO-04); hoặc vừa gỡ khỏi group mà vẫn vào được |
| **Giả thuyết** | (a) chưa chạy import; (b) dùng nested group, mà Okta không import group lồng nhau |
| **Phép thử** | So danh sách thành viên group trên Entra và trên Okta |
| **Khắc phục** | Chạy import; dùng group phẳng |
| **Ranh giới cần nhớ** | Group trên Okta chỉ quyết định **được vào Moodle hay không**. Role Teacher/Student trong course **không** tự đổi theo group, vì đó là quyết định thiết kế (xem tài liệu triển khai, mục 6.1) |

---

## 8. Mức 4: hạ tầng và phụ thuộc

**Dấu hiệu chung:** có thể ảnh hưởng **cả những người không dùng SSO** (admin đăng nhập local cũng lỗi), hoặc kèm timeout và lỗi 5xx.

### SSO-30. Moodle server không ra được Internet (egress 443)

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Đăng nhập Okta xong, quay về Moodle thì **rất chậm**, rồi Moodle báo lỗi hoặc ALB trả **504** |
| **Định vị** | Bước ⑤ ⑥: Moodle server gọi Okta nhưng bị treo |
| **Giả thuyết** | (a) security group mất rule egress 443; (b) NAT Gateway lỗi hoặc mất route `0.0.0.0/0`; (c) DNS ra ngoài lỗi; (d) secret sai (SSO-12) |
| **Phép thử phân biệt** | Từ `moodle-app-a`, `curl -m 10` tới token endpoint: **timeout** thì là (a), (b) hoặc (c); **trả 400/401** thì đường mạng tốt, chuyển sang (d). Thử tiếp `curl https://checkip.amazonaws.com`: nếu cũng timeout thì toàn bộ đường ra Internet bị hỏng, nghĩa là (a) hoặc (b) |
| **Câu hỏi tư duy** | Vì sao triệu chứng là **504 (timeout)** chứ không phải lỗi tức thì? (Gợi ý: gói tin bị *drop* thì phía gọi phải chờ hết timeout; ALB chờ tối đa 60 giây) |
| **Khắc phục** | Khôi phục rule hoặc route bằng **Terraform** (`plan` sẽ phát hiện drift), không sửa tay trên Console |
| **Bị cấm** | Mở egress `0.0.0.0/0` cho **mọi cổng**; mở ingress để "test" |
| **Diễn tập** | Chặn có giới hạn bằng rule REJECT (tương tự DB-01), chỉ áp dụng cho đích là IP của Okta |

### SSO-31. Moodle hoặc database lỗi, đăng nhập Okta xong vẫn hỏng

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Okta thành công, nhưng Moodle lỗi. **Admin đăng nhập local cũng lỗi** |
| **Phép thử phân biệt (quan trọng)** | Đăng nhập bằng tài khoản `admin` local: **cũng lỗi** thì không phải SSO, mà là Moodle hoặc DB (DB-01, CON-01…); **vẫn vào được** thì quay về Mức 2 |
| **Khắc phục** | Theo kịch bản Moodle tương ứng trong runbook |
| **Bài học** | Luôn có một **đường đối chứng không đi qua SSO** (break-glass admin) để tách lỗi SSO khỏi lỗi ứng dụng |

### SSO-32. Okta không khả dụng

| Mục | Nội dung |
|---|---|
| **Triệu chứng** | Nút Okta dẫn tới trang lỗi hoặc timeout; **Okta Dashboard cũng lỗi** |
| **Phép thử** | Mở `https://trial-3489054.okta.com` và trang trạng thái của Okta; thử từ một mạng khác |
| **Khắc phục** | Không sửa được từ phía ta. Thông báo cho người dùng, và dùng **break-glass admin** cho các việc khẩn cấp |
| **Bị cấm** | Bật lại đăng nhập local hàng loạt, hoặc tạo account manual cho mọi người để "chạy tạm" |
| **Diễn tập** | Không thể tắt Okta. Chỉ **mô phỏng** bằng cách chặn egress tới Okta (SSO-30) |

---

## 9. Mức 5: sự cố bảo mật

**Nguyên tắc:** **HUMAN_ONLY.** AI agent chỉ phát hiện, thu thập bằng chứng và cảnh báo. Ưu tiên: **ngăn chặn → bảo toàn bằng chứng → khắc phục → rút kinh nghiệm.**

### SSO-40. Client secret bị lộ

| Mục | Nội dung |
|---|---|
| **Tín hiệu** | Secret xuất hiện trong Git, ảnh chụp, chat hay log; hoặc có lượt dùng token lạ |
| **Ngăn chặn** | Tạo secret mới, cập nhật Moodle, kiểm tra đăng nhập, rồi **vô hiệu hóa secret cũ** |
| **Điều tra** | System Log của Okta: các lượt dùng client này từ IP lạ |
| **Bị cấm** | Chỉ xóa file hoặc ảnh chứa secret mà **không xoay vòng** secret: bản sao có thể đã tồn tại ở nơi khác |
| **Liên hệ thực tế** | Khi triển khai, API token Okta và access key AWS đã lộ qua ảnh chụp. Cách xử lý đúng là **thu hồi và thay mới**, không chỉ xóa ảnh |

### SSO-41. User bị chặn lại truy cập được

| Mục | Nội dung |
|---|---|
| **Tín hiệu** | User thuộc `moodle-denied`, hoặc đã nghỉ việc, vẫn có session Moodle |
| **Giả thuyết** | (a) app bị assign nhầm cho `Everyone` hoặc cho `moodle-denied`; (b) Catch-all Rule bị đổi thành Allow; (c) SSO-22 |
| **Phép thử** | Tab **Assignments** của app Moodle; danh sách rule của policy; System Log *app assignment changed*, *policy updated*, và **ai** đã thay đổi |
| **Ngăn chặn** | Khôi phục assignment và policy; suspend account Moodle liên quan |
| **Bài học** | Hai lớp chặn (assignment + policy) tồn tại để **một** lớp bị cấu hình sai thì vẫn còn lớp kia |

### SSO-42. Chiếm account qua trùng email

| Mục | Nội dung |
|---|---|
| **Rủi ro** | Nếu Moodle liên kết account theo **email**, kẻ tấn công tạo một user ở IdP khác (hoặc đổi email) trùng email nạn nhân là chiếm được account Moodle |
| **Phòng ngừa đang có** | Mapping `sub → username`, liên kết theo `(issuer, sub)` là định danh bất biến; *Login domains* giới hạn trong tenant; field email bị khóa |
| **Kiểm thử phòng ngừa** | Xác nhận `auth_oauth2_linked_login.username` là `sub` (dạng `00u…`), không phải email |
| **Bị cấm** | Bỏ *Login domains*; liên kết account thủ công theo email |

---

## 10. Giả thuyết hay bị nghĩ tới nhưng không áp dụng

Biết vì sao một giả thuyết **bị loại** cũng là một phần của tư duy.

| Giả thuyết | Vì sao không áp dụng với hệ thống này |
|---|---|
| "Đồng hồ server Moodle lệch nên token bị từ chối" | Moodle **không tự kiểm tra chữ ký hay thời hạn của id_token**. Nó dùng access token để gọi **userinfo**, và Okta là bên kiểm tra token. Lệch giờ trên **điện thoại** thì có thể làm sai mã TOTP (SSO-05) |
| "Cần JWKS (`/oauth2/v1/keys`) cho Moodle" | Cùng lý do trên: Moodle không xác minh id_token nên không cần JWKS. JWKS lỗi **không** làm hỏng đăng nhập Moodle |
| "WS-Federation hỏng làm Moodle không đăng nhập được" | WS-Fed chỉ phục vụ đăng nhập Microsoft 365 qua Okta. Luồng Moodle là OIDC, không liên quan |
| "Lỗi trên `/oauth2/default`" | Hệ thống dùng **org authorization server** (`/oauth2/v1/...`), không dùng custom authorization server |
| "Phải tạo lại org hoặc redeploy" | Đó là hành động **khi chưa có root cause**. Sự cố SSO-10 cho thấy làm lại sẽ lặp lại đúng lỗi cũ |

---

## 11. Diễn tập an toàn trên staging

**Nguyên tắc chung**

1. **Chỉ dùng user test** (`student.test`, `teacher.test`, user `moodle-denied`), **chỉ trên môi trường staging**.
2. **Chụp lại cấu hình trước khi tạo lỗi**: export cấu hình issuer từ DB Moodle, chụp app và policy trên Okta.
3. Mỗi kịch bản phải có **cách tạo lỗi**, **cách khôi phục** và **tiêu chí phục hồi** **trước** khi bắt đầu.
4. Ghi thời điểm bắt đầu và kết thúc để đo **MTTD** (thời gian phát hiện) và **MTTR** (thời gian khôi phục), giống các kịch bản Moodle sẵn có.
5. Không diễn tập kịch bản Mức 5 trên hệ thống thật. SSO-40 chỉ diễn tập trên giấy (tabletop).

| Mã | Cách tạo lỗi (staging) | Cách khôi phục |
|---|---|---|
| SSO-02 | Tạo user test mới, để ở trạng thái Staged | Activate |
| SSO-03 | Nhập sai mật khẩu `student.test` liên tục tới khi bị khóa | Unlock |
| SSO-04 | Gỡ `student.test` khỏi `moodle-students` **trên Entra**, rồi import | Thêm lại, rồi import |
| SSO-10 | Đổi 1 endpoint sang domain sai (PHP CLI hoặc UI) | Đổi lại; purge cache |
| SSO-11 | Thêm một ký tự vào redirect URI trên Okta | Sửa lại |
| SSO-12 | *Generate new secret* trên Okta, **không** cập nhật Moodle, rồi vô hiệu hóa secret cũ | Cập nhật secret mới vào Moodle |
| SSO-13 | Tắt plugin OAuth 2 | Bật lại |
| SSO-15 | Bỏ `moodle-teachers` khỏi rule MFA | Thêm lại |
| SSO-22 | Disable `student.test` trên Entra; thử đăng nhập **trước** và **sau** Full import | Enable lại; Full import |
| SSO-30 | Rule REJECT tới các IP của `trial-3489054.okta.com` trên 1 node | Gỡ rule; xác nhận `terraform plan` = No changes |
| SSO-41 | Tạm assign app cho `moodle-denied`, đăng nhập bằng user denied | Gỡ assignment |

---

## 12. Bài tập tự luyện

Trả lời **trước** khi xem lại kịch bản tương ứng. Với mỗi bài: nêu **bước nào trong luồng**, **2 giả thuyết**, và **phép thử rẻ nhất** để phân biệt chúng.

1. Sáng thứ Hai, **tất cả** sinh viên báo "đăng nhập Okta xong thì Moodle báo lỗi". Admin vẫn đăng nhập Moodle local được. Tuần trước có người "dọn dẹp" Okta. Bạn nghi điều gì trước?
2. Một giảng viên mới được thêm vào `moodle-teachers` trên Entra **10 phút trước**, nhưng vẫn bị Okta chặn. Các giảng viên khác vào bình thường.
3. Sau khi chuyển Moodle sang HTTPS, **không ai** đăng nhập qua Okta được. Okta hiện lỗi **có mô tả**.
4. Moodle trả 504 **chỉ** khi đăng nhập qua Okta; trang chủ và `/healthz.php` vẫn bình thường.
5. Một sinh viên nghỉ học **3 ngày trước** (đã disable trên Entra) vẫn nộp bài trên Moodle hôm nay.
6. Vì sao kịch bản SSO-10 cho ra trang 400 **không có** mô tả, còn SSO-11 thì **có**? Điều này giúp gì khi chẩn đoán?

<details>
<summary>Gợi ý đáp án</summary>

1. Bước ⑤ (SSO-12: secret bị xoay vòng hoặc xóa) hoặc bước ⑦ (SSO-14). Admin local vẫn vào được, nên loại SSO-31. Phép thử: xem *Client secrets* trên Okta (secret nào Active, tạo lúc nào), và từ node Moodle gọi thử token endpoint.
2. SSO-23 / SSO-04: chưa có import nào kể từ khi thêm vào group. Phép thử: so thành viên `moodle-teachers` trên Entra và Okta; xem lần import gần nhất.
3. SSO-11: `redirect_uri` đổi từ `http://` sang `https://`. Phép thử: so redirect URI trong URL authorize với cấu hình app.
4. SSO-30: bước ⑤ ⑥ bị treo vì egress. Phép thử: `curl -m 10` từ node Moodle tới token endpoint.
5. SSO-22: chưa có Full import (incremental không bắt được user bị disable), **hoặc** session Moodle cũ vẫn còn. Phép thử: trạng thái user trên Okta, và lần đăng nhập Okta gần nhất của user trong System Log.
6. Okta kiểm tra `client_id` (thuộc org nào) **trước** `redirect_uri`. Lỗi trống thường nghĩa là client hoặc org không được nhận ra: hãy kiểm tra **domain và client ID** trước tiên.

</details>
