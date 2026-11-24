from app.core.security import generate_api_key, hash_api_key, key_display_prefix
from app.models.payment import Merchant, MerchantApiKey
from app.repositories import MerchantRepository


class MerchantService:
    def __init__(self, *, repository: MerchantRepository) -> None:
        self.repository = repository

    def create_merchant(
        self, name: str, *, api_key: str | None = None, issue_api_key: bool = True, commit: bool = True
    ) -> tuple[Merchant, str | None]:
        """Create a merchant and issue its first API key.

        Returns the merchant together with the plaintext key, which is the only
        moment the raw key is available. ``api_key`` lets the development seed
        provision a deterministic key; production always generates a random one.
        """
        merchant = Merchant(name=name)
        self.repository.add_merchant(merchant)
        self.repository.flush()

        raw_key = None
        if issue_api_key:
            raw_key = api_key or generate_api_key()
            record = MerchantApiKey(
                merchant_id=merchant.id,
                key_prefix=key_display_prefix(raw_key),
                key_hash=hash_api_key(raw_key),
            )
            self.repository.add_api_key(record)
        self.repository.flush()
        if commit:
            self.repository.commit()
        self.repository.refresh(merchant)
        return merchant, raw_key

    def authenticate(self, raw_key: str) -> Merchant | None:
        record = self.repository.get_active_api_key_by_hash(hash_api_key(raw_key))
        if record is None:
            return None
        return record.merchant

    def has_any_merchant(self) -> bool:
        return self.repository.merchant_count() > 0
