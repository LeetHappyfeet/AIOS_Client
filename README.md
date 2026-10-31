# AIOS Client

AIOS Client lets an [AIOS](https://github.com/LeetHappyfeet/AIOS) character carry out
tasks on a computer. It uses an OpenAI-compatible model to read and edit files,
search a project, run terminal commands, and check the results.

It runs on the machine where the work happens, with a browser-based setup page.
AIOS and the model provider can run elsewhere.

## How it connects

AIOS authorizes and routes a work request. The client polls AIOS for assignments,
runs the task in your chosen workspace, and sends back progress, a result summary,
and verification evidence. Detailed command output and logs stay on the client.

You need:

- A running AIOS server with workspace support and its workspace API key.
- An existing AIOS character and a dedicated execution session.
- An OpenAI-compatible model with native tool calling, plus its API key if required.

For chat-driven work, link the execution session to the conversational session and
enable the allowed capabilities in AIOS. Installing the client does not configure
those server-side permissions. See [AIOS pairing](docs/v3-integration.md).

## Install on Ubuntu

Start with Git, Python 3, and Python's venv package installed. Run these commands
as the ordinary account that will own the client:

```bash
git clone --branch main https://github.com/LeetHappyfeet/AIOS_Client.git
cd AIOS_Client
bash install.sh --local-only
~/.local/bin/aios-client setup
```

The installer creates the Python environment and launcher, obtaining Python 3.12
if a suitable Python 3.11+ is unavailable. Open **http://127.0.0.1:8765** on that
machine.

**Installing on a VM or headless server?** Instead of `--local-only`, use
`--remote-url http://YOUR_VM_IP:8765`. Choose a setup password, start setup with
the same command above, then open that URL from your computer and sign in as
**admin**. Use HTTP only on a trusted private network; see the
[installation guide](docs/installation.md) for HTTPS, SSH tunnels, updates, and
background services.

Commands run with the client account's permissions. Use a dedicated ordinary
account or VM; the workspace folder does not sandbox terminal commands.

## Connect and run your first task

1. **AIOS connection:** enter your server address and workspace API key, save,
   then test the connection.
2. **Model endpoint:** enter the provider's base URL (usually ending in `/v1`),
   model ID, and provider key if required. Save and test tool calling.
3. **Agent profile:** enter the character ID and a writable workspace. Use
   **Find existing AIOS sessions** to select the execution session, then save
   and validate the binding.
4. Choose **Listen for AIOS work** to receive assignments, or **Run task** for
   a task entered in setup. Start small and include a required check so the
   client can verify the result.

On headless Linux, choose **Private local file** to save keys. The AIOS workspace
key and model-provider key are separate credentials.

Commands and results appear in the terminal running the client. Use **Stop** to
cancel work. A finished run alone does not mean its checks passed.

## More help

- [Installation and remote setup](docs/installation.md)
- [Credentials, logs, CLI commands, and verification](docs/client-reference.md)
- [AIOS pairing and delegation](docs/v3-integration.md)
- [Runtime tools and development](docs/coding-runtime.md)

### New chats without manual pairing

With an updated AIOS server and its workspace-registration migration, select
**Automatic chat routing** in the client. Save the character, human routing key,
workspace and connections, then **Register / validate workspace** and **Listen for
AIOS work**. Authorize that registered workspace once in AIOS Gradio → Workspace
Delegation. Future matching chats attach automatically; no session/instance/link
UUID copying is required. Existing manual profiles are preserved. See
[automatic routing](docs/client-reference.md#automatic-routing-for-new-chats).
