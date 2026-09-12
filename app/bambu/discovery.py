"""打印机局域网自动搜索。

多网卡（尤其是插着虚拟网卡、VPN/代理网卡的电脑）是搜索不稳的主因：
未指定出口网卡时，组播只会从「默认路由」那一张网卡发出去，于是每次只能碰运气
搜到一部分打印机。因此这里做了四件事：

1. **逐网卡发送**：为每个本地 IPv4 建立独立 socket，绑定到该地址并设置
   `IP_MULTICAST_IF`，保证 SSDP 组播从每张网卡都发得出去；
2. **搜索包重复发送**：整个搜索窗口内每 1.5 秒重发一次 M-SEARCH（UDP 会丢包，
   单发一次必然漏机器），并持续排空接收缓冲；
3. **网段单播扫描**：向本网段每个地址单独发一次 M-SEARCH，覆盖不响应组播的设备；
4. **双协议**：SSDP（新固件）+ 2021 端口 `M99999` 广播（旧固件），并被动监听
   239.255.255.250:1990 与 2021 端口的主动上报。
"""

from __future__ import annotations

import ctypes
import json
import socket
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from .models import PrinterInfo, detect_model

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1990
LEGACY_PORT = 2021
LEGACY_MAGIC = b"M99999"

#: 搜索窗口内重发 M-SEARCH 的间隔：打印机对每次搜索都会回应一轮，
#: 密集重发是提高「一次全搜到」概率的主要手段（实测按 1 秒重发最稳）。
SEARCH_INTERVAL = 1.0
#: 重发旧版广播的间隔
LEGACY_INTERVAL = 2.0
#: 网段单播扫描的间隔（单播最可靠，但包量大，取折中值）
SWEEP_INTERVAL = 4.0
#: 接收缓冲区（并发回包较多时防止丢包）
RCVBUF = 1 << 20

#: 拓竹序列号的长度区间。老机型是 15 位（01P09A470310013 / 20P6BJ632400723），
#: 但实测新机型 A2L 是 18 位（26A00A000000000000），因此不能写死单一长度。
#: 该区间只用于「没有任何拓竹专有字段时」的兜底判定，用来排除 uuid:、MAC 之类。
_SERIAL_LENGTH_RANGE = (12, 24)

MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 1\r\n"
    "ST: urn:bambulab-com:device:3dprinter:1\r\n"
    "\r\n"
).encode("ascii")

#: 这些网段不是打印机所在的局域网，扫描时跳过（代理/测试网段、链路本地地址）
SKIP_PREFIXES = ("198.18.", "198.19.", "169.254.")

#: 适配器名称里出现这些关键字基本可以确定不会有打印机，不做网段扫描
VIRTUAL_ADAPTER_HINTS = (
    "vmware",
    "virtualbox",
    "vethernet",
    "hyper-v",
    "loopback",
    "clash",
    "tailscale",
    "zerotier",
    "docker",
    "wsl",
    "natpierce",
    "frp",
    "openvpn",
    "wireguard",
    "tap-",
)


@dataclass(frozen=True)
class LocalInterface:
    """本机的一个 IPv4 网卡。"""

    ip: str
    prefix: str  # 网络号，例如 192.168.31
    broadcast: str  # 定向广播地址，例如 192.168.31.255
    name: str = ""
    mask: int = 24

    @property
    def scannable(self) -> bool:
        return not any(self.ip.startswith(p) for p in SKIP_PREFIXES)

    @property
    def sweepable(self) -> bool:
        """是否值得做网段单播扫描（虚拟网卡/代理网卡上不会有打印机）。"""
        if not self.scannable or self.mask < 22:
            return False
        name = self.name.lower()
        if any(keyword in name for keyword in VIRTUAL_ADAPTER_HINTS):
            return False
        # Hyper-V 的 vEthernet、WSL 网段
        return not self.name.lower().startswith("vethernet")

    @property
    def first_host(self) -> int:
        return 1

    @property
    def last_host(self) -> int:
        return 255 if self.mask >= 24 else (1 << (32 - self.mask)) - 2


def _prefix_of(ip: str) -> str:
    parts = ip.split(".")
    return ".".join(parts[:3]) if len(parts) == 4 else ip


class _SockaddrIn(ctypes.Structure):
    _fields_ = [
        ("sin_family", ctypes.c_ushort),
        ("sin_port", ctypes.c_ushort),
        ("sin_addr", ctypes.c_ubyte * 4),
        ("sin_zero", ctypes.c_char * 8),
    ]


