"""Nylo AI: local Flask UI backed by a signed-in Chrome browser.

Run: python3 app.py
Open: http://127.0.0.1:5000

The Flask UI starts immediately. Selenium initialization runs separately,
so a slow/failed Chrome launch will not prevent the local website from loading.
"""

from __future__ import annotations

import atexit
import os
import platform
import shutil
import tempfile
import threading
import time
from pathlib import Path

import bleach
from flask import Flask, jsonify, render_template, request
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB per request

# Settings
HOST = "127.0.0.1"
PORT = 5000
WEBSITE_URL = "https://chatgpt.com/"
HEADLESS = False  # Keep Chrome visible so you can log in.
PROFILE_DIR = BASE_DIR / "selenium_profile"
UPLOAD_ROOT = Path(tempfile.gettempdir()) / "nylo_uploads"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

ELEMENT_TIMEOUT = 25
ATTACHMENT_TIMEOUT = 50
RESPONSE_TIMEOUT = 180
MAX_RESPONSE_IMAGES = 12

# ChatGPT's website changes occasionally. Update these selectors if necessary.
TEXTBOX_SELECTORS = [
    "#prompt-textarea[contenteditable='true']",
    "div[contenteditable='true'][data-testid='composer-input']",
    "div.ProseMirror[contenteditable='true']",
    "textarea#mobile-composer-prompt",
    "textarea[name='prompt']",
    "textarea[placeholder*='Ask']",
    "textarea[placeholder*='Message']",
]
SEND_BUTTON_SELECTORS = [
    "button[data-testid='send-button']",
    "button[data-testid='composer-send-button']",
    "button[data-composer-submit]",
    "button[aria-label='Send prompt']",
    "button[aria-label='Send message']",
    "button[type='submit']",
]
FILE_INPUT_SELECTORS = [
    "#octane-mobile-composer-files-input",
    "#octane-mobile-composer-photos-input",
    "input[type='file'][multiple]",
    "input[type='file']",
]
ASSISTANT_SELECTORS = [
    "[data-message-author-role='assistant']",
    "li[data-message-role='assistant']",
]
RESPONSE_TEXT_SELECTORS = [
    "[data-assistant-markdown]",
    ".markdown",
    "[data-message-content]",
]
STOP_BUTTON_SELECTORS = [
    "button[data-testid='stop-button']",
    "button[aria-label='Stop streaming']",
    "button[aria-label='Stop generating']",
]

browser = None
browser_lock = threading.RLock()
browser_state = "starting"  # starting / ready / error
browser_error = None


def log(message: str) -> None:
    print(f"[Nylo] {message}", flush=True)


def build_browser():
    log("Starting Chrome...")
    options = Options()
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument("--disable-notifications")
    if HEADLESS:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1440,900")

    instance = webdriver.Chrome(options=options)
    instance.set_page_load_timeout(60)
    try:
        instance.get(WEBSITE_URL)
    except TimeoutException:
        log("ChatGPT took too long to load; keeping Chrome open.")
    log("Chrome launched. Sign into ChatGPT there if needed.")
    return instance


def get_browser():
    """Call while holding browser_lock."""
    global browser, browser_state, browser_error
    if browser is not None:
        try:
            _ = browser.current_url
            return browser
        except WebDriverException:
            try:
                browser.quit()
            except Exception:
                pass
            browser = None

    browser_state = "starting"
    browser_error = None
    try:
        browser = build_browser()
        browser_state = "ready"
        return browser
    except Exception as exc:
        browser_state = "error"
        browser_error = str(exc)
        log(f"Chrome startup failed: {exc}")
        raise


def launch_browser_in_background():
    def work():
        with browser_lock:
            try:
                get_browser()
            except Exception:
                # Flask keeps serving the website; /api/status shows the issue.
                pass

    threading.Thread(target=work, daemon=True, name="nylo-chrome").start()


def first_matching(instance, selectors, timeout=ELEMENT_TIMEOUT, visible=True):
    def find(_):
        for selector in selectors:
            try:
                elements = instance.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    if not visible or element.is_displayed():
                        return element
            except WebDriverException:
                continue
        return False

    return WebDriverWait(instance, timeout).until(find)


def assistant_messages(instance):
    for selector in ASSISTANT_SELECTORS:
        try:
            messages = instance.find_elements(By.CSS_SELECTOR, selector)
            if messages:
                return messages
        except WebDriverException:
            pass
    return []


