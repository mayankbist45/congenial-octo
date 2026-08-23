"""Generate a delayed-data model portfolio for an invited workspace user."""

from __future__ import annotations

import argparse

from src.workspace.service import WorkspaceService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    args = parser.parse_args()
    service = WorkspaceService()
    user = service.repo.user_for_email(args.email)
    job = service.repo.create_job(int(user["id"]))
    service.generate(job["id"], int(user["id"]))
    result = service.repo.job(job["id"], int(user["id"]))
    print(f"Workspace job {result['id']}: {result['status']} ({result['portfolio_id'] or result['error']})")


if __name__ == "__main__":
    main()
