from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Config(BaseSettings):
    database_url: str
    redis_url: str
    clerk_secret: SecretStr
    clerk_issuer: str = "https://clerk.example.invalid"
    clerk_webhook_secret: SecretStr = SecretStr("")
    stripe_secret: SecretStr
    storage_root: str = "./var/blobs"
    r2_bucket: str = ""
    r2_endpoint_url: str = ""
    r2_access_key_id: SecretStr = SecretStr("")
    r2_secret_access_key: SecretStr = SecretStr("")
