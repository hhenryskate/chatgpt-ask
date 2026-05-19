import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from patchright.sync_api import TimeoutError as PlaywrightTimeoutError
from patchright.sync_api import sync_playwright


CHATGPT_URL = "https://chatgpt.com/"
CHATGPT_COOKIE_URL = "https://chatgpt.com"
EMAILNATOR_URL = "https://www.emailnator.com/10minutemail"
EMAILNATOR_ORIGIN = "https://www.emailnator.com"


class ChatGPTDriverError(RuntimeError):
    pass


class NeedLoginError(ChatGPTDriverError):
    pass


class GuestSessionError(NeedLoginError):
    pass


class ChatGPTDriver:
    def __init__(
        self,
        profile_dir=None,
        cookies_path=None,
        headless=None,
        timeout_ms=120000,
    ):
        root = Path(__file__).resolve().parents[1]
        self.profile_dir = Path(
            profile_dir
            or os.environ.get("CHATGPT_PROFILE_DIR")
            or root / ".driver-chrome-profile"
        )
        self.cookies_path = Path(
            cookies_path
            or os.environ.get("CHATGPT_COOKIES_FILE")
            or root / ".chatgpt-cookies.json"
        )
        default_emailnator_profile = root.parent / "perplexity-ask" / ".driver-chrome-profile"
        if not default_emailnator_profile.exists():
            default_emailnator_profile = root / ".emailnator-chrome-profile"
        self.emailnator_profile_dir = Path(
            os.environ.get("CHATGPT_EMAILNATOR_PROFILE_DIR") or default_emailnator_profile
        )
        if headless is None:
            headless = os.environ.get("CHATGPT_HEADLESS", "").lower() in ("1", "true", "yes")
        self.headless = headless
        self.timeout_ms = int(os.environ.get("CHATGPT_TIMEOUT_MS", timeout_ms))

    def _launch(self, playwright):
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = []
        if not self.headless:
            args.extend(["--window-position=-32000,-32000", "--window-size=1200,900"])

        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            channel="chrome",
            headless=self.headless,
            no_viewport=True,
            args=args,
        )

    def _launch_profile(self, playwright, profile_dir):
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        args = []
        if not self.headless:
            args.extend(["--window-position=-32000,-32000", "--window-size=1200,900"])

        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=self.headless,
            no_viewport=True,
            args=args,
        )

    def _load_cookies(self, context):
        if not self.cookies_path.exists():
            return
        try:
            cookies = json.loads(self.cookies_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ChatGPTDriverError(f"Invalid cookie file: {self.cookies_path}") from exc
        if isinstance(cookies, list) and cookies:
            context.add_cookies(cookies)

    def _save_cookies(self, context):
        cookies = context.cookies([CHATGPT_COOKIE_URL])
        self.cookies_path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
        print(f"[chatgpt-driver] saved cookies: {self.cookies_path}", flush=True)

    def _has_authenticated_session_cookie(self, context):
        cookies = context.cookies([CHATGPT_COOKIE_URL])
        cookie_names = {cookie["name"] for cookie in cookies}
        return any(
            name.startswith("__Secure-next-auth.session-token")
            or name == "__Secure-next-auth.session-token"
            for name in cookie_names
        )

    def _has_visible_login_ui(self, page):
        selectors = [
            "a[href*='auth/login']",
            "button:has-text('Log in')",
            "button:has-text('Sign up')",
            "a:has-text('Log in')",
            "a:has-text('Sign up')",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() and locator.first.is_visible(timeout=1000):
                    return True
            except Exception:
                continue
        return False

    def _open_chat(self, page):
        print(f"[chatgpt-driver] opening {CHATGPT_URL}", flush=True)
        page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=self.timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            pass

    def _composer(self, page):
        selectors = [
            "#prompt-textarea",
            "[data-testid='prompt-textarea']",
            "div[contenteditable='true'][data-lexical-editor='true']",
            "div[contenteditable='true']",
            "textarea",
        ]
        for selector in selectors:
            locator = page.locator(selector)
            try:
                if locator.count() and locator.first.is_visible(timeout=1500):
                    return locator.first
            except Exception:
                continue
        return None

    def _send_button(self, page):
        selectors = [
            "[data-testid='send-button']",
            "button[aria-label='Send prompt']",
            "button[aria-label='Send message']",
            "button[aria-label*='Send']",
        ]
        for selector in selectors:
            locator = page.locator(selector)
            try:
                if locator.count() and locator.first.is_visible(timeout=1000):
                    return locator.first
            except Exception:
                continue
        return None

    def _click_visible_button_text(self, page, texts):
        return page.evaluate(
            """(texts) => {
                const normalized = texts.map((x) => x.toLowerCase());
                const nodes = [...document.querySelectorAll("button, a, [role='button']")];
                for (const node of nodes) {
                    const rect = node.getBoundingClientRect();
                    const text = (node.innerText || node.textContent || "").trim().toLowerCase();
                    if (!text || rect.width === 0 || rect.height === 0) continue;
                    if (normalized.some((target) => text.includes(target))) {
                        node.click();
                        return text;
                    }
                }
                return null;
            }""",
            texts,
        )

    def _click_visible_submit(self, page):
        return page.evaluate(
            """() => {
                const nodes = [...document.querySelectorAll("button[type='submit'], button")];
                for (const node of nodes) {
                    const rect = node.getBoundingClientRect();
                    const text = (node.innerText || node.textContent || "").trim().toLowerCase();
                    if (rect.width === 0 || rect.height === 0) continue;
                    if (
                        text === "continuar"
                        || text === "continue"
                        || text === "termina de crear tu cuenta"
                        || text === "finish creating your account"
                    ) {
                        node.click();
                        return text;
                    }
                }
                return null;
            }"""
        )

    def _assistant_messages(self, page):
        selectors = [
            "[data-message-author-role='assistant']",
            "article:has([data-message-author-role='assistant'])",
            "main article",
        ]
        for selector in selectors:
            locator = page.locator(selector)
            try:
                if locator.count():
                    return locator
            except Exception:
                continue
        return page.locator("[data-message-author-role='assistant']")

    def _main_text(self, page):
        try:
            return page.locator("main").inner_text(timeout=5000).strip()
        except Exception:
            return page.locator("body").inner_text(timeout=5000).strip()

    def _extract_new_main_text(self, before_text, current_text, prompt):
        current_text = current_text.strip()
        before_text = before_text.strip()

        if before_text and current_text.startswith(before_text):
            candidate = current_text[len(before_text):].strip()
        else:
            candidate = current_text

        if prompt in candidate:
            candidate = candidate.split(prompt, 1)[-1].strip()
        elif prompt in current_text:
            candidate = current_text.split(prompt, 1)[-1].strip()

        if "Detener respuesta" in candidate or "Stop generating" in candidate:
            return ""

        tail_markers = [
            "\nInvestiga a fondo",
            "\nAplicaciones",
            "\nSitios",
            "\nMejorar el plan",
            "\nChatGPT puede cometer errores.",
            "\nChatGPT can make mistakes.",
        ]
        for marker in tail_markers:
            index = candidate.find(marker)
            if index > 0:
                candidate = candidate[:index]

        if candidate.strip() in ("Investiga a fondo", "Busca", "Piensa"):
            return ""

        return candidate.strip()

    def _visible_limit_message(self, page):
        try:
            body = page.locator("body").inner_text(timeout=3000)
        except Exception:
            return ""

        patterns = [
            r"(Alcanzaste el límite[^\n]*)",
            r"(You've reached the[^\n]*)",
            r"(You’ve reached the[^\n]*)",
        ]
        for pattern in patterns:
            match = re.search(pattern, body, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return ""

    def _latest_chat_result(self, page, prompt):
        href = page.evaluate(
            """() => {
                const links = [...document.querySelectorAll("a[href*='/c/']")];
                return links.length ? links[0].href : "";
            }"""
        )
        if not href:
            return ""

        page.goto(href, wait_until="domcontentloaded", timeout=self.timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(3000)

        limit_message = self._visible_limit_message(page)
        if limit_message:
            return limit_message

        return self._extract_new_main_text("", self._main_text(page), prompt)

    def _dismiss_onboarding(self, page):
        clicked = self._click_visible_button_text(page, ["omitir", "skip"])
        if clicked:
            print(f"[chatgpt-driver] dismissed onboarding: {clicked}", flush=True)
            page.wait_for_timeout(3000)

    def _select_chat_tool(self, page, labels):
        if isinstance(labels, str):
            labels = [labels]

        plus = page.locator("[data-testid='composer-plus-btn']")
        if not plus.count():
            raise ChatGPTDriverError("ChatGPT tool menu button was not found.")

        plus.click(timeout=10000)
        page.wait_for_timeout(1000)
        clicked = page.evaluate(
            """(labels) => {
                const normalized = labels.map((label) => label.toLowerCase());
                const nodes = [...document.querySelectorAll("*")];
                for (const node of nodes) {
                    const text = (node.innerText || node.textContent || "").trim();
                    if (!text) continue;
                    const textLower = text.toLowerCase();
                    if (!normalized.some((label) => textLower === label)) continue;

                    const target = node.closest(
                        "[role='menuitemradio'], [role='menuitem'], button, a"
                    ) || node;
                    const rect = target.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;

                    target.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
                    target.click();
                    return {
                        text: (target.innerText || target.textContent || "").trim(),
                        role: target.getAttribute("role"),
                    };
                }
                return null;
            }""",
            labels,
        )
        if not clicked:
            body = page.locator("body").inner_text(timeout=10000)
            raise ChatGPTDriverError(
                f"ChatGPT tool option was not found: {labels}. Body: {body[:1000]}"
            )

        print(f"[chatgpt-driver] selected tool: {clicked['text']}", flush=True)
        page.wait_for_timeout(2500)

    def _select_mode(self, page, mode):
        if not mode or mode == "auto":
            return
        mode_labels = {
            "search": ["Busca en la web", "Search the web"],
            "reason": ["Razonamiento", "Think"],
            "deep_research": ["Investigar a fondo", "Deep research"],
        }
        if mode not in mode_labels:
            raise ChatGPTDriverError(f"Unsupported ChatGPT mode: {mode}")
        self._select_chat_tool(page, mode_labels[mode])

    def _assert_logged_in(self, page):
        if not self._has_authenticated_session_cookie(page.context):
            if self._has_visible_login_ui(page):
                raise GuestSessionError(
                    "ChatGPT is open in guest/anonymous mode. Run chatgpt-driver --login "
                    "and log in with a real ChatGPT account before using the MCP."
                )
            raise GuestSessionError(
                "No authenticated ChatGPT session cookie was found. Run chatgpt-driver --login "
                "and log in with a real ChatGPT account before using the MCP."
            )

        self._dismiss_onboarding(page)
        composer = self._composer(page)
        if composer is not None:
            return

        body = page.locator("body").inner_text(timeout=5000)
        if "Log in" in body or "Sign up" in body or "login" in page.url.lower():
            raise NeedLoginError(
                "ChatGPT is not logged in for this profile. Run chatgpt-driver --login, "
                "complete login in the opened browser, then run it again to save cookies."
            )
        raise ChatGPTDriverError("ChatGPT composer was not found. The web UI may have changed.")

    def login(self, wait_seconds=180):
        with sync_playwright() as playwright:
            context = self._launch(playwright)
            try:
                self._load_cookies(context)
                page = context.new_page()
                self._open_chat(page)
                print(
                    "[chatgpt-driver] if login is required, complete it in the browser window",
                    flush=True,
                )
                deadline = time.time() + wait_seconds
                while time.time() < deadline:
                    if self._has_authenticated_session_cookie(context):
                        self._save_cookies(context)
                        print("[chatgpt-driver] login/session ready", flush=True)
                        return
                    page.wait_for_timeout(1000)
                self._save_cookies(context)
                raise NeedLoginError("Login was not completed before timeout.")
            finally:
                context.close()

    def _emailnator_token_script(self):
        return (
            "const m=document.cookie.match(/XSRF-TOKEN=([^;]+)/);"
            "const token=m?decodeURIComponent(m[1]):'';"
        )

    def _emailnator_generate_email(self, page):
        print("[chatgpt-driver] generating Emailnator googlemail address", flush=True)
        result = page.evaluate(
            """async () => {
                const m = document.cookie.match(/XSRF-TOKEN=([^;]+)/);
                const token = m ? decodeURIComponent(m[1]) : "";
                const response = await fetch("/generate-email", {
                    method: "POST",
                    headers: {
                        "content-type": "application/json",
                        "x-requested-with": "XMLHttpRequest",
                        "x-xsrf-token": token,
                    },
                    body: JSON.stringify({ email: ["googleMail"] }),
                });
                return { status: response.status, text: await response.text() };
            }"""
        )
        if result["status"] != 200:
            raise ChatGPTDriverError(
                f"Emailnator generate-email failed: HTTP {result['status']} "
                f"{result['text'][:200]}"
            )
        data = json.loads(result["text"])
        email = data["email"][0]
        print(f"[chatgpt-driver] generated email: {email}", flush=True)
        self._emailnator_message_list(page, email)
        return email

    def _emailnator_message_list(self, page, email):
        return page.evaluate(
            """async (email) => {
                const m = document.cookie.match(/XSRF-TOKEN=([^;]+)/);
                const token = m ? decodeURIComponent(m[1]) : "";
                const response = await fetch("/message-list", {
                    method: "POST",
                    headers: {
                        "content-type": "application/json",
                        "x-requested-with": "XMLHttpRequest",
                        "x-xsrf-token": token,
                    },
                    body: JSON.stringify({ email }),
                });
                return await response.json();
            }""",
            email,
        )

    def _emailnator_open_message(self, page, email, message_id):
        return page.evaluate(
            """async ({ email, messageID }) => {
                const m = document.cookie.match(/XSRF-TOKEN=([^;]+)/);
                const token = m ? decodeURIComponent(m[1]) : "";
                const response = await fetch("/message-list", {
                    method: "POST",
                    headers: {
                        "content-type": "application/json",
                        "x-requested-with": "XMLHttpRequest",
                        "x-xsrf-token": token,
                    },
                    body: JSON.stringify({ email, messageID }),
                });
                return await response.text();
            }""",
            {"email": email, "messageID": message_id},
        )

    def _wait_for_chatgpt_code(self, page, email, timeout=120):
        print("[chatgpt-driver] waiting for ChatGPT verification email", flush=True)
        deadline = time.time() + timeout
        seen = {"ADSVPN"}
        while time.time() < deadline:
            messages = self._emailnator_message_list(page, email).get("messageData", [])
            for message in messages:
                message_id = message.get("messageID")
                if not message_id or message_id in seen:
                    continue
                seen.add(message_id)
                subject = message.get("subject", "")
                sender = message.get("from", "")
                if "ChatGPT" not in subject and "OpenAI" not in subject and "openai" not in sender:
                    continue
                body = self._emailnator_open_message(page, email, message_id)
                visible_code = re.search(
                    r"<!\[endif\]-->\s*(\d{6})\s*<!--\[if mso\]>",
                    body,
                    re.IGNORECASE,
                )
                codes = [visible_code.group(1)] if visible_code else re.findall(
                    r"(?<!\d)\d{6}(?!\d)", body
                )
                if codes:
                    code = codes[0]
                    print("[chatgpt-driver] verification code captured", flush=True)
                    return code
            page.wait_for_timeout(5000)
        raise ChatGPTDriverError("Timed out waiting for ChatGPT verification email.")

    def _submit_email_for_login(self, page, email):
        self._open_chat(page)
        email_input = page.locator("input[type='email'], input[name='email']")
        if not email_input.count():
            buttons = page.locator("button")
            for i in range(buttons.count()):
                button = buttons.nth(i)
                try:
                    text = button.inner_text(timeout=1000).strip().lower()
                except Exception:
                    continue
                if text in (
                    "iniciar sesión",
                    "log in",
                    "sign in",
                    "suscríbete gratis",
                    "sign up",
                    "registrarse",
                ):
                    print(f"[chatgpt-driver] opening auth form via button: {text}", flush=True)
                    button.click(timeout=5000, force=True)
                    page.wait_for_timeout(3000)
                    if email_input.count():
                        break

        if not email_input.count():
            body = page.locator("body").inner_text(timeout=10000)
            raise ChatGPTDriverError(f"ChatGPT email input was not found. Body: {body[:800]}")

        filled = page.evaluate(
            """(email) => {
                const inputs = [...document.querySelectorAll("input[type='email'], input[name='email']")];
                for (const input of inputs.reverse()) {
                    const rect = input.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    input.focus();
                    input.value = email;
                    input.dispatchEvent(new Event("input", { bubbles: true }));
                    input.dispatchEvent(new Event("change", { bubbles: true }));
                    return true;
                }
                return false;
            }""",
            email,
        )
        if not filled:
            body = page.locator("body").inner_text(timeout=10000)
            raise ChatGPTDriverError(f"Visible ChatGPT email input was not found. Body: {body[:800]}")
        clicked = self._click_visible_submit(page)
        if not clicked:
            page.locator("button[type='submit']").last.click(timeout=10000)
        page.wait_for_timeout(5000)

    def _submit_verification_code(self, page, code):
        code_input = page.locator("input[name='code'], input[placeholder*='Código'], input[placeholder*='Code']").first
        code_input.fill(code, timeout=15000)
        clicked = self._click_visible_submit(page)
        if not clicked:
            page.locator("button[name='intent'][value='validate']").click(timeout=10000)
        page.wait_for_timeout(10000)
        print(f"[chatgpt-driver] after verification URL: {page.url}", flush=True)

    def _complete_optional_profile_steps(self, page):
        body = page.locator("body").inner_text(timeout=10000)
        if "name" in body.lower() or "nombre" in body.lower():
            for selector, value in [
                ("input[name='name']", "MCP User"),
                ("input[name='fullName']", "MCP User"),
                ("input[placeholder*='Nombre']", "MCP User"),
                ("input[placeholder*='Name']", "MCP User"),
                ("input[name='age']", "30"),
                ("input[placeholder*='Edad']", "30"),
                ("input[placeholder*='Age']", "30"),
            ]:
                locator = page.locator(selector)
                if locator.count():
                    locator.first.fill(value, timeout=5000)
            self._click_visible_submit(page)
            page.wait_for_timeout(8000)

        body = page.locator("body").inner_text(timeout=10000)
        if "birth" in body.lower() or "nacimiento" in body.lower():
            fields = [
                ("input[name='birthday_month'], input[name='month']", "1"),
                ("input[name='birthday_day'], input[name='day']", "1"),
                ("input[name='birthday_year'], input[name='year']", "1990"),
            ]
            for selector, value in fields:
                locator = page.locator(selector)
                if locator.count():
                    locator.first.fill(value, timeout=5000)
            self._click_visible_submit(page)
            page.wait_for_timeout(8000)

    def create_account_once(self):
        with sync_playwright() as playwright:
            email_context = self._launch_profile(playwright, self.emailnator_profile_dir)
            chat_context = self._launch(playwright)
            try:
                self._load_cookies(chat_context)
                chat_page = chat_context.new_page()
                self._open_chat(chat_page)
                if self._has_authenticated_session_cookie(chat_context):
                    self._save_cookies(chat_context)
                    print("[chatgpt-driver] authenticated session already exists", flush=True)
                    return self.status()

                email_page = email_context.new_page()
                print(f"[chatgpt-driver] opening Emailnator: {EMAILNATOR_URL}", flush=True)
                email_page.goto(EMAILNATOR_URL, wait_until="domcontentloaded", timeout=self.timeout_ms)
                email_page.wait_for_timeout(5000)
                email = self._emailnator_generate_email(email_page)

                self._submit_email_for_login(chat_page, email)
                if "email-verification" not in chat_page.url:
                    body = chat_page.locator("body").inner_text(timeout=10000)
                    raise ChatGPTDriverError(
                        "ChatGPT did not show the email verification screen. "
                        f"Current URL: {chat_page.url}. Body: {body[:500]}"
                    )

                code = self._wait_for_chatgpt_code(email_page, email)
                self._submit_verification_code(chat_page, code)
                self._complete_optional_profile_steps(chat_page)

                try:
                    chat_page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    chat_page.wait_for_timeout(5000)
                except PlaywrightTimeoutError:
                    pass

                self._save_cookies(chat_context)
                authenticated = self._has_authenticated_session_cookie(chat_context)
                status = {
                    "email": email,
                    "authenticated": authenticated,
                    "composer": self._composer(chat_page) is not None,
                    "login_ui": self._has_visible_login_ui(chat_page),
                    "mode": "authenticated" if authenticated else "guest_or_logged_out",
                }
                if not status["authenticated"]:
                    raise NeedLoginError(
                        "Email verification completed, but ChatGPT did not create an authenticated "
                        "session cookie. A manual profile step may still be required."
                    )
                return status
            finally:
                chat_context.close()
                email_context.close()

    def status(self):
        with sync_playwright() as playwright:
            context = self._launch(playwright)
            try:
                self._load_cookies(context)
                page = context.new_page()
                self._open_chat(page)
                self._dismiss_onboarding(page)
                authenticated = self._has_authenticated_session_cookie(context)
                composer = self._composer(page) is not None
                login_ui = self._has_visible_login_ui(page)
                self._save_cookies(context)
                return {
                    "authenticated": authenticated,
                    "composer": composer,
                    "login_ui": login_ui,
                    "mode": "authenticated" if authenticated else "guest_or_logged_out",
                }
            finally:
                context.close()

    def ask(self, prompt, mode="auto"):
        with sync_playwright() as playwright:
            context = self._launch(playwright)
            try:
                self._load_cookies(context)
                page = context.new_page()
                self._open_chat(page)
                self._assert_logged_in(page)
                self._select_mode(page, mode)

                messages = self._assistant_messages(page)
                before_count = messages.count()
                before_main_text = self._main_text(page)
                composer = self._composer(page)
                if composer is None:
                    raise ChatGPTDriverError("ChatGPT composer disappeared before sending.")

                print("[chatgpt-driver] sending prompt", flush=True)
                composer.click(timeout=10000)
                composer.fill(prompt, timeout=10000)

                button = self._send_button(page)
                if button is not None and button.is_enabled(timeout=5000):
                    button.click(timeout=10000)
                else:
                    composer.press("Enter", timeout=10000)

                try:
                    page.wait_for_url("**/c/**", timeout=15000)
                except PlaywrightTimeoutError:
                    pass

                deadline = time.time() + (self.timeout_ms / 1000)
                last_text = ""
                stable_since = None
                while time.time() < deadline:
                    limit_message = self._visible_limit_message(page)
                    if limit_message:
                        self._save_cookies(context)
                        return limit_message

                    messages = self._assistant_messages(page)
                    count = messages.count()
                    text = ""
                    if count > before_count:
                        text = messages.nth(count - 1).inner_text(timeout=5000).strip()
                    else:
                        text = self._extract_new_main_text(
                            before_main_text,
                            self._main_text(page),
                            prompt,
                        )

                    if text and text == last_text:
                        stable_since = stable_since or time.time()
                        if time.time() - stable_since >= 2:
                            self._save_cookies(context)
                            return text
                    elif text:
                        last_text = text
                        stable_since = None
                    page.wait_for_timeout(1000)

                if last_text:
                    self._save_cookies(context)
                    return last_text
                latest_text = self._latest_chat_result(page, prompt)
                if latest_text:
                    self._save_cookies(context)
                    return latest_text
                raise ChatGPTDriverError("Timed out waiting for a ChatGPT response.")
            finally:
                context.close()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true", help="Open ChatGPT and save session cookies.")
    parser.add_argument("--create-account", action="store_true", help="Create one persistent ChatGPT account.")
    parser.add_argument("--ask", help="Send one prompt through the web session.")
    parser.add_argument(
        "--mode",
        choices=["auto", "search", "reason", "deep_research"],
        default="auto",
        help="ChatGPT composer mode to activate before sending.",
    )
    parser.add_argument("--status", action="store_true", help="Print detected session status.")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode.")
    parser.add_argument("--wait-seconds", type=int, default=180)
    args = parser.parse_args()

    driver = ChatGPTDriver(headless=args.headless)
    if args.login:
        driver.login(wait_seconds=args.wait_seconds)
        return
    if args.create_account:
        print(json.dumps(driver.create_account_once(), indent=2))
        return
    if args.status:
        print(json.dumps(driver.status(), indent=2))
        return
    if args.ask:
        print(driver.ask(args.ask, mode=args.mode))
        return
    parser.print_help()


if __name__ == "__main__":
    main()
