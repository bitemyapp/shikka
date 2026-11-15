"""One optional NativeLink service per private user state directory.

The supervisor owns the lifetime lock; NativeLink inherits it so a killed
supervisor cannot lead to two writers opening the same CAS. Control uses a
private Unix socket, never a PID from a stale file.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import stat
import ssl
import subprocess
import sys
import time
import threading

from .project import atomic_write, executable, output, read, write_json


def state_directory(value=None):
    if value:
        path = Path(value).expanduser().absolute()
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        path = base / "shikka/service-v1"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Cache state must be a real directory owned by you with mode 0700: " + str(path))
    if len(os.fsencode(path / "control.sock")) >= 104:
        raise ValueError("Cache state path is too long for a Unix socket; choose a shorter --state-dir.")
    return path


def lock_file(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
        os.close(fd)
        raise ValueError("Unsafe cache lock: " + str(path))
    return os.fdopen(fd, "r+")


def request(state, operation):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(3)
        sock.connect(str(state / "control.sock"))
        sock.sendall((operation + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(65536)
            if not chunk or len(data) > 1024**2:
                raise RuntimeError("Cache supervisor returned an incomplete response")
            data += chunk
        result = json.loads(data)
        if "error" in result:
            raise ValueError(result["error"])
        return result


def status(state):
    try:
        return request(state, "status")
    except (FileNotFoundError, ConnectionRefusedError):
        with lock_file(state / "owner.lock") as guard:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"running": False, "starting_or_orphaned": True, "state": str(state)}
        return {"running": False, "state": str(state)}


def stop_service(state):
    if not status(state).get("running"):
        return status(state)
    request(state, "stop")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = status(state)
        if not current.get("running") and not current.get("starting_or_orphaned"):
            return current
        time.sleep(.05)
    raise TimeoutError("Cache did not stop; inspect service.log")


def ensure(state, binary, max_bytes):
    if max_bytes <= 0:
        raise ValueError("Cache size must be positive")
    binary = executable(binary, "nativelink")
    with lock_file(state / "startup.lock") as startup:
        fcntl.flock(startup, fcntl.LOCK_EX)
        current = status(state)
        if current.get("running"):
            if current["binary"] != binary or current["max_bytes"] != max_bytes:
                raise ValueError("Cache settings differ from the running service; stop it before changing version or size.")
            return current
        if current.get("starting_or_orphaned"):
            raise ValueError("Cache storage is already owned by a starting or orphaned service; inspect service.log before recovery.")
        version = output([binary, "--version"])
        if "1.8.3" not in version.split():
            raise ValueError("Managed local-CAS layout is qualified with NativeLink 1.8.3; use that version or a remote cache config.")
        with (state / "supervisor.log").open("ab") as log:
            process = subprocess.Popen([sys.executable, "-m", "shikka.service", "--state", str(state), "--binary", binary,
                                        "--max-bytes", str(max_bytes)], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True, close_fds=True,
                                       env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent) + os.pathsep + os.environ.get("PYTHONPATH", "")))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Cache startup failed; inspect " + str(state / "supervisor.log"))
            current = status(state)
            if current.get("running"):
                # Reap it if this caller stays alive (e.g. library/test usage).
                threading.Thread(target=process.wait, daemon=True).start()
                return current
            time.sleep(.05)
        process.terminate()
        process.wait(timeout=10)
        raise TimeoutError("NativeLink startup timed out")


@contextlib.contextmanager
def lease(state, binary, max_bytes):
    # Serialize startup with stop until our shared activity lease is held.
    with lock_file(state / "leases.lock") as guard:
        fcntl.flock(guard, fcntl.LOCK_SH)
        info = ensure(state, binary, max_bytes)
        yield info


def configuration(state, port, max_bytes):
    def filesystem(name, size):
        return {"filesystem": {"content_path": str(state / name / "content"), "temp_path": str(state / name / "tmp"),
                               "eviction_policy": {"max_bytes": size}}}
    services = {kind: [{"instance_name": "shikka", key: store}] for kind, key, store in [
        ("cas", "cas_store", "cas"), ("bytestream", "cas_store", "cas"), ("ac", "ac_store", "ac")]}
    services["capabilities"] = [{"instance_name": "shikka", "remote_execution": {"scheduler": "scheduler"}}]
    services["health"] = {}
    return {"stores": [
        {"name": "cas", "verify": {"verify_hash": True, "verify_size": True, "backend": filesystem("cas", max_bytes)}},
        {"name": "ac", "completeness_checking": {"backend": filesystem("ac", min(max_bytes, 256 * 1024**2)), "cas_store": {"ref_store": {"name": "cas"}}}}],
        "schedulers": [{"name": "scheduler", "simple": {"supported_platform_properties": {
            "OSFamily": "exact", "ISA": "exact", "shikka.toolchain": "exact"}}}],
        "servers": [{"name": "local", "listener": {"http": {"socket_address": f"127.0.0.1:{port}", "tls": {
            "cert_file": str(state / "tls/endpoint.crt"), "key_file": str(state / "tls/endpoint.key"),
            "client_ca_file": str(state / "tls/ca.crt")}}}, "services": services}]}


def certificates(state):
    tls = state / "tls"
    if (tls / "client.pem").is_file():
        return
    import tempfile
    with tempfile.TemporaryDirectory(prefix="tls-", dir=state) as temporary:
        directory = Path(temporary)
        openssl = executable(None, "openssl")
        commands = [
            ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.crt", "-days", "3650", "-subj", "/CN=Shikka local cache CA", "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
            ["req", "-newkey", "rsa:2048", "-nodes", "-keyout", "endpoint.key", "-out", "endpoint.csr", "-subj", "/CN=localhost"],
            ["x509", "-req", "-in", "endpoint.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-out", "endpoint.crt", "-days", "3650", "-extfile", "extensions"],
        ]
        (directory / "extensions").write_text("basicConstraints=critical,CA:FALSE\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth,clientAuth\nsubjectAltName=IP:127.0.0.1,DNS:localhost\n")
        for command in commands:
            subprocess.run([openssl, *command], cwd=directory, check=True, capture_output=True)
        (directory / "client.pem").write_bytes((directory / "endpoint.crt").read_bytes() + (directory / "endpoint.key").read_bytes())
        for path in directory.iterdir(): path.chmod(0o600)
        # Only a completed credential set becomes visible to a service.
        os.rename(directory, tls)


def serve(state, binary, max_bytes):
    os.umask(0o077)
    child = None
    stop = False
    def terminate(_signum, _frame):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    with lock_file(state / "owner.lock") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        certificates(state)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        write_json(state / "service.json", configuration(state, port, max_bytes))
        control = state / "control.sock"
        control.unlink(missing_ok=True)
        activity = None
        try:
            with (state / "service.log").open("ab") as log:
                child = subprocess.Popen([binary, str(state / "service.json")], stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=log, pass_fds=(owner.fileno(),))
            context = ssl.create_default_context(cafile=str(state / "tls/ca.crt"))
            context.load_cert_chain(str(state / "tls/client.pem"))
            context.set_alpn_protocols(["h2"])
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if stop or child.poll() is not None:
                    raise RuntimeError("NativeLink exited during startup")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1) as connection:
                        with context.wrap_socket(connection, server_hostname="localhost"):
                            break
                except OSError as error:
                    last_error = error
                    time.sleep(.05)
            else:
                raise TimeoutError("NativeLink readiness failed: " + str(last_error))
            info = {"running": True, "state": str(state), "binary": binary, "max_bytes": max_bytes,
                    "port": port, "cas": str(state / "cas/content"), "supervisor_pid": os.getpid(), "pid": child.pid}
            write_json(state / "service-state.json", info)
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(control))
                server.listen(8)
                server.settimeout(.2)
                while not stop and child.poll() is None:
                    try: connection, _ = server.accept()
                    except TimeoutError: continue
                    with connection:
                        connection.settimeout(2)
                        try:
                            operation = connection.recv(64).decode().strip()
                            response = info
                            if operation == "stop":
                                activity = lock_file(state / "leases.lock")
                                try:
                                    fcntl.flock(activity, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                except BlockingIOError:
                                    activity.close()
                                    activity = None
                                    response = {"error": "Cache has active builds; stop after they finish."}
                                else:
                                    stop = True
                                    response = {"stopping": True}
                            elif operation != "status":
                                response = {"error": "Unknown cache operation"}
                            connection.sendall((json.dumps(response) + "\n").encode())
                        except (TimeoutError, OSError, UnicodeError):
                            continue
        finally:
            control.unlink(missing_ok=True)
            if child is not None and child.poll() is None:
                child.terminate()
                try: child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            (state / "service-state.json").unlink(missing_ok=True)
            if activity is not None:
                activity.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--max-bytes", required=True, type=int)
    args = parser.parse_args()
    serve(state_directory(args.state), args.binary, args.max_bytes)
