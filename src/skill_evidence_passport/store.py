"""仅追加事件存储。

业务状态全部来自事件重放：进程重启后重放 ``events.jsonl`` 即可恢复
幂等索引、通行证、申诉与撤权传播等派生状态，服务本身不落其他状态文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

try:  # 生产环境为 POSIX，测试环境退化到无线程间锁
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


class ProcessLock:
    """进程间互斥；同进程内用可重入线程锁，允许命令内部嵌套临界区。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.touch(exist_ok=True)
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._fh: Any = None

    def __enter__(self) -> "ProcessLock":
        self._thread_lock.acquire()
        self._depth += 1
        if self._depth == 1 and fcntl is not None:
            self._fh = self._path.open("a+")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._depth == 1 and fcntl is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
        self._depth -= 1
        self._thread_lock.release()


def canonical_json(payload: Any) -> bytes:
    """稳定字节序列，用作同号异内容比对的指纹。"""

    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def content_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


class EventStore:
    """JSONL 事件日志，追加带进程锁并 fsync。"""

    def __init__(self, directory: str | Path, clock: Callable[[], Any] | None = None) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events_path = self.directory / "events.jsonl"
        self.events_path.touch(exist_ok=True)
        self.lock = ProcessLock(self.directory / ".lock")

    def load(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        with self.events_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    def append(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, sort_keys=True, ensure_ascii=False)
        with self.lock:
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    def append_many(self, events: Iterable[dict[str, Any]]) -> None:
        with self.lock:
            with self.events_path.open("a", encoding="utf-8") as fh:
                for event in events:
                    fh.write(
                        json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n"
                    )
                fh.flush()
                os.fsync(fh.fileno())
