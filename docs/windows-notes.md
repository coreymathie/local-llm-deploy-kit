# Windows-specific notes

- Run PowerShell as **Administrator** for the installer (Ollama's installer needs it).
- If `git` is missing, install it from https://git-scm.com or via `winget install Git.Git`.
- Python 3.11+ needs to be on `PATH`. `winget install Python.Python.3.11` works.
- `ollama serve` on Windows runs as a user process by default. For headless servers, run it under a service with NSSM or Task Scheduler "at startup."
- Firewall: the installer binds to `127.0.0.1`, so no inbound rule is needed by default.
- NVIDIA GPU: Ollama auto-detects. Confirm with `ollama ps` after a model loads.
- Model cache lives at `%USERPROFILE%\.ollama`. Move it with `OLLAMA_MODELS` env var if the system drive is small.
