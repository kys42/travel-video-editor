#!/usr/bin/env python3
"""Read-only preflight for the travel-video pipeline."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


REQUIRED_COMMANDS = ("ffmpeg", "ffprobe", "uv")


def discover_project(explicit: str | None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("TRAVEL_VIDEO_PROJECT_ROOT"):
        candidates.append(Path(os.environ["TRAVEL_VIDEO_PROJECT_ROOT"]))
    candidates.extend([Path.cwd(), *Path.cwd().parents])
    fallback = Path("/Users/kys/projects/travel-video-editor")
    if fallback.exists():
        candidates.append(fallback)
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if (resolved / "pyproject.toml").is_file() and (
            resolved / "src/travel_video"
        ).is_dir():
            return resolved
    return None


def command_info(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    info: dict[str, Any] = {"available": path is not None, "path": path}
    if path:
        try:
            result = subprocess.run(
                [path, "-version" if name in {"ffmpeg", "ffprobe"} else "--version"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            first_line = (result.stdout or result.stderr).splitlines()
            info["version"] = first_line[0] if first_line else None
        except (OSError, subprocess.SubprocessError) as exc:
            info["version_error"] = str(exc)
    return info


def active_media_processes() -> list[dict[str, Any]]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    active: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        if "travel-video proxy-batch" in command or (
            "ffmpeg" in command and "1080p-h264" in command
        ):
            active.append({"pid": int(pid_text), "command": command})
    return active


def path_info(path_text: str | None) -> dict[str, Any]:
    if not path_text:
        return {"configured": False, "path": None, "exists": False}
    path = Path(path_text).expanduser()
    resolved = path.resolve()
    info: dict[str, Any] = {
        "configured": True,
        "path": str(path),
        "resolved": str(resolved),
        "exists": resolved.exists(),
        "is_dir": resolved.is_dir(),
        "writable": os.access(resolved, os.W_OK) if resolved.exists() else False,
    }
    if resolved.exists():
        usage = shutil.disk_usage(resolved)
        info["free_bytes"] = usage.free
        info["total_bytes"] = usage.total
    return info


def overlaps(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root")
    parser.add_argument("--source-root")
    parser.add_argument("--working-root")
    parser.add_argument(
        "--require-idle-proxy",
        action="store_true",
        help="Fail when a proxy-batch or proxy ffmpeg process is active",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()

    project = discover_project(args.project_root)
    source_text = args.source_root or os.environ.get("SOURCE_MEDIA_ROOT")
    working_text = args.working_root or os.environ.get("WORKING_MEDIA_ROOT")
    source = path_info(source_text)
    working = path_info(working_text)
    commands = {name: command_info(name) for name in (*REQUIRED_COMMANDS, "swift")}
    active_processes = active_media_processes()
    failures: list[str] = []
    warnings: list[str] = []

    if project is None:
        failures.append("travel-video-editor project root was not found")
    for name in REQUIRED_COMMANDS:
        if not commands[name]["available"]:
            failures.append(f"required command is unavailable: {name}")
    if not source["configured"]:
        warnings.append("source root is not configured; set SOURCE_MEDIA_ROOT")
    elif not source["is_dir"]:
        failures.append("configured source root is not an existing directory")
    if not working["configured"]:
        warnings.append("working root is not configured; set WORKING_MEDIA_ROOT")
    elif not working["is_dir"]:
        failures.append("configured working root is not an existing directory")
    elif not working["writable"]:
        failures.append("configured working root is not writable")
    if source.get("is_dir") and working.get("is_dir"):
        source_path = Path(str(source["resolved"]))
        working_path = Path(str(working["resolved"]))
        if overlaps(source_path, working_path):
            failures.append("source and working roots overlap")
        if source["writable"]:
            warnings.append(
                "source volume is writable; policy still requires treating it as immutable"
            )
    if active_processes:
        message = (
            "an active proxy-batch/ffmpeg process was detected; "
            "do not start a colliding batch"
        )
        if args.require_idle_proxy:
            failures.append(message)
        else:
            warnings.append(message)

    apple: dict[str, Any] = {
        "supported_os": platform.system() == "Darwin"
        and int(platform.mac_ver()[0].split(".")[0] or 0) >= 26,
        "swift_available": commands["swift"]["available"],
    }
    if project:
        venv_python = project / ".venv/bin/python"
        project_cli = project / ".venv/bin/travel-video"
        runtime: dict[str, Any] = {
            "python": str(venv_python),
            "python_exists": venv_python.is_file(),
            "travel_video": str(project_cli),
            "travel_video_exists": project_cli.is_file(),
            "help_ok": False,
        }
        if venv_python.is_file():
            version_result = subprocess.run(
                [str(venv_python), "--version"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            runtime["python_version"] = (
                version_result.stdout or version_result.stderr
            ).strip()
            version_probe = subprocess.run(
                [
                    str(venv_python),
                    "-c",
                    "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            runtime["python_major_minor"] = version_probe.stdout.strip()
            try:
                major, minor = (
                    int(part) for part in runtime["python_major_minor"].split(".")
                )
                runtime["python_requirement_ok"] = (major, minor) >= (3, 11)
            except (TypeError, ValueError):
                runtime["python_requirement_ok"] = False
        if project_cli.is_file():
            help_result = subprocess.run(
                [str(project_cli), "--help"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            runtime["help_ok"] = help_result.returncode == 0
            if not runtime["help_ok"]:
                runtime["help_error"] = help_result.stderr.strip()
        if not runtime["python_exists"] or not runtime["travel_video_exists"]:
            failures.append("project virtual environment is missing; run uv sync")
        elif not runtime.get("python_requirement_ok"):
            failures.append("project Python must satisfy >=3.11")
        elif not runtime["help_ok"]:
            failures.append("project travel-video CLI is not runnable")
        report_runtime = runtime
        package = project / "apple-speech" / "Package.swift"
        worker = project / "apple-speech" / ".build/release/apple-speech"
        apple.update(
            {
                "package": str(package),
                "package_exists": package.is_file(),
                "worker": str(worker),
                "worker_built": worker.is_file(),
                "capabilities_command": (
                    f"{worker} capabilities --output "
                    f"{project / 'work/apple-speech/capabilities.json'}"
                ),
            }
        )
        if package.is_file() and not worker.is_file():
            warnings.append("Apple Speech worker is not built")
        if not apple["supported_os"]:
            warnings.append("Apple Speech path requires macOS 26 or newer")

    report = {
        "schema_version": "travel-video-environment/v1",
        "ok": not failures,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "macos": platform.mac_ver()[0] or None,
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "project_root": str(project) if project else None,
        "source_root": source,
        "working_root": working,
        "commands": commands,
        "project_runtime": report_runtime if project else None,
        "active_media_processes": active_processes,
        "apple_speech": apple,
        "failures": failures,
        "warnings": warnings,
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
