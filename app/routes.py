import mimetypes
import os

from bs4 import BeautifulSoup
from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .services.certs import generate_cert, install_mkcert, mkcert_status
from .services.cloner import clone_site, delete_site, get_file_tree, list_sites
from .services.hijack import SessionManager

bp = Blueprint("main", __name__)


def _session_mgr() -> SessionManager:
    return current_app.config["SESSION_MANAGER"]


# ── Dashboard ──────────────────────────────────────────────────────────


@bp.route("/")
def index():
    sites = list_sites(current_app.config["SITES_DIR"])
    status = _session_mgr().get_status()
    mkcert = mkcert_status()
    return render_template("index.html", sites=sites, session_status=status, mkcert=mkcert)


# ── Clone ──────────────────────────────────────────────────────────────


@bp.route("/clone", methods=["POST"])
def clone():
    url = request.form.get("url", "").strip()
    if not url:
        flash("Please enter a URL", "error")
        return redirect(url_for("main.index"))

    result = clone_site(url, current_app.config["SITES_DIR"])

    if result["success"]:
        flash(f"Cloned {result['domain']} successfully!", "success")
    else:
        flash(result["error"], "error")

    # Return full page for htmx or redirect
    if request.headers.get("HX-Request"):
        sites = list_sites(current_app.config["SITES_DIR"])
        status = _session_mgr().get_status()
        return render_template("index.html", sites=sites, session_status=status)
    return redirect(url_for("main.index"))


# ── Delete Site ────────────────────────────────────────────────────────


@bp.route("/sites/<domain>/delete", methods=["POST"])
def site_delete(domain):
    result = delete_site(domain, current_app.config["SITES_DIR"])
    if result["success"]:
        flash(f"Deleted {domain}", "success")
    else:
        flash(result["error"], "error")

    if request.headers.get("HX-Request"):
        sites = list_sites(current_app.config["SITES_DIR"])
        status = _session_mgr().get_status()
        return render_template("index.html", sites=sites, session_status=status)
    return redirect(url_for("main.index"))


# ── Editor ─────────────────────────────────────────────────────────────


@bp.route("/editor/<domain>")
def editor(domain):
    site_dir = os.path.join(current_app.config["SITES_DIR"], domain)
    if not os.path.isdir(site_dir):
        flash("Site not found", "error")
        return redirect(url_for("main.index"))

    files = get_file_tree(site_dir)
    status = _session_mgr().get_status()

    # Find default file to show (index.html)
    default_file = None
    for f in files:
        if not f["is_dir"] and f["name"] in ("index.html", "index.htm"):
            default_file = f["path"]
            break
    if not default_file:
        for f in files:
            if not f["is_dir"] and f["path"].endswith((".html", ".htm")):
                default_file = f["path"]
                break

    return render_template(
        "editor.html",
        domain=domain,
        files=files,
        default_file=default_file,
        session_status=status,
    )


@bp.route("/editor/<domain>/file")
def get_file(domain):
    """Return file content for the source editor."""
    site_dir = os.path.join(current_app.config["SITES_DIR"], domain)
    file_path = request.args.get("path", "")
    full_path = os.path.normpath(os.path.join(site_dir, file_path))

    # Security: prevent path traversal
    if not full_path.startswith(site_dir):
        return "Forbidden", 403

    if not os.path.isfile(full_path):
        return "File not found", 404

    try:
        with open(full_path, "r", errors="replace") as f:
            content = f.read()
    except UnicodeDecodeError:
        return "Binary file - cannot edit", 400

    # Determine mode for CodeMirror
    ext = os.path.splitext(file_path)[1].lower()
    mode_map = {
        ".html": "htmlmixed",
        ".htm": "htmlmixed",
        ".css": "css",
        ".js": "javascript",
        ".json": "javascript",
        ".xml": "xml",
        ".svg": "xml",
    }
    mode = mode_map.get(ext, "htmlmixed")

    return render_template(
        "_source_editor.html", content=content, file_path=file_path, mode=mode, domain=domain
    )


@bp.route("/editor/<domain>/file", methods=["POST"])
def save_file(domain):
    """Save file content from the source editor."""
    site_dir = os.path.join(current_app.config["SITES_DIR"], domain)
    file_path = request.form.get("path", "")
    content = request.form.get("content", "")
    full_path = os.path.normpath(os.path.join(site_dir, file_path))

    if not full_path.startswith(site_dir):
        return "Forbidden", 403

    with open(full_path, "w") as f:
        f.write(content)

    return '<span class="text-green-400">Saved!</span>'


# ── Visual Preview & Editor ────────────────────────────────────────────


