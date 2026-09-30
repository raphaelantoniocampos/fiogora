import logging
import time
from typing import Callable, Optional
from urllib.parse import urlparse

from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from app.core.settings import settings
from app.infrastructure.automation.web.base_browser import BaseBrowser

logger = logging.getLogger(__name__)


class AhgoraBrowser(BaseBrowser):
    # Login page (login.ahgora.com.br, PO UI): email -> "Próximo" -> password -> "Entrar"
    # -> company combo -> "Continuar" -> redirect to app.ahgora.com.br
    LOGIN_HOST = "login.ahgora.com.br"
    LOGIN_TIMEOUT = 60
    # The OAuth redirect chain ends loading app.ahgora.com.br, which can take over a minute
    REDIRECT_TIMEOUT = 180
    BANNER_TIMEOUT = 15
    EMAIL_INPUT = "input[name='email']"
    PASSWORD_INPUT = "input[name='password']"
    COMPANY_INPUT = "input[name='company']"
    COMPANY_OPTION = "div.po-item-list__option"

    def __init__(
        self,
        ahgora_password: Optional[str] = None,
        ahgora_user: Optional[str] = None,
        ahgora_company: Optional[str] = None,
        ahgora_url: Optional[str] = None,
        log_callback: Optional[Callable[[str, str], None]] = None,
        headless: Optional[bool] = None,
        cancel_event=None,
    ):
        url = ahgora_url if ahgora_url else getattr(settings, "AHGORA_URL", "")
        super().__init__(
            url=url,
            ahgora_password=ahgora_password,
            ahgora_user=ahgora_user,
            ahgora_company=ahgora_company,
            log_callback=log_callback,
            headless=headless,
            cancel_event=cancel_event,
        )
        try:
            self._login()
        except NoSuchElementException:
            # The login page is a multi-step SPA, so restart it from the first step
            self.driver.get(url)
            self._login()

    def download_employees(self):
        self._log("INFO", "Starting employees download from Ahgora")
        try:
            self.driver.get(self.driver.current_url.replace("home", "funcionarios"))
            self._click_plus_button()
            self._export_to_csv()
            self._log("INFO", "Download of employees from Ahgora completed")
        finally:
            self.close_driver()

    def _login(self) -> None:
        user = self.ahgora_user
        psw = self.ahgora_password or ""
        company = self.ahgora_company

        if not all([user, psw, company]):
            raise ValueError(
                "Ahgora credentials not set (AHGORA_USER, password from frontend, AHGORA_COMPANY)"
            )

        self._enter_username(user)
        self._click_button("Próximo")
        self._enter_password(psw)
        self._click_button("Entrar")
        self._check_login_error()
        self._select_company(company)
        self._click_button("Continuar")
        self._wait_redirect_to_app()
        self._close_banner()
        self.wait(self.DELAY)

    def _enter_username(self, user: str) -> None:
        self.send_keys(
            self.EMAIL_INPUT,
            user,
            selector_type=By.CSS_SELECTOR,
            clear_first=True,
            typing_delay=0.01,
        )

    def _enter_password(self, password: str) -> None:
        self.send_keys(
            self.PASSWORD_INPUT,
            password,
            selector_type=By.CSS_SELECTOR,
            clear_first=True,
            typing_delay=0.01,
        )

    @staticmethod
    def _button_xpath(label: str) -> str:
        # PO UI buttons render as <button><div><span class="po-button-label">label</span></div></button>
        # and carry the `disabled` attribute while loading, so wait until it is enabled.
        return f"//button[not(@disabled)][.//span[normalize-space()='{label}']]"

    def _click_button(self, label: str) -> None:
        self.click_element(self._button_xpath(label))

    def _is_displayed(self, xpath: str) -> bool:
        try:
            return any(
                el.is_displayed() for el in self.driver.find_elements(By.XPATH, xpath)
            )
        except StaleElementReferenceException:
            return False

    def _select_company(self, company: str) -> None:
        """Search the company in the PO UI combo (server-side filter) and pick the matching option."""
        self.send_keys(
            self.COMPANY_INPUT,
            company,
            selector_type=By.CSS_SELECTOR,
            clear_first=True,
            typing_delay=0.01,
        )
        self.retry_func(lambda: self._click_company_option(company), max_tries=15)

    def _click_company_option(self, company: str) -> None:
        # Option labels are "<code> - <name>"; the combo may split them with highlight tags
        target = company.strip().upper()
        for option in self.driver.find_elements(By.CSS_SELECTOR, self.COMPANY_OPTION):
            if target in option.text.upper():
                option.click()
                return
        raise ValueError(f"Empresa '{company}' não encontrada no Ahgora")

    def _wait_redirect_to_app(self) -> None:
        """
        Wait until the login page redirects to the Ahgora app, handling the connected devices limit.
        The redirect goes companies -> email -> email?client_id=pontoweb (OAuth) -> app.ahgora.com.br/home,
        and the current url only changes once the (slow) app page starts rendering.
        """
        deadline = time.time() + self.REDIRECT_TIMEOUT
        while time.time() < deadline:
            url = urlparse(self.driver.current_url)
            if url.netloc != self.LOGIN_HOST and not url.path.startswith("/login"):
                return
            if self._is_displayed(self._button_xpath("Continuar login")):
                self._end_previous_rpa_session()
                self._click_button("Continuar login")
            self.wait(0.5)
        raise TimeoutError(
            f"Ahgora login did not redirect to the app (current url: {self.driver.current_url})"
        )

    def _end_previous_rpa_session(self) -> None:
        """
        Every run opens a new session from a fresh Firefox profile, so after a few runs Ahgora
        blocks the login with the connected devices limit. End one session left by a previous
        RPA run (Firefox on Linux); never end sessions that look like a person's browser.
        """
        rows = [
            row
            for row in self.driver.find_elements(By.CSS_SELECTOR, "po-modal tbody tr")
            if "FIREFOX" in row.text.upper() and "LINUX" in row.text.upper()
        ]
        if not rows:
            error_msg = (
                "Limite de dispositivos conectados no Ahgora atingido. "
                "Encerre uma sessão manualmente e tente novamente."
            )
            self._log("ERROR", error_msg)
            raise ValueError(error_msg)

        self._log(
            "WARNING",
            "Limite de dispositivos conectados no Ahgora atingido, encerrando sessão anterior do RPA",
        )
        rows[0].find_element(By.CSS_SELECTOR, "po-table-icon po-icon").click()
        # Wait for the device to be removed and the table to refresh
        self.wait(self.DELAY * 2)

    def _close_banner(self) -> None:
        """
        Dismiss the "Ajuste de ponto" modal (static backdrop) that /home may open after an AJAX
        check for pending punch adjustments, only on some days of the month. Its "Entendi" button
        is always in the DOM (hidden and disabled), so only act when the modal is actually shown.
        """
        if not self._wait_adjust_punch_modal():
            return

        self._log("INFO", "Closing Ahgora 'Ajuste de ponto' modal")
        try:
            # "Entendi" is only enabled after a delay configured by the company
            self.retry_func(
                lambda: WebDriverWait(self.driver, self.DELAY)
                .until(EC.element_to_be_clickable((By.ID, "buttonAdjustPunch")))
                .click(),
                max_tries=20,
            )
        except Exception as e:
            # Every flow leaves /home through driver.get, which discards the modal anyway
            self._log("WARNING", f"Could not close Ahgora 'Ajuste de ponto' modal: {e}")

    def _wait_adjust_punch_modal(self) -> bool:
        """Return True once the modal is shown, or False once the page is loaded with no pending AJAX."""
        idle_script = (
            "return document.readyState === 'complete'"
            " && !!window.jQuery && jQuery.active === 0"
        )
        deadline = time.time() + self.BANNER_TIMEOUT
        idle_checks = 0
        while time.time() < deadline:
            if self._is_displayed("//*[@id='modalAdjustPunchAlert']"):
                return True
            try:
                idle = self.driver.execute_script(idle_script)
            except Exception:
                idle = False
            # Require a few idle checks in a row: the check only starts shortly after page load
            idle_checks = idle_checks + 1 if idle else 0
            if idle_checks >= 3:
                return False
            self.wait(0.5)
        return False

    def _check_login_error(self) -> None:
        """Wait for the password step to resolve: either the company step loads or the error is shown."""
        deadline = time.time() + self.LOGIN_TIMEOUT
        while time.time() < deadline:
            if self.driver.find_elements(By.CSS_SELECTOR, self.COMPANY_INPUT):
                return
            if self._is_displayed(
                "//*[contains(text(), 'Dados incorretos, tente novamente.')]"
            ):
                error_msg = "Usuário ou senha Ahgora inválido"
                self._log("ERROR", error_msg)
                raise ValueError(error_msg)
            self.wait(0.5)
        raise TimeoutError(
            f"Ahgora login did not reach the company step (current url: {self.driver.current_url})"
        )

    def _click_plus_button(self) -> None:
        self.click_element("mais", selector_type=By.ID)

    def _export_to_csv(self) -> None:
        self.click_element("exportar", selector_type=By.ID)
        self.select_dropdown_option("formatExport", "csv_todos", selector_type=By.ID)
        self.click_element("sendFormat", selector_type=By.ID)
        # Give some time for the download to start/finish
        self.wait(10)

    def add_employee(self, payload: dict) -> None:
        """
        Navigates to the employee page and adds a new employee.
        :param payload: Dictionary containing employee details (from Fiorilli)
        """
        name = payload.get("name", "")
        self._log("INFO", f"Adding employee to Ahgora: {name}")

        # Ensure we are on the employee page
        self.driver.get(self.driver.current_url.replace("home", "funcionarios"))
        self.wait(self.DELAY)

        # Click the 'Novo Funcionário' button
        self.click_element("//button[contains(text(), 'Novo Funcionário')]")
        self.wait(self.DELAY * 2)

        # Fill General Data
        self.send_keys("dados-nome", name, By.ID)

        pis = str(payload.get("pis_pasep", ""))
        if pis == "0" or not pis:
            pis = "00000000000"
        self.send_keys("dados-pis", pis, By.ID, typing_delay=0.1)

        self.wait(self.DELAY)

        cpf = str(payload.get("cpf", ""))
        if cpf:
            self.send_keys("dados-cpf", cpf, By.ID, clear_first=True, typing_delay=0.1)

        birth_date = str(payload.get("birth_date", ""))
        if birth_date:
            self.send_keys("dados-dt_nascimento", birth_date, By.ID)

        # Sex
        sex = str(payload.get("sex", ""))
        if sex:
            self.send_keys("dados-sexo", sex, By.ID)

        # RegimeTrab
        self.send_keys("dados-regimetrab", "Estatutário", By.ID)

        # Company Relation
        employee_id = str(payload.get("id", ""))
        self.send_keys("dados.matricula", employee_id, By.ID)

        admission_date = str(payload.get("admission_date", ""))
        if admission_date:
            self.send_keys("dados-dt_admissao", admission_date, By.ID)

        # Password
        self.send_keys("dados-cod_cracha", "12345", By.ID)

        position = str(payload.get("position", ""))
        if position:
            self.send_keys("dados.cargo", position, By.ID)

        department = str(payload.get("department", ""))
        if department:
            self._set_autocomplete_select("dados-departamento", department)
            try:
                self._update_location_multiselect(department)
            except Exception as e:
                self._log(
                    "WARNING",
                    f"Could not update location multiselect automatically: {e}",
                )

        # Click Save
        self.click_element("(//button[contains(text(), 'Salvar')])[last()]")

        # Small wait for the request to process
        self.wait(self.DELAY * 8)
        self._log("INFO", f"Finished adding employee: {name} ({employee_id})")

    def update_employee(self, payload: dict) -> None:
        """
        Updates an existing employee.
        """
        name = payload.get("name_expected", "")
        employee_id = str(payload.get("id", ""))
        self._log("INFO", f"Updating employee in Ahgora: {name}")

        # Navigate to employee page
        self.driver.get(
            self.driver.current_url.replace(
                "home", f"funcionarios/edita/?matric={employee_id}"
            )
        )
        self.wait(self.DELAY)

        try:
            # Note: payload columns are suffixed with _fiorilli and _ahgora
            # Only update if the normalized value has changed

            has_changes = False
            change_logs = []

            if payload.get("name_expected_norm") != payload.get("name_actual_norm"):
                if payload.get("name_expected"):
                    self.send_keys(
                        "dados-nome", payload["name_expected"], By.ID, clear_first=True
                    )
                    has_changes = True
                    change_logs.append(
                        f"Updated name: {payload.get('name_actual')} -> {payload.get('name_expected')}"
                    )

            if payload.get("position_expected_norm") != payload.get(
                "position_actual_norm"
            ):
                if payload.get("position_expected"):
                    self._set_autocomplete_select(
                        "dados.cargo", payload["position_expected"]
                    )
                    has_changes = True
                    change_logs.append(
                        f"Updated position: {payload.get('position_actual')} -> {payload.get('position_expected')}"
                    )

            if payload.get("admission_date_expected_norm") != payload.get(
                "admission_date_actual_norm"
            ):
                if payload.get("admission_date_expected"):
                    self.send_keys(
                        "dados-dt_admissao",
                        payload["admission_date_expected"],
                        By.ID,
                        clear_first=True,
                    )
                    has_changes = True
                    change_logs.append(
                        f"Updated admission_date: {payload.get('admission_date_actual')} -> {payload.get('admission_date_expected')}"
                    )

            if payload.get("department_expected_norm") != payload.get(
                "department_actual_norm"
            ):
                if payload.get("department_expected"):
                    department_value = settings.EXCEPTIONS_AND_TYPOS.get(
                        payload["department_expected"], payload["department_expected"]
                    )
                    self._set_autocomplete_select(
                        "dados-departamento", department_value
                    )
                    has_changes = True
                    change_logs.append(
                        f"Updated department: {payload.get('department_actual')} -> {department_value}"
                    )

            if payload.get("department_expected") and settings.UPDATE_LOCATIONS:
                try:
                    loc_changed = self._update_location_multiselect(
                        payload["department_expected"]
                    )
                    if loc_changed:
                        has_changes = True
                        change_logs.append(
                            f"Updated location mapping based on department: {payload.get('department_expected')}"
                        )
                except Exception as e:
                    self._log(
                        "WARNING",
                        f"Could not update location multiselect automatically: {e}",
                    )

            if has_changes:
                # Click Save
                self.click_element("(//button[contains(text(), 'Salvar')])[last()]")
                self.wait(self.DELAY * 8)
                for change in change_logs:
                    self._log("INFO", change)
                self._log("INFO", f"Finished updating employee: {name} ({employee_id})")
            else:
                self._log(
                    "INFO",
                    f"No specific fields were changed for {name} ({employee_id}), skipping save.",
                )
        except Exception as e:
            self._log(
                "ERROR", f"Failed to find or edit employee {name} ({employee_id}): {e}"
            )
            raise e

    def remove_employee(self, payload: dict) -> None:
        """
        Marks an employee as dismissed.
        """
        name = payload.get("name", "")
        employee_id = str(payload.get("id", ""))
        dismissal_date = str(payload.get("dismissal_date", ""))
        department = str(payload.get("department", ""))
        position = str(payload.get("position", ""))

        self._log(
            "INFO", f"Removing employee in Ahgora: {name} - {position} - {department}"
        )

        self.driver.get(self.driver.current_url.replace("home", "funcionarios"))
        self.wait(self.DELAY)

        # Search for the employee
        self.send_keys("filtro_funcionarios", employee_id, By.ID)
        self.wait(self.DELAY)
        self.send_enter_key("filtro_funcionarios", By.ID)
        self.wait(self.DELAY)

        try:
            # Click to Delete/Dismiss
            self.click_element(
                f"//a[contains(@title, 'Demitir funcionario {name.upper()}') or contains(@class, 'icone_remover')]"
            )
            self.wait(self.DELAY)

            # Form field for dismissal date
            try:
                self.send_keys("dt_demissao", dismissal_date, By.ID, clear_first=True)
                self.wait(self.DELAY)
                self.click_element(
                    "//*[@id='funcionarios']/tbody/tr[1]/td/div/div[2]/div/button[2]"
                )
                self.wait(self.DELAY * 2)
            except Exception as e:
                self._log(
                    "INFO",
                    "No specific dismissal date field found, assumed standard removal",
                )
                raise e

            self._log(
                "INFO",
                f"Finished removing employee: {name} ({employee_id}) - {dismissal_date}",
            )
        except Exception as e:
            self._log("ERROR", f"Failed to remove employee {name} ({employee_id}): {e}")
            raise e

    def upload_leaves_file(self, file_path: str) -> None:
        """
        Uploads a CSV/TXT file of leaves to Ahgora for validation (step 1).
        Does NOT save the records.
        """
        self._log("INFO", "Starting leave upload to Ahgora")

        import_path = settings.AHGORA_URL.replace("home", "afastamentos/importa")
        if import_path == settings.AHGORA_URL:  # Defense if URL structure was weird
            import_path = "https://app.ahgora.com.br/afastamentos/importa"

        self.driver.get(import_path)
        self.wait(self.DELAY * 2)

        try:
            # Find the file input element and send the file path
            file_input = self.driver.find_element(By.XPATH, "//input[@type='file']")
            file_input.send_keys(file_path)
            self.wait(self.DELAY)

            # Ensure the specific layout is selected (pw_afimport_01)
            try:
                self.click_element("pw_afimport_01", By.ID)
            except Exception:
                self._log("DEBUG", "Could not find layout selector, assuming default.")

            # Click the upload/process button
            # Button labeled 'Obter Registros'
            self.click_element("//*/form/div[6]/button[2]")

            self.wait(self.DELAY * 5)  # Let the upload process
            self._log("INFO", f"Finished uploading leaves file from {file_path}")
        except Exception as e:
            self._log("ERROR", f"Failed to upload leaves file: {e}")
            raise e

    def extract_import_errors(self) -> list[dict]:
        """
        Extracts errors from the Ahgora import validation screen.
        Expects errors in the format: '[10] Intersecção com afastamento existente'.
        Returns a list of dicts: [{'row': 10, 'error': 'Intersecção...'}]
        """
        import re

        errors = []
        try:
            # The validation screen displays a log of processing, usually inside the DOM.
            # A robust way is to pull all body text and search line by line.
            self.click_element(
                selector="obterErro", selector_type=By.ID, delay=1, max_tries=240
            )
            body_text = self.driver.find_element(By.ID, "obterErro").text

            # Match logs like "Intersecção com afastamento... [15]"
            lines = body_text.split("\n")
            regex = r"(.+?)\s*\[(\d+)\]$"

            for line in lines:
                line = line.strip()
                match = re.search(regex, line)
                if match:
                    error_msg = match.group(1).strip()
                    row_idx = int(match.group(2))
                    errors.append({"row": row_idx, "error": error_msg})

            self._log("INFO", f"Extracted {len(errors)} validation errors.")
            return errors
        except Exception as e:
            self._log(
                "WARNING", f"Failed to extract import errors (could be 0 errors): {e}"
            )
            return []

    def confirm_import(self) -> None:
        """
        Clicks the save/confirm button to finalize the import of valid records.
        """
        try:
            self.click_element(selector="sendLeave", selector_type=By.ID)
            self.wait(self.DELAY * 20)
            self._log("INFO", "Successfully confirmed and saved leaves import.")
        except Exception as e:
            self._log("ERROR", f"Failed to confirm leaves import: {e}")
            raise e

    def _set_autocomplete_select(self, element_id: str, value: str) -> None:
        """
        Handles <select> elements that are transformed into ui-autocomplete inputs.
        Directly setting the value via Javascript to bypass clear() errors on uneditable inputs.
        """
        try:
            # Locate the select element to check if it has the option
            script = f"""
                var select = document.getElementById('{element_id}');
                if (select) {{
                    // Try to find exact or partial match in options
                    var options = select.options;
                    var matchFound = false;
                    for (var i = 0; i < options.length; i++) {{
                        if (options[i].text.trim().toUpperCase() === '{value.upper()}') {{
                            select.selectedIndex = i;
                            matchFound = true;
                            break;
                        }}
                    }}
                    if (!matchFound) {{
                        for (var i = 0; i < options.length; i++) {{
                            if (options[i].text.trim().toUpperCase().includes('{value.upper()}')) {{
                                select.selectedIndex = i;
                                break;
                            }}
                        }}
                    }}
                    // Trigger change event for jQuery/React listeners
                    var event = new Event('change', {{ bubbles: true }});
                    select.dispatchEvent(event);
                    
                    // Also attempt to update the visible sibling input if it's a combobox
                    var siblingInput = select.nextElementSibling;
                    if (siblingInput && siblingInput.tagName.toLowerCase() === 'input') {{
                        siblingInput.value = '{value}';
                        siblingInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }}
                }}
            """
            self.driver.execute_script(script)
            self.wait(1)
        except Exception as e:
            self._log(
                "WARNING", f"Failed to set autocomplete select '{element_id}': {e}"
            )
            # Fallback to standard send keys without clear
            self.send_keys(element_id, value, By.ID, clear_first=False)

    def _update_location_multiselect(self, department_name: str) -> bool:
        """
        Interacts with the Bootstrap multiselect to update the 'Localização' field.
        Uses the department_to_location.csv mapping if it exists.
        Returns True if any checkbox was changed, False otherwise.
        """
        import csv

        from app.core.settings import settings

        csv_path = settings.DATA_DIR / "mappings" / "department_to_location.csv"

        target_locations = []
        if csv_path.exists():
            try:
                with open(csv_path, mode="r", encoding="latin1") as f:
                    reader = csv.reader(f)
                    for row in reader:
                        if (
                            len(row) >= 2
                            and row[0].strip().upper()
                            == department_name.strip().upper()
                        ):
                            val = row[1].strip()
                            if val.startswith("[") and val.endswith("]"):
                                import ast

                                try:
                                    target_locations = [
                                        x.strip().upper() for x in ast.literal_eval(val)
                                    ]
                                except Exception:
                                    target_locations = [val.upper()]
                            else:
                                target_locations = [
                                    x.strip().upper() for x in val.split(";")
                                ]
                            break
            except Exception as e:
                self._log("WARNING", f"Could not read department_to_location.csv: {e}")

        if not target_locations:
            self._log(
                "INFO",
                f"No location mapping found for department '{department_name}', skipping.",
            )
            return False

        self._log(
            "INFO",
            f"Enforcing locations {target_locations} for department '{department_name}'.",
        )

        dropdown_btn_xpath = "//*[@id='form_funcionario']/div/div[2]/div[1]/div[2]/div[8]/div[2]/div/div/div/button"
        try:
            self.click_element(dropdown_btn_xpath, max_tries=3)
        except Exception:
            try:
                self.click_element(
                    "//button[contains(@class, 'multiselect dropdown-toggle')]",
                    max_tries=3,
                )
            except Exception as e:
                self._log("WARNING", f"Could not find multiselect button: {e}")
                return False

        self.wait(1)

        script = """
            var targetLocs = arguments[0];
            var labels = document.querySelectorAll("ul.multiselect-container label.checkbox");
            var changed = false;
            
            for (var i = 0; i < labels.length; i++) {
                var labelText = (labels[i].textContent || labels[i].innerText).trim().toUpperCase();
                var input = labels[i].querySelector("input[type='checkbox']");
                var li = labels[i].closest('li');
                
                if (li && !li.classList.contains('filter') && !li.classList.contains('multiselect-all') && input) {
                    var shouldBeChecked = targetLocs.some(loc => labelText === loc || labelText.includes(loc));
                    if (shouldBeChecked && !input.checked) {
                        input.click();
                        changed = true;
                    } else if (!shouldBeChecked && input.checked) {
                        input.click();
                        changed = true;
                    }
                }
            }
            return changed;
        """
        changed = False
        try:
            changed = self.driver.execute_script(script, target_locations)
        except Exception as e:
            self._log("WARNING", f"Failed to set locations via JS: {e}")

        # Close the dropdown
        try:
            self.click_element(dropdown_btn_xpath, max_tries=2)
        except Exception:
            try:
                self.click_element(
                    "//button[contains(@class, 'multiselect dropdown-toggle')]",
                    max_tries=2,
                )
            except Exception:
                pass

        return changed
