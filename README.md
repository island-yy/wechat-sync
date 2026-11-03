# wechat-sync

Sync, search, and query your own local WeChat messages on macOS and Windows. Use it as a standalone tool or an Agent Skill, independently of Violoop.

Features include keyword, contact, conversation, and time filters, continuous incremental sync, a local HTTP API, and JSONL export. The tool reads messages already stored on your computer. It does not send messages or download or parse image, voice, or video attachments.

## Platform support

| Platform | Initialization requirements | Tested environment |
| --- | --- | --- |
| macOS | macOS 11+, WeChat 4.1 or later, Python 3.9+, Apple Command Line Tools | Apple Silicon, WeChat 4.1.13 |
| Windows | Windows 10/11 x64, x64 WeChat 4.x, 64-bit Python 3.11+ | Windows 11 x64, Python 3.13.17, WeChat 4.1.13.63 |

The bundled macOS database library includes arm64 and x86_64 builds, but has not been validated on a physical Intel Mac. Initialization on Windows ARM64 or WeChat 3.x is not supported. Not every version within these ranges has been tested; changes to WeChat key derivation or database schemas may require updates.

## Download and initialize

```sh
git clone https://github.com/island-yy/wechat-sync.git
cd wechat-sync
```

Alternatively, use GitHub's **Download ZIP** option, extract the archive, and open the repository directory. Run all commands on the computer where WeChat is installed, as the currently signed-in user.

### macOS

For first-time setup, run this command in Terminal:

```sh
/usr/bin/python3 scripts/wechat_sync.py init
```

Select your own WeChat account, wait for the capture-ready message, then scan the login QR code in the temporary WeChat copy. Allow developer tool access when macOS prompts you. After successful initialization, the key is stored in your login Keychain and sync runs in the background. Only the temporary copy is re-signed; your installed WeChat app is unchanged, and SIP can remain enabled.

### Windows

Install 64-bit Python 3.11+ first, then run these commands in PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\setup.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\run.ps1 init
```

The setup script creates a `.venv` inside the repository and downloads pinned dependencies. When prompted, fully exit WeChat through its system tray menu, then reopen it from your desktop. Wait for the capture-ready message before logging in. After validation, the key is stored in the current user's Windows Credential Manager.

See the [Windows guide](references/windows.md) for custom data directories, permissions, and troubleshooting.

## Query and manage

Use the following commands on macOS. On Windows, replace `python3 scripts/wechat_sync.py` with `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\run.ps1`:

```sh
python3 scripts/wechat_sync.py status --json
python3 scripts/wechat_sync.py search 'meeting' --limit 20
python3 scripts/wechat_sync.py contacts 'Alice'
python3 scripts/wechat_sync.py conversations 'Project'
python3 scripts/wechat_sync.py messages --conversation 'USERNAME_FROM_QUERY' --since 2026-10-01
python3 scripts/wechat_sync.py changes --after 0 --limit 50
python3 scripts/wechat_sync.py export-jsonl
python3 scripts/wechat_sync.py stop
python3 scripts/wechat_sync.py start
```

Queries return JSON and remain available for collected messages after the service stops. `start` reuses the saved key, and repeated starts preserve a single instance. Reinitialize only when the key becomes invalid or you switch accounts.

The HTTP API listens on `127.0.0.1:8765` by default. All endpoints require a Bearer token. See the [API reference](references/api.md) for parameters, authentication, and incremental consumption, and [Data and troubleshooting](references/data.md) for fields and coverage limits.

## Use as an Agent Skill

Place the complete repository in your agent's skills directory, keeping `SKILL.md`, `scripts/`, `references/`, and the native library together. Have the model read [SKILL.md](SKILL.md) to select the appropriate initialization and query commands for the platform.

The user completes the initial login and system permission prompts. Subsequent queries and sync reuse stored credentials; the model does not need to read or receive the database key.

## Data storage

- macOS: `~/Library/Application Support/WeChat-Export/`
- Windows: `%LOCALAPPDATA%\WeChat-Export\`

Database keys are stored in the system credential store, not in configuration files. WeChat databases are accessed through native SQLCipher read-only connections. Query results stay on your computer, and chat content is not uploaded. The HTTP token is separate from the database key.

## Tests

Run the test suite from the repository directory. It uses synthetic encrypted data and does not access your real WeChat data or saved credentials:

```sh
python3 -B -m unittest discover -s scripts/tests -v
```

On Windows, replace `python3` with `.venv\Scripts\python.exe`. Tests requiring an unavailable platform or optional dependency are skipped. Validation includes native Windows tests, real login, incremental collection of new messages sent from a phone, Chinese keyword search, credential reuse, single-instance startup, and shutdown. See [Data and troubleshooting](references/data.md) for the full validation scope.

## License and sources

Project scripts and documentation are released under the [MIT License](LICENSE). Third-party code and dependencies retain their respective licenses; see [Third-party notices](scripts/THIRD_PARTY_NOTICES.md) for sources and full notices. Source URLs, pinned versions, build parameters, and checksums for the macOS database library are recorded in [BUILD.json](scripts/native/BUILD.json).
