# 🤖 J.A.R.V.I.S — Autonomous AI Desktop Assistant & Agent OS

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Framework](https://img.shields.io/badge/GUI-PySide6-green.svg)](https://pypi.org/project/PySide6/)
[![AI Engine](https://img.shields.io/badge/AI-Gemini%20Native%20Function%20Calling-orange.svg)](https://ai.google.dev/)
[![License](https://img.shields.io/badge/License-MIT-brightgreen.svg)](LICENSE)

**J.A.R.V.I.S** is an advanced, voice-controlled autonomous AI desktop agent that brings Sci-Fi assistant capabilities into real life. Powered by **Gemini Native Function Calling**, it executes complex, multi-step sequential tasks across your local OS, browser, and social media platforms.

---

## 🚀 Key Features

- 🎙️ **Voice-First Interaction:**
  - **Speech-to-Text (STT):** High-accuracy voice command recognition using OpenAI's Whisper.
  - **Text-to-Speech (TTS):** Natural and expressive voice synthesis powered by Edge-TTS.

- 🧠 **Autonomous Multi-Step Execution (Agent Loop):**
  - Powered by Gemini Native Function Calling to chaining tools dynamically.
  - Executes sequential user intents (e.g., *"Open Instagram, go to Reels, like the first video, and post a comment"*).

- 📸 **Browser & Social Media Automation:**
  - Automatic browser environment detection.
  - Native integration for navigating platforms, automated interactions (likes, comments, content fetching).

- ✈️ **Telegram Userbot Engine:**
  - Read channel feeds, summarize latest news, and automate chat responses via Telethon / Pyrogram.

- 🖥️ **System Control & Telemetry:**
  - Application launcher/killer and process manager.
  - Real-time hardware monitoring (CPU, RAM, GPU temperature, and VRAM usage).

- 💻 **Cyberpunk UI (PySide6):**
  - Futuristic dark-themed desktop interface with dynamic system resource visualizers and status logs.

---

## 🛠️ Tech Stack

- **Core:** Python 3.10+
- **GUI:** PySide6 (Qt for Python)
- **AI / LLM:** Google Gemini API (`google-generativeai`), Native Function Calling
- **Audio Processing:** OpenAI Whisper, Edge-TTS
- **Automation:** Selenium / Playwright, Telethon
- **System Telemetry:** `psutil`, `pynvml`

---

## ⚡ Quick Start

### 1. Clone the repository
```bash
git clone [https://github.com/YOUR_USERNAME/jarvis-ai-agent.git](https://github.com/YOUR_USERNAME/jarvis-ai-agent.git)
cd jarvis-ai-agent
