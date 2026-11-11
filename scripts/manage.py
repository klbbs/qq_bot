#!/usr/bin/env python3
"""Keep a sanitised source archive and safely deploy QQ-only changes locally."""
from __future__ import annotations

import argparse
import ast
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
QQ = Path.home() / ".local/share/qq-hermes"
HERMES = Path.home() / ".hermes"
CONTENT = Path.home() / ".local/share/hermes-content"
PYTHON = QQ / ".venv/bin/python"
PRIVATE = REPO / ".local-settings.json"
SERVICE = "qq-hermes-nonebot.service"


def run(args, *, check=True):
    r = subprocess.run([str(a) for a in args], cwd=REPO, text=True, capture_output=True)
    if check and r.returncode:
        raise RuntimeError("Command failed: " + str(args[0]))
    return r


def settings():
    if PRIVATE.exists():
        data = json.loads(PRIVATE.read_text())
        control = QQ / "qq_control.py"
        if "__QQ_DOCUMENT_GROUP__" not in data and control.exists():
            match = re.search(r"group_id (\d{7,12})", control.read_text())
            if match:
                data["__QQ_DOCUMENT_GROUP__"] = match[1]
                PRIVATE.write_text(json.dumps(data, indent=2))
                PRIVATE.chmod(0o600)
        return data
    owner = re.search(r"OWNER_ID\s*=\s*['\"](\d+)['\"]", (QQ / "practice_commands.py").read_text())
    bot = re.search(r"get_bot\(['\"](\d+)['\"]\)", (QQ / "qq_features.py").read_text())
    if not owner or not bot:
        raise RuntimeError("Could not identify the existing dedicated QQ accounts")
    data = {"__HOME__": str(Path.home()), "__QQ_OWNER__": owner[1], "__QQ_BOT__": bot[1]}
    acm = Path.home() / ".local/share/hermes-acm/acm_client.py"
    if acm.exists():
        # Redact deployment addresses too; no session/config files are exported.
        origin = re.search(r"https://(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?", acm.read_text())
        if origin:
            data["__ACM_ORIGIN__"] = origin[0]
            data["__ACM_HOST__"] = origin[0].split("://", 1)[1]
    control = QQ / "qq_control.py"
    if control.exists():
        group = re.search(r"group_id (\d{7,12})", control.read_text())
        if group:
            data["__QQ_DOCUMENT_GROUP__"] = group[1]
    PRIVATE.write_text(json.dumps(data, indent=2))
    PRIVATE.chmod(0o600)
    return data


def transform(text, *, restore=False):
    mapping = settings()
    pairs = mapping.items() if restore else ((v, k) for k, v in mapping.items())
    for before, after in sorted(pairs, key=lambda item: -len(item[0])):
        text = text.replace(before, after)
    return text


def export_pairs(include_qq=True):
    if include_qq:
        for p in QQ.glob("*.py"):
            yield p, Path("src/qq") / p.name
        yield QQ / "qq_api_contract.json", Path("src/qq/qq_api_contract.json")
    for name in ["content_worker.py", "platform_features.py", "bili_extra.py", "heybox_extra.py", "extensions.py", "transcription.py", "platform_contract.json", "qq_api_contract.json", "HEYBOX-CLI-LICENSE", "HEYBOX-CORE-LICENSE"]:
        yield CONTENT / name, Path("src/content") / name
    for name in ["acm-lab", "character-library", "content-research", "deepseek_promptpack", "owner-guard", "practice-problem", "qq-file-read", "qq_group_window"]:
        base = HERMES / "plugins" / name
        for p in base.iterdir():
            if p.suffix == ".py" or p.name == "plugin.yaml":
                yield p, Path("src/plugins") / name / p.name
    yield Path.home() / ".local/share/hermes-acm/acm_client.py", Path("src/acm/acm_client.py")
    package = QQ / ".venv/lib/python3.12/site-packages/nonebot_plugin_hermes"
    for p in package.rglob("*.py"):
        yield p, Path("vendor/nonebot_plugin_hermes") / p.relative_to(package)
    for base, name in [(QQ, "qq.lock.txt"), (CONTENT, "content.lock.txt")]:
        yield base / "requirements.lock.txt", Path("requirements") / name


def scan(files):
    failures = []
    private_values = [v for k, v in json.loads(PRIVATE.read_text()).items() if k != "__HOME__"] if PRIVATE.exists() else []
    token_path = QQ / ".outbound-token"
    if token_path.exists():
        private_values.append(token_path.read_text().strip())
    try:
        from dotenv import dotenv_values
        for path in [QQ / ".env", HERMES / ".env"]:
            for key, value in dotenv_values(path).items():
                if value and len(value) >= 12 and any(k in key.upper() for k in ["KEY", "TOKEN", "SECRET", "PASSWORD"]):
                    private_values.append(value)
    except ImportError:
        pass
    for path in files:
        path = Path(path)
        if not path.is_file():
            continue
        text = path.read_text()
        if re.search(r"sk-[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----", text):
            failures.append(str(path.relative_to(REPO)))
        elif any(value in text for value in private_values if value):
            failures.append(str(path.relative_to(REPO)))
    if failures:
        raise RuntimeError("Private values detected in: " + ", ".join(sorted(set(failures))))


def files_to_publish():
    result = run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    files = [REPO / name for name in result.stdout.split("\0") if name]
    for p in files:
        if p.is_symlink() or p.name == PRIVATE.name or p.suffix in {".sqlite3", ".db", ".log", ".key", ".pem"} or p.name.startswith(".env") and p.name != ".env.example":
            raise RuntimeError("Private/non-source file selected for publication: " + p.name)
    return files


