from abc import ABC, abstractmethod

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.payment import Merchant, MerchantApiKey


class MerchantRepository(ABC):
    @abstractmethod
    def add_merchant(self, merchant: Merchant) -> None:
        raise NotImplementedError

    @abstractmethod
    def add_api_key(self, api_key: MerchantApiKey) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_active_api_key_by_hash(self, key_hash: str) -> MerchantApiKey | None:
        raise NotImplementedError

    @abstractmethod
    def merchant_count(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def flush(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def commit(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def refresh(self, instance: object) -> None:
        raise NotImplementedError


class SqlAlchemyMerchantRepository(MerchantRepository):
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_merchant(self, merchant: Merchant) -> None:
        self.session.add(merchant)

    def add_api_key(self, api_key: MerchantApiKey) -> None:
        self.session.add(api_key)

    def get_active_api_key_by_hash(self, key_hash: str) -> MerchantApiKey | None:
        statement = (
            select(MerchantApiKey)
            .where(MerchantApiKey.key_hash == key_hash)
            .where(MerchantApiKey.revoked_at.is_(None))
        )
        return self.session.scalar(statement)

    def merchant_count(self) -> int:
        return len(list(self.session.scalars(select(Merchant.id))))

    def flush(self) -> None:
        self.session.flush()

    def commit(self) -> None:
        self.session.commit()

    def refresh(self, instance: object) -> None:
        self.session.refresh(instance)
