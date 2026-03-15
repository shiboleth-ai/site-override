import os
import shutil
import subprocess
from urllib.parse import urlparse


def clone_site(url: str, sites_dir: str) -> dict:
    """Clone a website using wget. Returns metadata dict."""
    parsed = urlparse(url)
    if not parsed.scheme:
        url = f"https://{url}"
        parsed = urlparse(url)

    domain = parsed.netloc
    if not domain:
        return {"success": False, "error": "Invalid URL"}

    site_dir = os.path.join(sites_dir, domain)
    if os.path.exists(site_dir):
        return {"success": False, "error": f"Site '{domain}' already cloned. Delete it first to re-clone."}

    # Check wget is available
    if not shutil.which("wget"):
        return {"success": False, "error": "wget is not installed. Run: brew install wget"}

    try:
        result = subprocess.run(
            [
                "wget",
                "--mirror",
                "--convert-links",
                "--adjust-extension",
                "--page-requisites",
                "--no-parent",
                "-e", "robots=off",
                "--restrict-file-names=windows",
                "--timeout=30",
                "--tries=3",
                "-l", "5",
                "-P", sites_dir,
                url,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        # Partial clone may exist, keep it
        pass

    if os.path.isdir(site_dir):
        return {"success": True, "domain": domain, "path": site_dir}

    return {"success": False, "error": f"Clone failed. wget output:\n{result.stderr[-500:] if result else 'timeout'}"}


def list_sites(sites_dir: str) -> list[dict]:
    """List all cloned sites."""
    sites = []
    if not os.path.isdir(sites_dir):
        return sites
    for name in sorted(os.listdir(sites_dir)):
        site_path = os.path.join(sites_dir, name)
        if os.path.isdir(site_path) and not name.startswith("."):
            sites.append({"domain": name, "path": site_path})
    return sites


def get_file_tree(site_dir: str, prefix: str = "") -> list[dict]:
    """Walk site directory and return a flat list with depth info for rendering."""
    items = []
    try:
        entries = sorted(os.listdir(os.path.join(site_dir, prefix)))
    except OSError:
        return items

    dirs = []
    files = []
    for entry in entries:
        rel_path = os.path.join(prefix, entry) if prefix else entry
        full_path = os.path.join(site_dir, rel_path)
        if os.path.isdir(full_path):
            dirs.append((entry, rel_path))
        else:
            files.append((entry, rel_path))

    # Dirs first, then files
    for name, rel_path in dirs:
        depth = rel_path.count(os.sep)
        items.append({"name": name, "path": rel_path, "is_dir": True, "depth": depth})
        items.extend(get_file_tree(site_dir, rel_path))
    for name, rel_path in files:
        depth = rel_path.count(os.sep)
        items.append({"name": name, "path": rel_path, "is_dir": False, "depth": depth})

    return items


def delete_site(domain: str, sites_dir: str) -> dict:
    """Delete a cloned site."""
    site_dir = os.path.join(sites_dir, domain)
    if not os.path.isdir(site_dir):
        return {"success": False, "error": "Site not found"}
    shutil.rmtree(site_dir)
    return {"success": True}
