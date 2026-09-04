from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    env: str = "dev"
    anthropic_api_key: str = ""
    database_url: str = "sqlite:///./betsy.db"

    # --- Turn engine ------------------------------------------------------
    # Sonnet-class per docs/architecture.md. The decision is deliberately a setting
    # rather than a constant: the eval harness is what should choose between this and
    # `claude-opus-5`, on measured extraction accuracy against the scenario set and
    # measured token cost, and neither number exists yet. Revisit once the harness
    # runs end to end — a change here is one line and a re-run.
    turn_model: str = "claude-sonnet-5"

    # Extraction is a bounded task and this call sits in front of a waiting patient,
    # so latency is a real cost. Adaptive thinking stays on at low effort rather than
    # being disabled: on current models, disabling thinking is what makes a model
    # write a tool call into visible prose, and low effort is both cheaper and safer.
    turn_effort: str = "low"

    # Enough for a couple of paragraphs of reply plus a full extraction, and small
    # enough that a runaway response fails fast rather than stalling the turn.
    turn_max_tokens: int = 4096

    # docs/architecture.md step 2: schema validation fails -> retry <=2 -> hard-fail.
    # A hard failure is Tier 1 on its own, so this is a safety-relevant number, not a
    # robustness knob.
    turn_max_validation_retries: int = 2

    # A patient is waiting on this call. The SDK's ten-minute default is written for
    # batch work and would leave a chat window hanging.
    turn_timeout_seconds: float = 60.0


settings = Settings()
