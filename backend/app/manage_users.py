"""Operator commands with passwords accepted through a private prompt."""

import argparse
from getpass import getpass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.db.base import *  # noqa: F403
from app.db.session import SessionLocal
from app.models.payment import Merchant
from app.repositories import SqlAlchemyMerchantRepository
from app.repositories.auth_repository import SqlAlchemyAuthRepository
from app.services.auth_service import AuthError, AuthService
from app.services.merchant_service import MerchantService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("create", "reset-password", "disable"))
    parser.add_argument("--email", required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--merchant-id", type=UUID)
    group.add_argument("--new-merchant")
    args = parser.parse_args()
    with SessionLocal() as session:
        service = AuthService(SqlAlchemyAuthRepository(session), settings)
        user = service.repository.get_user(args.email.lower())
        try:
            if args.action == "create":
                if user or not (args.merchant_id or args.new_merchant):
                    parser.error("Create requires a new user and an explicit merchant.")
                merchant_id = args.merchant_id
                if args.new_merchant:
                    merchant, _ = MerchantService(repository=SqlAlchemyMerchantRepository(session)).create_merchant(
                        args.new_merchant, issue_api_key=False, commit=False
                    )
                    merchant_id = merchant.id
                elif session.scalar(select(Merchant.id).where(Merchant.id == merchant_id)) is None:
                    parser.error("Merchant does not exist.")
                service.create_user(merchant_id, args.email, getpass("New password (15–128 characters): "))
            elif user is None:
                parser.error("User does not exist.")
            elif args.action == "reset-password":
                service.set_password(user, getpass("New password (15–128 characters): "))
            else:
                service.disable_user(user)
        except (AuthError, ValueError, SQLAlchemyError):
            session.rollback()
            parser.exit(1, "User operation could not be completed. Check the input.\n")
    print("User operation completed.")


if __name__ == "__main__":
    main()
