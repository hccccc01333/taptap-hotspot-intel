"""Check the current Git index, including files hidden by .gitignore.

This policy is a publication guard, not a certification of historical privacy.
It prints paths and field names only, never account or collected data values.
"""
import csv
import io
import subprocess
from pathlib import PurePosixPath


PRIVATE_PREFIXES = ("data/events/", "data/raw/", "data/raw_lake/", "data/event_bus/",
                    "data/state/", "data/v2/", "data/v3/", "runtime/state/", "trend/state/",
                    ".toolchain/")
PRIVATE_FILES = {"webapp/auth_users.json", "runtime/reports/scheduler_status_latest.md"}
IDENTITY_COLUMNS = {"user_name", "user_id", "avatar_url", "author_name", "author_id"}


def path_problem(name):
    path = PurePosixPath(name)
    if name in PRIVATE_FILES or name.startswith(PRIVATE_PREFIXES):
        return "local runtime/account data"
    if path.name.startswith(".env") and path.name not in (".env.example", ".env.sample"):
        return "local environment configuration"
    if path.name.endswith((".sqlite3-wal", ".sqlite3-shm")) or path.suffix in (".key", ".pem"):
        return "runtime sidecar or private key file"
    return None


def csv_problem(blob):
    try:
        header = next(csv.reader(io.StringIO(blob.decode("utf-8-sig"))), [])
    except (UnicodeError, csv.Error):
        return "unreadable CSV header; review before publication"
    fields = sorted(IDENTITY_COLUMNS.intersection(header))
    return "plaintext identity columns: " + ", ".join(fields) if fields else None


def main():
    names = subprocess.check_output(["git", "ls-files", "-z"]).decode("utf-8").split("\0")
    findings = []
    for name in filter(None, names):
        problem = path_problem(name)
        if problem is None and name.lower().endswith(".csv"):
            blob = subprocess.check_output(["git", "show", ":" + name])
            problem = csv_problem(blob)
        if problem:
            findings.append((name, problem))
    for name, problem in findings:
        print(f"Publication rejected: {name}: {problem}")
    if findings:
        raise SystemExit(1)
    print("Publication guard passed for the current index; Git history was not checked.")


if __name__ == "__main__":
    main()
