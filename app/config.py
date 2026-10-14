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
    # Pro is sold through Google Play billing and Apple's in-app purchase. The stores take
    # the money; these settings are what this backend needs to ask them whether a purchase
    # is real. Each store is configured on its own, and a store with nothing set answers
    # 503 on its routes rather than half-working.

    #: Set to sell, and to lock what Pro unlocks: the two are one switch, so a student is
    #: never shown a lock with no way through it. Left false, every store route answers
    #: 503 and every gate passes.
    jeene_billing_enabled: bool = False
    #: Shared secret the reconciler cron presents. Not a user credential and never
    #: reaches a client: the route it opens can grant access.
    jeene_reconcile_secret: str | None = None

    #: The Android app's package name, as Play Console knows it.
    google_play_package_name: str | None = None
    #: Server only, for ever. The whole JSON key of the service account Play Console lets
    #: read orders and consume purchases. A leak lets somebody refund and consume purchases
    #: on the account, so it lives only in Render's environment.
    google_play_service_account_json: str | None = None
    #: The service account Pub/Sub signs its push tokens as, and the audience it signs
    #: them for. Together they are the whole of the authentication on the notifications
    #: route: a push that does not carry a token Google signed for exactly this pair is
    #: refused.
    google_pubsub_push_service_account: str | None = None
    google_pubsub_push_audience: str | None = None

    #: The iOS bundle id. Every signed transaction names one, and a transaction for any
    #: other app is refused however good its signature.
    apple_bundle_id: str | None = None
    #: The app's numeric Apple ID (App Store Connect → App Information). Apple's library
    #: requires it to accept a production transaction; without it only the test
    #: environment verifies.
    apple_app_id: int | None = None
    #: The In-App Purchase key, for the App Store Server API (the reconciler reads missed
    #: notifications through it). Optional: purchases verify without it, from Apple's
    #: signature alone. The private key is server only, for ever.
    apple_iap_key_id: str | None = None
    apple_iap_issuer_id: str | None = None
    apple_iap_private_key: str | None = None
    #: Local development only. Xcode's StoreKit testing signs transactions with a local
    #: certificate Apple never issued, so accepting them means accepting a signature
    #: nobody checks. Never set on a deployment.
    apple_accept_xcode_transactions: bool = False


settings = Settings()
