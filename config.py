"""
config.py - Central configuration for YouTube Automation Dashboard
Fill in your API keys here or use a .env file.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Google Gemini (AI Script Generation) ────────────────────────────────────
# Get your free key at: https://ai.google.dev
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY_HERE")
GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-3.6-flash")
# Fast model for helper tasks (loanword rewrite, narration QA transcription)
GEMINI_HELPER_MODELS = os.getenv("GEMINI_HELPER_MODELS", "gemini-3.5-flash-lite,gemini-3.6-flash").split(",")

# ─── NVIDIA NIM (AI Script Generation / Llama 3) ─────────────────────────────
# Get your free key at: https://build.nvidia.com
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "YOUR_NVIDIA_API_KEY_HERE")
NVIDIA_TEXT_MODEL = os.getenv("NVIDIA_TEXT_MODEL", "deepseek-ai/deepseek-v4.1-flash")


# ─── Pexels (Free Stock Images) ──────────────────────────────────────────────
# Get your free key at: https://www.pexels.com/api/
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "YOUR_PEXELS_API_KEY_HERE")

# ─── YouTube OAuth (YouTube Data API v3) ─────────────────────────────────────
# Download client_secrets.json from Google Cloud Console
# Project > Credentials > OAuth 2.0 Client ID (Desktop App)
CLIENT_SECRETS_FILE = os.getenv("CLIENT_SECRETS_FILE", "client_secrets.json")
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
                  "https://www.googleapis.com/auth/youtube.readonly",
                  # manage playlists (series → playlist automation)
                  "https://www.googleapis.com/auth/youtube",
                  # caption tracks (captions().insert needs force-ssl; without it the upload 403s)
                  "https://www.googleapis.com/auth/youtube.force-ssl"]
OAUTH_TOKEN_FILE = "token.json"

# ─── Channel Info ─────────────────────────────────────────────────────────────
CHANNEL_ID = "UConCDHOp-Cy0RNQps4WbMnw"
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "The Faceless")
# Added to every upload (description hashtags / tags)
CHANNEL_HASHTAGS = ["#TheFaceless", "#Shorts"]
CHANNEL_TAGS = ["the faceless", "thefaceless", "hindi stories", "hinglish"]

# ─── Video Settings ───────────────────────────────────────────────────────────
VIDEO_WIDTH = 2560          # long videos: 1440p (YouTube serves >=1440p uploads in its higher-quality codec)
VIDEO_HEIGHT = 1440
SHORT_WIDTH = 1080
SHORT_HEIGHT = 1920
FPS = 30
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "static", "outputs")

# ─── Narration voice (Edge TTS) — viral Shorts are fast-paced ───────────────
# "auto" = Gemini TTS (expressive, free tier) with Edge TTS fallback · "edge" = Edge only
TTS_ENGINE = os.getenv("TTS_ENGINE", "auto")
GEMINI_TTS_MODEL = os.getenv("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts")
GEMINI_TTS_VOICE = os.getenv("GEMINI_TTS_VOICE", "Charon")
# Edge fallback: no pitch shifting (it makes the voice sound processed)
TTS_RATE = os.getenv("TTS_RATE", "+5%")
TTS_PITCH = os.getenv("TTS_PITCH", "+0Hz")

# ─── Captions (burned in with ffmpeg/libass so Hindi shapes correctly) ───────
# Folder containing a font with Devanagari glyphs + its family name.
SUBTITLE_FONTS_DIR = os.getenv("SUBTITLE_FONTS_DIR", "/System/Library/Fonts")
SUBTITLE_FONT = os.getenv("SUBTITLE_FONT", "Kohinoor Devanagari")

# ─── Google Veo 3.1 (AI Animated Video Generation) ───────────────────────────
# Uses the same Gemini API key — no extra setup needed
VEO_MODEL = os.getenv("VEO_MODEL", "veo-3.1-generate-preview")
# Veo = real moving AI animation, but it is billed per second of video (a 9-scene Short can
# cost $10+). Off by default; set USE_VEO=true in .env to enable it.
USE_VEO = os.getenv("USE_VEO", "false").lower() in ("1", "true", "yes")
# Used when Veo is unavailable: AI still image per scene, animated with Ken Burns
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gemini-3.1-flash-image")
# Free fallback for AI images. Without a token: 1 image / 45 s + small logo.
# Get a key at https://enter.pollinations.ai to remove both limits.
POLLINATIONS_TOKEN = os.getenv("POLLINATIONS_TOKEN", "")
POLLINATIONS_MODEL = os.getenv("POLLINATIONS_MODEL", "flux")
# Free local AI images on this Mac's GPU: Z-Image Turbo (Apache-2.0, commercial use allowed) via
# mflux/MLX in its own env:  ~/.local/bin/uv venv --python 3.12 .venv-mflux &&
#                            ~/.local/bin/uv pip install --python .venv-mflux/bin/python mflux
LOCAL_IMAGES = os.getenv("LOCAL_IMAGES", "true").lower() in ("1", "true", "yes")
# Z-Image Turbo (4-bit, MLX) — fetch with: python3 fetch_model.py mflux-community/z-image-turbo-mflux-q4 models/z-image-turbo-q4
LOCAL_IMAGE_MODEL = os.getenv("LOCAL_IMAGE_MODEL", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                  "models", "z-image-turbo-q4"))
# Default look: "animated_3d" | "anime" | "comic" | "stock" (real stock footage)
VISUAL_STYLE = os.getenv("VISUAL_STYLE", "animated_3d")

# ─── Flask Settings ───────────────────────────────────────────────────────────
FLASK_PORT = 5055
FLASK_DEBUG = True
