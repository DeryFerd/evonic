import os
import sys
import logging

# Load .env file — prefer envcrypt (supports encrypted values), fall back to dotenv
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib', 'envcrypt', 'libs', 'python'))
try:
    import envcrypt
    envcrypt.load(".env")
except Exception:
    from dotenv import load_dotenv
    load_dotenv()

_logger = logging.getLogger(__name__)


def _get_env_int(name: str, default: int, min_val: int = None, max_val: int = None) -> int:
    """Read an integer environment variable with validation and bounds clamping."""
    try:
        value = int(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        _logger.warning("Invalid %s, using default %s", name, default)
        return default
    if min_val is not None and value < min_val:
        _logger.warning("%s=%d below minimum %d, clamping to %d", name, value, min_val, min_val)
        return min_val
    if max_val is not None and value > max_val:
        _logger.warning("%s=%d above maximum %d, clamping to %d", name, value, max_val, max_val)
        return max_val
    return value

# Two-Pass Extraction Configuration
# PASS 1: LLM generates answer with reasoning
# PASS 2: LLM extracts ONLY the final answer in strict format
TWO_PASS_ENABLED = os.getenv("TWO_PASS_ENABLED", "1") == "1"
TWO_PASS_TEMPERATURE = float(os.getenv("TWO_PASS_TEMPERATURE", "0.0"))

# Domain Evaluator Configuration
# Override default evaluator for specific domains
# Available types: two_pass, keyword, sql_executor, tool_call
# Example: EVALUATOR_MATH=keyword would use keyword matching for math (not recommended)
EVALUATOR_OVERRIDES = {
    # "math": "keyword",        # Override math to use keyword evaluator
    # "conversation": "two_pass",  # Override conversation to use two-pass
}

def get_evaluator_type(domain: str) -> str:
    """Get configured evaluator type for domain"""
    # Check environment variable first
    env_key = f"EVALUATOR_{domain.upper()}"
    env_value = os.getenv(env_key)
    if env_value:
        return env_value.lower()
    
    # Check config overrides
    if domain in EVALUATOR_OVERRIDES:
        return EVALUATOR_OVERRIDES[domain].lower()
    
    return "default"

# Database paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_shared_db_dir = os.path.join(BASE_DIR, "shared", "db")
if not os.path.isdir(_shared_db_dir):
    os.makedirs(_shared_db_dir, exist_ok=True)

DB_PATH = os.path.join(_shared_db_dir, "evonic.db")
TEST_DB_PATH = os.path.join(BASE_DIR, "seed", "test_db.sqlite")

# Flask
_secret_key = os.getenv("SECRET_KEY", "")
if _secret_key:
    SECRET_KEY = _secret_key
else:
    import secrets as _secrets
    SECRET_KEY = _secrets.token_hex(32)
    _logger.warning(
        "SECRET_KEY not set — generated a random key. "
        "Set the SECRET_KEY env var for persistent sessions across restarts."
    )
HOST = os.getenv("HOST", "0.0.0.0")
PORT = _get_env_int("PORT", 8080, min_val=1, max_val=65535)
DEBUG = os.getenv("DEBUG", "1") == "1"

# Authentication
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
TURNSTILE_SITE_KEY = os.getenv("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.getenv("TURNSTILE_SECRET_KEY", "")

# Real-time log verbosity
LOG_FULL_THINKING = os.getenv("LOG_FULL_THINKING", "0") == "1"
LOG_FULL_RESPONSE = os.getenv("LOG_FULL_RESPONSE", "0") == "1"

# Raw LLM API call logging (markdown)
LLM_API_LOG_ENABLED = os.getenv("LLM_API_LOG_ENABLED", "0") == "1"
LLM_API_LOG_FILE = os.getenv("LLM_API_LOG_FILE", os.path.join(BASE_DIR, "logs", "llm_api_calls.md"))

# Event stream logging to file
EVENT_LOG_FILE = os.getenv("EVENT_LOG_FILE", os.path.join(BASE_DIR, "logs", "events.log"))

# Docker sandbox configuration (shared by runpy, bash, etc.)
SANDBOX_WORKSPACE = os.getenv("SANDBOX_WORKSPACE", BASE_DIR)
SANDBOX_IDLE_TIMEOUT = _get_env_int("SANDBOX_IDLE_TIMEOUT", 1800, min_val=1, max_val=43200)  # 30 min
SANDBOX_MEMORY_LIMIT = os.getenv("SANDBOX_MEMORY_LIMIT", "512m")
SANDBOX_CPU_LIMIT = os.getenv("SANDBOX_CPU_LIMIT", "1")
SANDBOX_NETWORK = os.getenv("SANDBOX_NETWORK", "bridge")  # 'none' or 'bridge'
SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "evonic-sandbox:latest")
SANDBOX_MAX_CONTAINERS = _get_env_int("SANDBOX_MAX_CONTAINERS", 10, min_val=1, max_val=100)

# SSH backend configuration (used by sshc tool / SSHBackend)
SSH_DEFAULT_TIMEOUT = _get_env_int("SSH_DEFAULT_TIMEOUT", 30, min_val=1, max_val=3600)   # seconds per command
SSH_IDLE_TIMEOUT = _get_env_int("SSH_IDLE_TIMEOUT", 1800, min_val=1, max_val=43200)       # 30 min idle disconnect

# Cloud Workplace connector relay (WebSocket server for Evonet)
CONNECTOR_WS_HOST = os.getenv("CONNECTOR_WS_HOST", "0.0.0.0")
CONNECTOR_WS_PORT = _get_env_int("CONNECTOR_WS_PORT", 8081, min_val=1024, max_val=65535)
CONNECTOR_PING_INTERVAL = _get_env_int("CONNECTOR_PING_INTERVAL", 30, min_val=5, max_val=300)
CONNECTOR_PING_TIMEOUT = _get_env_int("CONNECTOR_PING_TIMEOUT", 10, min_val=1, max_val=60)
CONNECTOR_PAIRING_CODE_TTL = _get_env_int("CONNECTOR_PAIRING_CODE_TTL", 300, min_val=60, max_val=3600)  # seconds

AGENT_MAX_TOOL_ITERATIONS = _get_env_int("AGENT_MAX_TOOL_ITERATIONS", 100, min_val=1, max_val=1000)
EVAL_MAX_TOOL_ITERATIONS = _get_env_int("EVAL_MAX_TOOL_ITERATIONS", 30, min_val=1, max_val=500)
AGENT_MAX_TOOL_RESULT_CHARS = _get_env_int("AGENT_MAX_TOOL_RESULT_CHARS", 8000, min_val=1, max_val=1_048_576)
AGENT_MAX_SUMMARIZE_BATCH = _get_env_int("AGENT_MAX_SUMMARIZE_BATCH", 20, min_val=1, max_val=500)
AGENT_TIMEOUT_RETRIES = _get_env_int("AGENT_TIMEOUT_RETRIES", 2, min_val=0, max_val=20)
AGENT_QUEUE_WORKERS = _get_env_int("AGENT_QUEUE_WORKERS", 2, min_val=1, max_val=32)

# Release version (written by supervisor during staging; "dev" in flat-repo mode)
EVONIC_VERSION = "dev"
_version_file = os.path.join(BASE_DIR, "VERSION")
if os.path.exists(_version_file):
    with open(_version_file) as _vf:
        EVONIC_VERSION = _vf.read().strip()
