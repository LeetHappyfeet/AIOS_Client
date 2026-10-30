# Ubuntu installation and remote setup

This installation target includes the v3 worker, persistent headless credentials,
installer, authenticated remote UI, diagnostics and optional systemd user services.
Use `main` for the combined client. Setup displays the installed branch and commit
so it is clear what is running.

## Administrator preparation

Use your ordinary administrator account to install the bootstrap prerequisites and
create the agent account if it does not already exist:

```bash
sudo apt update
sudo apt install -y python3 python3-venv git
sudo adduser aiosagent
```

Keep the agent account out of sudo and Docker groups. Log in directly over SSH as
`aiosagent` for installation and user-service management. No new account is needed
for each character. Your SSH/Linux password, UI administrator password, AIOS worker
key and model provider key serve different purposes.

## Install as aiosagent

For a new checkout:

```bash
mkdir -p ~/apps
cd ~/apps
git clone --branch main https://github.com/LeetHappyfeet/AIOS_Client.git
cd AIOS_Client
bash install.sh --remote-url http://192.168.1.161:8765
```

Replace the example IP with your VM's reachable address. Existing checkouts can use:

```bash
cd ~/apps/AIOS_Client
git fetch origin
git switch main
git pull --ff-only origin main
bash install.sh --remote-url http://192.168.1.161:8765
```

The installer displays the current account, home, checkout and workspace before asking
to proceed. It refuses root. It uses an available Python >=3.11 or downloads user-local
uv and Python 3.12 through uv. Ubuntu's system Python is unchanged. Internet access to
PyPI and uv's Python distribution host is required when bootstrapping. There is no
automatic sudo or OS package installation. An incompatible existing `.venv` is left
intact with instructions to rename it; the installer does not delete it.

The application environment is always `.venv`. Existing `.venv312` installations can
remain alongside it; use the new launcher after installation. The default workspace is
`~/workspace/test-project`; override it with `--workspace /absolute/project/path`.
The installer saves that default for the setup form. Projects need not live beside
the application. Rerunning keeps profiles, credentials, and an existing UI password.

Choose a UI password of at least 12 characters when prompted. It is stored as a salted
scrypt hash in `~/.config/aios-client/client.json.ui.json`, separately from exported
profiles. It is not an AIOS or provider key and is never printed by the installer.

Start the UI:

```bash
~/.local/bin/aios-client setup
```

If `~/.local/bin` is on PATH, `aios-client setup` works directly. You do not need to
activate a virtual environment. Open **http://192.168.1.161:8765** from your other
computer and sign in as **admin** with the chosen UI password. No SSH tunnel is needed.
The VM firewall must permit your computer to reach that TCP port; the installer does
not open firewall rules or router ports automatically.

The UI controls command execution as the client account. Plain HTTP is intended for a
trusted private network; use HTTPS or a VPN on untrusted networks. HTTP Basic credentials
are not encrypted in transit over HTTP. To use a certificate directly:

```bash
~/.local/bin/aios-client configure-ui --host 0.0.0.0 \
  --public-url https://playground.example:8765 \
  --tls-certfile /absolute/cert.pem --tls-keyfile /absolute/key.pem
~/.local/bin/aios-client setup
```

Use a certificate trusted by your browser. The exact configured browser origin must
match the URL you open. Foreign Host/Origin requests and changes without the setup
request token are rejected even after login. Forwarded proxy headers are not trusted.
A TLS-terminating reverse proxy must preserve the configured Host and allow the
configured Origin; bind the backend to loopback to keep it private.

To change the address or reset the UI password later:

```bash
~/.local/bin/aios-client configure-ui --host 0.0.0.0 --public-url http://192.168.1.161:8765
~/.local/bin/aios-client configure-ui --reset-password
```

Restart the UI afterward. Repeated incorrect logins are temporarily throttled. Browsers
cache Basic authentication; close the browser session to clear a cached login.

For local-only setup, install with `bash install.sh --local-only`. It retains the
original loopback-only behavior; a password is optional on loopback. From Windows use
`ssh -N -L 8765:127.0.0.1:8765 happyfeet@192.168.1.161` and open
`http://127.0.0.1:8765`. If changing an existing authenticated install to local-only,
its saved password remains required.

## Save the connections once

1. **AIOS:** enter its address/port, choose Private local file, paste its worker key
   into API key and Save connection. Test the connection.
2. **Model:** enter the provider's OpenAI-compatible base URL and exact model ID,
   choose Private local file, paste its key and Save endpoint. Load models if supported,
   then test tools. A generation/tool probe can incur provider charges.
3. **Agent:** choose a profile name and worker label, the exact stored AIOS character
   ID, and a writable project directory. Find sessions and explicitly select the
   dedicated execution session. Save agent, then Validate binding.

References are under Advanced and are filled automatically. Local keys use owner-only,
unencrypted files under `client.json.credentials/`. They survive reboots and are not
included in profile exports. A blank password field keeps the saved key. The Linux
account running commands can access its own credential files; this is not a sandbox.
AIOS itself still needs the matching worker key configured in its own launch settings.
The installer does not modify your AIOS server or database.

One Linux account and saved connection/model can serve several character profiles.
Character IDs, routing users, sessions and instances remain distinct from Linux users.
There is no general character-list API in the inspected AIOS version: this client
retains exact-ID session discovery. No sessions means you must verify the ID and prepare
an active execution session through AIOS. The installer does not invent or create
character/session IDs, environment links, or server-side permissions. Keep the
conversational origin separate from the linked execution instance as documented in
[v3 integration](v3-integration.md).

## Diagnose and verify

```bash
~/.local/bin/aios-client doctor --agent "Shego Playground"
# Optional generation probe; provider charges may apply:
~/.local/bin/aios-client doctor --agent "Shego Playground" --probe-model
```

Doctor checks configuration, workspace accessibility, credential resolution, AIOS
protocol, matching session coordinates and model listing. It prints no keys and makes
no binding/assignment writes. Session discovery is not authoritative bind validation;
use Validate binding for that. A provider may support generation but not `/models`;
the listing failure and optional probe are reported separately. Existing `check` binds
the profile and probes generation, so it has different effects from doctor.

For an initial assignment use a small task with an explicit required check. A final
model message or `finished` run alone is not passing host verification. See the README's
assignment examples. No live provider call is made during installation.

## Optional background services

After stopping a foreground setup server, install and start a UI user service:

```bash
~/.local/bin/aios-client service install --kind ui --enable
systemctl --user status aios-client-ui
journalctl --user -u aios-client-ui -f
systemctl --user stop aios-client-ui
systemctl --user start aios-client-ui
```

The installer also accepts `--service` to do this at the end. Without `--enable`, the
service command only writes the unit file; it does not start it. For startup at boot
before login, an administrator must run once:

```bash
sudo loginctl enable-linger aiosagent
```

A missing user bus is common after `sudo -iu aiosagent`; log in directly as aiosagent
over SSH, then run `systemctl --user daemon-reload` and enable/start the saved unit.

For unattended listening after configuring an agent, you can instead install a worker:

```bash
~/.local/bin/aios-client service install --kind worker --agent "Shego Playground" --enable
```

Use saved file credentials for unattended services; shell-exported variables are not
automatically inherited by the service manager. Do not start that same agent from the
UI while its worker service is active. To change an existing unit to a different agent,
stop/disable it, explicitly remove or edit the unit, then reinstall. Unit generation
preserves a differently configured existing service instead of silently overwriting it.
No passwordless sudo privileges, root services or firewall changes are installed.
