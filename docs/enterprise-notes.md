# Enterprise & air-gap notes

## Air-gap install

1. On a connected box: pull Ollama + the model, then package `~/.ollama/models/` and ship it to the air-gapped host at the same path. No further outbound call is needed at inference time.
2. Ship the gateway as a wheel + `requirements.txt` resolved offline (`pip download -r requirements.txt -d wheels/`), install with `pip install --no-index --find-links=wheels/ -r requirements.txt` on target.

## Service installation

### macOS (launchd)
Save as `~/Library/LaunchAgents/com.private-llm-platform.gateway.plist`, then `launchctl load`.
Keys: `OLLAMA_HOST`, `GATEWAY_PORT`, working dir = the cloned repo.

### Linux (systemd)
`[Service] ExecStart=/path/to/.venv/bin/uvicorn gateway.main:app --host 127.0.0.1 --port 8080`
`[Install] WantedBy=multi-user.target`

### Windows
Install `NSSM` (`nssm install LLDK-Gateway`), set the application to the `python.exe` from the venv and the args to `-m uvicorn gateway.main:app --host 127.0.0.1 --port 8080`.

## Lock down the network interface

Replace `GATEWAY_HOST=127.0.0.1` with the VPN interface's address, not `0.0.0.0`.

## TLS

Terminate TLS at Caddy or Nginx in front and keep the gateway on localhost. Caddy one-liner:
```
example.internal {
    reverse_proxy 127.0.0.1:8080
}
```

## Group Policy / Intune (Windows)

The PowerShell installer honors three env vars (`LLDK_MODEL`, `LLDK_DIR`, `LLDK_REPO`), so a GPO startup script can set those before invoking it.
