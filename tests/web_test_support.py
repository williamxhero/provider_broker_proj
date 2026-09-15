import socket


_CHROMIUM_BLOCKED_PORTS = frozenset({
    1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53,
    69, 77, 79, 87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115,
    117, 119, 123, 135, 137, 139, 143, 161, 179, 389, 427, 465, 512,
    513, 514, 515, 526, 530, 531, 532, 540, 548, 554, 556, 563, 587,
    601, 636, 989, 990, 993, 995, 2049, 3659, 4045, 6000, 6665, 6666,
    6667, 6668, 6669, 6697,
})


def safe_loopback_socket() -> socket.socket:
    """Reserve an ephemeral loopback port accepted by Chromium."""
    while True:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        if sock.getsockname()[1] not in _CHROMIUM_BLOCKED_PORTS:
            sock.listen(socket.SOMAXCONN)
            return sock
        sock.close()
