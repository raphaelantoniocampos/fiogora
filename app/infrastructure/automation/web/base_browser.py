import logging
import time
import threading
from abc import ABC
from typing import Any, Callable, List, Union, Optional

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    MoveTargetOutOfBoundsException,
    NoSuchElementException,
    StaleElementReferenceException,
)
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from app.core.settings import settings

logger = logging.getLogger(__name__)


class BrowserCancelledException(Exception):
    pass


class BaseBrowser(ABC):
    MAX_TRIES = 30
    DELAY = 1
    IGNORED_EXCEPTIONS = (
        ElementClickInterceptedException,
        ElementNotInteractableException,
        MoveTargetOutOfBoundsException,
        NoSuchElementException,
        StaleElementReferenceException,
    )

    def __init__(
        self,
        url: str,
        fiorilli_password: Optional[str] = None,
        ahgora_password: Optional[str] = None,
        fiorilli_user: Optional[str] = None,
        ahgora_user: Optional[str] = None,
        ahgora_company: Optional[str] = None,
        log_callback: Optional[Callable[[str, str], None]] = None,
        headless: Optional[bool] = None,
        cancel_event: Optional[threading.Event] = None,
    ):
        self.fiorilli_password = fiorilli_password
        self.ahgora_password = ahgora_password
        self.fiorilli_user = (
            fiorilli_user
            if fiorilli_user is not None
            else getattr(settings, "FIORILLI_USER", "")
        )
        self.ahgora_user = (
            ahgora_user
            if ahgora_user is not None
            else getattr(settings, "AHGORA_USER", "")
        )
        self.ahgora_company = (
            ahgora_company
            if ahgora_company is not None
            else getattr(settings, "AHGORA_COMPANY", "")
        )
        self.log_callback = log_callback
        self.headless = headless if headless is not None else settings.HEADLESS_MODE
        self.cancel_event = cancel_event
        self.driver = self._get_web_driver()
        if url:
            self.driver.get(url)

    def _log(self, level: str, message: str):
        if self.log_callback:
            try:
                self.log_callback(level, message)
            except Exception as e:
                logger.error(f"Failed to execute log_callback: {e}")

        # Fallback to standard logger
        if level == "INFO":
            logger.info(message)
        elif level == "ERROR":
            logger.error(message)
        elif level == "WARNING":
            logger.warning(message)
        elif level == "DEBUG":
            logger.debug(message)
        else:
            logger.info(message)

    def _get_web_driver(self) -> webdriver.Firefox:
        import os

        os.environ["MOZ_DISABLE_CONTENT_SANDBOX"] = "1"

        options = webdriver.FirefoxOptions()
        if self.headless:
            options.add_argument("-headless")

        options.set_preference("security.sandbox.content.level", 0)

        # Ensure download directory exists
        settings.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

        options.set_preference("browser.download.folderList", 2)
        options.set_preference("browser.download.dir", str(settings.DOWNLOADS_DIR))

        driver = webdriver.Firefox(options=options)
        driver.implicitly_wait(self.DELAY)
        return driver

    def close_driver(self):
        try:
            self.driver.quit()
        except Exception:
            pass

    def check_cancel(self):
        if self.cancel_event and self.cancel_event.is_set():
            self._log("WARNING", "Browser execution cancelled via kill-switch")
            self.close_driver()
            raise BrowserCancelledException("Task cancelled by user.")

    def wait(self, seconds: float):
        end_time = time.time() + seconds
        while time.time() < end_time:
            self.check_cancel()
            time.sleep(min(0.5, end_time - time.time()))

    def retry_func(self, func: Callable[[], Any], max_tries: int = MAX_TRIES) -> Any:
        error = None
        for i in range(max_tries):
            self.check_cancel()
            try:
                self.wait(self.DELAY)
                self.check_cancel()
                return func()
            except BrowserCancelledException as e:
                raise e
            except Exception as e:
                error = e
                if i >= max_tries - 1:
                    logger.error(f"Failed after {max_tries} attempts: {e}")
                    raise e
                self.wait(self.DELAY)
        if error:
            raise error

    def click_element(
        self,
        selector: str,
        selector_type=By.XPATH,
        delay=DELAY,
        ignored_exceptions=IGNORED_EXCEPTIONS,
        max_tries=MAX_TRIES,
    ):
        self.retry_func(
            lambda: self._click_element_helper(
                selector, selector_type, delay, ignored_exceptions
            ),
            max_tries,
        )

    def _click_element_helper(
        self,
        selector,
        selector_type,
        delay,
        ignored_exceptions,
    ):
        WebDriverWait(self.driver, delay, ignored_exceptions=ignored_exceptions).until(
            EC.presence_of_element_located((selector_type, selector))
        ).click()

    def send_keys(
        self,
        selector: str,
        keys: str,
        selector_type=By.XPATH,
        delay=DELAY,
        ignored_exceptions=IGNORED_EXCEPTIONS,
        max_tries=MAX_TRIES,
        clear_first=False,
        typing_delay=0,
    ):
        self.retry_func(
            lambda: self._send_keys_helper(
                selector,
                keys,
                selector_type,
                delay,
                ignored_exceptions,
                clear_first,
                typing_delay,
            ),
            max_tries,
        )

    def _send_keys_helper(
        self,
        selector,
        keys,
        selector_type,
        delay,
        ignored_exceptions,
        clear_first=False,
        typing_delay=0,
    ):
        element = WebDriverWait(
            self.driver, delay, ignored_exceptions=ignored_exceptions
        ).until(EC.presence_of_element_located((selector_type, selector)))
        if clear_first:
            self.driver.execute_script(
                "arguments[0].value = '';", element
            )

        if typing_delay:
            for char in keys:
                element.send_keys(char)
                time.sleep(typing_delay)
        else:
            # Use JS to set value directly — send_keys drops spaces in some inputs
            self.driver.execute_script(
                """
                var el = arguments[0];
                el.value = arguments[1];
                el.dispatchEvent(new Event('input', { bubbles: true }));
                """,
                element,
                keys,
            )

    def right_click_element(
        self,
        selector: str,
        selector_type=By.XPATH,
        delay=DELAY,
        ignored_exceptions=IGNORED_EXCEPTIONS,
        max_tries=MAX_TRIES,
    ):
        self.retry_func(
            lambda: self._right_click_element_helper(
                selector, selector_type, delay, ignored_exceptions
            ),
            max_tries,
        )

    def _right_click_element_helper(
        self,
        selector,
        selector_type,
        delay,
        ignored_exceptions,
    ):
        ActionChains(self.driver).context_click(
            WebDriverWait(
                self.driver, delay, ignored_exceptions=ignored_exceptions
            ).until(EC.presence_of_element_located((selector_type, selector)))
        ).perform()

    def select_and_send_keys(
        self,
        selector: str,
        keys: Union[str, List[str]],
        selector_type=By.XPATH,
        delay=DELAY,
        ignored_exceptions=IGNORED_EXCEPTIONS,
        max_tries=MAX_TRIES,
    ):
        if isinstance(keys, list):
            for i, key in enumerate(keys):
                element_selector = f"({selector})[{i + 1}]"
                self.retry_func(
                    lambda: self._select_and_send_keys_helper(
                        element_selector,
                        key,
                        selector_type,
                        delay,
                        ignored_exceptions,
                    ),
                    max_tries,
                )
        else:
            self.retry_func(
                lambda: self._select_and_send_keys_helper(
                    selector,
                    keys,
                    selector_type,
                    delay,
                    ignored_exceptions,
                ),
                max_tries,
            )

    def _select_and_send_keys_helper(
        self,
        selector,
        keys,
        selector_type,
        delay,
        ignored_exceptions,
    ):
        element = WebDriverWait(
            self.driver, delay, ignored_exceptions=ignored_exceptions
        ).until(EC.presence_of_element_located((selector_type, selector)))

        ActionChains(self.driver).click(element).key_down(Keys.CONTROL).send_keys(
            "a"
        ).key_up(Keys.CONTROL).send_keys(keys).perform()

    def select_dropdown_option(
        self,
        selector: str,
        value: str,
        selector_type=By.XPATH,
        delay=DELAY,
        ignored_exceptions=IGNORED_EXCEPTIONS,
        max_tries=MAX_TRIES,
    ):
        self.retry_func(
            lambda: self._select_dropdown_option_helper(
                selector, value, selector_type, delay, ignored_exceptions
            ),
            max_tries,
        )

    def _select_dropdown_option_helper(
        self, selector, value, selector_type, delay, ignored_exceptions
    ):
        from selenium.webdriver.support.ui import Select

        element = WebDriverWait(
            self.driver, delay, ignored_exceptions=ignored_exceptions
        ).until(EC.presence_of_element_located((selector_type, selector)))
        Select(element).select_by_value(value)

    def wait_desappear(
        self,
        selector: str,
        selector_type=By.XPATH,
        delay=10,
        ignored_exceptions=IGNORED_EXCEPTIONS,
        max_tries=50,
    ):
        return self.retry_func(
            lambda: self._wait_desappear_helper(
                selector, selector_type, (delay * 1.1), ignored_exceptions
            ),
            max_tries,
        )

    def _wait_desappear_helper(
        self,
        selector,
        selector_type,
        delay,
        ignored_exceptions,
    ):
        WebDriverWait(self.driver, delay, ignored_exceptions=ignored_exceptions).until(
            EC.invisibility_of_element_located((selector_type, selector))
        )

    def move_to_element(
        self,
        selector: str,
        selector_type=By.XPATH,
        delay=DELAY,
        ignored_exceptions=IGNORED_EXCEPTIONS,
        max_tries=MAX_TRIES,
    ):
        self.retry_func(
            lambda: self._move_to_element_helper(
                selector, selector_type, delay, ignored_exceptions
            ),
            max_tries,
        )

    def _move_to_element_helper(
        self,
        selector,
        selector_type,
        delay,
        ignored_exceptions,
    ):
        ActionChains(self.driver).move_to_element(
            WebDriverWait(
                self.driver, delay, ignored_exceptions=ignored_exceptions
            ).until(EC.presence_of_element_located((selector_type, selector)))
        ).perform()

    def send_enter_key(
        self,
        selector: str,
        selector_type=By.XPATH,
        delay=DELAY,
        ignored_exceptions=IGNORED_EXCEPTIONS,
        max_tries=MAX_TRIES,
    ):
        self.retry_func(
            lambda: self._send_enter_key_helper(
                selector, selector_type, delay, ignored_exceptions
            ),
            max_tries,
        )

    def _send_enter_key_helper(
        self,
        selector,
        selector_type,
        delay,
        ignored_exceptions,
    ):
        element = WebDriverWait(
            self.driver, delay, ignored_exceptions=ignored_exceptions
        ).until(EC.presence_of_element_located((selector_type, selector)))
        element.send_keys(Keys.ENTER)
