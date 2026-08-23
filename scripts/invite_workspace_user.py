"""Create or reactivate an invite for the customer research workspace."""

from __future__ import annotations

import argparse

from src.workspace.service import WorkspaceRepository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    parser.add_argument("--role", choices=("admin", "member"), default="member")
    args = parser.parse_args()
    WorkspaceRepository().create_invite(args.email, args.role, created_by="cli")
    print(f"Active {args.role} invite created for {args.email.lower()}.")


if __name__ == "__main__":
    main()
