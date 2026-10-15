import asyncio
import re
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path


class TransferCache:
    def __init__(self, root: Path, keep: int) -> None:
        self.root = root.resolve()
        self.keep = keep
        self.active: set[Path] = set()
        self.lock = asyncio.Lock()

    async def _prune_locked(self) -> None:
        if not self.root.exists():
            return
        directories = sorted(
            (
                path
                for path in self.root.iterdir()
                if re.fullmatch(r"relay-\d{20}-[A-Za-z0-9_-]{24}", path.name)
                and not path.is_symlink()
                and path.is_dir()
            ),
            reverse=True,
        )
        for directory in directories[self.keep :]:
            if directory in self.active:
                continue
            removal = asyncio.create_task(asyncio.to_thread(shutil.rmtree, directory))
            try:
                await asyncio.shield(removal)
            except asyncio.CancelledError:
                await removal
                raise

    async def prune(self) -> None:
        async with self.lock:
            await self._prune_locked()

    @asynccontextmanager
    async def stage(self, transfer_id: str) -> AsyncIterator[Path]:
        directory = None
        try:
            async with self.lock:
                self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
                directory = self.root / f"relay-{time.time_ns():020d}-{transfer_id}"
                directory.mkdir(mode=0o700)
                self.active.add(directory)
                await self._prune_locked()
            yield directory
        finally:
            if directory is not None:
                async with self.lock:
                    self.active.discard(directory)
                    await self._prune_locked()
