from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = ""
    database_url_migrate: str = ""

    port: int = 8899
    bind: str = "127.0.0.1"
    chat_tokens_file: str = ""
    ntfy_base: str = "https://ntfy.sh"
    operator_ntfy: str = ""  # §11 dead-letter alerts; empty disables them

    backup_s3_endpoint: str = ""
    backup_s3_bucket: str = ""
    backup_s3_key: str = ""
    backup_s3_secret: str = ""
    backup_gpg_recipient: str = ""
    export_git_dir: str = ""

    feature_embeddings: bool = False
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    log_level: str = "info"
