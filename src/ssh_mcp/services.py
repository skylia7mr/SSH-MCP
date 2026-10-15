from ssh_mcp.auth.service import PairingService, TokenService
from ssh_mcp.auth.web import AuthState
from ssh_mcp.config import Settings
from ssh_mcp.host_keys.service import HostKeyTrust
from ssh_mcp.jobs.service import TaskService
from ssh_mcp.ssh.registry import SessionRegistry
from ssh_mcp.transfers import TERMINAL, TransferService


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
        self.transfers = TransferService(settings, self.registry)

    async def cancel_session_work(self, session_id: str) -> None:
        for run in list(self.tasks.runs.values()):
            if run.request.session_id == session_id and not run.done.is_set():
                await self.tasks.cancel(run.run_id)
        for record in list(self.transfers.records.values()):
            if record.status not in TERMINAL and session_id in (
                record.source_session_id,
                record.target_session_id,
            ):
                await self.transfers.cancel(record.transfer_id)
