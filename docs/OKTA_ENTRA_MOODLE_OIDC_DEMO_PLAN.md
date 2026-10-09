# Kế hoạch demo Moodle SSO với Okta và Microsoft Entra ID

**Thời lượng:** 7 ngày, 07--13/10/2026  
**Mục tiêu:** Hoàn thiện và trình diễn luồng đăng nhập Moodle dùng Okta làm
OpenID Connect (OIDC) Identity Provider, với Microsoft Entra ID là nguồn quản
lý vòng đời tài khoản.

## 1. Phạm vi và nguyên tắc kiến trúc

```text
Quản trị viên Entra
        |
        | tạo, sửa, vô hiệu hóa user và group
        v
Microsoft Entra ID
        |
        | đồng bộ profile và group
        v
Okta Universal Directory --> Okta OIDC + MFA
                                  ^
                                  | Authorization Code + OIDC
Người dùng --> Moodle -----------+
                  |
                  | tạo/liên kết local account, local session
                  v
       Moodle role, enrolment và course access
```

### Trách nhiệm của từng hệ thống

| Hệ thống | Trách nhiệm | Không thuộc trách nhiệm |
|---|---|---|
| Microsoft Entra ID | Nguồn quản lý user, group, trạng thái tài khoản | Cấp Moodle session, role/course enrolment |
| Okta | Đồng bộ profile/group, xác thực, MFA, phát hành OIDC token | Lưu mật khẩu tại Moodle, quản lý course Moodle |
| Moodle | OIDC relying party; tạo/liên kết local account; session, role, quyền và ghi danh khóa học | Xác thực mật khẩu Entra/Okta |

### Các nguyên tắc không thay đổi

- Người dùng chỉ nhập mật khẩu và MFA tại Okta. Moodle không nhận, lưu hoặc
  xác thực mật khẩu Entra/Okta.
- Đồng bộ tài khoản chỉ truyền profile/group cần thiết, **không đồng bộ mật
  khẩu**.
- Khóa liên kết định danh là cặp `issuer + sub` OIDC của Okta. Email chỉ là
  thuộc tính hiển thị hoặc đối chiếu, không phải khóa tin cậy duy nhất.
- Okta group chỉ quyết định quyền dùng ứng dụng Moodle. Moodle vẫn là nguồn
  quản lý role Teacher/Student và enrolment trong course.
- Cần duy trì một local Moodle break-glass administrator, được quản lý và
  audit riêng, không dùng trong demo thông thường.

## 2. Cấu hình đích

### Entra ID -> Okta

- Tạo nhóm: `moodle-demo-students`, `moodle-demo-teachers`,
  `moodle-demo-denied`.
- Đồng bộ tối thiểu: Entra Object ID hoặc `employeeId`, UPN, email, given name,
  family name, account status và membership của các group demo.
- Chỉ import group/user được chọn cho demo; không import toàn tenant.
- Dùng tích hợp Microsoft 365/Graph hoặc cơ chế provisioning được tenant Okta
  hỗ trợ. Xác nhận loại sync tương thích với Entra Connect trước khi bật.
- Không dùng nested group trong demo vì Okta Microsoft Graph import không hỗ
  trợ nested-group import.

### Okta OIDC application

- Loại ứng dụng: **OIDC Web Application**.
- Grant type: **Authorization Code**.
- Redirect URI:
  `https://<moodle-host>/admin/oauth2callback.php`
- Scopes tối thiểu: `openid profile email`.
- Claims cần dùng: `sub`, `email`, `email_verified`, `given_name`,
  `family_name`, `preferred_username`.
- Chỉ assign Moodle app cho hai group teacher/student.
- Bắt MFA cho Moodle app và kiểm tra policy trước demo.
- Client secret chỉ được lưu trong vùng cấu hình bí mật của Moodle/Okta; không
  đưa vào Git, image Docker, `.env` đã commit hoặc ảnh chụp màn hình.

### Moodle

- Moodle trong repo hiện sử dụng bản 5.2.3.
- Tạo OAuth 2 service kiểu OpenID Connect, dùng issuer/discovery document của
  Okta.
- Bật `OAuth 2 authentication` và nút đăng nhập `Đăng nhập bằng Okta`.
- Bật JIT account creation trong phạm vi demo cho user đã được Okta assign app.
- Khóa các trường profile do IdP quản lý: email, first name, last name.
- Không tự động map Okta group sang Moodle role/enrolment trong tuần demo đầu;
  việc đó là hạng mục automation riêng phải được phê duyệt.

## 3. Kế hoạch bảy ngày

