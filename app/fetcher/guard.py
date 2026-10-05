"""拒绝指向本机 / 内网 / 云元数据地址的 URL。

为什么需要：抓取与连通性测试都是**服务端发起**的请求。一个能填 URL 的用户
（或被 CSRF 驱动的浏览器）可以让服务去访问 127.0.0.1、10.0.0.0/8、
169.254.169.254 这类地址，然后把响应体存成文章正文再从 /story/{id} 读出来
—— 这是标准的 SSRF 回传通道。实测 ``POST /settings`` 填
``http://127.0.0.1:18080`` 时确实被服务端拨号了（返回 Connection refused，
说明请求发出去了）。

只做**字面检查**不够：``http://127.1``、``http://2130706433``、
``http://[::1]``、``0x7f000001`` 都是环回地址的别名。所以要真正解析一次
主机名。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


def _is_blocked_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """这个地址是否属于不该被服务端主动访问的网段。"""
    if addr.is_loopback:
        return True
    if addr.is_link_local:
        return True          # 169.254.0.0/16，含云厂商的元数据地址
    if addr.is_private:
        return True          # 10/8、172.16/12、192.168/16、fc00::/7
    if addr.is_reserved or addr.is_multicast or addr.is_unspecified:
        return True
    # IPv4 映射成 IPv6 的写法（::ffff:127.0.0.1）同样要挡住
    mapped = getattr(addr, "ipv4_mapped", None)
    return bool(mapped is not None and _is_blocked_ip(mapped))


def is_internal_host(host: str) -> bool:
    """主机名是否会解析到内网地址。解析不了就当作「拦下」而不是放行。"""
    name = (host or "").strip().strip("[]")
    if not name:
        return True
    # 字面 IP 先直接判，省掉一次 DNS
    try:
        return _is_blocked_ip(ipaddress.ip_address(name))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False        # 解析失败交给真正的抓取去报错，不必在这里拦
    for info in infos:
        try:
            if _is_blocked_ip(ipaddress.ip_address(info[4][0])):
                return True
        except ValueError:
            continue
    return False


def is_metadata_host(host: str) -> bool:
    """主机名是否是云厂商元数据地址（169.254.169.254 之类）。

    **只拦这一类**，不拦环回和整个内网 —— 因为这个应用本来就是单用户自托管的，
    而且这两种情况都是正当用法：
      · 把 RSS 指到本机/局域网的 FreshRSS、miniflux 桥接；
      · 网关就架在内网（实测这台机器的 LLM 入口就是 host.docker.internal，
        解析出来是 192.168.65.254）。
    一刀切拦掉内网会把这个项目自己的部署方式打死。

    真正的 SSRF 入口（跨站表单提交）由 :func:`app.main._same_origin_guard`
    在源头拦住了；这一层是纵深防御，补的是「本机用户/脚本误配」这条路径。
    """
    name = (host or "").strip().strip("[]")
    if not name:
        return True
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        # 主机名：只有解析到 link-local 才算（.internal 之类不会命中）
        try:
            infos = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            return False
        for info in infos:
            try:
                candidate = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if candidate.is_link_local or candidate.is_unspecified:
                return True
        return False
    return addr.is_link_local or addr.is_unspecified


def blocked_reason(url: str) -> str:
    """返回拦截原因；``""`` 表示放行。"""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return "地址格式不对"
    if not host:
        return "地址里没有主机名"
    if is_metadata_host(host):
        return f"{host} 是本机/链路本地地址（云元数据接口一类），出于安全考虑不能抓取"
    return ""
