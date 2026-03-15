# Site Override

A QA tool for macOS that lets you clone a live website, edit its content locally, and temporarily hijack the domain so your browser shows the modified version when you visit the real URL.

Built for QA teams who need to simulate website changes for diffing and testing — without touching the real site.

## How It Works

```
1. Clone    →  Enter a URL, the site is mirrored locally via wget
2. Edit     →  Visual click-to-edit or full source editor (CodeMirror)
3. Hijack   →  Domain is redirected to your local copy via /etc/hosts
4. End      →  One click restores everything back to normal
```

When a session is active, typing the domain (e.g. `consumerfinance.gov`) in any browser on your machine will show your edited local copy instead of the live site. HTTPS works seamlessly with [mkcert](https://github.com/FiloSottile/mkcert) — no browser warnings.

## Requirements

- **macOS** (uses `/etc/hosts` and `osascript` for native sudo prompts)
- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** — Python package manager
- **wget** — for site cloning
- **mkcert** (recommended) — for trusted local HTTPS certificates

```bash
brew install wget mkcert
mkcert -install    # one-time: adds local CA to your system keychain
```

## Quick Start

```bash
git clone https://github.com/shiboleth-ai/site-override.git
cd site-override
uv sync
uv run python app/main.py
```

Open **http://127.0.0.1:5000** in your browser.

## Usage

### 1. Clone a Website

Enter a URL (e.g. `https://consumerfinance.gov`) in the clone form. The tool runs `wget --mirror` to download the site with all its assets (HTML, CSS, JS, images).

### 2. Edit Content

Click **Edit** on a cloned site. Two editing modes:

- **Visual Editor** — Click "Click to Edit" in the toolbar, then click any text on the page to modify it inline. Hit "Save Changes" when done.
- **Source Editor** — Click any file in the sidebar to open it in a code editor. Supports HTML, CSS, JS with syntax highlighting.

### 3. Start a Hijack Session

Click **Hijack** on a cloned site. The tool will:

1. Generate an SSL certificate for the domain (mkcert if available, self-signed otherwise)
2. Add `127.0.0.1 <domain>` to `/etc/hosts` (macOS password prompt will appear)
3. Start a local HTTP/HTTPS server on ports 80/443
4. Flush the DNS cache

Now visiting the domain in your browser shows your local edited copy.

### 4. End the Session

Click the prominent red **END SESSION** button in the top banner. This:

1. Stops the local server
2. Removes the `/etc/hosts` entry
3. Flushes DNS cache

The domain goes back to serving the real site.

## Safety

- **Signal handlers** — `Ctrl+C`, `SIGTERM`, and process exit all trigger cleanup that kills the local server
- **Crash recovery** — On startup, the app checks for stale sessions from previous crashes and cleans up
- **Manual cleanup** — If something goes wrong, run:
  ```bash
  sudo sed -i "" "/# SITE-OVERRIDE-MANAGED/d" /etc/hosts
  sudo dscacheutil -flushcache
  sudo killall -HUP mDNSResponder
  ```

## Architecture

```
site-override/
├── app/
│   ├── __init__.py              # Flask app factory
│   ├── main.py                  # Entry point with signal handlers
│   ├── routes.py                # All HTTP routes
│   ├── services/
│   │   ├── certs.py             # mkcert / self-signed cert generation
│   │   ├── cloner.py            # wget-based site cloning
│   │   ├── hijack.py            # /etc/hosts + session management
│   │   └── hijack_server.py     # Standalone server (runs with sudo)
│   ├── templates/               # Jinja2 + htmx templates
│   └── static/                  # CSS + visual editor JS
├── pyproject.toml
└── uv.lock
```

**Key tech:** Flask, htmx, Tailwind CSS (CDN), CodeMirror 5, BeautifulSoup (for editor injection), cryptography (cert generation).

## Notes

- Only one hijack session can be active at a time
- The hijack is system-wide — all browsers and apps on your machine will resolve the domain to localhost
- Sites with complex SPAs or heavy client-side rendering may not clone perfectly with wget
- The cloned site's internal links are converted to relative paths by wget's `--convert-links`

## License

MIT