class _SocketAddress(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


class _UnicastAddress(ctypes.Structure):
    pass


_UnicastAddress._fields_ = [
    ("Length", wintypes.ULONG),
    ("Flags", wintypes.DWORD),
    ("Next", ctypes.POINTER(_UnicastAddress)),
    ("Address", _SocketAddress),
    ("PrefixOrigin", ctypes.c_int),
    ("SuffixOrigin", ctypes.c_int),
    ("DadState", ctypes.c_int),
    ("ValidLifetime", wintypes.ULONG),
    ("PreferredLifetime", wintypes.ULONG),
    ("LeaseLifetime", wintypes.ULONG),
    ("OnLinkPrefixLength", ctypes.c_ubyte),
]


class _AdapterAddresses(ctypes.Structure):
    pass


_AdapterAddresses._fields_ = [
    ("Length", wintypes.ULONG),
    ("IfIndex", wintypes.DWORD),
    ("Next", ctypes.POINTER(_AdapterAddresses)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_UnicastAddress)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", wintypes.LPWSTR),
    ("Description", wintypes.LPWSTR),
    ("FriendlyName", wintypes.LPWSTR),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", wintypes.DWORD),
    ("Flags", wintypes.DWORD),
    ("Mtu", wintypes.DWORD),
    ("IfType", wintypes.DWORD),
    ("OperStatus", ctypes.c_int),
]


def _adapter_interfaces() -> list[LocalInterface]:
    """用 Windows 适配器表枚举本机 IPv4（含真实子网掩码）。

    依赖主机名解析枚举网卡并不可靠：实测会出现「真实局域网网卡偶尔不在列表里」，
    这正是自动搜索时好时坏、只能搜到一部分设备的原因。
    """
    try:
        iphlpapi = ctypes.windll.iphlpapi
    except (AttributeError, OSError):  # 非 Windows
        return []

    AF_INET = 2
    GAA_FLAG_SKIP_ANYCAST = 0x02
    GAA_FLAG_SKIP_MULTICAST = 0x04
    GAA_FLAG_SKIP_DNS_SERVER = 0x08
    ERROR_BUFFER_OVERFLOW = 111
    IF_TYPE_SOFTWARE_LOOPBACK = 24
    IF_OPER_STATUS_UP = 1

    flags = GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER
    size = wintypes.ULONG(16 * 1024)
    buffer = ctypes.create_string_buffer(size.value)
    result = iphlpapi.GetAdaptersAddresses(
        wintypes.ULONG(AF_INET),
        wintypes.ULONG(flags),
        None,
        ctypes.cast(buffer, ctypes.POINTER(_AdapterAddresses)),
        ctypes.byref(size),
    )
    if result == ERROR_BUFFER_OVERFLOW:
        buffer = ctypes.create_string_buffer(size.value)
        result = iphlpapi.GetAdaptersAddresses(
            wintypes.ULONG(AF_INET),
            wintypes.ULONG(flags),
            None,
            ctypes.cast(buffer, ctypes.POINTER(_AdapterAddresses)),
            ctypes.byref(size),
        )
    if result != 0:
        return []

    interfaces: list[LocalInterface] = []
    adapter = ctypes.cast(buffer, ctypes.POINTER(_AdapterAddresses))
    while adapter:
        node = adapter.contents
        if node.IfType != IF_TYPE_SOFTWARE_LOOPBACK and node.OperStatus == IF_OPER_STATUS_UP:
            unicast = node.FirstUnicastAddress
            while unicast:
                entry = unicast.contents
                if entry.Address.lpSockaddr:
                    sockaddr = ctypes.cast(entry.Address.lpSockaddr, ctypes.POINTER(_SockaddrIn)).contents
                    if sockaddr.sin_family == AF_INET:
                        ip = socket.inet_ntoa(bytes(bytearray(sockaddr.sin_addr)))
                        mask = int(entry.OnLinkPrefixLength) or 24
                        mask = max(8, min(30, mask))
                        mask_bits = (0xFFFFFFFF << (32 - mask)) & 0xFFFFFFFF
                        ip_value = int.from_bytes(socket.inet_aton(ip), "big")
                        network = ip_value & mask_bits
                        broadcast = network | (~mask_bits & 0xFFFFFFFF)
                        interfaces.append(
                            LocalInterface(
                                ip=ip,
                                prefix=socket.inet_ntoa(network.to_bytes(4, "big")),
                                broadcast=socket.inet_ntoa(broadcast.to_bytes(4, "big")),
                                name=node.FriendlyName or node.Description or "",
                                mask=mask,
                            )
                        )
                unicast = entry.Next
        adapter = node.Next
    return interfaces


def local_interfaces() -> list[LocalInterface]:
    """枚举本机 IPv4 网卡（默认路由所在网卡排在最前）。"""
    interfaces = _adapter_interfaces()
    if not interfaces:
        # 兜底：主机名解析 + 默认路由探测
        addresses: list[str] = []
        for probe in (("223.5.5.5", 53), ("8.8.8.8", 53), ("1.1.1.1", 53)):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.connect(probe)
                    addresses.append(sock.getsockname()[0])
                    break
            except OSError:
                continue
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                addresses.append(info[4][0])
        except OSError:
            pass
        seen: set[str] = set()
        for ip in addresses:
            if ip in seen or ip.startswith("127."):
                continue
            seen.add(ip)
            interfaces.append(
                LocalInterface(ip=ip, prefix=_prefix_of(ip), broadcast=_prefix_of(ip) + ".255")
            )

    # 默认路由出口（用于探测外网）所在网卡排最前；代理/隧道网段（198.18/19）最后
    default_ip = ""
    for probe in (("223.5.5.5", 53), ("8.8.8.8", 53)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(probe)
                default_ip = sock.getsockname()[0]
                break
        except OSError:
            continue

    def sort_key(iface: LocalInterface) -> tuple[int, int]:
        skipped = 1 if not iface.scannable else 0
        is_default = 0 if iface.ip == default_ip else 1
        return (skipped, is_default)

    return sorted(interfaces, key=sort_key)


def _new_socket(bind_ip: str = "", bind_port: int = 0, multicast_if: str = "") -> Optional[socket.socket]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for option, value in (
        (socket.SO_REUSEADDR, 1),
        (socket.SO_BROADCAST, 1),
        (socket.SO_RCVBUF, RCVBUF),
    ):
        try:
            sock.setsockopt(socket.SOL_SOCKET, option, value)
        except OSError:
            pass
    if multicast_if:
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(multicast_if))
        except OSError:
            pass
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    except OSError:
        pass
    try:
        sock.bind((bind_ip, bind_port))
    except OSError:
        sock.close()
        return None
    sock.setblocking(False)
    return sock


def parse_announcement(data: bytes, source_ip: str = "") -> Optional[PrinterInfo]:
    """解析 SSDP 或旧版 JSON 广播内容。"""
    try:
        text = data.decode("utf-8", errors="replace").strip()
    except Exception:
        return None
    if not text:
        return None

    if text.upper().startswith("M-SEARCH"):
        return None  # 自己发出的搜索包被本机监听端口收到

    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        ip = str(obj.get("dev_ip") or source_ip or "")
        serial = str(obj.get("dev_id") or "")
        name = str(obj.get("dev_name") or "")
        firmware = str(obj.get("dev_version") or "")
        if not (ip or serial) or not (serial or name):
            return None
        return PrinterInfo(
            ip=ip,
            serial=serial,
            name=name,
            firmware=firmware,
            model=detect_model(serial, name),
            discovered=True,
        )

    # SSDP 文本响应
    headers: dict[str, str] = {}
    lines = text.splitlines()[1:] if text.upper().startswith("HTTP") else text.splitlines()
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()

    ip = headers.get("location", "") or source_ip
    serial = headers.get("usn", "")
    if not (ip or serial):
        return None

    # `devmodel` 是代号（P1S=C12、A2L=N9、H2C=O1C/O1C2），与型号名毫无字面关系；
    # `devname` 才是人可读的名字。两者必须**分开**传给 detect_model，
    # 否则代号会被当成型号名去匹配关键字，永远匹配不上。
    dev_model = headers.get("devmodel.bambu.com", "")
    name = headers.get("devname.bambu.com", "")
    if not _looks_like_bambu(headers, dev_model, name, serial=serial):
        # 局域网里其它 UPnP/SSDP 设备（路由器、NAS 等）的回包，直接忽略
        return None
    return PrinterInfo(
        ip=ip,
        serial=serial,
        name=name,
        firmware=headers.get("devversion.bambu.com", ""),
        model=detect_model(serial, name, dev_model),
        discovered=True,
    )


def _looks_like_bambu(
    headers: dict[str, str],
    dev_model: str = "",
    name: str = "",
    *,
    serial: str = "",
) -> bool:
    """过滤掉非拓竹设备：要求带拓竹专有字段、拓竹 SSDP 标识、或像拓竹序列号的字符串。

    参数全部用关键字传，避免位置歧义：``dev_model`` 是 ``devmodel.bambu.com`` 的代号
    （C12 / N9 / O1C…），``name`` 是 ``devname.bambu.com``，``serial`` 是 ``usn``。
    ``devmodel`` 既是「拓竹设备的标记」也参与机型识别，所以任一非空即认可。

    最后那条是**兜底启发式**：拓竹序列号的长度并不固定（老机型 15 位、
    实测 A2L 18 位），所以用区间而不是等值判断，详见 ``_SERIAL_LENGTH_RANGE``。
    """
    if dev_model or headers.get("devname.bambu.com") or headers.get("devversion.bambu.com"):
        return True
    # 拓竹设备的 SSDP 标识：`nt` 出现在设备主动发出的 NOTIFY 里，
    # `st` 出现在对 M-SEARCH 的应答里（两者都要看，否则只带 ST 的回包会被漏判）
    for header in ("nt", "st"):
        notification = headers.get(header, "").lower()
        if "bambulab" in notification or "3dprinter" in notification:
            return True
    serial = (serial or "").strip()
    # 拓竹序列号是大写字母数字，长度**并非固定 15 位**：
    # 实测 A2L（序列号 26A00A000000000000）就是 18 位（见 docs/FIELD_NOTES.md）。
    # 以前写死 15 位，会让「只带 ST 头、不带 dev* 头」的新机型被当成陌生设备漏掉，
    # 所以这里放宽成一个区间，只用于排除明显不是序列号的字符串（uuid:、MAC 等）。
    return _SERIAL_LENGTH_RANGE[0] <= len(serial) <= _SERIAL_LENGTH_RANGE[1] and (
        serial.isalnum() and serial.upper() == serial
    )


class DiscoveryService:
    """一次自动搜索会话。找到新打印机时回调 on_found。"""

    def __init__(
        self,
        on_found: Callable[[PrinterInfo], None],
        on_finished: Optional[Callable[[], None]] = None,
        timeout: float = 12.0,
    ) -> None:
        self._on_found = on_found
        self._on_finished = on_finished
        self.timeout = timeout
        self._found: dict[str, PrinterInfo] = {}
        #: IP -> 已登记记录。用于避免「同一台设备被两条通道各登记一次」
        #: （一条带序列号、一条只认得出 IP 时会变成两条）。见 :meth:`_register`。
        self._by_ip: dict[str, PrinterInfo] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._sockets: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self.packets_sent = 0
        self.sweep_done = False
        #: 已经出过结果的网段，后续扫描只聚焦这些网段（避免对着虚拟网卡空扫）
        self._productive_prefixes: set[str] = set()
        #: 每台设备是通过哪条通道发现的（排障用）
        self.hit_sources: dict[str, set[str]] = {}
        #: 有新设备出现时立刻补扫其网段
        self._sweep_requested = False

    # ------------------------------------------------------------------ 公共接口
    @property
    def results(self) -> list[PrinterInfo]:
        with self._lock:
            return list(self._found.values())

    def start(self) -> None:
        self._stop_event.clear()
        self._threads = [threading.Thread(target=self._run, name="bambu-discovery", daemon=True)]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        for sock in list(self._sockets):
            try:
                sock.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=1.5)

    def run_blocking(self) -> list[PrinterInfo]:
        self.start()
        deadline = time.time() + self.timeout
        while time.time() < deadline and not self._stop_event.is_set():
            time.sleep(0.1)
        self.stop()
        return self.results

    # ------------------------------------------------------------------ 内部实现
    def _register(self, info: PrinterInfo) -> None:
        """登记一台设备，**保证同一台设备只出现一次**。

        只用 ``serial or ip`` 当键是不够的：同一台打印机可能被两条通道分别发现，
        而两条通道给出的信息不一定都带序列号 —— 例如 SSDP 应答里没带 ``USN``
        （或带了别的东西）时只能退回用 IP 当键，于是同一台机器会以
        「序列号键」和「IP 键」各登记一次，用户就在搜索列表里看到重复项。

        所以这里**双重索引**：序列号与 IP 各查一遍，命中任一个就认为是同一台，
        并把信息合并到已有记录上（缺什么补什么）。
        """
        if not info.ip and not info.serial:
            return
        serial = (info.serial or "").strip()
        ip = (info.ip or "").strip()
        key = serial or ip
        # 记录出过结果的网段，供后续扫描聚焦
        if ip.count(".") == 3:
            self._productive_prefixes.add(_prefix_of(ip))
        with self._lock:
            existing = self._found.get(key)
            if existing is None and ip:
                # 同一个 IP 之前是以序列号为键登记的（或反之）：算同一台
                existing = self._by_ip.get(ip)
            if existing is None:
                self._found[key] = info
                if ip:
                    self._by_ip[ip] = info
                is_new = True
            else:
                # 补充信息；注意也补序列号，这样后续两条通道能对到同一条记录上
                if not existing.ip and ip:
                    existing.ip = ip
                    self._by_ip[ip] = existing
                if not existing.serial and serial:
                    existing.serial = serial
                if not existing.name and info.name:
                    existing.name = info.name
                if not existing.firmware and info.firmware:
                    existing.firmware = info.firmware
                if not existing.model.is_known and info.model.is_known:
                    existing.model = info.model
                # 补齐序列号后，原来那个以 IP 为键的条目要并到序列号键下，
                # 否则 results 会同时吐出两条指向同一台设备的记录
                if existing.serial and key != existing.serial and existing.serial not in self._found:
                    self._found.pop(key, None)
                    self._found[existing.serial] = existing
                is_new = False
        if is_new:
            if info.ip.count(".") == 3:
                self._sweep_requested = True
            try:
                self._on_found(info)
            except Exception:
                pass

    def _drain(self, sock: socket.socket, kind: str = "") -> None:
        """把缓冲区里的回包全部取出来（避免一次只收一个导致丢包）。"""
        for _ in range(64):
            try:
                data, addr = sock.recvfrom(4096)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            info = parse_announcement(data, addr[0] if addr else "")
            if info is not None:
                if kind:
                    self.hit_sources.setdefault(info.serial or info.ip, set()).add(kind)
                self._register(info)

    def _send_search(self, targets: list[socket.socket], interfaces: list[LocalInterface]) -> None:
        for sock, iface in zip(targets, interfaces):
            # 组播（新固件）
            for destination in (SSDP_ADDR, iface.broadcast, "255.255.255.255"):
                try:
                    sock.sendto(MSEARCH, (destination, SSDP_PORT))
                    self.packets_sent += 1
                except OSError:
                    continue

    def _sweep_subnet(
        self, targets: list[socket.socket], interfaces: list[LocalInterface], focus: bool = False
    ) -> None:
        """向本网段每个地址单播一次 M-SEARCH，覆盖不响应组播的设备。

        ``focus=True`` 时只扫描已经出过结果的网段，避免反复对着虚拟网卡空扫。
        """
        productive = self._productive_prefixes
        for sock, iface in zip(targets, interfaces):
            if not iface.sweepable:
                continue
            if focus and productive and iface.prefix not in productive:
                continue
            network = int.from_bytes(socket.inet_aton(iface.prefix), "big") if iface.prefix else 0
            size = min(256, 1 << (32 - iface.mask))
            for offset in range(1, size - 1):
                address = socket.inet_ntoa((network + offset).to_bytes(4, "big"))
                if address == iface.ip:
                    continue
                try:
                    sock.sendto(MSEARCH, (address, SSDP_PORT))
                    self.packets_sent += 1
                except OSError:
                    continue

    def _send_legacy(self, targets: list[socket.socket], interfaces: list[LocalInterface]) -> None:
        """旧固件：2021 端口 M99999 广播。"""
        for sock, iface in zip(targets, interfaces):
            for destination in (iface.broadcast, "255.255.255.255"):
                try:
                    sock.sendto(LEGACY_MAGIC, (destination, LEGACY_PORT))
                    self.packets_sent += 1
                except OSError:
                    continue

    def _run(self) -> None:
        deadline = time.time() + self.timeout
        interfaces = local_interfaces()
        if not interfaces:
            interfaces = [LocalInterface(ip="0.0.0.0", prefix="", broadcast="255.255.255.255")]

        # 每个网卡一个发送/接收 socket：保证组播从每张网卡都发得出去，回包也回到同一个 socket
        senders: list[socket.socket] = []
        send_interfaces: list[LocalInterface] = []
        for iface in interfaces:
            sock = _new_socket(bind_ip=iface.ip, multicast_if=iface.ip)
            if sock is None and iface.ip:
                sock = _new_socket(multicast_if=iface.ip)
            if sock is not None:
                senders.append(sock)
                send_interfaces.append(iface)
                self._sockets.append(sock)

        # 被动监听：SSDP 通知（1990）与旧版广播（2021）
        listeners: list[socket.socket] = []
        ssdp_listener = _new_socket(bind_port=SSDP_PORT)
        if ssdp_listener is not None:
            joined = False
            for iface in interfaces:
                try:
                    mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton(iface.ip)
                    ssdp_listener.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                    joined = True
                except OSError:
                    continue
            if joined or not interfaces:
                listeners.append(ssdp_listener)
                self._sockets.append(ssdp_listener)
            else:
                ssdp_listener.close()
        legacy_listener = _new_socket(bind_port=LEGACY_PORT)
        if legacy_listener is not None:
            listeners.append(legacy_listener)
            self._sockets.append(legacy_listener)

        # 首轮：组播 + 网段扫描 + 旧版广播
        self._send_search(senders, send_interfaces)
        self._send_legacy(senders, send_interfaces)
        self._sweep_subnet(senders, send_interfaces)
        self.sweep_done = True

        next_search = time.time() + SEARCH_INTERVAL
        next_legacy = time.time() + LEGACY_INTERVAL
        next_sweep = time.time() + SWEEP_INTERVAL
        while not self._stop_event.is_set() and time.time() < deadline:
            for sock in senders:
                self._drain(sock, "iface")
            for index, sock in enumerate(listeners):
                self._drain(sock, "listener-ssdp" if index == 0 else "listener-legacy")
            now = time.time()
            if now >= next_search:
                self._send_search(senders, send_interfaces)
                next_search = now + SEARCH_INTERVAL
            if now >= next_legacy:
                self._send_legacy(senders, send_interfaces)
                next_legacy = now + LEGACY_INTERVAL
            if self._sweep_requested or now >= next_sweep:
                # 新发现设备所在的网段立刻再加扫一轮，其余按固定间隔
                self._sweep_subnet(senders, send_interfaces, focus=self._sweep_requested)
                self._sweep_requested = False
                next_sweep = now + SWEEP_INTERVAL
            time.sleep(0.05)

        # 收尾：再排空一次，把最后一批回包收进来
        for _ in range(10):
            for sock in senders:
                self._drain(sock, "iface")
            for index, sock in enumerate(listeners):
                self._drain(sock, "listener-ssdp" if index == 0 else "listener-legacy")
            time.sleep(0.05)

        for sock in senders + listeners:
            try:
                sock.close()
            except OSError:
                pass
        if self._on_finished is not None:
            try:
                self._on_finished()
            except Exception:
                pass


