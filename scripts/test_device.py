#!/usr/bin/env python3
"""Run a foreground test with a disposable simulator or QEMU process."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import uuid

SIMCTL = ["/usr/bin/xcrun", "simctl"]


class Cancelled(Exception):
    pass


@contextmanager
def child(argv, env):
    """Own the process group, including test helpers left running after the test."""
    proc = subprocess.Popen(argv, env=env, start_new_session=True)
    try:
        yield proc
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                proc.poll()
                os.killpg(proc.pid, 0)
                time.sleep(0.05)
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # On macOS, killpg() on a zombie-only process group raises EPERM instead of ESRCH.
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        proc.wait()


def run_test(argv, env, checkpoint):
    checkpoint()
    with child(argv, env) as proc:
        while proc.poll() is None:
            checkpoint()
            time.sleep(0.15)
        return proc.returncode if proc.returncode >= 0 else 128 - proc.returncode


def dispose_simulator(name, udid):
    # The unique name also covers simctl creating a device but failing before returning its ID.
    for attempt in range(3):
        try:
            devices = json.loads(subprocess.check_output(
                SIMCTL + ["list", "devices", "-j"], timeout=30))["devices"]
            key, value = ("udid", udid) if udid else ("name", name)
            matches = [d for group in devices.values() for d in group if d.get(key) == value]
            if not matches:
                return
            if len(matches) != 1:
                raise ValueError(f"ambiguous simulator identity: {name}")
            target = str(uuid.UUID(matches[0]["udid"])).upper()
            if matches[0]["state"] != "Shutdown":
                subprocess.run(SIMCTL + ["shutdown", target], check=True, timeout=90)
            subprocess.run(SIMCTL + ["delete", target], check=True, timeout=90)
            return
        except (OSError, subprocess.SubprocessError):
            if attempt == 2:
                print(f"Simulator cleanup needs attention: {name} {udid or ''}", file=sys.stderr)
                raise
            time.sleep(1)


def simulator(args, test, checkpoint):
    name = f"agent-tmp-job-{os.getpid()}-{uuid.uuid4().hex}"
    udid = None
    try:
        command = SIMCTL + ["create", name, args.type]
        if args.runtime:
            command.append(args.runtime)
        raw = subprocess.check_output(command, text=True, timeout=120).strip()
        udid = str(uuid.UUID(raw)).upper()
        env = {**os.environ, "AGENT_TEST_UDID": udid}
        print(f"Temporary simulator {name} {udid}", file=sys.stderr, flush=True)
        rc = run_test(SIMCTL + ["bootstatus", udid, "-b"], env, checkpoint)
        return rc or run_test(test, env, checkpoint)
    finally:
        dispose_simulator(name, udid)


def _disables_snapshot(option):
    """`snapshot=<false>` in any spelling QEMU's boolean parser accepts (off/no/false/n/0)."""
    key, _, value = option.partition("=")
    return key.strip().lower() == "snapshot" and value.strip().lower() in ("off", "no", "false", "n", "0")


def qemu(args, test, checkpoint):
    for index, arg in enumerate(args.launch[1:], start=1):
        if arg in ("-daemonize", "--daemonize"):
            raise ValueError("The temporary VM must run in the foreground")
        if arg in ("-blockdev", "--blockdev"):
            raise ValueError("Use a snapshot-compatible -drive for temporary disk images")
        if arg in ("-readconfig", "--readconfig") or arg.startswith(("-readconfig=", "--readconfig=")):
            # A config file can define drives and block devices that bypass the checks below.
            raise ValueError("Pass temporary VM drives on the command line, not through -readconfig")
        if arg in ("-drive", "--drive") and index + 1 < len(args.launch):
            # QEMU escapes a comma inside a filename by doubling it.
            options = args.launch[index + 1].replace(",,", "\0").split(",")
            if any(_disables_snapshot(o) for o in options):
                raise ValueError("Temporary VM drives must keep snapshots enabled")
    # A short private path fits the Unix domain socket limit on macOS.
    with tempfile.TemporaryDirectory(prefix="agent-vm-", dir="/tmp") as directory:
        monitor = Path(directory) / "qmp.sock"
        command = [*args.launch, "-snapshot", "-qmp", f"unix:{monitor},server=on,wait=off"]
        env = {**os.environ, "AGENT_TEST_VM_DIR": directory, "AGENT_TEST_QMP": str(monitor)}
        deadline = time.monotonic() + args.startup_timeout
        with child(command, {**env, "TMPDIR": directory}) as vm:
            while vm.poll() is None and not monitor.is_socket():
                checkpoint()
                if time.monotonic() >= deadline:
                    raise RuntimeError("QEMU did not create its monitor before the startup timeout")
                time.sleep(0.1)
            if vm.poll() is not None:
                raise RuntimeError(f"QEMU exited during startup (status {vm.returncode})")
            env["AGENT_TEST_VM_PID"] = str(vm.pid)

            def check_vm():
                checkpoint()
                if vm.poll() is not None:
                    raise RuntimeError(f"QEMU exited during test (status {vm.returncode})")

            return run_test(test, env, check_vm)


def parse(argv):
    p = argparse.ArgumentParser(prog="agent-test-device", description=__doc__, epilog="Append -- followed by the foreground test command.")
    modes = p.add_subparsers(dest="backend", required=True)
    sim = modes.add_parser("simulator", help="Create an iOS simulator and export AGENT_TEST_UDID")
    sim.add_argument("--type", required=True, help="simctl device type")
    sim.add_argument("--runtime", help="simctl runtime (defaults to latest compatible)")
    vm = modes.add_parser("qemu", help="Run QEMU with temporary disk snapshots and export AGENT_TEST_QMP")
    vm.add_argument("--startup-timeout", type=int, default=120, metavar="SECONDS",
                    help="Time to wait for the QEMU monitor (default: 120)")
    vm.add_argument("launch", nargs=argparse.REMAINDER, help="QEMU executable and foreground arguments")
    split = argv.index("--") if "--" in argv else len(argv)
    args = p.parse_args(argv[:split])
    if split + 1 >= len(argv) or (args.backend == "qemu" and not args.launch):
        p.error("expected simulator options or a QEMU launch command, then -- TEST [ARGS ...]")
    return args, argv[split + 1:]


def main(argv):
    # The private helper owns the device. Its pipe reaches EOF even if the public
    # launcher is killed with SIGKILL, in which case a normal finally in the launcher cannot run.
    owner_fd = None
    if argv and argv[0].startswith("--owner-fd="):
        owner_fd = int(argv.pop(0).partition("=")[2])
    args, test = parse(argv)
    cancelled = 0

    def on_signal(signum, _frame):
        nonlocal cancelled
        cancelled = signum

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)
    if owner_fd is not None:
        def checkpoint():
            if cancelled or select.select([owner_fd], [], [], 0)[0]:
                raise Cancelled()
        try:
            checkpoint()
            return (simulator if args.backend == "simulator" else qemu)(args, test, checkpoint)
        except Cancelled:
            return 128 + (cancelled or signal.SIGTERM)
        finally:
            os.close(owner_fd)

    reader, writer = os.pipe()
    try:
        supervisor = subprocess.Popen([sys.executable, __file__, f"--owner-fd={reader}", *argv],
                                      pass_fds=(reader,), start_new_session=True)
    finally:
        os.close(reader)
    try:
        while not cancelled and supervisor.poll() is None:
            time.sleep(0.15)
    finally:
        os.close(writer)
    rc = supervisor.wait()
    return 128 + cancelled if cancelled else rc


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.exit(f"agent-test-device: {exc}")
