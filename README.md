# AIOS Client

AIOS Client lets an [AIOS](https://github.com/LeetHappyfeet/AIOS) character do work on a computer you control. The client runs where the files and tools are. AIOS sends authorized tasks, and the client uses your configured model to work through them and report the results.

## What it can do

- **Workspace tasks** — search and edit files, inspect Git changes, run terminal commands, and verify expected outputs.
- **Research** — search your configured SearXNG, Calibre, and local document sources, and cite passages it reads. You choose which sources each agent can use.
- **Project notebooks** — pick up decisions and next steps from earlier attempts on the same goal when your AIOS server supports workspace notebooks.
- **Run monitoring** — follow progress, verification, and report delivery in the browser or terminal. Multiple agent profiles can listen at once.

## How it compares with ChatGPT and Grok

Like ChatGPT and Grok, AIOS Client gives a model tools to search for information and run code. ChatGPT and Grok offer hosted tools, and their desktop products can also work with local files. [ChatGPT](https://help.openai.com/en/articles/6825453-chatgpt-for-developers) · [Grok tools](https://docs.x.ai/developers/tools/overview) · [Grok Bot](https://docs.x.ai/grok-bot/security)

With AIOS Client, you host the worker and choose its model and research sources. Install it on a spare Ubuntu PC or virtual machine and chat from another device. Once you authorize the workspace in AIOS, the character can run commands, edit files, and verify results on that computer using the Linux account that runs the client.

## Install on Ubuntu

You’ll need a running AIOS server with workspace support, an existing AIOS character, and the server’s workspace API key. For automatic chat routing, update AIOS and apply its workspace-registration migration before setup. You’ll also need a model that supports tool calling through an OpenAI-compatible API. Have its base URL, model ID, and API key if required.

On the computer that will do the work, install the prerequisites from an administrator account:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
```

Then open a terminal as the ordinary Linux user that will run the client:

```bash
git clone --branch development https://github.com/LeetHappyfeet/AIOS_Client.git
cd AIOS_Client
bash install.sh --local-only
~/.local/bin/aios-client setup
```

The installer needs internet access to install dependencies. It creates a private Python environment and downloads Python 3.12 if Python 3.11 or newer is unavailable. Open <http://127.0.0.1:8765> on that computer. To start the client again later, run `~/.local/bin/aios-client setup`.

**Using a VM or a server without a desktop?** Replace the install command with `bash install.sh --remote-url http://YOUR_SERVER_IP:8765`, using the server’s actual IP address. Choose a setup password, then run the same setup command and sign in as **admin** at that address. Use plain HTTP only on a trusted private network. The [installation guide](docs/installation.md) covers SSH tunnels, HTTPS, and background services.

Use a dedicated Linux account without sudo. Terminal commands have that account’s permissions and can reach files outside the selected workspace.

## Connect and run your first task

1. Under **AIOS connection**, enter the AIOS server address and workspace API key. Choose **Save connection**, then **Test connection**.
2. Under **Generation endpoint**, enter the model’s base URL, model ID, and provider key if required. Choose **Save endpoint**, then **Test generation & tools**. On a server without an OS keyring, choose **Private local file** when saving keys.
3. Under **Agent profile**, keep **Automatic chat routing** selected. Enter a profile name, a worker label, the exact AIOS character ID, the human routing key AIOS uses for your chats, and a writable workspace folder. Leave the scope at `conversation` for ordinary chats. Select the saved connections, then choose **Save agent**, **Register / validate workspace**, and **Listen for AIOS work**.
4. In AIOS, open **Workspace Delegation**, refresh the registered workspaces, and authorize this worker and folder for current and future chats. Choose an execution policy. Leave the listener running.
5. Open a new chat with the matching character and try a small task. For example, ask it to create `hello.txt` containing `Hello from AIOS` and verify the file’s contents. Check the run status for the verification result.

## More help

- [Automatic routing, existing profiles, and troubleshooting](docs/client-reference.md#automatic-routing-for-new-chats)
- [Research source setup and permissions](docs/research.md#configure-sources-from-the-setup-page-or-a-json-file)
- [Notebook support](docs/client-reference.md#coding-behavior-and-records)
