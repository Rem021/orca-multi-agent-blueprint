#!/usr/bin/env python3
"""Read USB enumeration identity; detect reconnects missed by mount polling (parameterized)."""
import argparse
import datetime
import fcntl
import json
import os
import pathlib
import plistlib
import subprocess
import tempfile


def command(argv):
    return subprocess.check_output(argv, timeout=25, stderr=subprocess.PIPE)


def contains_disk(node, disk):
    return node.get("BSD Name") == disk or any(
        contains_disk(child, disk) for child in node.get("IORegistryEntryChildren", [])
    )


def leaf_usb_matches(node, disk):
    matches = []
    for child in node.get("IORegistryEntryChildren", []):
        matches.extend(leaf_usb_matches(child, disk))
    if matches:
        return matches
    if node.get("sessionID") and node.get("idVendor") is not None and contains_disk(node, disk):
        return [node]
    return []


def snapshot(vol_path: str, expected_uuid: str):
    sample = {
        "observed_at": datetime.datetime.now().astimezone().isoformat(),
        "state": "absent",
        "volume_path": vol_path,
        "volume_uuid": expected_uuid,
    }
    if not os.path.ismount(vol_path):
        return sample
    volume = plistlib.loads(command(["/usr/sbin/diskutil", "info", "-plist", vol_path]))
    actual_uuid = volume.get("VolumeUUID", "")
    if volume.get("MountPoint") != vol_path:
        sample["state"] = "wrong_volume"
        return sample
    if expected_uuid and expected_uuid not in ("AUTO", "LOCAL", "INTERNAL") and actual_uuid != expected_uuid:
        sample["state"] = "wrong_volume"
        return sample
    sample["volume_uuid"] = actual_uuid or expected_uuid
    sample["state"] = "ok"
    sample["boot_uuid"] = command(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"]).decode().strip()
    try:
        devices = plistlib.loads(command(["/usr/sbin/ioreg", "-a", "-r", "-c", "IOUSBHostDevice", "-l"]))
        unique = {}
        for root in devices:
            for device in leaf_usb_matches(root, volume["DeviceIdentifier"]):
                unique[device["IORegistryEntryID"]] = device
        matches = list(unique.values())
        if len(matches) == 1 and matches[0].get("sessionID"):
            device = matches[0]
            sample.update(
                usb_session_id=device["sessionID"],
                usb_registry_id=device["IORegistryEntryID"],
                usb_vid=device.get("idVendor"),
                usb_pid=device.get("idProduct"),
            )
    except Exception:
        pass
    return sample


def transition(previous, current):
    """An OS reboot, first sample, or observed absence is not a missed reconnect."""
    if (
        previous.get("state") == current.get("state") == "ok"
        and previous.get("volume_uuid") == current.get("volume_uuid")
        and previous.get("boot_uuid") == current.get("boot_uuid")
        and previous.get("usb_session_id") is not None
        and current.get("usb_session_id") is not None
        and previous["usb_session_id"] != current["usb_session_id"]
    ):
        return {
            "event": "usb_reenumerated_between_samples",
            "since": previous["observed_at"],
            "observed_at": current["observed_at"],
            "volume_uuid": current.get("volume_uuid"),
            "previous_session_id": previous["usb_session_id"],
            "current_session_id": current["usb_session_id"],
            "meaning": "USB session changed while both mount samples were present; cause unknown",
        }
    return None


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".usb-observation-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def poll(vol_path: str, expected_uuid: str, state_path: pathlib.Path, events_path: pathlib.Path):
    try:
        previous = json.loads(state_path.read_text())
    except (FileNotFoundError, ValueError):
        previous = {}
    try:
        current = snapshot(vol_path, expected_uuid)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return "busy"
    event = transition(previous, current)
    current["missed_reconnects_observed"] = previous.get("missed_reconnects_observed", 0) + bool(event)
    if event:
        current["last_reconnect"] = event
        with events_path.open("a") as output:
            output.write(json.dumps(event, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
    elif previous.get("last_reconnect"):
        current["last_reconnect"] = previous["last_reconnect"]
    atomic_json(state_path, current)
    return "reconnected" if event else current["state"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--volume", default=os.environ.get("AI_VOLUME_PATH", "/Volumes/AI_Workspace"))
    parser.add_argument("--uuid", default=os.environ.get("AI_VOLUME_UUID", "AUTO"))
    parser.add_argument("--state", type=pathlib.Path, required=True)
    parser.add_argument("--events", type=pathlib.Path, required=True)
    args = parser.parse_args()
    with pathlib.Path(str(args.state) + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("busy")
            return
        print(poll(args.volume, args.uuid, args.state, args.events))


if __name__ == "__main__":
    main()
