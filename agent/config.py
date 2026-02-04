import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = "gemini-3-flash-preview"
THINKING_LEVEL = "minimal"
CHALLENGE_URL = "https://serene-frangipane-7fd25b.netlify.app/"
MAX_TIME_SECONDS = 300
