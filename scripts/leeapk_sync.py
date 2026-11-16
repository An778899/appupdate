import os
import re
import time
import pathlib
import zipfile
import requests
from playwright.sync_api import sync_playwright

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ.get("GITHUB_REPOSITORY", "An778899/appupdate")
HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

DOWNLOAD_DIR = pathlib.Path("temp_downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

CATEGORIES = {
    "app": "https://leeapk.com/apps/",
    "game": "https://leeapk.com/games/",
}

MAX_ITEMS_PER_RUN = int(os.environ.get("MAX_ITEMS_PER_RUN", "5"))


def slugify(name: str) -> str:
    clean = re.sub(
        r"\b(mod|apk|xapk|apks|premium|unlocked|pro|full|version|v?\d+\.\d+.*)\b",
        "",
        name,
        flags=re.I,
    )
    clean = re.sub(r"[^a-zA-Z0-9]+", "_", clean).strip("_").lower()
    return clean or "unknown_app"


def is_valid_standalone_apk(file_path: pathlib.Path) -> bool:
    """Rejects .xapk / .apks bundles by verifying AndroidManifest.xml is at the root."""
    if not file_path.exists() or file_path.stat().st_size < 1024 * 100:
        return False
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            names = zf.namelist()
            # XAPK files contain inner .apk files and manifest.json instead of root AndroidManifest.xml
            if "manifest.json" in names and "AndroidManifest.xml" not in names:
                print(f"Skipping {file_path.name}: Detected XAPK bundle inside archive.")
                return False
            return "AndroidManifest.xml" in names and "classes.dex" in names
    except zipfile.BadZipFile:
        return False


def get_existing_releases() -> dict:
    releases = {}
    page = 1
    while True:
        r = requests.get(
            f"https://api.github.com/repos/{REPO}/releases",
            headers=HEADERS,
            params={"per_page": 100, "page": page},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        for rel in data:
            releases[rel["tag_name"]] = rel
        page += 1
    return releases


def dismiss_adblock_modal(page):
    try:
        btn = page.locator("text=I Disabled Blocker").first
        if btn.is_visible(timeout=2500):
            btn.click()
            page.wait_for_timeout(1000)
    except Exception:
        pass


def extract_listing_urls(page, category_url: str) -> list[str]:
    page.goto(category_url, wait_until="domcontentloaded", timeout=60000)
    dismiss_adblock_modal(page)
    page.wait_for_timeout(2000)

    links = page.evaluate(
        """() => {
            const anchors = Array.from(document.querySelectorAll('a[href]'));
            const urls = new Set();
            for (const a of anchors) {
                const href = a.href;
                if (
                    href.startsWith('https://leeapk.com/') &&
                    !href.includes('/apps/') &&
                    !href.includes('/games/') &&
                    !href.includes('/news/') &&
                    !href.includes('/page/') &&
                    !href.includes('/category/') &&
                    !href.includes('/tag/') &&
                    !href.includes('#') &&
                    href !== 'https://leeapk.com/'
                ) {
                    urls.add(href);
                }
            }
            return Array.from(urls);
        }"""
    )
    return links[:MAX_ITEMS_PER_RUN]


def parse_app_detail(page, url: str) -> dict | None:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    dismiss_adblock_modal(page)
    page.wait_for_timeout(1500)

    meta = page.evaluate(
        """() => {
            const h1 = document.querySelector('h1')?.innerText?.trim() || '';
            const bodyText = document.body.innerText;

            let version = '';
            const rows = Array.from(document.querySelectorAll('tr, li, p, div'));
            for (const el of rows) {
                const txt = el.innerText || '';
                if (/^Version\\s*[:\\n]/i.test(txt.trim())) {
                    version = txt.replace(/^Version\\s*[:\\n]?/i, '').trim().split('\\n')[0];
                    break;
                }
            }
            if (!version) {
                const m = (h1 + ' ' + bodyText).match(/\\bv?(\\d+\\.\\d+(?:\\.\\d+)*)\\b/i);
                if (m) version = m[1];
            }

            const iconEl = document.querySelector('article img, .entry-content img, img[class*="icon"], img[alt*="APK"]');
            const iconUrl = iconEl ? iconEl.src : '';

            const dlAnchor = Array.from(document.querySelectorAll('a[href]')).find(a =>
                /download/i.test(a.innerText) && a.href.startsWith('http')
            );
            const downloadPageUrl = dlAnchor ? dlAnchor.href : '';

            return { title: h1, version, iconUrl, downloadPageUrl };
        }"""
    )

    if not meta["title"] or not meta["downloadPageUrl"]:
        return None

    dl_url = meta["downloadPageUrl"]
    # Strictly skip .xapk, .apks, or .obb links
    if re.search(r"\.(xapk|apks|obb|zip|rar)(\?|$)", dl_url, re.I):
        print(f"Skipping {meta['title']}: Download URL is an XAPK/archive.")
        return None

    if re.search(r"\.apk(\?|$)", dl_url, re.I):
        apk_url = dl_url
    else:
        page.goto(dl_url, wait_until="domcontentloaded", timeout=60000)
        dismiss_adblock_modal(page)
        page.wait_for_timeout(5000)

        apk_url = page.evaluate(
            """() => {
                const anchors = Array.from(document.querySelectorAll('a[href]'));
                for (const a of anchors) {
                    const h = a.href || '';
                    const txt = (a.innerText || '').toLowerCase();
                    // Ignore any button or URL mentioning xapk or apks
                    if (txt.includes('xapk') || txt.includes('apks') || /\\.(xapk|apks|zip|obb)(\\?|$)/i.test(h)) {
                        continue;
                    }
                    if (/\\.apk(\\?|$)/i.test(h)) {
                        return h;
                    }
                }
                return null;
            }"""
        )

    if not apk_url:
        print(f"Skipping {meta['title']}: No direct .apk link found (or only .xapk available).")
        return None

    meta["apkUrl"] = apk_url
    return meta


def download_binary(url: str, dest_path: pathlib.Path, referer: str) -> bool:
    try:
        with requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": referer},
            stream=True,
            timeout=180,
        ) as r:
            r.raise_for_status()
            # Check Content-Disposition header to make sure server isn't sending a .xapk file
            content_disp = r.headers.get("Content-Disposition", "").lower()
            if ".xapk" in content_disp or ".apks" in content_disp:
                print(f"Rejected {url}: Server returned XAPK/APKS in Content-Disposition.")
                return False

            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
        return dest_path.exists() and dest_path.stat().st_size > 1024
    except Exception as e:
        print(f"Download failed for {url}: {e}")
        return False


def create_or_update_release(tag: str, release_name: str, body: str, files: list[pathlib.Path], existing: dict):
    if tag in existing:
        rel = existing[tag]
        for asset in rel.get("assets", []):
            if any(asset["name"] == f.name for f in files):
                requests.delete(
                    f"https://api.github.com/repos/{REPO}/releases/assets/{asset['id']}",
                    headers=HEADERS,
                    timeout=30,
                )
    else:
        resp = requests.post(
            f"https://api.github.com/repos/{REPO}/releases",
            headers=HEADERS,
            json={
                "tag_name": tag,
                "name": release_name,
                "body": body,
                "draft": False,
                "prerelease": False,
            },
            timeout=30,
        )
        resp.raise_for_status()
        rel = resp.json()

    upload_url = rel["upload_url"].split("{")[0]
    for file_path in files:
        mime = "application/vnd.android.package-archive" if file_path.suffix == ".apk" else "image/png"
        with open(file_path, "rb") as f:
            up = requests.post(
                upload_url,
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Content-Type": mime,
                },
                params={"name": file_path.name},
                data=f,
                timeout=300,
            )
            up.raise_for_status()
        print(f"Uploaded: {file_path.name} -> Release {tag}")


