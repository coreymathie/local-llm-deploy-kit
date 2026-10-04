# Corey Mathie, 2026
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # Inference backend: "ollama" (default) or "openai_compatible" (vLLM, SGLang, TGI, NVIDIA NIM, ...)
    BACKEND: Literal["ollama", "openai_compatible"] = "ollama"
    OLLAMA_HOST: str = "http://127.0.0.1:11434"
    OPENAI_COMPAT_BASE_URL: str = "http://127.0.0.1:8000/v1"
    OPENAI_COMPAT_API_KEY: str = ""
    GATEWAY_HOST: str = "127.0.0.1"
    GATEWAY_PORT: int = 8080
    GATEWAY_DEFAULT_MODEL: str = "llama3.1:8b"
    GATEWAY_EMBED_MODEL: str = "nomic-embed-text"
    GATEWAY_MAX_UPLOAD_MB: int = 20
    GATEWAY_RATE_LIMIT_PER_MIN: int = 60
    GATEWAY_LOG_PROMPTS: bool = False
    GATEWAY_REDACT_PROMPTS: bool = False
    GATEWAY_DB_PATH: str = "./gateway.db"
    GATEWAY_LOG_DIR: str = "./logs"
    GATEWAY_ADMIN_BOOTSTRAP_KEY: str = ""
    # Prometheus /metrics. If GATEWAY_METRICS_TOKEN is set, scrapers must send it as a bearer token.
    GATEWAY_METRICS_ENABLED: bool = True
    GATEWAY_METRICS_TOKEN: str = ""

    # OIDC / JWT bearer authentication, accepted alongside API keys (see gateway/identity.py).
    GATEWAY_OIDC_ENABLED: bool = False
    GATEWAY_OIDC_ISSUER: str = ""  # must equal the token's "iss"
    GATEWAY_OIDC_AUDIENCE: str = ""  # must appear in the token's "aud"
    GATEWAY_OIDC_JWKS_URL: str = ""  # empty: discovered from {issuer}/.well-known/openid-configuration
    GATEWAY_OIDC_ALGORITHMS: str = "RS256,ES256"  # allow-list; only RS256 and ES256 are accepted
    GATEWAY_OIDC_CLOCK_SKEW_SECONDS: int = 60
    GATEWAY_OIDC_JWKS_CACHE_SECONDS: int = 3600
    GATEWAY_OIDC_GROUPS_CLAIM: str = "groups"  # dotted paths work, e.g. realm_access.roles
    GATEWAY_OIDC_USERNAME_CLAIM: str = "sub"
    # JSON object: IdP group -> gateway roles ("admin", "user", "reader:<collection>", "reader:*").
    GATEWAY_OIDC_GROUP_ROLES: dict[str, list[str]] = {}
    GATEWAY_OIDC_ALLOW_HTTP: bool = False  # allow http:// issuer/JWKS URLs (https is required otherwise)

    # Collections with no ACL: "open" (any caller with the user role may read them; the v0.5 behavior)
    # or "restricted" (only admins and explicit grants).
    GATEWAY_COLLECTION_DEFAULT_ACCESS: Literal["open", "restricted"] = "open"

    # Document retrieval (see gateway/retrieval.py): vector | bm25 | hybrid (Reciprocal Rank Fusion of both).
    GATEWAY_RETRIEVAL_MODE: Literal["vector", "bm25", "hybrid"] = "hybrid"
    GATEWAY_RRF_K: int = 60
    # Second stage: none | lexical | cross_encoder (needs sentence-transformers and a local model path).
    GATEWAY_RERANKER: Literal["none", "lexical", "cross_encoder"] = "none"
    GATEWAY_RERANK_CANDIDATES: int = 20
    GATEWAY_CROSS_ENCODER_MODEL: str = ""

    # Encryption at rest (gateway/crypto.py): passages and vectors sealed with per-document keys,
    # wrapped by a key-encryption key from a keyring file or one base64 key in the environment.
    GATEWAY_ENCRYPT_AT_REST: bool = False
    GATEWAY_ENCRYPTION_KEY_FILE: str = ""
    GATEWAY_ENCRYPTION_KEY: str = ""
    GATEWAY_AUDIT_ENCRYPT_TEXT: bool = False  # seal prompt/response/question/answer fields in audit entries

    # Model supply chain (gateway/supply_chain.py): pinned digests and a policy for unpinned/mismatched models.
    GATEWAY_MODEL_LOCK_FILE: str = ""
    GATEWAY_MODEL_POLICY: Literal["off", "warn", "enforce"] = "warn"
    GATEWAY_MODEL_VERIFY_INTERVAL_SECONDS: int = 300


settings = Settings()
