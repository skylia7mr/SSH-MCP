from ssh_mcp.auth.service import PairingService, TokenService
from ssh_mcp.auth.web import AuthState
from ssh_mcp.config import Settings
from ssh_mcp.host_keys.service import HostKeyTrust
from ssh_mcp.jobs.service import TaskService
from ssh_mcp.ssh.registry import SessionRegistry


class Services:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.auth = AuthState(
            PairingService(settings.pairing_ttl_seconds, settings.pairing_max_attempts),
            TokenService(settings.mcp_token),
        )
        self.trust = HostKeyTrust(settings.known_hosts, settings.ssh_connect_timeout_seconds)
        self.registry = SessionRegistry(settings, self.trust)
        self.tasks = TaskService(settings, self.registry)
