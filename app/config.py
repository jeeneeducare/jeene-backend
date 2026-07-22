from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    database_url: str | None = None
    # Firebase Admin credential: either the service-account JSON as a string
    # (FIREBASE_SERVICE_ACCOUNT_JSON, used on Render) or a path to the JSON file
    # (GOOGLE_APPLICATION_CREDENTIALS, used locally). Either one is enough.
    firebase_service_account_json: str | None = None
    google_application_credentials: str | None = None


settings = Settings()