@bp.route("/preview/<domain>/", defaults={"filepath": "index.html"})
@bp.route("/preview/<domain>/<path:filepath>")
def preview(domain, filepath):
    """Serve cloned site files with visual editor overlay injected into HTML."""
    site_dir = os.path.join(current_app.config["SITES_DIR"], domain)
    full_path = os.path.normpath(os.path.join(site_dir, filepath))

    if not full_path.startswith(site_dir):
        return "Forbidden", 403

    # Try with .html extension if not found
    if not os.path.isfile(full_path):
        for ext in (".html", ".htm"):
            alt = full_path + ext
            if os.path.isfile(alt):
                full_path = alt
                break

    if not os.path.isfile(full_path):
        return "File not found", 404

    mime_type, _ = mimetypes.guess_type(full_path)
    if not mime_type:
        mime_type = "application/octet-stream"

    with open(full_path, "rb") as f:
        content = f.read()

    # Inject editor overlay into HTML files
    if mime_type and "html" in mime_type:
        try:
            html_str = content.decode("utf-8", errors="replace")
            soup = BeautifulSoup(html_str, "html.parser")

            # Inject editor CSS
            style_tag = soup.new_tag("link", rel="stylesheet",
                                     href=url_for("static", filename="css/editor-overlay.css"))
            # Inject editor JS
            script_tag = soup.new_tag("script",
                                      src=url_for("static", filename="js/editor-inject.js"))
            # Data attributes for the editor
            meta_tag = soup.new_tag("meta", attrs={
                "name": "site-override-domain",
                "content": domain,
            })
            meta_path = soup.new_tag("meta", attrs={
                "name": "site-override-path",
                "content": filepath,
            })

            if soup.head:
                soup.head.append(style_tag)
                soup.head.append(meta_tag)
                soup.head.append(meta_path)
            else:
                head = soup.new_tag("head")
                head.append(style_tag)
                head.append(meta_tag)
                head.append(meta_path)
                if soup.html:
                    soup.html.insert(0, head)

            if soup.body:
                soup.body.append(script_tag)
            else:
                body = soup.new_tag("body")
                body.append(script_tag)
                if soup.html:
                    soup.html.append(body)

            content = str(soup).encode("utf-8")
        except Exception:
            pass  # Serve original if injection fails

    return Response(content, mimetype=mime_type)


@bp.route("/editor/<domain>/visual-save", methods=["POST"])
def visual_save(domain):
    """Save HTML from the visual editor."""
    site_dir = os.path.join(current_app.config["SITES_DIR"], domain)
    file_path = request.form.get("path", "")
    html_content = request.form.get("html", "")
    full_path = os.path.normpath(os.path.join(site_dir, file_path))

    if not full_path.startswith(site_dir):
        return "Forbidden", 403

    # Strip our injected elements before saving
    soup = BeautifulSoup(html_content, "html.parser")

    # Remove our injected elements
    for tag in soup.find_all("meta", attrs={"name": lambda n: n and n.startswith("site-override")}):
        tag.decompose()
    for tag in soup.find_all("link", href=lambda h: h and "editor-overlay" in str(h)):
        tag.decompose()
    for tag in soup.find_all("script", src=lambda s: s and "editor-inject" in str(s)):
        tag.decompose()
    # Remove the injected toolbar
    for tag in soup.find_all(id="so-toolbar"):
        tag.decompose()
    # Remove contenteditable attributes
    for tag in soup.find_all(attrs={"contenteditable": True}):
        del tag["contenteditable"]

    with open(full_path, "w") as f:
        f.write(str(soup))

    return '{"status": "saved"}', 200, {"Content-Type": "application/json"}


# ── Session Management ─────────────────────────────────────────────────


@bp.route("/session/start", methods=["POST"])
def session_start():
    domain = request.form.get("domain", "").strip()
    if not domain:
        flash("No domain specified", "error")
        return redirect(url_for("main.index"))

    site_dir = os.path.join(current_app.config["SITES_DIR"], domain)
    if not os.path.isdir(site_dir):
        flash("Site not found", "error")
        return redirect(url_for("main.index"))

    # Generate cert (tries mkcert first, falls back to self-signed)
    cert_path, key_path, is_trusted = generate_cert(
        domain, current_app.config["CERTS_DIR"]
    )

    result = _session_mgr().start_session(domain, site_dir, cert_path, key_path)

    if result["success"]:
        if is_trusted:
            flash(
                f"Session active! {domain} now points to your local copy. "
                f"Certificate is trusted by your browser (via mkcert).",
                "success",
            )
        else:
            flash(
                f"Session active! {domain} now points to your local copy. "
                f"Your browser will show a certificate warning - click through it. "
                f"Set up mkcert to avoid this.",
                "success",
            )
    else:
        flash(result["error"], "error")

    if request.headers.get("HX-Request"):
        sites = list_sites(current_app.config["SITES_DIR"])
        status = _session_mgr().get_status()
        return render_template("index.html", sites=sites, session_status=status)
    return redirect(url_for("main.index"))


@bp.route("/session/stop", methods=["POST"])
def session_stop():
    result = _session_mgr().stop_session()

    if result["success"]:
        flash(f"Session ended. {result.get('domain', 'Domain')} restored to normal.", "success")
    else:
        flash(result["error"], "error")

    if request.headers.get("HX-Request"):
        sites = list_sites(current_app.config["SITES_DIR"])
        status = _session_mgr().get_status()
        return render_template("index.html", sites=sites, session_status=status)
    return redirect(url_for("main.index"))


@bp.route("/session/status")
def session_status():
    status = _session_mgr().get_status()
    return render_template("_session_bar.html", session_status=status)


# ── mkcert Setup ───────────────────────────────────────────────────────


@bp.route("/mkcert/setup", methods=["POST"])
def mkcert_setup():
    result = install_mkcert()
    if result["success"]:
        flash(
            "mkcert installed and local CA set up! "
            "Hijacked sites will now show trusted HTTPS with no browser warnings.",
            "success",
        )
        # Delete any existing self-signed certs so they get regenerated with mkcert
        certs_dir = current_app.config["CERTS_DIR"]
        for f in os.listdir(certs_dir):
            os.unlink(os.path.join(certs_dir, f))
    else:
        flash(result["error"], "error")

    if request.headers.get("HX-Request"):
        sites = list_sites(current_app.config["SITES_DIR"])
        status = _session_mgr().get_status()
        mkcert = mkcert_status()
        return render_template("index.html", sites=sites, session_status=status, mkcert=mkcert)
    return redirect(url_for("main.index"))
