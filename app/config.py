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
