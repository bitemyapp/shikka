"""Real managed NativeLink lifecycle; set SHIKKA_TEST_NATIVELINK to enable."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import socket
import ssl
import tempfile
import unittest
from shikka import service


class Locks(unittest.TestCase):
    def test_symlink_and_hardlink_locks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("unchanged")
            target.chmod(0o600)
            (root / "symlink").symlink_to(target)
            os.link(target, root / "hardlink")
            for name in ["symlink", "hardlink"]:
                with self.assertRaises((OSError, ValueError)):
                    service.lock_file(root / name)
            self.assertEqual(target.read_text(), "unchanged")


@unittest.skipUnless(os.environ.get("SHIKKA_TEST_NATIVELINK"), "requires NativeLink 1.8.3")
class Lifecycle(unittest.TestCase):
    def test_concurrent_start_authentication_leases_and_restart(self):
        with tempfile.TemporaryDirectory(prefix="shikka-service-") as directory:
            state = service.state_directory(directory)
            binary = os.environ["SHIKKA_TEST_NATIVELINK"]
            try:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    starts = list(pool.map(lambda _: service.ensure(state, binary, 64 * 1024**2), range(3)))
                self.assertEqual(len({info["pid"] for info in starts}), 1)
                first = starts[0]
                context = ssl.create_default_context(cafile=str(state / "tls/ca.crt"))
                # TLS 1.3 may reject at first read rather than handshake.
                with self.assertRaises((ssl.SSLError, ConnectionResetError)):
                    with socket.create_connection(("127.0.0.1", first["port"]), timeout=3) as raw:
                        with context.wrap_socket(raw, server_hostname="localhost") as client:
                            client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                            client.recv(4096)
                context.load_cert_chain(str(state / "tls/client.pem"))
                context.set_alpn_protocols(["h2"])
                with socket.create_connection(("127.0.0.1", first["port"]), timeout=3) as raw:
                    with context.wrap_socket(raw, server_hostname="localhost") as client:
                        self.assertTrue(client.getpeercert())
                with service.lease(state, binary, 64 * 1024**2):
                    with self.assertRaisesRegex(ValueError, "active builds"):
                        service.stop_service(state)
                self.assertFalse(service.stop_service(state)["running"])
                second = service.ensure(state, binary, 64 * 1024**2)
                self.assertNotEqual(first["pid"], second["pid"])
                self.assertEqual(first["cas"], second["cas"])
            finally:
                service.stop_service(state)


if __name__ == "__main__":
    unittest.main()
