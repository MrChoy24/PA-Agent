"""单实例互斥锁：同一台机器上只允许一个 PA Agent 进程持有。

为什么需要它
------------
``config/settings.json`` 是 GUI 与无头守护进程共用的唯一配置文件，而
``save_settings()`` 是**整份覆盖**而不是差异写。两个进程同时跑时：

* 「同一账户双重下单」—— 两边都各自读 ``auto_trade.enabled``，各自触发；
* ``max_daily_orders`` / ``max_notional_usdt`` 之类的额度各自计数，实际翻倍；
* 19 处 ``save_settings()`` 互相覆盖，先写的那份配置被后写者整份抹掉。

锁文件放在 ``config/.runtime.lock``，内容是 JSON：``role`` / ``pid`` /
``host`` / ``machine`` / ``container`` / ``proc_start`` / ``started_at``。
**只做本机互斥**：GUI 在本机、daemon 跑在远程服务器是完全正常的部署形态
（两台机器各自一把锁，互不干扰）。

四层身份，缺一不可
------------------
判断「锁还是不是活的」不能只看 PID，容器里尤其不能：

``machine``
    稳定的机器标识。Linux 读 ``/etc/machine-id``（Docker 默认把它从宿主机
    绑定挂载进容器，所以同一台机器上的所有容器共享它），其余平台退回
    hostname。**machine 不同 = 真的是另一台机器**，配置目录被共享挂载时
    绝不去抢别人的锁。

``container``
    容器标识（容器内就是 hostname，Docker 给的是 12 位容器 ID）。容器**重建**
    后 ID 会变，此时旧锁一定来自一个已经消失的容器 —— 直接判定陈旧。

``proc_start``
    进程启动时刻（Linux 读 ``/proc/self/stat`` 第 22 个字段）。``docker restart``
    会保留容器 ID，新进程的 PID 又恰好还是 1，只有靠它才能认出「PID 相同但
    已经是另一个进程」。

``pid``
    兜底。同一台机器、同一个容器、同一个进程启动时刻，才轮到 PID 判活。

历史坑（2026-11-14 服务器日志）
-------------------------------
早期版本只比 ``host``：容器重建后 hostname 变成新 ID，旧锁被当成「别的机器
的锁」永久视为活着，``restart: unless-stopped`` 于是每 61 秒崩一次，除了手动
``rm config/.runtime.lock`` 没有任何自愈路径。现在的规则是「容器 ID 变了就是
陈旧，直接接管」。

另外 ``acquire()`` 现在返回 :class:`LockContext`，**占用检查在构造时就做完**，
所以 ``try: ctx = acquire(...)  except RuntimeError: ...`` 能真正接住。改成
``@contextmanager`` 时异常只在 ``__enter__()`` 才抛，两个调用点的 try/except
都是空的。

紧急开关
--------
``PA_AGENT_IGNORE_RUNTIME_LOCK=1`` 完全跳过检查（不读也不写锁文件）。只在
确认「误判导致服务起不来、又来不及排查」时临时用，日志会打 WARNING。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 锁文件默认路径（相对 ``config/`` 目录）。
LOCK_FILE_NAME = ".runtime.lock"

#: 跳过单实例检查的环境变量（1/true/yes/on 生效）。
IGNORE_ENV = "PA_AGENT_IGNORE_RUNTIME_LOCK"

#: 以下都是惰性缓存的进程级常量。
_HOSTNAME = ""
_MACHINE = ""
_PROC_START = -1.0
_CONTAINER: "str | None" = None


def _hostname() -> str:
    """主机名（容器内即容器 ID）。"""
    global _HOSTNAME
    if not _HOSTNAME:
        try:
            _HOSTNAME = socket.gethostname() or "unknown-host"
        except OSError:  # pragma: no cover
            _HOSTNAME = "unknown-host"
    return _HOSTNAME


def _machine_id() -> str:
    """稳定的机器标识；容器之间共享（Docker 把宿主机 machine-id 挂进来）。"""
    global _MACHINE
    if _MACHINE:
        return _MACHINE
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            text = Path(candidate).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            _MACHINE = text
            return _MACHINE
    # Windows / macOS / 精简容器：hostname 是能拿到的最稳定标识。
    _MACHINE = f"host:{_hostname()}"
    return _MACHINE


def _container_id() -> str:
    """容器标识；不在容器里返回空串。"""
    global _CONTAINER
    if _CONTAINER is not None:
        return _CONTAINER

    in_container = False
    try:
        if Path("/.dockerenv").exists():  # Docker
            in_container = True
        elif Path("/run/.containerenv").exists():  # Podman
            in_container = True
        else:
            cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore")
            in_container = any(
                marker in cgroup
                for marker in ("docker", "kubepods", "containerd", "lxc", "podman")
            )
    except OSError:
        in_container = False

    _CONTAINER = _hostname() if in_container else ""
    return _CONTAINER


def _in_container() -> bool:
    return bool(_container_id())


def _parse_proc_start(raw: str) -> float:
    """从 ``/proc/<pid>/stat`` 内容里取出 starttime（第 22 字段，单位 clock tick）。

    ``comm`` 字段（第 2 个）可能含空格和括号，所以必须从**最后一个** ``)``
    之后再切。切出来的 ``fields[0]`` 是第 3 个字段（state），因此第 22 个
    字段是 ``fields[19]``。

    **这个下标必须选「进程生命周期内恒定」的字段**：若选到 utime 之类会变的
    字段，第二次判断时必然算出「启动时刻变了」，把真正在跑的实例误判成陈旧，
    反而造成两个进程同时下单。``starttime`` 是恒定的，只用它。
    """
    try:
        fields = raw[raw.rindex(")") + 2 :].split()
        return float(fields[19])
    except (ValueError, IndexError):
        return 0.0


def _proc_start_time() -> float:
    """本进程的启动时刻（Linux ``/proc/self/stat`` 第 22 字段）；拿不到返回 0。"""
    global _PROC_START
    if _PROC_START >= 0:
        return _PROC_START
    try:
        raw = Path("/proc/self/stat").read_text(encoding="utf-8")
    except OSError:
        _PROC_START = 0.0
        return _PROC_START
    _PROC_START = _parse_proc_start(raw)
    return _PROC_START


def _ignore_requested() -> bool:
    return (os.environ.get(IGNORE_ENV, "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


@dataclass(frozen=True)
class LockHolder:
    """锁文件里记录的持有者信息。"""

    role: str
    pid: int
    host: str
    started_at: float
    #: 稳定机器标识；旧版本锁文件没有这个字段，读出来是空串。
    machine: str = ""
    #: 容器标识；非容器为空串。
    container: str = ""
    #: 进程启动时刻（Linux）；非 Linux 为 0。
    proc_start: float = 0.0

    def describe(self) -> str:
        try:
            started = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)
            )
        except (OSError, ValueError, OverflowError):
            started = "?"
        where = self.host or "?"
        if self.container and self.container != where:
            where = f"{where}/容器 {self.container}"
        return f"{self.role}（pid={self.pid}，{where}，启动于 {started}）"


def default_lock_path() -> Path:
    """``config/.runtime.lock``。"""
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    return SETTINGS_JSON_PATH.parent / LOCK_FILE_NAME


def _pid_alive(pid: int) -> bool:
    """判断 *pid* 对应的进程还在不在。"""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        # Windows 上 os.kill(pid, 0) 会真的去 TerminateProcess，不能用。
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但不属于当前用户 —— 那也算「还活着」。
        return True
    except OSError:  # pragma: no cover
        return False
    return True


def read_holder(path: Path | None = None) -> LockHolder | None:
    """读取锁文件；不存在 / 内容坏 / 结构不符都返回 None。"""
    path = path or default_lock_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return LockHolder(
            role=str(raw.get("role", "") or "unknown"),
            pid=int(raw.get("pid", 0) or 0),
            host=str(raw.get("host", "") or ""),
            started_at=float(raw.get("started_at", 0.0) or 0.0),
            machine=str(raw.get("machine", "") or ""),
            container=str(raw.get("container", "") or ""),
            proc_start=float(raw.get("proc_start", 0.0) or 0.0),
        )
    except (TypeError, ValueError):
        return None


def _proc_start_of(pid: int) -> float:
    """任意进程的 starttime；读不到返回 0（非 Linux / 无权限 / 进程已消失）。

    Linux 上 PID 会被复用：一个早已死掉的守护进程的 PID 可能被某个无关进程
    占用，此时 ``_pid_alive()`` 返回 True，锁就永远解不开。比对 ``starttime``
    能认出「PID 一样但不是同一个进程」。
    """
    if pid <= 0 or os.name == "nt":
        return 0.0
    if pid == os.getpid():
        return _proc_start_time()
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0.0
    return _parse_proc_start(raw)


def _proc_changed(holder: LockHolder) -> bool:
    """持有者记录的进程已不是现在跑在这个 PID 上的那个进程。

    两种形态都能认出来：

    * ``docker restart``：容器 ID 不变，PID 还是 1，但已是新进程；
    * PID 复用：旧进程早死了，PID 被别的进程占了。
    """
    if holder.proc_start <= 0:
        # 旧锁文件没有这个字段，只能靠 _pid_alive 兜底。
        return False
    current = _proc_start_of(holder.pid)
    if current <= 0:
        return False
    return abs(current - holder.proc_start) > 1e-6


def lock_verdict(holder: LockHolder | None) -> str:
    """判定锁的状态：``"free"`` / ``"alive"`` / ``"stale"`` / ``"foreign"``。

    * ``free``     —— 没有锁文件（或内容坏到读不出来）。
    * ``alive``    —— 持有者确实还在跑，不能抢。
    * ``stale``    —— 持有者已经不存在了，接管。
    * ``foreign``  —— 别的机器的锁（共享挂载），一律当成活着，不碰。

    容器是这里最容易判断错的地方，所以规则按「在不在容器里」分流：

    * **容器内**只信 ``container``（容器 ID）。**绝不能拿 machine-id 判
      foreign** —— Docker 镜像里的 ``/etc/machine-id`` 可能是空文件，那时
      只能退回 hostname，而容器 hostname 就是容器 ID，重建一次就变，会把
      自己的旧锁认成别人的锁，永久卡死（2026-11-14 的事故正是这个形态）。
    * 没有 ``machine`` / ``container`` 字段的是旧版本锁文件。容器内这类锁
      必然来自一个已经消失的容器，直接判陈旧，给升级留一条自愈路径。
    """
    if holder is None:
        return "free"

    ours_ctr = _container_id()
    legacy = not holder.machine and not holder.container

    if _in_container():
        if legacy:
            # 旧版本锁：容器 hostname 每次重建都变，必然是上个容器的遗留。
            return "stale"
        if holder.container:
            if holder.container != ours_ctr:
                # 容器 ID 不会复用 —— 上个容器已经没了。
                return "stale"
            if _proc_changed(holder):
                # 同一个容器（docker restart）：PID 还是 1，但已是新进程。
                return "stale"
            return "alive" if _pid_alive(holder.pid) else "stale"
        # 我们在容器里，锁却是宿主机进程写的 —— 只可能来自共享挂载，别抢。
        return "foreign"

    # 以下是我们不在容器里的情况。
    if holder.container:
        return "foreign"
    if holder.machine:
        if holder.machine != _machine_id():
            return "foreign"
    elif holder.host and holder.host != _hostname():
        return "foreign"
    if _proc_changed(holder):
        return "stale"
    return "alive" if _pid_alive(holder.pid) else "stale"


def is_holder_alive(holder: LockHolder | None) -> bool:
    """持有者是否仍然「算占用」（别的机器的锁一律当作活着，不去抢）。"""
    return lock_verdict(holder) in ("alive", "foreign")


def check(path: Path | None = None) -> LockHolder | None:
    """若已有**活着的**持有者则返回它，否则返回 None（表示可以启动）。

    这是一次只读探测，不写锁文件。
    """
    holder = read_holder(path)
    return holder if is_holder_alive(holder) else None


class LockContext:
    """``acquire()`` 的返回值；占用检查在**构造时**完成。

    这样 ``try: ctx = acquire(...)  except RuntimeError: ...`` 才能真正接住
    「已有实例在跑」。早期版本用 ``@contextmanager``，异常只在 ``__enter__()``
    抛出，两个调用点的 try/except 都是摆设，用户只会看到一屏 traceback。
    """

    __slots__ = ("holder", "path", "_wrote", "_released")

    def __init__(
        self,
        role: str,
        *,
        path: Path | None = None,
        take_over_stale: bool = True,
    ) -> None:
        self.path = path or default_lock_path()
        self._wrote = False
        self._released = False
        self.holder = LockHolder(
            role=role,
            pid=os.getpid(),
            host=_hostname(),
            started_at=time.time(),
            machine=_machine_id(),
            container=_container_id(),
            proc_start=_proc_start_time(),
        )

        if _ignore_requested():
            logger.warning(
                "环境变量 %s 已生效：跳过单实例检查，允许并行运行。"
                " 仅用于紧急绕过锁问题，长期开着会导致重复下单与配置互相覆盖。",
                IGNORE_ENV,
            )
            return

        holder = read_holder(self.path)
        verdict = lock_verdict(holder)

        if verdict == "alive" and holder is not None:
            raise RuntimeError(
                f"已有 PA Agent 正在运行：{holder.describe()}。"
                " 同一台机器上同时运行两个实例会导致重复下单与配置互相覆盖。"
            )
        if verdict == "foreign" and holder is not None:
            # 配置目录被别的机器共享挂载。不去抢，也不提议删除 —— 那是别人在跑。
            raise RuntimeError(
                f"运行锁属于另一台机器：{holder.describe()}。"
                f" 锁文件 {self.path} 上的持有者不在本机，本实例不会接管它。"
                " 如果确认那台机器已经停用，请在那台机器上正常关闭，"
                f"或手动删除 {self.path}。"
            )
        if verdict == "stale" and holder is not None and not take_over_stale:
            raise RuntimeError(
                f"锁文件 {self.path} 已被 {holder.describe()} 占用（疑似陈旧）。"
            )
        if verdict == "stale" and holder is not None:
            logger.warning(
                "接管陈旧的运行锁（原持有者 %s 已不在运行）：%s",
                holder.describe(),
                self.path,
            )

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "role": self.holder.role,
                        "pid": self.holder.pid,
                        "host": self.holder.host,
                        "machine": self.holder.machine,
                        "container": self.holder.container,
                        "proc_start": self.holder.proc_start,
                        "started_at": self.holder.started_at,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self._wrote = True
        except OSError as exc:
            # 只读挂载 / 权限不足时不阻塞启动：锁是保险，不是启动前提。
            logger.warning(
                "无法写入运行锁 %s（%s）；本次不做单实例保护。"
                " 若在容器内，请确认挂载目录可写：chown -R 10001:10001 config",
                self.path,
                exc,
            )
            return

        logger.info("已获取运行锁：%s（%s）", self.holder.describe(), self.path)

    def __enter__(self) -> LockHolder:
        return self.holder

    def __exit__(self, *exc_info: Any) -> bool:
        self.release()
        return False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if not self._wrote:
            return
        release_file(self.path, self.holder)


def acquire(
    role: str,
    *,
    path: Path | None = None,
    take_over_stale: bool = True,
) -> LockContext:
    """获取单实例锁；已被占用则**当场**抛 ``RuntimeError``。

    用法::

        with instance_lock.acquire("gui"):
            ...  # 整个 GUI 生命周期

    也可以手动管理::

        ctx = instance_lock.acquire("daemon")   # 这里就可能抛
        try:
            ...
        finally:
            ctx.release()

    ``take_over_stale=False`` 时，即便旧锁是陈旧的也不接管（用于诊断）。
    """
    return LockContext(role, path=path, take_over_stale=take_over_stale)


def release_file(path: Path | None = None, holder: LockHolder | None = None) -> None:
    """释放锁；只在锁确实属于 *holder* 时才删除，避免误删别人的锁。"""
    path = path or default_lock_path()
    current = read_holder(path)
    if holder is not None and current is not None:
        if current.pid != holder.pid or current.host != holder.host:
            logger.warning(
                "运行锁 %s 已被 %s 接管，本次不删除", path, current.describe()
            )
            return
    try:
        path.unlink()
        logger.info("已释放运行锁：%s", path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("删除运行锁 %s 失败（%s）；留待下次启动时判定为陈旧锁", path, exc)


#: 兼容旧调用点：``release(path, holder)``。
release = release_file


__all__ = [
    "IGNORE_ENV",
    "LOCK_FILE_NAME",
    "LockContext",
    "LockHolder",
    "acquire",
    "check",
    "default_lock_path",
    "is_holder_alive",
    "lock_verdict",
    "read_holder",
    "release",
    "release_file",
]
