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
    # APNs provider auth (§11): user pushes go straight to Apple. Empty key
    # file disables the channel — Notify rows then dead-letter and page ntfy.
    apns_key_file: str = ""  # ~/.inkling/apns-AuthKey_<KEYID>.p8
    apns_key_id: str = ""
    apns_team_id: str = "LJA3588388"
    apns_topic: str = "com.sharduldhande.inkling"  # bundle id

    backup_s3_endpoint: str = ""
    backup_s3_bucket: str = ""
    backup_s3_key: str = ""
    backup_s3_secret: str = ""
    backup_gpg_recipient: str = ""
    export_git_dir: str = ""
    restore_drill_url: str = ""  # scratch DB for §13.3; empty skips the drill

    feature_embeddings: bool = False
    calendar_config_dir: str = "~/.inkling"  # calendars-<user>.json feed lists (§12)
    log_level: str = "info"
    log_format: str = "plain"  # "json" for journald (§15)
