"""Stop refresh streams before Uvicorn waits for active HTTP requests to drain."""

import socket

import uvicorn

from ssh_mcp.services import Services


class WorkbenchServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, services: Services) -> None:
        super().__init__(config)
        self.services = services

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self.services.tasks.closing = True
        self.services.tasks.wake.set()
        await super().shutdown(sockets)
