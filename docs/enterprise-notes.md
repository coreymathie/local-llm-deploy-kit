# Enterprise and air-gap notes

This document covers host-level installation for single-server deployments: running the gateway as an
operating-system service, restricting its network interface, terminating TLS in front of it, and distributing
the Windows installer through Group Policy or Intune. Cluster deployment is in [kubernetes.md](kubernetes.md);
the full air-gapped procedure is in [compliance.md](compliance.md#air-gapped-deployment).

## Air-gapped install (summary)

1. On a connected host: pull Ollama and the model, then package `~/.ollama/models/` and ship it to the
   air-gapped host at the same path. No further outbound call is needed at inference time.
2. Ship the gateway as a wheel + `requirements.txt` resolved offline
   (`pip download -r requirements.txt -d wheels/`), and install with
   `pip install --no-index --find-links=wheels/ -r requirements.txt` on the target.

## Service installation

### macOS (launchd)

Save as `~/Library/LaunchAgents/com.private-llm-platform.gateway.plist`, then `launchctl load`.
Keys: `OLLAMA_HOST`, `GATEWAY_PORT`, working dir = the cloned repo.

### Linux (systemd)

`[Service] ExecStart=/path/to/.venv/bin/uvicorn gateway.main:app --host 127.0.0.1 --port 8080`
`[Install] WantedBy=multi-user.target`

### Windows

Install `NSSM` (`nssm install LLDK-Gateway`), set the application to the `python.exe` from the venv and the
arguments to `-m uvicorn gateway.main:app --host 127.0.0.1 --port 8080`.

## Network interface

`GATEWAY_HOST=127.0.0.1` is replaced with the VPN interface's address, never `0.0.0.0`.

## TLS

TLS terminates at Caddy or Nginx in front, and the gateway stays on localhost. Caddy configuration:

```
example.internal {
    reverse_proxy 127.0.0.1:8080
}
```

## Group Policy / Intune (Windows)

The PowerShell installer honors three environment variables (`LLDK_MODEL`, `LLDK_DIR`, `LLDK_REPO`), so a GPO
startup script can set them before invoking it.
