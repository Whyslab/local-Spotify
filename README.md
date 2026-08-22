# 🎵 localSpotify

Self-hosted музыкальная платформа для создания собственной Spotify-подобной библиотеки.

localSpotify позволяет создавать личную музыкальную библиотеку из YouTube источников с автоматической загрузкой, обработкой и воспроизведением.

## Features

- FastAPI backend
- Web interface
- Background workers
- yt-dlp downloader
- FFmpeg processing pipeline
- SQLite task storage
- Navidrome integration
- Subsonic-compatible clients

Main idea:

You own the files.  
You own the server.  
You own the library.

---

# Project Status

Status: Production Ready

Components:

- Backend API: Production
- Web Interface: Available
- Queue System: Stable
- Worker Processing: Stable
- yt-dlp Pipeline: Working
- FFmpeg Processing: Working
- Metadata Processing: Working
- Retry System: Working
- Recovery System: Working
- Navidrome Integration: Ready
- systemd Deployment: Ready
- Automated Tests: Passing

---

# Music Library

Features:

- Local music storage
- YouTube URL import
- Automatic downloading
- Audio conversion
- Metadata normalization
- Cover processing
- Clean file naming
- Organized library structure

---

# Processing Pipeline

The system provides:

- Background queue
- Multiple workers
- SQLite persistence
- Duplicate detection
- Retry handling
- Failed task tracking
- Crash recovery
- Temporary file cleanup

---

# Web Interface

Available locally:

http://127.0.0.1:8787


Features:

- API token authentication
- Add YouTube tracks
- Add multiple URLs
- View system health
- Monitor queue
- Track task status

---

# Playback

Supported:

- Navidrome
- Subsonic compatible clients
- iPhone applications
- Desktop applications


Flow:

localSpotify

↓

Music Library

↓

Navidrome

↓

Subsonic API

↓

Mobile / Desktop Clients

---

# Tech Stack

Backend:

- Python
- FastAPI
- Uvicorn
- SQLite
- asyncio workers
- yt-dlp
- FFmpeg
- Metadata tools
- systemd
- Linux
- Navidrome
- Git

---

# Architecture

User

↓

Web Interface

↓

FastAPI API

↓

Task Queue

↓

Worker System

↓

yt-dlp + FFmpeg

↓

Normalized Music Library

↓

Navidrome

↓

Clients

---

# Installation

Requirements:

- Python 3.10+
- FFmpeg
- yt-dlp
- SQLite
- Linux


Recommended:

- Arch Linux
- systemd
- Navidrome


Clone repository:

git clone https://github.com/Whyslab/local-Spotify.git

cd local-Spotify


Create environment:

python -m venv .venv

source .venv/bin/activate


Install dependencies:

pip install -r adder/requirements.txt

---

# Configuration

Create environment file:

cp adder/.env.example adder/.env


Example:

API_TOKEN=your_secure_token


API token protects access to the API and Web Interface.

---

# Running

Start application:

cd adder

python app.py


Open:

http://127.0.0.1:8787

---

# Production Deployment

Recommended stack:

systemd

↓

localSpotify

↓

Music Library

↓

Navidrome

↓

Clients


Features:

- Automatic restart
- Persistent storage
- Background processing
- Self-hosted operation

---

# Project Structure

local-Spotify/

- adder/
- workers/
- database/
- music/
- systemd/
- README.md

---

# Security

Implemented:

- API token authentication
- Local deployment support
- Controlled file access
- No external database required

---

# Testing

Validated:

- FastAPI startup
- Health endpoint
- API authentication
- URL processing
- Queue execution
- SQLite storage
- yt-dlp downloading
- FFmpeg conversion
- Retry handling
- Worker recovery

---

# Philosophy

No subscriptions.  
No ads.  
No cloud dependency.

Your music.  
Your hardware.  
Your rules.

---

# License

Personal self-hosted project.
