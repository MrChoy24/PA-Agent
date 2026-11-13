"""跨进程文件锁（Windows / POSIX 同一套接口）。

自动下单流水 ``auto_orders.jsonl`` 是**追加写**的：GUI 与无头守护进程
如果同时在跑（没有单实例锁时完全可能），两个进程的 ``write()`` 可能交织，
把一行 JSON 撕成两半；而 ``SIGKILL`` 时缓冲区里的半行也会留在文件里。

这里提供一个很小的互斥锁 + 原子追加函数：

* :func:`lock` —— 上下文管理器，加独占锁（阻塞，带超时）
* :func:`append_line` —— 加锁 + 写一行 + ``flush`` + ``os.fsync``

实现方式是**旁路锁文件**（``<target>.lock``）而不是锁目标文件本身：
目标文件可能被外部工具打开/删除，锁它会让行为不可预测。
Windows 用 ``msvcrt.locking``，POSIX 用 ``fcntl.flock``；
两者都是「建议锁」，但在本进程组内部（GUI + daemon）足够用。

所有失败都**不抛异常**（除 ``TimeoutError`` 以外的内部错误会降级为无锁写入），
因为写日志失败绝不该让交易流程中断。
"""

from __future__ import annotations

import errno
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import IO, Iterator

logger = logging.getLogger(__name__)

#: 默认等待锁的时间。写一行流水很快，5 秒已经非常宽裕。
DEFAULT_TIMEOUT_S = 5.0

#: 同进程内的线程锁：``msvcrt.locking`` / ``flock`` 对同一进程的不同线程
#: 有时不互斥（POSIX flock 是按打开的文件描述符算的，这里每线程自己开一个，
#: 所以其实能互斥；但 Windows 下同一进程内重入会死锁），
#: 用一把线程锁兜住同进程并发。
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[path] = lock
        return lock


def _lock_file(fh: IO) -> None:
    """对已打开的文件句柄加独占锁。"""
    if os.name == "nt":
        import msvcrt

        # 锁 1 个字节；文件为空时也要能锁，所以先确保有 1 字节
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"\0")
            fh.flush()
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _unlock_file(fh: IO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:  # noqa: PERF203 — 解锁失败无所谓，进程退出会释放
        pass


@contextmanager
def lock(target: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> Iterator[None]:
    """在 *target* 上持有跨进程独占锁（写入期间调用）。

    锁文件是 ``<target>.lock``，与目标文件放在同一目录（保证同一文件系统）。
    拿不到锁超过 *timeout_s* 时抛 :class:`TimeoutError` —— 由调用方决定
    是跳过还是硬写；:func:`append_line` 的选择是**降级为无锁写入**。
    """
    path = os.path.abspath(target)
    lock_path = path + ".lock"
    parent = os.path.dirname(lock_path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass

    tlock = _thread_lock(lock_path)
    if not tlock.acquire(timeout=timeout_s):
        raise TimeoutError(f"等待文件锁超时（同进程）: {lock_path}")

    fh: IO | None = None
    locked = False
    try:
        try:
            fh = open(lock_path, "a+b")
        except OSError as exc:
            logger.debug("无法打开锁文件 %s（降级为无锁）: %s", lock_path, exc)
            yield
            return

        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            try:
                _lock_file(fh)
                locked = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13, 36):
                    logger.debug("文件锁异常（降级为无锁）: %s", exc)
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"等待文件锁超时: {lock_path}") from exc
                time.sleep(0.02)
        yield
    finally:
        if fh is not None:
            if locked:
                _unlock_file(fh)
            try:
                fh.close()
            except OSError:
                pass
        tlock.release()


def append_line(target: str, line: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> bool:
    """加锁追加一行文本并落盘（``flush`` + ``fsync``）。

    返回 ``True`` 表示成功；失败只记日志、返回 ``False``，**从不抛异常**。
    *line* 末尾的换行符会自动补齐。
    """
    text = line if line.endswith("\n") else line + "\n"
    path = os.path.abspath(target)
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass

    try:
        with lock(path, timeout_s=timeout_s):
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
        return True
    except TimeoutError:
        # 拿不到锁：宁可少一条流水，也不要写出撕开的半行
        logger.warning("写 %s 时等待文件锁超时，本次记录已跳过", path)
        return False
    except OSError as exc:
        # 加锁本身不可用（网络盘、权限）时退回无锁追加，内容仍然写出
        logger.debug("文件锁不可用，改为无锁追加 %s: %s", path, exc)
        try:
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            return True
        except OSError as exc2:
            logger.warning("写 %s 失败: %s", path, exc2)
            return False


__all__ = ["DEFAULT_TIMEOUT_S", "append_line", "lock"]
