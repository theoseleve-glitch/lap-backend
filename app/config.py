from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Config(BaseSettings):
    database_url: str
    redis_url: str
    clerk_secret: SecretStr
    clerk_issuer: str = "https://clerk.example.invalid"
    clerk_webhook_secret: SecretStr = SecretStr("")
    stripe_secret: SecretStr
    stripe_webhook_secret: SecretStr = SecretStr("")
    # Stripe Price IDs — created in the Stripe dashboard. Empty in
    # dev/test; deploys must set these. T148+ checkout flows read them.
    stripe_price_lap_monthly: str = ""
    stripe_price_lap_yearly: str = ""
    stripe_price_waitlist_founder: str = ""
    # Frontend origin used to build Stripe Checkout success/cancel
    # redirect URLs and the OAuth/Clerk return URLs.
    app_base_url: str = "https://app.lap-coach.racing"
    storage_root: str = "./var/blobs"
    r2_bucket: str = ""
    r2_endpoint_url: str = ""
    r2_access_key_id: SecretStr = SecretStr("")
    r2_secret_access_key: SecretStr = SecretStr("")
