"""Restart only the listener socket; no Portal workers or brokerage connections."""
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import Mock, call, patch

from app.portal_server import bind_portal_socket


class PortalSocketTests(unittest.TestCase):
    def test_posix_sets_reuse_before_binding_only_loopback(self):
        sock = Mock()
        with patch('app.portal_server.os.name', 'posix'):
            bind_portal_socket(sock, 5329)
        self.assertEqual(sock.mock_calls, [call.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1),
                                          call.bind(('127.0.0.1', 5329))])

    def test_windows_never_enables_address_reuse(self):
        sock = Mock()
        with patch('app.portal_server.os.name', 'nt'):
            bind_portal_socket(sock, 5329)
        sock.setsockopt.assert_not_called()
        sock.bind.assert_called_once_with(('127.0.0.1', 5329))

    @unittest.skipUnless(os.name == 'posix', 'POSIX listener semantics')
    def test_another_active_listener_cannot_share_the_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as first:
            bind_portal_socket(first, 0)
            first.listen(1)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as second:
                with self.assertRaises(OSError):
                    bind_portal_socket(second, first.getsockname()[1])

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Real Linux TCP TIME_WAIT regression')
    def test_graceful_tcp_close_rebinds_immediately_during_time_wait(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as first:
            bind_portal_socket(first, 0)
            first.listen(1)
            port = first.getsockname()[1]
            with socket.create_connection(('127.0.0.1', port), timeout=2) as client:
                accepted, _ = first.accept()
                with accepted:
                    accepted.settimeout(2)
                    accepted.sendall(b'ok')
                    self.assertEqual(client.recv(2), b'ok')
                    # The server performs the active close, so its local port
                    # remains in TIME_WAIT after the client finishes closing.
                    accepted.shutdown(socket.SHUT_WR)
                    self.assertEqual(client.recv(1), b'')
                    client.shutdown(socket.SHUT_WR)
                    self.assertEqual(accepted.recv(1), b'')
        rows = Path('/proc/net/tcp').read_text().splitlines()[1:]
        self.assertTrue(any(int(row.split()[1].split(':')[1], 16) == port and row.split()[3] == '06' for row in rows),
                        'Test must create an actual TIME_WAIT connection before rebinding')
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as plain:
            with self.assertRaises(OSError):
                plain.bind(('127.0.0.1', port))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as replacement:
            bind_portal_socket(replacement, port)
            replacement.listen(1)
            self.assertEqual(replacement.getsockname(), ('127.0.0.1', port))


if __name__ == '__main__':
    unittest.main()
