"""One-shot detection and restart of the Vesktop instance being modified."""

import json
import os
from pathlib import Path
import signal
import subprocess
import time


APP_ID = "dev.vencord.Vesktop"


def flatpak_instances() -> list[str]:
    output = subprocess.run(["flatpak", "ps", "--json"], check=True, capture_output=True, text=True)
    return [item["instance"] for item in json.loads(output.stdout) if item.get("application") == APP_ID]


def native_processes() -> dict[int, list[str]]:
    result = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != os.getuid() or entry.joinpath("comm").read_text().strip().lower() != "vesktop":
                continue
            argv = [part.decode(errors="replace") for part in entry.joinpath("cmdline").read_bytes().split(b"\0") if part]
            if not argv or any(arg.startswith("--type=") for arg in argv):
                continue
            environment = entry.joinpath("environ").read_bytes()
            if b"FLATPAK_ID=dev.vencord.Vesktop\0" in environment:
                continue
            if any(item.startswith(b"APPIMAGE=") for item in environment.split(b"\0")) or ".appimage" in argv[0].lower():
                raise ValueError("The experimental patch supports Flatpak and system packages; close the AppImage and use a supported installation")
            executable = Path(argv[0])
            if executable.name.lower() != "vesktop" and "vesktop" not in executable.name.lower():
                continue
            result[int(entry.name)] = argv
        except (OSError, UnicodeError):
            continue
    return result


def running(kind: str) -> dict:
    if kind == "Flatpak":
        instances = flatpak_instances()
        return {"kind": kind, "instances": instances} if instances else {}
    processes = native_processes()
    if not processes:
        return {}
    commands = list({tuple(argv) for argv in processes.values()})
    return {"kind": kind, "pids": list(processes), "commands": commands}


def stop(info: dict) -> None:
    if not info:
        return
    if info["kind"] == "Flatpak":
        for instance in info["instances"]:
            try:
                subprocess.run(["flatpak", "kill", instance], check=True)
            except subprocess.CalledProcessError:
                if instance in flatpak_instances():
                    raise
        deadline = time.monotonic() + 10
        while any(instance in flatpak_instances() for instance in info["instances"]):
            if time.monotonic() >= deadline:
                raise RuntimeError("Vesktop Flatpak did not stop in time")
            time.sleep(0.1)
    else:
        for pid in info["pids"]:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 10
        while any(Path(f"/proc/{pid}").exists() for pid in info["pids"]):
            if time.monotonic() >= deadline:
                raise RuntimeError("Vesktop did not stop in time")
            time.sleep(0.1)


def start(info: dict) -> None:
    if not info:
        return
    commands = [["flatpak", "run", APP_ID]] if info["kind"] == "Flatpak" else info["commands"]
    for command in commands:
        subprocess.Popen(command, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"Started Vesktop ({info['kind']}).")
