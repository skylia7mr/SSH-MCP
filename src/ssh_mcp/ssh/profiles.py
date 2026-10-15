import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, TypeAdapter, ValidationError

from ssh_mcp.errors import ToolError


class ConnectionProfile(BaseModel):
    session_id: str
    alias: str
    host: str
    port: int
    username: str
    created_at: datetime
    auth_type: Literal["password", "key"] = "password"
    private_key: SecretStr | None = Field(default=None, repr=False)


class ProfileStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[ConnectionProfile]:
        try:
            profiles = TypeAdapter(list[ConnectionProfile]).validate_json(self.path.read_bytes())
            self.path.chmod(0o600)
            return profiles
        except FileNotFoundError:
            return []
        except (OSError, ValidationError):
            raise ToolError(
                "CONNECTION_STORE_ERROR", "无法读取连接配置文件，请检查格式和权限", 500
            ) from None

    def save(self, profiles: list[ConnectionProfile]) -> None:
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            entries = []
            for profile in profiles:
                entry = profile.model_dump(mode="json", exclude={"private_key"})
                entry["private_key"] = (
                    profile.private_key.get_secret_value() if profile.private_key else None
                )
                entries.append(entry)
            descriptor, name = tempfile.mkstemp(prefix=".connections-", dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(entries, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.path)
        except OSError:
            raise ToolError(
                "CONNECTION_STORE_ERROR", "无法保存连接配置，请检查目录权限", 500
            ) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
