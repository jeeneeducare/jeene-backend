from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str | None = None

    class Config:
        env_file = ".env"


settings = Settings()
