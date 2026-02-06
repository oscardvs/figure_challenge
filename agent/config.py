import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (parent of agent/)
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = "gemini-3-flash-preview"
THINKING_LEVEL = "minimal"
CHALLENGE_URL = "https://serene-frangipane-7fd25b.netlify.app/"
MAX_TIME_SECONDS = 400
