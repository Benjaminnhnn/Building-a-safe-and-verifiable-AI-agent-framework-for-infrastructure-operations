# Mô hình Moodle/PostgreSQL/container/endpoint

from pydantic import BaseModel, Field, model_validator
from .common import Environment, ResourceType

class Resource(BaseModel):
    # PLACEHOLDER LÀ GÌ:
    # Đây là ID định danh resource trong hệ thống AIOps, ví dụ Moodle app, PostgreSQL, container, endpoint.
    # LẤY Ở ĐÂU:
    # Lấy từ topology thật, Docker Compose service name, Prometheus job, hoặc tên EC2 role.
    # SỬA NHƯ NÀO:
    # Nếu service trong docker-compose tên là "db" thì dữ liệu thật nên dùng resource_id như "db" hoặc "postgres-db",
    # nhưng phải dùng thống nhất ở Evidence, Incident, Action và Scenario.
    resource_id: str = Field(..., examples=["moodle-app"])
    # PLACEHOLDER: Tên hiển thị của resource. Ví dụ đổi "Moodle Web" thành tên thật như "Moodle Staging Web".
    name: str = Field(..., examples=["Moodle Web"])
    type: ResourceType
    environment: Environment
    # PLACEHOLDER: Role chịu trách nhiệm resource này. Nếu nhóm bạn dùng tên khác thì sửa trong dữ liệu thật.
    owner_role: str = Field(..., examples=["infrastructure_engineer"])

    # PLACEHOLDER: Host/EC2 role thật chứa resource, ví dụ "web-ec2", "core-ec2", "monitor-ec2".
    # Nếu resource không gắn trực tiếp với host thì để None trong dữ liệu thật.
    host: str | None = None
    # PLACEHOLDER: Tên service thật trong docker-compose. Xem trong release/docker-compose.*.yml.
    service_name: str | None = None
    # PLACEHOLDER: Endpoint thật để probe, ví dụ URL Moodle hoặc blackbox target.
    endpoint: str | None = None

    # Docker image, e.g. moodle:4.3
    image: str | None = None
    compose_project: str | None = None
    version: str | None = None

    # PLACEHOLDER: Danh sách resource_id thật mà resource này phụ thuộc.
    # Ví dụ Moodle phụ thuộc PostgreSQL thì depends_on chứa ID của PostgreSQL.
    depends_on: list[str] = Field(default_factory=list)
    # PLACEHOLDER: Tag thật để lọc theo hệ thống, môi trường, component hoặc owner.
    tags: dict[str, str] = Field(default_factory=dict)
    
    @model_validator(mode='after')
    def validate_resource(self):
        if self.resource_id in self.depends_on:
            raise ValueError("Resource cannot depend on itself")
        if self.endpoint is not None and "://" in self.endpoint:
            if not (self.endpoint.startswith("http://") or self.endpoint.startswith("https://")):
                raise ValueError("Endpoint URL must start with http:// or https://")
        return self
