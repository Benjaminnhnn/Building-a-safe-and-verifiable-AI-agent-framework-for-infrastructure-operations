# Retrieval-Augmented Generation (RAG) & Runbook Knowledge Base (`agent_src/core/rag/`)

Thư mục này quản lý **cơ sở tri thức vận hành (Operational Knowledge Base)** và **động cơ tìm kiếm ngữ nghĩa (RAG Engine)** của hệ thống AI Agent.

Giúp Agent tiếp cận các quy trình vận hành chuẩn (SOP/Runbooks) được phê duyệt và học hỏi từ các sự cố tương tự trong quá khứ thông qua cơ chế phản hồi của Operator.

---

## Danh mục các file và chức năng chi tiết

### 1. `rag_engine.py`
- **Tác dụng**: Động cơ RAG trung tâm (`RAGEngine`, `get_rag_instance`). Quản lý 2 bộ sưu tập vector ChromaDB độc lập:
  - `standard_runbooks`: Chứa các tài liệu runbook chuẩn từ `config/knowledge_base/*.md`, được chia nhỏ theo tiêu đề Markdown khi khởi động.
  - `incident_memory`: Lưu trữ bài học từ các sự cố trong quá khứ và phản hồi đã được phê duyệt từ Operator qua Telegram.
- **Ảnh hưởng tới toàn bộ AI Agent**: Cung cấp ngữ cảnh vận hành và kịch bản ứng cứu chuẩn xác cho LLM khi thực hiện chẩn đoán và lập kế hoạch, ngăn chặn hiện tượng ảo giác (hallucination).

### 2. `runbook_registry.py`
- **Tác dụng**: Quản lý phiên bản runbook (`RunbookRegistry`), theo dõi các sửa đổi công cụ vận hành (`tool_revisions`), và hỗ trợ tạo bản thảo runbook mới (`create_runbook_draft`) dựa trên kết quả xử lý sự cố thực tế.
- **Ảnh hưởng tới toàn bộ AI Agent**: Đảm bảo tri thức vận hành luôn được cập nhật liên tục, có thể theo dõi được lịch sử sửa đổi (version-controlled) và gắn kết trực tiếp với mã nguồn Git.

### 3. `__init__.py`
- **Tác dụng**: Khởi tạo subpackage `core.rag`.
- **Ảnh hưởng tới toàn bộ AI Agent**: Cung cấp namespace chuẩn cho toàn bộ phân hệ tri thức và tìm kiếm ngữ nghĩa.

