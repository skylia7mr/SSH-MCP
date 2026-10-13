import uvicorn

from ssh_mcp.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "ssh_mcp.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