def compose_message(instance, textbox, message):
    if not message:
        return
    textbox.click()
    tag = (textbox.tag_name or "").lower()
    if tag in ("textarea", "input"):
        textbox.clear()
    else:
        modifier = Keys.COMMAND if platform.system() == "Darwin" else Keys.CONTROL
        textbox.send_keys(modifier, "a")
        textbox.send_keys(Keys.BACKSPACE)

    textbox.send_keys(message)
    value = textbox.get_attribute("value") if tag in ("textarea", "input") else textbox.text
    if not value or message.strip() not in value.strip():
        raise RuntimeError("Couldn't enter text in ChatGPT's composer. The page layout may have changed.")


def save_files(files):
    if not files:
        return None, []
    folder = Path(tempfile.mkdtemp(prefix="request_", dir=UPLOAD_ROOT))
    paths = []
    for index, uploaded in enumerate(files):
        if not uploaded or not uploaded.filename:
            continue
        filename = secure_filename(uploaded.filename) or f"attachment_{index}"
        path = folder / f"{index}_{filename}"
        uploaded.save(path)
        paths.append(str(path.resolve()))
    return folder, paths


def attach_files(instance, paths):
    if not paths:
        return
    control = first_matching(instance, FILE_INPUT_SELECTORS, visible=False)
    # A 'multiple' input accepts newline-separated files; single input takes one.
    if len(paths) > 1 and control.get_attribute("multiple") is None:
        raise RuntimeError("The upload control accepts only one file at a time.")
    control.send_keys("\n".join(paths))
    log(f"Attached {len(paths)} file(s).")
    time.sleep(1)


def click_send(instance, textbox):
    def find_enabled(_):
        for selector in SEND_BUTTON_SELECTORS:
            try:
                for button in instance.find_elements(By.CSS_SELECTOR, selector):
                    if (
                        button.is_displayed()
                        and button.is_enabled()
                        and button.get_attribute("aria-disabled") != "true"
                    ):
                        return button
            except WebDriverException:
                continue
        return False

    try:
        button = WebDriverWait(instance, ATTACHMENT_TIMEOUT).until(find_enabled)
        button.click()
    except TimeoutException:
        if textbox is None:
            raise RuntimeError("Couldn't find ChatGPT's Send button.")
        textbox.send_keys(Keys.ENTER)


def wait_for_reply(instance, old_count):
    try:
        reply = WebDriverWait(instance, RESPONSE_TIMEOUT).until(
            lambda d: (msgs[-1] if len(msgs := assistant_messages(d)) > old_count else False)
        )
    except TimeoutException as exc:
        raise RuntimeError("The message was sent, but no new assistant reply appeared.") from exc

    # Wait for text to settle and the 'Stop' button to disappear.
    previous = None
    stable_since = time.monotonic()
    deadline = time.monotonic() + RESPONSE_TIMEOUT
    while time.monotonic() < deadline:
        try:
            latest = assistant_messages(instance)[-1]
            text_now = latest.text.strip()
            if text_now != previous:
                previous = text_now
                stable_since = time.monotonic()
            generating = False
            for selector in STOP_BUTTON_SELECTORS:
                if any(b.is_displayed() for b in instance.find_elements(By.CSS_SELECTOR, selector)):
                    generating = True
                    break
            if text_now and not generating and time.monotonic() - stable_since >= 2.0:
                return latest
        except (WebDriverException, IndexError):
            pass
        time.sleep(0.5)
    raise RuntimeError("Timed out while waiting for the assistant's response to finish.")


def response_body_for(reply):
    for selector in RESPONSE_TEXT_SELECTORS:
        try:
            matches = reply.find_elements(By.CSS_SELECTOR, selector)
            if matches:
                return matches[-1]
        except WebDriverException:
            pass
    return reply


