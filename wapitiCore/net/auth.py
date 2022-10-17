import sys
from typing import Tuple, List, Dict
import asyncio
from urllib.parse import urlparse
import importlib.util

from httpx import RequestError
from arsenic import get_session, browsers, services
from arsenic.errors import ArsenicError

from wapitiCore.net import Request, Response
from wapitiCore.parsers.html import Html
from wapitiCore.main.log import logging
from wapitiCore.net.crawler_configuration import CrawlerConfiguration
from wapitiCore.net.crawler import AsyncCrawler
from wapitiCore.net.cookies import headless_cookies_to_cookiejar


async def check_http_auth(crawler_configuration: CrawlerConfiguration) -> bool:
    async with AsyncCrawler.with_configuration(crawler_configuration) as crawler:
        response = await crawler.async_get(crawler_configuration.base_request)

        if response.status in (401, 403, 404):
            return False
        return True


def _create_login_request(
        login_form: Request, username, username_field_idx, password, password_field_idx
) -> Tuple[Request, Dict]:
    form = {}
    post_params = login_form.post_params
    get_params = login_form.get_params

    if login_form.method == "POST":
        post_params[username_field_idx][1] = username
        post_params[password_field_idx][1] = password
        form["login_field"] = post_params[username_field_idx][0]
        form["password_field"] = post_params[password_field_idx][0]
    else:
        get_params[username_field_idx][1] = username
        get_params[password_field_idx][1] = password
        form["login_field"] = get_params[username_field_idx][0]
        form["password_field"] = get_params[password_field_idx][0]

    login_request = Request(
        path=login_form.url,
        method=login_form.method,
        post_params=post_params,
        get_params=get_params,
        referer=login_form.referer,
        link_depth=login_form.link_depth
    )

    return login_request, form


async def async_try_form_login(
        crawler_configuration: CrawlerConfiguration,
        headless_mode: str = "no",
) -> Tuple[bool, dict, List[str]]:
    """
    Try to authenticate with the provided url and credentials.
    Returns if the authentication has been successful, the used form variables and the disconnect urls.
    """
    # Step 1: Fetch the login page and try to extract the login form, keep cookies too
    if headless_mode != "no":
        proxy_settings = None
        if crawler_configuration.proxy:
            proxy = urlparse(crawler_configuration.proxy).netloc
            proxy_settings = {
                "proxyType": 'manual',
                "httpProxy": proxy,
                "sslProxy": proxy
            }

        service = services.Geckodriver()
        browser = browsers.Firefox(
            proxy=proxy_settings,
            acceptInsecureCerts=True,
            **{
                "moz:firefoxOptions": {
                    "prefs": {
                        "network.proxy.allow_hijacking_localhost": True,
                        "devtools.jsonview.enabled": False,
                        # "security.cert_pinning.enforcement_level": 0,
                    },
                    "args": ["-headless"] if headless_mode == "hidden" else []
                }
            }
        )
        try:
            async with get_session(service, browser) as headless_client:
                await headless_client.get(
                    crawler_configuration.form_credential.url,
                    timeout=crawler_configuration.timeout
                )
                await asyncio.sleep(.1)
                page_source = await headless_client.get_page_source()
                crawler_configuration.cookies = headless_cookies_to_cookiejar(await headless_client.get_all_cookies())
        except (ArsenicError, asyncio.TimeoutError) as exception:
            logging.error(f"[!] {exception.__class__.__name__} with URL {crawler_configuration.form_credential.url}")
            return False, {}, []
    else:
        async with AsyncCrawler.with_configuration(crawler_configuration) as crawler:
            try:
                response: Response = await crawler.async_get(
                    Request(crawler_configuration.form_credential.url),
                    follow_redirects=True
                )
                crawler_configuration.cookies = crawler.cookie_jar
                page_source = response.content
            except ConnectionError:
                logging.error("[!] Connection error with URL", crawler_configuration.form_credential.url)
                return False, {}, []
            except RequestError as exception:
                logging.error(
                    f"[!] {exception.__class__.__name__} with URL {crawler_configuration.form_credential.url}"
                )
                return False, {}, []

    disconnect_urls = []
    page = Html(page_source, crawler_configuration.form_credential.url)

    login_form, username_field_idx, password_field_idx = page.find_login_form()
    if login_form:
        # Step 2: submit the login form, keep new cookies
        login_request, form = _create_login_request(
            login_form,
            crawler_configuration.form_credential.username,
            username_field_idx,
            crawler_configuration.form_credential.password,
            password_field_idx,
        )

        async with AsyncCrawler.with_configuration(crawler_configuration) as crawler:
            login_response = await crawler.async_send(
                login_request,
                follow_redirects=True
            )

            html = Html(login_response.content, login_response.url)

            # ensure logged in
            is_logged_in = html.is_logged_in()
            if is_logged_in:
                logging.success("Login success")
                disconnect_urls = html.extract_disconnect_urls()
            else:
                logging.warning("Login failed : Credentials might be invalid")

            # In every case keep the cookies
            crawler_configuration.cookies = crawler.cookie_jar
            return is_logged_in, form, disconnect_urls

    logging.warning("Login failed : No login form detected")
    return False, {}, []


async def load_form_script(
        filepath: str,
        crawler_configuration: CrawlerConfiguration,
        headless: str = "no"
):
    """Load the Python script at filepath and call the run function in it with several parameters"""
    spec = importlib.util.spec_from_file_location("plugin", filepath)

    try:
        module = importlib.util.module_from_spec(spec)
    except (FileNotFoundError, AttributeError):
        logging.error(f"Unable to load auth script '{filepath}'. Check path, access rights or syntax.")
        sys.exit(1)

    spec.loader.exec_module(module)
    # We expect the auth script to set cookies on the crawler_configuration object but everything can be done here
    try:
        await module.run(crawler_configuration, crawler_configuration.form_credential.url, headless)
    except AttributeError:
        logging.error("run() method seems to be missing in your auth script")
        sys.exit(1)
    except TypeError as exception:
        raise RuntimeError("The provided auth script seems to have some syntax issues") from exception