def discover(timeout: float = 12.0) -> list[PrinterInfo]:
    """阻塞式自动搜索，返回找到的打印机列表。

    返回前统一过一遍 :func:`merge_devices` 去重 —— 这是**唯一的收口点**，
    桌面端、网页端、安卓版都从这里拿结果，所以在这之前不管哪条通道多报了
    一条，调用方都不会看到重复项（用户看到的就是「同一台设备出现两次」）。
    """
    found: list[PrinterInfo] = []
    service = DiscoveryService(found.append, timeout=timeout)
    return merge_devices([], service.run_blocking())


def merge_devices(existing: Iterable[PrinterInfo], found: Iterable[PrinterInfo]) -> list[PrinterInfo]:
    """把搜索结果合并进已有列表（按序列号或 IP 去重）。

    与 :meth:`DiscoveryService._register` 同样的道理：只按
    ``serial or ip`` 去重是不够的 —— 同一台设备在一条记录里带序列号、
    在另一条里只认得出 IP 时，会被当成两台加进去。所以序列号与 IP 都要查。
    """
    result: list[PrinterInfo] = []
    index: dict[str, PrinterInfo] = {}

    def lookup(info: PrinterInfo) -> Optional[PrinterInfo]:
        if info.serial and info.serial in index:
            return index[info.serial]
        if info.ip and info.ip in index:
            return index[info.ip]
        return None

    def remember(info: PrinterInfo) -> None:
        if info.serial:
            index[info.serial] = info
        if info.ip:
            index[info.ip] = info

    for info in existing:
        result.append(info)
        remember(info)
    for info in found:
        match = lookup(info)
        if match is None:
            result.append(info)
            remember(info)
        else:
            if not match.ip:
                match.ip = info.ip
            if not match.serial:
                match.serial = info.serial
            if not match.name:
                match.name = info.name
            if not match.firmware:
                match.firmware = info.firmware
            if not match.model.is_known and info.model.is_known:
                match.model = info.model
            remember(match)  # 补齐后新键也要指向同一条记录
    return result
