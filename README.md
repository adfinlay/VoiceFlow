# VoiceFlow

> Open-source voice dictation for **Windows and Linux**. Hold a hotkey to record, release and the transcript is pasted at your cursor. Runs offline with Whisper. Free, no account needed. (macOS works but isn't officially supported yet.)

**Website:** [get-voice-flow.vercel.app](https://get-voice-flow.vercel.app/) · **Source:** [github.com/infiniV/VoiceFlow](https://github.com/infiniV/VoiceFlow)

<p align="center">
  <img src="media/dashboard.png" alt="VoiceFlow Dashboard" width="100%">
</p>

# Own your Voice.

**Dictate freely with local AI. Zero latency. Zero data leaks. Zero cost.**

VoiceFlow brings OpenAI's Whisper directly to your machine. Every word you speak is processed entirely on your hardware—your voice data never leaves your device. Built for privacy-conscious professionals who demand speed and reliability.

> **Linux Support**
> VoiceFlow runs natively on Linux with Wayland & X11 support, evdev hotkeys, CUDA GPU acceleration (with CPU fallback), and AppImage packaging. [Download the Linux release](https://github.com/infiniV/VoiceFlow/releases/latest)

<p align="center">
  <a href="https://get-voice-flow.vercel.app/">
    <img src="https://img.shields.io/badge/Visit_Website-000000?style=for-the-badge&logo=vercel&logoColor=white" alt="Website">
  </a>
  <a href="https://github.com/infiniV/VoiceFlow/releases/latest">
    <img src="https://img.shields.io/badge/Download_for_Windows-000000?style=for-the-badge&logo=windows&logoColor=white" alt="Download Windows">
  </a>
  <a href="https://github.com/infiniV/VoiceFlow/releases/latest">
    <img src="https://img.shields.io/badge/Download_for_Linux-000000?style=for-the-badge&logo=linux&logoColor=white" alt="Download Linux">
  </a>
  <a href="https://github.com/infiniV/VoiceFlow">
    <img src="https://img.shields.io/badge/View_Source-000000?style=for-the-badge&logo=github&logoColor=white" alt="GitHub">
  </a>
</p>

---

### Why Pay for Noise?

Cloud dictation services charge monthly fees while harvesting your voice data. VoiceFlow is free, fully local, and yours forever.

| Feature | VoiceFlow | Cloud Services |
| :--- | :---: | :---: |
| **Cost** | **$0.00** | $10-15/mo |
| **Data Privacy** | **100% Local** | Cloud Processed |
| **Offline Support** | **Full Capability** | None |
| **Latency** | **Real-time** | Network Dependent |
| **Account Required** | **No** | Yes |
| **Open Source** | **MIT License** | Proprietary |

---

### Unbreakable Privacy

Everything runs on localhost. Your microphone data never leaves your RAM. We can't sell your data because we never see it.

*   **Air-Gapped Safe**: Works completely offline after initial model download.
*   **Open Source**: Audit every line of code yourself.
*   **No Telemetry**: Zero tracking, zero analytics, zero cloud calls.

---

### How It Works

No hidden processes, no cloud uploads. Just transparent, local AI at every step.

#### 1. Ready
VoiceFlow waits silently in your system tray. A minimal popup indicates recording status.

#### 2. Listening
Activate with your hotkey and speak naturally. Audio stays in RAM only—the interface visualizes your voice amplitude in real-time.

#### 3. Transcribe & Paste
Release the hotkey. Local AI processes your audio instantly, then auto-pastes text at your cursor.

The dashboard shown above is your live dictation log — today's words and entries appear inline next to lifetime totals, with your active model, language, microphone, and compute device on display.

---

### Guided Setup

A seven-step setup picks the right microphone, compute device, and model for your hardware — no manual configuration. Dark mode by default; light and system themes are one click away.

<p align="center">
  <img src="media/onboarding.png" alt="VoiceFlow Onboarding" width="100%">
</p>

---

### Custom Hotkeys

Configure your preferred keyboard shortcuts with two recording modes to match your workflow.

*   **Hold Mode**: Hold to record, release to transcribe. Perfect for quick dictation bursts.
*   **Toggle Mode**: Press once to start, press again to stop. Ideal for longer recordings.

---

### Neural Engine

Choose from 16+ Whisper models optimized for different use cases. Each option shows speed, accuracy, parameter count, and disk footprint so you can pick what your hardware can comfortably run.

<p align="center">
  <img src="media/model-picker.png" alt="Model Picker" width="100%">
</p>

#### Model Categories
*   **Standard** (Tiny → Large-v3): From 75MB to 3GB. Balance speed and accuracy for your hardware.
*   **Turbo** (~1.6GB): Best speed-to-quality ratio. Recommended for daily use.
*   **English-only** (.en variants): Optimized specifically for English with improved accuracy.
*   **Distilled**: Faster inference with minimal quality loss.

#### Core Features
*   **99+ Languages**: Automatic language detection built-in.
*   **Custom Hotkeys**: Configure your own shortcuts with Hold or Toggle modes.
*   **Local History**: Searchable SQLite database of all your transcriptions.
*   **Auto-Paste**: Text appears directly at your cursor—no copy-paste needed.

---

### Ready to go local?

Take back control of your voice data. Open source and forever free.

### [Visit get-voice-flow.vercel.app](https://get-voice-flow.vercel.app/) · [Download latest release](https://github.com/infiniV/VoiceFlow/releases/latest)

*Windows 10/11 (.exe) • Linux (.AppImage / .tar.gz) • 64-bit*

<br>
<br>

---

# For Developers

Build and contribute to VoiceFlow.

### Quick Start

```powershell
# Clone and setup
git clone https://github.com/infiniV/VoiceFlow.git
cd VoiceFlow
pnpm run setup

# Development with hot-reload
pnpm run dev

# Build installer
pnpm run build:installer
```

### Architecture

| Layer | Technology |
| :--- | :--- |
| **Core** | Pyloid (PySide6 + QtWebEngine) |
| **Inference** | faster-whisper (CTranslate2) |
| **Frontend** | React 18, Vite, Tailwind CSS v4 |
| **UI** | shadcn/ui, Lucide React |

[Releases](https://github.com/infiniV/VoiceFlow/releases) • [Issues](https://github.com/infiniV/VoiceFlow/issues) • [License](LICENSE)