| Ngày | Công việc | Đầu ra và tiêu chí hoàn thành |
|---|---|---|
| 1 | Chốt tenant Entra/Okta dev, hostname Moodle HTTPS, owner, chính sách user trùng email và break-glass admin. | One-page design, RACI nhỏ, mapping thuộc tính, bốn test accounts. |
| 2 | Tạo user/group demo trong Entra; cấu hình Entra -> Okta import/provisioning và mapping. | Hai user hợp lệ và group demo xuất hiện đúng trong Okta; có log sync. |
| 3 | Tạo Okta OIDC app, redirect URI, scopes, claims, assignment và MFA policy. | User được assign có thể tới Okta; user denied bị chặn. |
| 4 | Cấu hình Moodle OAuth 2/OIDC, discovery, login button, JIT/link và profile-field locking. | Student đăng nhập Okta lần đầu và có local Moodle account/session. |
| 5 | Tạo course demo; gán teacher/student và kiểm thử local authorization/enrolment. | Teacher sửa course được; student chỉ truy cập nội dung được ghi danh. |
| 6 | Security/UAT: callback lỗi, MFA fail, unassign/deactivate, duplicate email, session/logout, redaction evidence. | Test matrix đạt; không lộ password hoặc token trong log/tài liệu. |
| 7 | Rehearsal, quay/chụp evidence, hoàn thiện runbook, rollback và trình bày demo. | Demo 10--12 phút chạy được; checklist và backlog sau demo sẵn sàng. |

## 4. Kịch bản demo 10--12 phút

### Vai trò demo

- Quản trị viên Entra
- Quản trị viên Okta
- Quản trị viên Moodle
- `lecturer.demo`
- `student.demo`
- `blocked.demo`

### Kịch bản

1. **Quản trị tài khoản (2 phút):** tạo hoặc hiển thị `student.demo` trong
   Entra, thêm vào `moodle-demo-students`, sau đó hiển thị user/group đã đồng
   bộ trong Okta.
2. **Kiểm soát truy cập Okta (1 phút):** cho thấy user được assign Moodle app;
   `blocked.demo` không được assign.
3. **OIDC login (3 phút):** mở Moodle, chọn `Đăng nhập bằng Okta`, redirect
   sang Okta, hoàn thành MFA và quay về Moodle với session hợp lệ.
4. **Chứng minh không chuyển mật khẩu (1 phút):** chỉ ra điểm nhập mật khẩu là
   Okta và đường redirect; không hiển thị token hoặc password.
5. **Authorization độc lập (2 phút):** `lecturer.demo` sửa course;
   `student.demo` chỉ truy cập nội dung đã được Moodle ghi danh.
6. **Negative case (2 phút):** thử `blocked.demo` hoặc unassign
   `student.demo`; Okta từ chối login mới. Bản ghi local Moodle được giữ lại
   để audit, không tự xóa lịch sử học tập.
7. **Evidence (1 phút):** hiển thị Okta System Log, Moodle event/log và role /
   enrolment của course.

## 5. Test matrix và tiêu chí nghiệm thu

| Tình huống | Kỳ vọng |
|---|---|
| User mới đã assign Moodle app | Okta xác thực thành công; Moodle tạo hoặc liên kết account hợp lệ. |
| User chưa assign app | Okta từ chối trước khi Moodle cấp session. |
| Redirect URI sai | Okta từ chối; không redirect tới URL tùy ý. |
| MFA không hoàn thành | Không có Moodle session. |
| User bị deactivate/unassign | Login mới bị từ chối; Moodle local record không tự xóa. |
| Teacher và student | Course capability đúng theo role local Moodle. |
| Thay đổi group IdP | Không tự thay đổi Moodle role/enrolment nếu chưa có automation được duyệt. |
| Password và token | Không tồn tại trong Git, image, log, screenshot hoặc tài liệu demo. |

## 6. Evidence cần thu thập

- Ảnh cấu hình app assignment và MFA policy của Okta (đã che client secret).
- Ảnh Moodle OAuth 2 service (đã che client secret).
- Okta System Log cho login thành công và login bị từ chối.
- Moodle event/log cho account creation/link và user login.
- Ảnh role và enrolment course của lecturer/student.
- Biên bản test matrix có thời điểm, người chạy và kết quả.

Không ghi raw ID token, access token, password, client secret, tenant secret,
SSH key hoặc địa chỉ hạ tầng nhạy cảm vào evidence.

## 7. Rủi ro và rollback

| Rủi ro | Giảm thiểu | Rollback |
|---|---|---|
| Sai redirect URI/issuer | Dùng exact HTTPS hostname; test trước với account riêng | Disable login service/button trong Moodle, giữ local admin |
| Mapping sai hoặc duplicate email | Dùng `issuer + sub`; test JIT với account không trùng trước | Disable JIT, xử lý link thủ công |
| User sync quá rộng | Scope bằng group demo | Disable import/provisioning job và unassign app |
| MFA/policy chặn cả admin | Tài khoản break-glass được kiểm thử trước | Dùng local break-glass account theo quy trình audit |
| Lộ secret trong demo | Che giá trị; dùng secret store; không quay trang secret | Rotate client secret và rà soát log ngay |

## 8. Tài liệu tham chiếu

- [Moodle OAuth 2 services](https://docs.moodle.org/401/en/OAuth_2_services)
- [Moodle OAuth 2 authentication](https://docs.moodle.org/401/en/OAuth_2_authentication)
- [Okta Authorization Code flow](https://developer.okta.com/docs/guides/implement-grant-type/)
- [Okta import users from Microsoft Graph](https://help.okta.com/en-us/Content/Topics/Apps/Office365-Deployment/import-ms-graph.htm)
- [Microsoft Entra automatic user provisioning](https://learn.microsoft.com/en-us/entra/identity/app-provisioning/user-provisioning)
