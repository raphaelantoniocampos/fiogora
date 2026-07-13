import logging
from datetime import date, datetime
from typing import Optional
from time import sleep

from dateutil.relativedelta import relativedelta
from selenium.webdriver.common.by import By

from app.core.settings import settings
from app.infrastructure.automation.web.base_browser import BaseBrowser

logger = logging.getLogger(__name__)


class FiorilliBrowser(BaseBrowser):
    def __init__(
        self,
        fiorilli_password: Optional[str] = None,
        fiorilli_user: Optional[str] = None,
        fiorilli_url: Optional[str] = None,
        log_callback=None,
        headless=None,
        cancel_event=None,
    ):
        url = fiorilli_url if fiorilli_url else getattr(settings, "FIORILLI_URL", "")
        super().__init__(
            url=url,
            fiorilli_password=fiorilli_password,
            fiorilli_user=fiorilli_user,
            log_callback=log_callback,
            headless=headless,
            cancel_event=cancel_event,
        )

    def download_employees(self):
        logger.info("Starting employees download from Fiorilli")
        try:
            self._login()
            self._navigate_to_maintenance_section()
            self._navigate_to_worker_registration()
            self._wait_for_screen_to_load()
            self._select_situation()
            self._input_content()
            self._click_add_button()
            self._click_filter_button()
            self._wait_for_processing()
            self._right_click_grid()
            self._move_to_grid_option()
            self._click_grid_option()
            self._click_export_option()
            self._click_export_txt_option()
            self._wait_for_export_to_complete()
            logger.info("Download of employees from Fiorilli completed")
        finally:
            self.close_driver()

    def download_leaves(self):
        logger.info("Starting leaves download from Fiorilli")
        try:
            self._login()
            self._navigate_to_utilities_section()
            self._navigate_to_import_export_section()
            self._navigate_to_export_section()
            self._navigate_to_export_file_section()
            self._insert_date_for_input(name="PontoFerias2")
            self._insert_date_for_input(name="PontoAfastamentos2")
            self._close_tab()
            logger.info("Download of leaves from Fiorilli completed")
        finally:
            self.close_driver()

    def _login(self) -> None:
        user = self.fiorilli_user
        psw = self.fiorilli_password

        if not user or not psw:
            raise ValueError(
                "Fiorilli credentials not set (FIORILLI_USER, password from frontend)"
            )

        self._enter_username("//input[@placeholder='(Usuário)']", user)
        self._enter_password("//input[@placeholder='(Senha)']", psw)
        self._click_login_button()
        self._check_login_error()
        self._wait_for_login_to_complete()
        sleep(self.DELAY)

    def _enter_username(self, selector: str, user: str) -> None:
        self.send_keys(selector, user, selector_type=By.XPATH)

    def _enter_password(self, selector: str, password: str) -> None:
        self.send_keys(selector, password, selector_type=By.XPATH)

    def _click_login_button(self) -> None:
        self.click_element("//span[text()='Entrar']")

    def _wait_for_login_to_complete(self) -> None:
        self.wait_desappear("//*[contains(text(), 'Acessando SIP 7.5')]")

    def _check_login_error(self) -> None:
        """Check if the login error label is displayed after clicking Entrar."""
        self.wait(0.5)
        try:
            error_element = self.driver.find_element(
                By.XPATH,
                "//label[contains(text(), 'Acesso Negado')]",
            )
            error_msg = "Usuário ou senha Fiorilli inválido"
            if error_element:
                self._log(
                    "ERROR",
                    error_msg,
                )
                raise ValueError(error_msg)
        except ValueError:
            raise
        except Exception:
            pass

    def _navigate_to_maintenance_section(self) -> None:
        self.click_element("//*[contains(text(), '2 - Manutenção')]")

    def _navigate_to_worker_registration(self) -> None:
        self.click_element("//*[contains(text(), '2.1 - Cadastro de Trabalhadores')]")

    def _wait_for_screen_to_load(self) -> None:
        self.wait_desappear("//*[contains(text(), 'Abrindo a tela, aguarde...')]")

    def _select_situation(self) -> None:
        self.click_element("(//div[contains(@class, 'x-boundlist-list-ct')]//li)[1]")

    def _input_content(self) -> None:
        content_input_xpath = "//div[contains(@style, 'border:none;font-family:Segoe UI;left:0px;top:22px')]//div[contains(@data-ref, 'inputWrap')]//input[contains(@data-ref, 'inputEl') and contains(@style, 'font-family:Segoe UI') and contains(@role, 'textbox') and contains(@aria-hidden, 'false') and contains(@aria-disabled, 'false')]"
        self.select_and_send_keys(content_input_xpath, "0\\1\\2\\3\\4\\5\\6")

    def _click_add_button(self) -> None:
        plus_button_xpath = "//div[contains(@style, 'border:none;font-family:Segoe UI;left:0px;top:22px')]//span[contains(@class, 'x-btn-icon-el x-btn-icon-el-default-small fas fa-plus')]"
        self.click_element(plus_button_xpath)

    def _click_filter_button(self) -> None:
        filter_button_xpath = "//div[contains(@style, 'border:none;font-family:Segoe UI;left:0px;top:275px;width:294px;height:41px')]//*[contains(text(), 'Filtrar')]"
        self.click_element(filter_button_xpath)

    def _wait_for_processing(self) -> None:
        self.wait_desappear(
            "//div[contains(@class, 'x-mask-loading')]//div[contains(text(), 'Aguarde')]",
        )

    def _right_click_grid(self) -> None:
        grid_xpath = "//div[contains(@class, 'x-grid-item-container')]//table[contains(@style, ';width:0')]//td[contains(@style , ';font-family:Segoe UI') and not(contains(@class, 'unselectable'))][1]"
        self.right_click_element(grid_xpath)

    def _move_to_grid_option(self) -> None:
        self.move_to_element(
            "//span[contains(text(), 'Grid') and contains(@data-ref, 'textEl')]"
        )

    def _click_grid_option(self) -> None:
        self.click_element(
            "//span[contains(text(), 'Grid') and contains(@data-ref, 'textEl')]"
        )

    def _click_export_option(self) -> None:
        self.click_element(
            "//div[contains(@aria-hidden, 'false')]//div//div//div//div//div//a//span[contains(text(), 'Exportar') and contains(@class, 'x-menu-item-text x-menu-item-text-default x-menu-item-indent-no-separator x-menu-item-indent-right-arrow')]",
        )

    def _click_export_txt_option(self) -> None:
        self.click_element("//span[contains(text(), 'Exportar em TXT')]")

    def _wait_for_export_to_complete(self) -> None:
        self.wait_desappear("//*[contains(text(), 'Exportando')]")

    def _navigate_to_utilities_section(self) -> None:
        self.click_element("//span[contains(text(), '7 - Utilitários')]")

    def _navigate_to_import_export_section(self) -> None:
        for i in range(2):
            self.click_element(
                "//span[contains(text(), '7.14 - Importar/Exportar')]",
            )

    def _navigate_to_export_section(self) -> None:
        for i in range(2):
            self.click_element(
                "//span[contains(text(), '7.14.2 - Exportar')]",
            )

    def _navigate_to_export_file_section(self) -> None:
        self.click_element("//span[contains(text(), '7.14.2.2 - Exportar Arquivo')]")

    def _insert_date_for_input(self, name: str) -> None:
        self._insert_date_fiorilli_input(name=name)

    def _insert_date_fiorilli_input(self, name: str) -> None:
        self._select_input_field(name)
        self._fill_input_field()
        self._click_proceed_button()
        self._click_process_button()

    def _select_input_field(self, name: str) -> None:
        self.click_element(f"//div[contains(text(), '{name}')]")

    def _fill_input_field(self) -> None:
        today = datetime.today()
        today_str = today.strftime("%d/%m/%Y")
        start_date = (
            today - relativedelta(months=settings.LEAVES_MONTHS_AGO)
        ).strftime("%d/%m/%Y")
        year_end = date(today.year, 12, 31).strftime("%d/%m/%Y")
        self.select_and_send_keys(
            f"//input[@value='{today_str}']",
            [
                start_date,
                year_end,
            ],
        )

    def _click_proceed_button(self) -> None:
        self.click_element("//span[contains(text(), 'Prosseguir')]")

    def _click_process_button(self) -> None:
        self.click_element("//span[contains(text(), 'Processar')]")

    def _close_tab(self) -> None:
        self.click_element(
            "//div//div//div[contains(@class, 'x-panel-body x-panel-body-default x-abs-layout-ct x-panel-body-default x-panel-default-outer-border-trbl')]//div//div//div//div//div//div//div//a//span//span//span[contains(@class, 'x-btn-icon-el x-btn-icon-el-default-small x-uni-btn-icon fas fa-sign-out-alt')]",
        )
