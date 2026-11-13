# YouTube Automation Dashboard — Setup Guide

## 1. Install FFmpeg

FFmpeg is required by MoviePy to encode videos:

```bash
brew install ffmpeg
```

Verify: `ffmpeg -version`

---

## 2. Install Python Dependencies

```bash
cd /Users/devesh/AI-ML-RoadMap/youtube-automation
pip install -r requirements.txt
```

---

## 3. Get Your API Keys

### 🤖 Gemini API Key (Required)
1. Go to [https://ai.google.dev](https://ai.google.dev)
2. Click **Get API Key** → **Create API Key**
3. Copy your key

### 📸 Pexels API Key (Optional but recommended)
1. Go to [https://www.pexels.com/api/](https://www.pexels.com/api/)
2. Create a free account
3. Go to your profile → API → Your API Key

---

## 4. Configure API Keys

Create a `.env` file in the `youtube-automation/` directory:

```bash
cp .env.example .env
```

Edit `.env` and fill in your keys:

```
GEMINI_API_KEY=AIzaSy...your_key_here
PEXELS_API_KEY=your_pexels_key_here
```

---

## 5. Set Up YouTube OAuth (for uploading)

This is needed to upload videos to your channel.

### Step 1: Create a Google Cloud Project
1. Go to [https://console.cloud.google.com/](https://console.cloud.google.com/)
2. Click **New Project** → Name it `YouTube Automation`
3. Click **Create**

### Step 2: Enable YouTube Data API v3
1. In your project, go to **APIs & Services → Library**
2. Search for **YouTube Data API v3**
3. Click **Enable**

### Step 3: Create OAuth 2.0 Credentials
1. Go to **APIs & Services → Credentials**
2. Click **+ Create Credentials → OAuth 2.0 Client ID**
3. Choose **Desktop Application** as the type
4. Name it `YouTube Automation Dashboard`
5. Click **Create**
6. Download the JSON file
7. **Rename it to `client_secrets.json`**
8. **Move it to** `/Users/devesh/AI-ML-RoadMap/youtube-automation/client_secrets.json`

### Step 4: Configure OAuth Consent Screen
1. Go to **APIs & Services → OAuth consent screen**
2. Choose **External** (or Internal if using Google Workspace)
3. Fill in:
   - App name: `YouTube Automation`
   - User support email: your email
   - Developer contact: your email
4. Click **Save and Continue**
5. Add scopes: `youtube.upload` and `youtube.readonly`
6. Add your Google account as a **test user**

> **Note:** On first upload, a browser window will open asking you to authorize the app.
> A `token.json` file will be saved and reused for future uploads.

---

## 6. Start the Dashboard

```bash
cd /Users/devesh/AI-ML-RoadMap/youtube-automation
python server.py
```

Open your browser at: **http://localhost:5055**

---

## Workflow

1. Type a topic (e.g. "History of Ancient Egypt")
2. Choose Long-form or Short
3. Click **Preview Script** to review the AI-generated script
4. Click **Run Full Pipeline** to build and optionally upload
5. Monitor progress in the Job Queue
6. Download or watch the video on YouTube

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `ffmpeg not found` | `brew install ffmpeg` |
| `ModuleNotFoundError` | `pip install -r requirements.txt` |
| `client_secrets.json not found` | Follow Step 5 above |
| `GEMINI_API_KEY invalid` | Check your key at ai.google.dev |
| Video renders but no audio | Install pydub: `pip install pydub` |
| Upload fails with quota | YouTube allows 6 uploads/day on free tier |