def capture(support_only=False):
    if not support_only and run(["git", "rev-parse", "--verify", "HEAD"], check=False).returncode == 0:
        if run(["git", "diff", "--quiet", "HEAD"], check=False).returncode:
            raise RuntimeError("Commit/review local edits before capturing runtime sources")
    pairs = list(export_pairs(not support_only))
    for source, dest in pairs:
        if not source.is_file():
            raise RuntimeError("Missing source: " + source.name)
        target = REPO / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(transform(source.read_text()))
    # Sanitise pre-staged QQ sources in the initial export, retaining local edits.
    if support_only:
        for p in (REPO / "src/qq").glob("*"):
            if p.is_file():
                p.write_text(transform(p.read_text()))
    scan(files_to_publish())
    manifest = {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in files_to_publish() if p.relative_to(REPO).parts[0] in {"src", "vendor", "requirements"}}
    (REPO / "source-manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    print(f"Source archive refreshed: {len(manifest)} files; credentials and account data excluded.")


def check():
    scan(files_to_publish())
    for p in files_to_publish():
        if p.suffix == ".py":
            ast.parse(p.read_text(), filename=str(p.relative_to(REPO)))
    result = run([PYTHON if PYTHON.exists() else sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], check=False)
    if result.returncode:
        print(result.stderr[-8000:])
        raise RuntimeError("Tests failed")
    print("Source/privacy checks and tests passed.")


def status():
    import httpx
    token = (QQ / ".outbound-token").read_text().strip()
    with httpx.Client(trust_env=False, timeout=8) as client:
        r = client.get("http://127.0.0.1:18800/hermes-outbound/health", headers={"Authorization": "Bearer " + token})
        r.raise_for_status()
        data = r.json()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return data


def idle():
    path = Path.home() / ".local/share/hermes-acm/practice/practice.sqlite3"
    if path.exists():
        with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as db:
            active = db.execute("SELECT count(*) FROM rounds WHERE status IN ('drawing','grading')").fetchone()[0]
            active += db.execute("SELECT count(*) FROM seats WHERE status='grading'").fetchone()[0]
            if active:
                raise RuntimeError("Practice drawing/grading is active; retry deployment after it finishes")
    import httpx
    from dotenv import dotenv_values
    env = dotenv_values(QQ / ".env")
    with httpx.Client(trust_env=False, timeout=8) as client:
        r = client.get(env["HERMES_API_URL"].rstrip("/") + "/health/detailed", headers={"Authorization": "Bearer " + env["HERMES_API_KEY"]})
        r.raise_for_status()
        if r.json().get("api_server", {}).get("active_runs", 0):
            raise RuntimeError("A chat reply is active; retry deployment after it finishes")


def atomic(path, content):
    fd, tmp = tempfile.mkstemp(prefix=".qq-source-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def deploy(apply):
    check()
    changes = []
    for p in (REPO / "src/qq").glob("*"):
        if p.suffix not in {".py", ".json"} or not p.is_file():
            continue
        content = transform(p.read_text(), restore=True).encode()
        dest = QQ / p.name
        if not dest.exists() or dest.read_bytes() != content:
            changes.append((dest, content))
    policy = QQ / "delivery-policy.json"
    if not policy.exists():
        changes.append((policy, (REPO / "config/delivery-policy.example.json").read_bytes()))
    print("QQ files to update:", ", ".join(p.name for p, _ in changes) or "none")
    if not apply or not changes:
        return
    idle()
    backup = HERMES / "backups" / ("qq-delivery-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    backup.mkdir(parents=True, mode=0o700)
    before = {str(p): p.exists() for p, _ in changes}
    for p, _ in changes:
        if p.exists():
            shutil.copy2(p, backup / p.name)
            (backup / p.name).chmod(0o600)
    (backup / "state.json").write_text(json.dumps(before))
    protected = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in [QQ / ".env", QQ / ".outbound-token", HERMES / "config.yaml", HERMES / ".env"] if p.exists()}
    run(["systemctl", "--user", "stop", SERVICE])
    try:
        # Closing the receive socket fences new QQ work before the final idle check.
        idle()
        for p, content in changes:
            atomic(p, content)
        run(["systemctl", "--user", "start", SERVICE])
        for _ in range(20):
            try:
                health = status()
                if health.get("transport_reported_online"):
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("QQ bridge did not reconnect")
        listener = run(["ss", "-lntp", "sport = :18800"]).stdout
        pid = run(["systemctl", "--user", "show", SERVICE, "-p", "MainPID", "--value"]).stdout.strip()
        if "pid=" + pid + "," not in listener:
            raise RuntimeError("QQ listener does not match its managed service")
        for name, digest in protected.items():
            if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
                raise RuntimeError("Protected configuration changed; investigate before continuing")
    except BaseException:
        run(["systemctl", "--user", "stop", SERVICE], check=False)
        for p, _ in changes:
            if before[str(p)]:
                shutil.copy2(backup / p.name, p)
            else:
                p.unlink(missing_ok=True)
        run(["systemctl", "--user", "start", SERVICE], check=False)
        raise
    print("Deployed QQ only; private backup:", backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["capture", "check", "status", "deploy"])
    parser.add_argument("--initial-support", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.command == "capture":
        capture(args.initial_support)
    elif args.command == "check":
        check()
    elif args.command == "status":
        status()
    else:
        deploy(args.apply)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Avoid dumping requests, URLs with credentials, or tracebacks into logs.
        if isinstance(exc, RuntimeError):
            print(str(exc), file=sys.stderr)
        else:
            print("Operation failed (" + type(exc).__name__ + "); existing state retained.", file=sys.stderr)
        raise SystemExit(1)
