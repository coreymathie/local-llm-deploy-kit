# Windows notes

This document lists the Windows-specific prerequisites and operating details for the installer and a
single-host deployment. Service installation and Group Policy distribution are in
[enterprise-notes.md](enterprise-notes.md).

- Run PowerShell as **Administrator** for the installer (Ollama's installer requires it).
- If `git` is missing, install it from https://git-scm.com or with `winget install Git.Git`.
- Python 3.11+ must be on `PATH`; `winget install Python.Python.3.11` provides it.
- `ollama serve` on Windows runs as a user process by default. Headless servers run it as a service with NSSM
  or with Task Scheduler "at startup."
- Firewall: the installer binds to `127.0.0.1`, so no inbound rule is needed by default.
- NVIDIA GPU: Ollama detects it automatically. Confirm with `ollama ps` after a model loads.
- The model cache lives at `%USERPROFILE%\.ollama`. The `OLLAMA_MODELS` environment variable moves it if the
  system drive is small.
