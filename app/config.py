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

    # Ask Jeene, the doubt solver. Its own model rather than the planner's: the two
    # calls want different things — a plan is a small structured object chosen from a
    # catalogue, an answer is prose a student will read as the app's own — and being
    # able to move one without the other is the point of separating them. Same key.
    jeene_doubts_model: str | None = None
    jeene_doubts_enabled: bool = False

    # Signs the short-lived links the app uses to open files we host — today the chapter
    # notes viewer. Optional so a laptop needs no setup: without it every process signs
    # with its own random secret, which is correct for one worker and broken for two.
    jeene_asset_secret: str | None = None

    # Hosts the notes viewer may stream a PDF from, comma-separated. Empty means "any
    # public https host", which is the safe default; naming hosts is stricter still and
    # is also how a local setup permits a plain-http fixture server.
    jeene_notes_storage_hosts: str | None = None

    # --- Billing ---------------------------------------------------------------------
    #: Publishable. Reaches the app with every created order — it has to, the checkout
    #: SDK needs it — and is useless on its own.
    razorpay_key_id: str | None = None
    #: Server only, for ever. It authenticates API calls *and* is the HMAC key that
    #: proves a success callback came from Razorpay, so a leak is both a way to spend
    #: from the account and a way to forge a purchase.
    razorpay_key_secret: str | None = None
    #: Server only. Signs webhook bodies; set from the Razorpay dashboard when the
    #: endpoint is registered.
    razorpay_webhook_secret: str | None = None
    #: Server only. Signs price quotes. Rotating it invalidates outstanding quotes,
    #: which is harmless — they live ten minutes.
    jeene_quote_secret: str | None = None
    #: Set to allow real charges. Left false, the gateway is never constructed and the
    #: money routes answer 503, so a half-configured deployment cannot take a payment.
    jeene_billing_enabled: bool = False
    #: Shared secret the reconciler cron presents. Not a user credential and never
    #: reaches a client: the route it opens can settle payments.
    jeene_reconcile_secret: str | None = None


settings = Settings()
