import uvicorn

from ssh_mcp.app import create_app
from ssh_mcp.config import get_settings
from ssh_mcp.http_server import WorkbenchServer


def main() -> None:
    settings = get_settings()
    app = create_app(settings)
    server = WorkbenchServer(
        uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            workers=1,
            access_log=False,
            timeout_graceful_shutdown=10,
        ),
        app.state.services,
    )
    server.run()


if __name__ == "__main__":
    main()