def main():
    existing_releases = get_existing_releases()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        for section, list_url in CATEGORIES.items():
            print(f"=== Scanning {section.upper()} ({list_url}) ===")
            detail_urls = extract_listing_urls(page, list_url)

            for detail_url in detail_urls:
                try:
                    info = parse_app_detail(page, detail_url)
                    if not info:
                        continue

                    base_slug = slugify(info["title"])
                    # XD STORE Naming Rule:
                    # Apps -> name.apk + name.png
                    # Games -> name_game.apk + name_game.png
                    asset_stem = f"{base_slug}_game" if section == "game" else base_slug
                    version_str = re.sub(r"[^0-9A-Za-z._-]", "", info["version"] or "1.0.0")
                    tag_name = f"{asset_stem}-v{version_str}"

                    if tag_name in existing_releases:
                        print(f"Already uploaded: {tag_name}, skipping.")
                        continue

                    apk_file = DOWNLOAD_DIR / f"{asset_stem}.apk"
                    icon_file = DOWNLOAD_DIR / f"{asset_stem}.png"

                    print(f"Downloading APK: {info['title']} (v{version_str})...")
                    if not download_binary(info["apkUrl"], apk_file, detail_url):
                        continue

                    # Double-check the downloaded file is a real APK and not a renamed .xapk
                    if not is_valid_standalone_apk(apk_file):
                        apk_file.unlink(missing_ok=True)
                        continue

                    upload_list = [apk_file]
                    if info["iconUrl"] and download_binary(info["iconUrl"], icon_file, detail_url):
                        upload_list.append(icon_file)

                    clean_title = re.split(r"\s+mod\s+apk|\s+apk", info["title"], flags=re.I)[0].strip()
                    release_title = f"{clean_title} v{version_str}"
                    release_notes = f"Version: {version_str}\nSection: {section.upper()}"

                    create_or_update_release(
                        tag=tag_name,
                        release_name=release_title,
                        body=release_notes,
                        files=upload_list,
                        existing=existing_releases,
                    )

                    for f in upload_list:
                        f.unlink(missing_ok=True)

                    time.sleep(2)
                except Exception as err:
                    print(f"Error on {detail_url}: {err}")

        browser.close()


if __name__ == "__main__":
    main()
