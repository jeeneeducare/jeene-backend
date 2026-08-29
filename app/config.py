from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    database_url: str | None = None
    # Firebase Admin credential: either the service-account JSON as a string
    # (FIREBASE_SERVICE_ACCOUNT_JSON, used on Render) or a path to the JSON file
    # (GOOGLE_APPLICATION_CREDENTIALS, used locally). Either one is enough.
    firebase_service_account_json: str | None = None
    google_application_credentials: str | None = None

    # The study planner. All three are optional and their absence is not an error: with
    # no key, or with the flag off, every plan comes from the deterministic planner and
    # no network call is made. That is the shipping configuration.
    openai_api_key: str | None = None
    jeene_planner_model: str | None = None
    jeene_planner_enabled: bool = False

    # Signs the short-lived links the app uses to open files we host — today the chapter
    # notes viewer. Optional so a laptop needs no setup: without it every process signs
    # with its own random secret, which is correct for one worker and broken for two.
    jeene_asset_secret: str | None = None


settings = Settings()