def export_html(instance, root):
    """Build small semantic HTML in the browser; sanitize again in Python."""
    raw = instance.execute_script(
        r"""
        const root = arguments[0];
        const escapeHTML = text => String(text)
          .replace(/&/g, '&amp;').replace(/</g, '&lt;')
          .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
          .replace(/'/g, '&#39;');
        const safeURL = href => {
          try {
            const url = new URL(href, location.href);
            return ['https:', 'http:', 'mailto:'].includes(url.protocol)
              ? escapeHTML(url.href) : '';
          } catch { return ''; }
        };
        function walk(node) {
          if (node.nodeType === Node.TEXT_NODE) return escapeHTML(node.nodeValue || '');
          if (node.nodeType !== Node.ELEMENT_NODE) return '';
          if (node.hidden || node.getAttribute('aria-hidden') === 'true') return '';
          const styles = getComputedStyle(node);
          if (styles.display === 'none' || styles.visibility === 'hidden') return '';
          const tag = node.tagName.toLowerCase();
          if (['script','style','svg','path','canvas','button','noscript','img'].includes(tag)) return '';
          const children = () => Array.from(node.childNodes).map(walk).join('');
          if (tag === 'br' || tag === 'hr') return `<${tag}>`;
          if (tag === 'pre') {
            return `<pre><code>${escapeHTML(node.textContent || '')}</code></pre>`;
          }
          if (tag === 'a') {
            const href = safeURL(node.getAttribute('href'));
            return href ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${children()}</a>` : children();
          }
          const supported = new Set(['h1','h2','h3','h4','h5','h6','p','strong','b',
            'em','i','del','s','code','blockquote','ul','ol','li','table','thead',
            'tbody','tfoot','tr','th','td','div','span']);
          return supported.has(tag) ? `<${tag}>${children()}</${tag}>` : children();
        }
        return Array.from(root.childNodes).map(walk).join('').trim();
        """,
        root,
    ) or ""
    tags = [
        "h1", "h2", "h3", "h4", "h5", "h6", "p", "strong", "b", "em", "i",
        "del", "s", "code", "pre", "blockquote", "ul", "ol", "li", "table",
        "thead", "tbody", "tfoot", "tr", "th", "td", "div", "span", "br", "hr", "a",
    ]
    return bleach.clean(
        raw, tags=tags,
        attributes={"a": ["href", "target", "rel"]},
        protocols=["http", "https", "mailto"], strip=True,
    )


def extract_images(reply):
    output, seen = [], set()
    for element in reply.find_elements(By.TAG_NAME, "img"):
        if len(output) >= MAX_RESPONSE_IMAGES:
            break
        try:
            if not element.is_displayed():
                continue
            size = element.size
            if size.get("width", 0) < 24 or size.get("height", 0) < 24:
                continue
            alt = (element.get_attribute("alt") or "Image").strip()
            source = element.get_attribute("src") or ""
            key = source or (alt, size.get("width"), size.get("height"))
            if key in seen:
                continue
            seen.add(key)
            try:
                capture = element.screenshot_as_base64
                if capture:
                    output.append({"src": f"data:image/png;base64,{capture}", "alt": alt})
                    continue
            except WebDriverException:
                pass
            if source.startswith(("https://", "http://", "data:image/")):
                output.append({"src": source, "alt": alt})
        except WebDriverException:
            continue
    return output


def send_to_chatgpt(message, paths):
    instance = get_browser()
    textbox = first_matching(instance, TEXTBOX_SELECTORS)
    old_count = len(assistant_messages(instance))
    attach_files(instance, paths)
    compose_message(instance, textbox, message)
    click_send(instance, textbox)
    log("Waiting for ChatGPT's reply...")
    reply = wait_for_reply(instance, old_count)
    body = response_body_for(reply)
    text = (body.text or "").strip()
    rich_html = export_html(instance, body)
    images = extract_images(reply)
    if not (text or rich_html or images):
        raise RuntimeError("ChatGPT replied, but the content could not be read.")
    return {"response": text, "html": rich_html, "images": images}


def stop_browser():
    global browser
    if browser is not None:
        try:
            browser.quit()
        except Exception:
            pass
        browser = None


atexit.register(stop_browser)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    return jsonify({
        "ok": browser_state == "ready",
        "chrome_running": browser_state == "ready" and browser is not None,
        "state": browser_state,
        "error": browser_error,
        "headless": HEADLESS,
        "url": WEBSITE_URL,
    })


@app.route("/api/chat", methods=["POST"])
def chat():
    if request.is_json:
        data = request.get_json(silent=True) or {}
        message = str(data.get("message", "")).strip()
        uploads = []
    else:
        message = str(request.form.get("message", "")).strip()
        uploads = [x for x in request.files.getlist("files") if x.filename]

    if not message and not uploads:
        return jsonify({"error": "Type a message or attach a file first."}), 400

    folder = None
    try:
        folder, paths = save_files(uploads)
        with browser_lock:
            return jsonify(send_to_chatgpt(message, paths))
    except Exception as exc:
        log(f"Chat error: {exc}")
        return jsonify({"error": str(exc)}), 500
    finally:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)


@app.route("/api/new-chat", methods=["POST"])
def new_chat():
    try:
        with browser_lock:
            instance = get_browser()
            instance.get(WEBSITE_URL)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/open-browser", methods=["POST"])
def open_browser():
    try:
        with browser_lock:
            get_browser()
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.errorhandler(RequestEntityTooLarge)
def file_too_large(_):
    return jsonify({"error": "Files are too large. Maximum total request size: 20 MB."}), 413


if __name__ == "__main__":
    print(f"\nNYLO AI - Local website: http://{HOST}:{PORT}\n", flush=True)
    launch_browser_in_background()
    app.run(host=HOST, port=PORT, debug=False, threaded=True, use_reloader=False)
