import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/test_device.py"
sys.path.insert(0, str(SCRIPT.parent))
import test_device as device


class SimulatorTests(unittest.TestCase):
    target = "AAAAAAAA-BBBB-CCCC-DDDD-FFFFFFFFFFFF"

    def test_test_failure_uses_booted_id_and_triggers_cleanup(self):
        args = argparse.Namespace(type="phone", runtime="runtime")
        with mock.patch.object(device.subprocess, "check_output", return_value=self.target), \
             mock.patch.object(device, "run_test", side_effect=[0, 17]) as run, \
             mock.patch.object(device, "dispose_simulator") as dispose:
            self.assertEqual(device.simulator(args, ["test"], lambda: None), 17)
        boot, test = run.call_args_list
        self.assertEqual(boot.args[0], device.SIMCTL + ["bootstatus", self.target, "-b"])
        self.assertEqual(test.args[1]["AGENT_TEST_UDID"], self.target)
        self.assertEqual(dispose.call_args.args[1], self.target)

    def test_boot_failure_skips_test_and_cleans(self):
        args = argparse.Namespace(type="phone", runtime=None)
        with mock.patch.object(device.subprocess, "check_output", return_value=self.target), \
             mock.patch.object(device, "run_test", return_value=4) as run, \
             mock.patch.object(device, "dispose_simulator") as dispose:
            self.assertEqual(device.simulator(args, ["test"], lambda: None), 4)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(dispose.call_args.args[1], self.target)

    def test_disposal_uses_owned_id_even_after_rename(self):
        devices = {"runtime": [{"name": "renamed", "udid": self.target, "state": "Booted"},
                               {"name": "original", "udid": "other", "state": "Booted"}]}
        with mock.patch.object(device.subprocess, "check_output", return_value=json.dumps({"devices": devices})), \
             mock.patch.object(device.subprocess, "run") as run:
            device.dispose_simulator("original", self.target)
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [device.SIMCTL + [verb, self.target] for verb in ("shutdown", "delete")])


FAKE_QEMU = """
import os, socket, sys, time
from pathlib import Path
assert '-snapshot' in sys.argv
assert Path(os.environ['TMPDIR']) == Path(os.environ['AGENT_TEST_QMP']).parent
sock = socket.socket(socket.AF_UNIX)
sock.bind(os.environ['AGENT_TEST_QMP'])
sock.listen()
time.sleep(60)
"""

PROBE = """
import json, os, sys, time
from pathlib import Path
receipt = Path(sys.argv[1])
receipt.write_text(json.dumps([int(os.environ['AGENT_TEST_VM_PID']), os.environ['AGENT_TEST_VM_DIR'], os.getpid()]))
while not receipt.with_suffix('.done').exists():
    time.sleep(.05)
sys.exit(19)
"""


class QemuLifecycleTests(unittest.TestCase):
    def test_concurrent_instances_failure_and_owner_death(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            processes = []
            receipts = [root / "first.json", root / "second.json"]
            try:
                for receipt in receipts:
                    processes.append(subprocess.Popen([
                        sys.executable, str(SCRIPT), "qemu", sys.executable, "-c", FAKE_QEMU,
                        "--", sys.executable, "-c", PROBE, str(receipt)]))
                self.wait_until(lambda: all(p.is_file() and p.stat().st_size for p in receipts))
                first, second = (json.loads(r.read_text()) for r in receipts)
                self.assertNotEqual(first[0], second[0])
                self.assertNotEqual(first[1], second[1])
                processes[0].kill()
                processes[0].wait(timeout=10)
                self.wait_until(lambda: not Path(first[1]).exists())
                self.assertIsNone(processes[1].poll())
                os.kill(second[0], 0)
                receipts[1].with_suffix(".done").touch()
                self.assertEqual(processes[1].wait(timeout=15), 19)
                for vm_pid, directory, test_pid in (first, second):
                    self.assertFalse(Path(directory).exists())
                    for pid in (vm_pid, test_pid):
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
            finally:
                for proc in processes:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=15)

    def test_incompatible_disk_or_detached_launch_is_rejected_before_startup(self):
        for options in (["-blockdev", "file"], ["-drive", "file=base,snapshot=off"], ["-daemonize"]):
            with self.subTest(options=options), mock.patch.object(device.subprocess, "Popen") as launch:
                args, test = device.parse(["qemu", "qemu-system-arm", *options, "--", "test"])
                with self.assertRaises(ValueError):
                    device.qemu(args, test, lambda: None)
                launch.assert_not_called()

    def test_missing_monitor_times_out_and_disposes_process(self):
        with tempfile.TemporaryDirectory() as folder:
            receipt = Path(folder) / "dir"
            launch = "import os,sys,time; open(sys.argv[1],'w').write(os.environ['TMPDIR']); time.sleep(30)"
            r = subprocess.run([
                sys.executable, str(SCRIPT), "qemu", "--startup-timeout", "1",
                sys.executable, "-c", launch, str(receipt), "--", "false",
            ], timeout=15, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("startup timeout", r.stderr)
            self.assertFalse(Path(receipt.read_text()).exists())

    def wait_until(self, condition):
        deadline = time.monotonic() + 15
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("device lifecycle did not reach the expected state")
            time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
