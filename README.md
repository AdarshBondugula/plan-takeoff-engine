# AI Takeoff Pro 🏗️📐

**AI Takeoff Pro** is a multi-page architectural blueprint analysis and quantity takeoff estimation engine powered by FastAPI, PyMuPDF, and Vision AI (Gemini & OpenRouter).

---

## ✨ Features

- **Automated Blueprint Ingestion**: Extracts text, raster images, and vector geometry from complex multi-page architectural PDFs.
- **Multimodal Visual Takeoff**: Leverages Google Gemini vision models for high-precision plan detection, foundation boundary recognition, stepped/jogged geometry detection, and net area/perimeter calculations.
- **Engineering Schedules & Keynotes**: Extracts door/window schedules, finish schedules, wall assemblies, and structural notes.
- **Dynamic Excel & Markdown Reports**: Generates structured takeoff spreadsheets (`.xlsx`) with automated formulas, along with clean Markdown summaries.
- **Interactive Web Interface**: Real-time progress monitoring, page-by-page visual inspection, and instant download of takeoff outputs.

---

## 🚀 Quick Start

### 1. Prerequisites
- Python 3.10+
- Valid Gemini API key (and optionally OpenRouter API key)

### 2. Installation

```bash
# Clone the repository
git clone <YOUR-REPOSITORY-URL>
cd AI-Takeoff-Pro

# Create and activate virtual environment
python -m venv venv
# Windows (PowerShell):
.\venv\Scripts\Activate.ps1
# Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Environment Configuration

Copy the sample environment file and configure your API keys:

```bash
cp .env.example .env
```

Edit `.env`:
```env
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-2.5-flash
PORT=8000
HOST=127.0.0.1
APP_NAME=AI Takeoff Pro
```

### 4. Run the Application

```bash
uvicorn main:app --reload --port 8000
```

Access the web interface at `http://localhost:8000`.

---

## 🛠️ Project Structure

```
├── main.py                    # FastAPI server & route handlers
├── services/
│   ├── takeoff_service.py     # AI takeoff estimation & geometric logic
│   ├── pdf_extractor.py       # PDF parsing & image extraction
│   ├── converter_service.py   # Document conversion orchestrator
│   └── engineering_extractor.py # Schedules, assemblies & keynotes extractor
├── templates/                 # Web dashboard UI
├── static/                    # Frontend styles & scripts
├── tests/                     # Unit and integration test suites
└── .env.example               # Template environment configuration
```

---

## 📄 License
Private & Proprietary. All rights reserved.
