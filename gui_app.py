import os
import sys
import json
import traceback
from typing import List, Dict, Any, Optional

import pdfplumber
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QPixmap, QImage
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QLineEdit, QPushButton, QFileDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QTextBrowser,
    QPlainTextEdit, QComboBox, QSplitter, QListWidget, QListWidgetItem,
    QMessageBox, QProgressBar, QGroupBox, QFormLayout, QSpinBox, QSizePolicy
)

# Импортируем ваше ядро из workflow.py
from workflow import (
    AppConfig,
    DatasheetParser,
    KiCadSymbolGenerator,
    KiCadWorkflowEngine,
    resolve_kicad_cli
)

SETTINGS_FILE = "ui_settings.json"


# =====================================================================
# ФОНОВЫЕ ПОТОКИ (QThread), ЧТОБЫ ИНТЕРФЕЙС НЕ ЗАВИСАЛ
# =====================================================================
class PdfExtractWorker(QThread):
    """Поток для локального извлечения страниц из PDF (без вызова ИИ)."""
    log_signal = Signal(str)
    finished_signal = Signal(list)
    error_signal = Signal(str)

    def __init__(self, pdf_path: str, max_pages: int):
        super().__init__()
        self.pdf_path = pdf_path
        self.max_pages = max_pages

    def run(self):
        try:
            pages = DatasheetParser.extract_relevant_data(
                self.pdf_path,
                max_pages=self.max_pages,
                log_fn=lambda msg: self.log_signal.emit(msg)
            )
            self.finished_signal.emit(pages)
        except Exception as e:
            self.error_signal.emit(f"{e}\n{traceback.format_exc()}")


class AiPinoutWorker(QThread):
    """Поток для анализа извлеченных страниц через ИИ и генерации пинов."""
    log_signal = Signal(str)
    finished_signal = Signal(list, dict)  # pins, audit
    error_signal = Signal(str)

    def __init__(self, config: AppConfig, pages_data: List[Dict[str, Any]], comp_name: str, pkg_hint: str):
        super().__init__()
        self.config = config
        self.pages_data = pages_data
        self.comp_name = comp_name
        self.pkg_hint = pkg_hint

    def run(self):
        try:
            engine = KiCadWorkflowEngine(self.config, logger=lambda msg: self.log_signal.emit(msg))

            system_prompt = (
                "Ты — ведущий инженер-схемотехник, создающий символы по стандарту KiCad Library Conventions (KLC).\n"
                "Твоя задача — извлечь полную таблицу выводов (pinout) микросхемы из даташита.\n\n"
                "СТРОГИЕ ПРАВИЛА ФОРМАТИРОВАНИЯ И ГРУППИРОВКИ:\n"
                "1. Выбери корпус из подсказки или самый массовый и укажи точное число выводов (`expected_pin_count`).\n"
                "2. Имена выводов (`name`): если у пина есть критически важная функция (XTAL, RESET, RXD/TXD, SDA/SCL, MOSI/MISO/SCK), "
                "укажи её через слэш, например: 'PB6/XTAL1', 'PD0/RXD'.\n"
                "3. Инверсные сигналы (active-low, с чертой сверху, символами '#', '/', 'n' перед именем) записывай в формате KiCad: '~{RESET}', '~{CS}'.\n"
                "4. Электрический тип (`type`): используй ТОЛЬКО 'input', 'output', 'bidirectional', 'power_in', 'power_out', 'passive', 'tri_state', 'open_collector', 'no_connect'. "
                "ВНИМАНИЕ: AREF и VREF всегда 'passive' или 'input', а не 'power_in'!\n"
                "5. Сторона (`side`): 'left' (питание VCC/VDD, сброс RESET, тактирование XTAL/OSC, опорное AREF, земля GND/VSS) "
                "или 'right' (порты GPIO PA/PB/PC/PD, выходы и шины данных).\n\n"
                "Верни СТРОГО валидный JSON:\n"
                "{\n"
                '  "audit": {"selected_package": "DIP-28", "expected_pin_count": 28, "extracted_pin_count": 28, "missing_or_fixed_notes": ""},\n'
                '  "pins": [\n'
                '    {"num": "1", "name": "PC6/~{RESET}", "type": "bidirectional", "side": "left"}\n'
                "  ]\n"
                "}"
            )

            pkg_note = f"Предпочтительный корпус: {self.pkg_hint}\n" if self.pkg_hint else ""
            user_prompt = (
                f"Компонент: {self.comp_name}\n{pkg_note}"
                f"Данные из даташита:\n{json.dumps(self.pages_data, ensure_ascii=False)}"
            )

            self.log_signal.emit(f"[*] Отправка данных в ИИ ({self.config.model})...")
            response = engine.client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                response_format={"type": "json_object"},
                temperature=self.config.temperature
            )

            parsed = json.loads(response.choices[0].message.content or "{}")
            audit = parsed.get("audit", {})
            pins = parsed.get("pins", [])

            clean_pins = engine._validate_and_deduplicate_pins(pins)
            expected = audit.get("expected_pin_count")
            if isinstance(expected, int) and expected > 0 and len(clean_pins) != expected:
                self.log_signal.emit(f"[!] Коррекция выводов ({len(clean_pins)} != {expected})...")
                clean_pins = engine._auto_correct_pins(
                    self.pages_data, clean_pins, expected, audit.get("selected_package", "")
                )

            self.finished_signal.emit(clean_pins, audit)
        except Exception as e:
            self.error_signal.emit(f"{e}\n{traceback.format_exc()}")


class ErcAgentWorker(QThread):
    """Поток для запуска ERC-агента."""
    log_signal = Signal(str)
    finished_signal = Signal(str)
    error_signal = Signal(str)

    def __init__(self, config: AppConfig, sch_path: str, custom_prompt: str):
        super().__init__()
        self.config = config
        self.sch_path = sch_path
        self.custom_prompt = custom_prompt

    def run(self):
        try:
            engine = KiCadWorkflowEngine(self.config, logger=lambda msg: self.log_signal.emit(msg))
            report_md = engine.run_erc_agent(self.sch_path, self.custom_prompt)
            self.finished_signal.emit(report_md)
        except Exception as e:
            self.error_signal.emit(f"{e}\n{traceback.format_exc()}")


# =====================================================================
# ГЛАВНОЕ ОКНО ПРОГРАММЫ
# =====================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("KiCad AI Studio — Генератор УГО и ERC-Аудитор")
        self.resize(1220, 820)

        self.config = AppConfig()
        self.extracted_pages: List[Dict[str, Any]] = []
        self.current_pins: List[Dict[str, str]] = []
        self.current_pdf_path: str = ""

        self._init_ui()
        self._load_settings()

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # Вкладки
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs, stretch=4)

        self._build_tab_pdf2sym()
        self._build_tab_pdf_inspector()
        self._build_tab_erc()
        self._build_tab_settings()

        # Нижняя панель логов и прогресса
        log_group = QGroupBox("Журнал операций (Live Log)")
        log_layout = QVBoxLayout(log_group)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        log_layout.addWidget(self.progress_bar)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setFont(QFont("Consolas", 9))
        self.log_output.setMaximumHeight(150)
        log_layout.addWidget(self.log_output)

        main_layout.addWidget(log_group, stretch=1)

    # -----------------------------------------------------------------
    # ВКЛАДКА 1: ГЕНЕРАТОР УГО (PDF -> KiCad)
    # -----------------------------------------------------------------
    def _build_tab_pdf2sym(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Форма параметров
        form_group = QGroupBox("Параметры извлечения компонента")
        form_layout = QFormLayout(form_group)

        # Путь к PDF
        pdf_row = QHBoxLayout()
        self.pdf_path_edit = QLineEdit()
        self.pdf_path_edit.setPlaceholderText("Выберите PDF-даташит микросхемы...")
        btn_browse_pdf = QPushButton("Обзор PDF...")
        btn_browse_pdf.clicked.connect(self._browse_pdf)
        pdf_row.addWidget(self.pdf_path_edit)
        pdf_row.addWidget(btn_browse_pdf)
        form_layout.addRow("Даташит (PDF):", pdf_row)

        # Имя и корпус
        meta_row = QHBoxLayout()
        self.comp_name_edit = QLineEdit("ATmega328P")
        self.comp_name_edit.setPlaceholderText("Например: ATmega328P, STM32F103C8T6")
        self.pkg_hint_edit = QLineEdit("DIP-28")
        self.pkg_hint_edit.setPlaceholderText("Например: DIP-28, TQFP-32, SOIC-8 (опционально)")
        meta_row.addWidget(QLabel("Имя компонента:"))
        meta_row.addWidget(self.comp_name_edit)
        meta_row.addWidget(QLabel("Корпус:"))
        meta_row.addWidget(self.pkg_hint_edit)
        form_layout.addRow("Компонент:", meta_row)

        # Выходной файл
        out_row = QHBoxLayout()
        self.out_sym_edit = QLineEdit(os.path.abspath("generated_lib.kicad_sym"))
        btn_browse_out = QPushButton("Куда сохранить...")
        btn_browse_out.clicked.connect(self._browse_out_sym)
        out_row.addWidget(self.out_sym_edit)
        out_row.addWidget(btn_browse_out)
        form_layout.addRow("Файл .kicad_sym:", out_row)

        layout.addWidget(form_group)

        # Кнопки действий
        btn_row = QHBoxLayout()
        self.btn_step1_extract = QPushButton("1. Найти страницы в PDF (Предпросмотр)")
        self.btn_step1_extract.setMinimumHeight(30)
        self.btn_step1_extract.setFont(QFont("Segoe UI", 10))
        self.btn_step1_extract.clicked.connect(self._run_pdf_extraction)

        self.btn_step2_ai = QPushButton("2. Извлечь распиновку через ИИ")
        self.btn_step2_ai.setMinimumHeight(30)
        self.btn_step2_ai.setFont(QFont("Segoe UI", 10))
        self.btn_step2_ai.clicked.connect(self._run_ai_pinout)

        self.btn_step3_save = QPushButton("3. Сгенерировать .kicad_sym из таблицы")
        self.btn_step3_save.setMinimumHeight(30)
        self.btn_step3_save.setFont(QFont("Segoe UI", 10))
        self.btn_step3_save.clicked.connect(self._save_sym_from_table)

        btn_row.addWidget(self.btn_step1_extract)
        btn_row.addWidget(self.btn_step2_ai)
        btn_row.addWidget(self.btn_step3_save)
        layout.addLayout(btn_row)

        # Статус самопроверки ИИ
        self.audit_label = QPlainTextEdit()
        self.audit_label.setReadOnly(True)
        self.audit_label.setMaximumHeight(65)
        self.audit_label.setStyleSheet(
            "background-color: #f4f9f4; color: #1b7e28; font-weight: bold; "
            "border: 1px solid #d3e5d3; border-radius: 4px; padding: 4px;"
        )
        layout.addWidget(self.audit_label)

        # Интерактивная таблица выводов (можно редактировать перед сохранением)
        self.pins_table = QTableWidget(0, 4)
        self.pins_table.setHorizontalHeaderLabels(["№ Пина (num)", "Имя (name)", "Электр. тип (type)", "Сторона (side)"])
        header = self.pins_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        layout.addWidget(self.pins_table)

        # Кнопки управления строками таблицы
        table_btns = QHBoxLayout()
        btn_add_pin = QPushButton("+ Добавить пин")
        btn_add_pin.clicked.connect(self._add_empty_pin_row)
        btn_del_pin = QPushButton("- Удалить выбранный пин")
        btn_del_pin.clicked.connect(self._remove_selected_pin_row)
        btn_open_inspector = QPushButton("Перейти в Инспектор страниц PDF ->")
        btn_open_inspector.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        table_btns.addWidget(btn_add_pin)
        table_btns.addWidget(btn_del_pin)
        table_btns.addStretch()
        table_btns.addWidget(btn_open_inspector)
        layout.addLayout(table_btns)

        self.tabs.addTab(tab, "1. Генератор УГО (PDF -> KiCad)")

    # -----------------------------------------------------------------
    # ВКЛАДКА 2: ВИЗУАЛЬНЫЙ ИНСПЕКТОР СТРАНИЦ PDF
    # -----------------------------------------------------------------
    def _build_tab_pdf_inspector(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        info_label = QLabel(
            "Здесь отображаются страницы, которые алгоритм отобрал из PDF-даташита. "
            "Вы можете визуально сверить страницу оригинала с тем, как извлеклись таблицы и текст."
        )
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        splitter = QSplitter(Qt.Horizontal)

        # Левая колонка: список найденных страниц
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.addWidget(QLabel("Отобранные страницы:"))
        self.pages_list_widget = QListWidget()
        self.pages_list_widget.currentRowChanged.connect(self._on_inspector_page_selected)
        left_layout.addWidget(self.pages_list_widget)
        splitter.addWidget(left_widget)

        # Центральная колонка: визуальный рендер страницы PDF
        center_widget = QWidget()
        center_layout = QVBoxLayout(center_widget)
        center_layout.addWidget(QLabel("Визуальный вид страницы PDF:"))
        self.pdf_image_label = QLabel("Выберите страницу слева для просмотра")
        self.pdf_image_label.setAlignment(Qt.AlignCenter)
        self.pdf_image_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.pdf_image_label.setStyleSheet("background-color: #2b2b2b; color: #aaa; border: 1px solid #444;")
        self.pdf_image_label.setMinimumWidth(380)
        center_layout.addWidget(self.pdf_image_label, stretch=1)
        splitter.addWidget(center_widget)

        # Правая колонка: извлеченные таблицы и сырой текст (то, что видит ИИ)
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.addWidget(QLabel("Извлеченные структуры (отправляются в ИИ):"))
        self.extracted_content_view = QPlainTextEdit()
        self.extracted_content_view.setReadOnly(False)  # Позволяем пользователю даже подправить текст перед отправкой в ИИ!
        self.extracted_content_view.setFont(QFont("Consolas", 9))
        right_layout.addWidget(self.extracted_content_view)

        btn_apply_text_edits = QPushButton("Сохранить ручные правки текста для этой страницы")
        btn_apply_text_edits.clicked.connect(self._save_inspector_text_edit)
        right_layout.addWidget(btn_apply_text_edits)

        splitter.addWidget(right_widget)
        splitter.setSizes([200, 480, 500])

        layout.addWidget(splitter, stretch=1)
        self.tabs.addTab(tab, "2. Инспектор PDF (Извлеченные страницы)")

    # -----------------------------------------------------------------
    # ВКЛАДКА 3: ERC-АУДИТОР СХЕМ
    # -----------------------------------------------------------------
    def _build_tab_erc(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        form_group = QGroupBox("Параметры проверки схемы (.kicad_sch)")
        form_layout = QFormLayout(form_group)

        sch_row = QHBoxLayout()
        self.sch_path_edit = QLineEdit()
        default_sch = os.path.join("files-to-test", "test-project-kicad", "test-project-kicad.kicad_sch")
        if os.path.exists(default_sch):
            self.sch_path_edit.setText(os.path.abspath(default_sch))
        btn_browse_sch = QPushButton("Обзор схемы...")
        btn_browse_sch.clicked.connect(self._browse_sch)
        sch_row.addWidget(self.sch_path_edit)
        sch_row.addWidget(btn_browse_sch)
        form_layout.addRow("Файл схемы:", sch_row)

        self.erc_prompt_edit = QLineEdit()
        self.erc_prompt_edit.setPlaceholderText("Опционально: укажите, на что обратить особое внимание (или оставьте пустым)")
        form_layout.addRow("Доп. инструкция ИИ:", self.erc_prompt_edit)

        layout.addWidget(form_group)

        self.btn_run_erc = QPushButton("Запустить полный ERC-аудит схемы")
        self.btn_run_erc.setStyleSheet("font-weight: bold; padding: 8px;")
        self.btn_run_erc.clicked.connect(self._run_erc_audit)
        layout.addWidget(self.btn_run_erc)

        # Браузер с поддержкой красивого рендеринга Markdown-таблиц!
        self.erc_report_browser = QTextBrowser()
        self.erc_report_browser.setOpenExternalLinks(True)
        self.erc_report_browser.setFont(QFont("Segoe UI", 10))
        layout.addWidget(self.erc_report_browser, stretch=1)

        self.tabs.addTab(tab, "3. ERC-Аудитор схем")

    # -----------------------------------------------------------------
    # ВКЛАДКА 4: НАСТРОЙКИ (API KEY, МОДЕЛЬ, ПУТЬ К KICAD)
    # -----------------------------------------------------------------
    def _build_tab_settings(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        ai_group = QGroupBox("Настройки ИИ-провайдера (OpenAI-совместимый API)")
        ai_form = QFormLayout(ai_group)

        # Пресеты провайдеров
        self.provider_combo = QComboBox()
        self.provider_combo.addItems([
            "DeepSeek (https://api.deepseek.com)",
            "OpenAI (https://api.openai.com/v1)",
            "OpenRouter (https://openrouter.ai/api/v1)",
            "Локальная Ollama (http://localhost:11434/v1)",
            "Свой URL"
        ])
        self.provider_combo.currentIndexChanged.connect(self._on_provider_preset_changed)
        ai_form.addRow("Пресет провайдера:", self.provider_combo)

        # API Key с кнопкой Показать/Скрыть
        key_row = QHBoxLayout()
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("Введите ваш API-ключ (например, sk-...)")
        btn_toggle_key = QPushButton("Показать/Скрыть")
        btn_toggle_key.clicked.connect(self._toggle_api_key_visibility)
        key_row.addWidget(self.api_key_edit)
        key_row.addWidget(btn_toggle_key)
        ai_form.addRow("API Ключ:", key_row)

        self.base_url_edit = QLineEdit("https://api.deepseek.com")
        ai_form.addRow("Base URL:", self.base_url_edit)

        self.model_edit = QLineEdit("deepseek-chat")
        ai_form.addRow("Модель:", self.model_edit)

        self.max_pages_spin = QSpinBox()
        self.max_pages_spin.setRange(1, 25)
        self.max_pages_spin.setValue(8)
        ai_form.addRow("Макс. страниц из PDF в ИИ:", self.max_pages_spin)

        layout.addWidget(ai_group)

        # Настройки KiCad
        kicad_group = QGroupBox("Интеграция с KiCad (kicad-cli)")
        kicad_form = QFormLayout(kicad_group)

        kicad_row = QHBoxLayout()
        self.kicad_path_edit = QLineEdit()
        self.kicad_path_edit.setPlaceholderText("Оставьте пустым для автопоиска или укажите путь к kicad-cli.exe")
        btn_browse_kicad = QPushButton("Обзор...")
        btn_browse_kicad.clicked.connect(self._browse_kicad_cli)
        btn_detect_kicad = QPushButton("Проверить / Найти автоматически")
        btn_detect_kicad.clicked.connect(self._check_kicad_cli)
        kicad_row.addWidget(self.kicad_path_edit)
        kicad_row.addWidget(btn_browse_kicad)
        kicad_row.addWidget(btn_detect_kicad)
        kicad_form.addRow("Путь к kicad-cli:", kicad_row)

        self.kicad_status_label = QLabel("Статус: не проверено")
        kicad_form.addRow("Статус обнаружения:", self.kicad_status_label)

        layout.addWidget(kicad_group)

        btn_save_settings = QPushButton("Сохранить настройки")
        btn_save_settings.setStyleSheet("font-weight: bold; padding: 8px;")
        btn_save_settings.clicked.connect(self._save_settings)
        layout.addWidget(btn_save_settings)

        layout.addStretch()
        self.tabs.addTab(tab, "4. Настройки (API & KiCad)")

    # =====================================================================
    # ЛОГИКА РАБОТЫ ВКЛАДОК И ОБРАБОТЧИКИ СОБЫТИЙ
    # =====================================================================
    def log_message(self, msg: str):
        self.log_output.appendPlainText(msg)
        self.log_output.verticalScrollBar().setValue(self.log_output.verticalScrollBar().maximum())

    def _set_busy(self, busy: bool):
        if busy:
            self.progress_bar.setRange(0, 0)  # Бегущий индикатор
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
        self.btn_step1_extract.setEnabled(not busy)
        self.btn_step2_ai.setEnabled(not busy)
        self.btn_run_erc.setEnabled(not busy)

    def _sync_config_from_ui(self):
        self.config.api_key = self.api_key_edit.text().strip()
        self.config.base_url = self.base_url_edit.text().strip()
        self.config.model = self.model_edit.text().strip()
        self.config.kicad_cli_path = self.kicad_path_edit.text().strip()
        self.config.max_pdf_pages_to_send = self.max_pages_spin.value()
        if self.config.kicad_cli_path:
            os.environ["KICAD_CLI_PATH"] = self.config.kicad_cli_path

    # --- Шаг 1: Извлечение страниц из PDF и заполнение Инспектора ---
    def _run_pdf_extraction(self):
        pdf_path = self.pdf_path_edit.text().strip()
        if not pdf_path or not os.path.isfile(pdf_path):
            QMessageBox.warning(self, "Файл не найден", "Укажите существующий PDF-файл даташита.")
            return

        self._sync_config_from_ui()
        self.current_pdf_path = pdf_path
        self._set_busy(True)
        self.log_message(f"[*] Запуск локального сканирования PDF: {pdf_path}")

        self.pdf_worker = PdfExtractWorker(pdf_path, self.config.max_pdf_pages_to_send)
        self.pdf_worker.log_signal.connect(self.log_message)
        self.pdf_worker.finished_signal.connect(self._on_pdf_extracted)
        self.pdf_worker.error_signal.connect(self._on_worker_error)
        self.pdf_worker.start()

    def _on_pdf_extracted(self, pages: List[Dict[str, Any]]):
        self._set_busy(False)
        self.extracted_pages = pages
        self.pages_list_widget.clear()

        if not pages:
            QMessageBox.warning(self, "Страницы не найдены", "В PDF не найдено страниц с ключевыми словами распиновки.")
            return

        for idx, p in enumerate(pages):
            page_num = p.get("page", "?")
            tables_cnt = len(p.get("tables_markdown", []))
            item = QListWidgetItem(f"Стр. №{page_num} (таблиц: {tables_cnt})")
            item.setData(Qt.UserRole, idx)
            self.pages_list_widget.addItem(item)

        self.pages_list_widget.setCurrentRow(0)
        self.log_message(f"[+] Отобрано страниц: {len(pages)}. Вы можете просмотреть их во вкладке '2. Инспектор PDF'.")

    def _on_inspector_page_selected(self, row: int):
        if row < 0 or row >= len(self.extracted_pages):
            return
        page_data = self.extracted_pages[row]
        page_num = page_data.get("page", 1)

        # Формируем текст, который видит ИИ
        tables_md = page_data.get("tables_markdown", [])
        raw_snippet = page_data.get("raw_text_snippet", "")

        display_parts = [f"=== СТРАНИЦА №{page_num} ==="]
        if tables_md:
            display_parts.append(f"\n--- ИЗВЛЕЧЕННЫЕ ТАБЛИЦЫ ({len(tables_md)} шт.) ---")
            for t_idx, t_str in enumerate(tables_md, 1):
                display_parts.append(f"[Таблица {t_idx}]:\n{t_str}\n")
        display_parts.append("\n--- СЫРОЙ ТЕКСТ СТРАНИЦЫ ---\n" + raw_snippet)
        self.extracted_content_view.setPlainText("\n".join(display_parts))

        # Рендерим визуальную картинку страницы PDF через pdfplumber
        if self.current_pdf_path and os.path.isfile(self.current_pdf_path):
            try:
                with pdfplumber.open(self.current_pdf_path) as pdf:
                    pdf_page = pdf.pages[page_num - 1]
                    page_img = pdf_page.to_image(resolution=110).original  # PIL Image
                    img_bytes = page_img.convert("RGB").tobytes("raw", "RGB")
                    qimg = QImage(img_bytes, page_img.width, page_img.height, page_img.width * 3, QImage.Format_RGB888)
                    pixmap = QPixmap.fromImage(qimg)
                    scaled_pix = pixmap.scaled(
                        self.pdf_image_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
                    )
                    self.pdf_image_label.setPixmap(scaled_pix)
            except Exception as e:
                self.pdf_image_label.setText(f"Визуальное превью недоступно:\n{e}")

    def _save_inspector_text_edit(self):
        row = self.pages_list_widget.currentRow()
        if row < 0 or row >= len(self.extracted_pages):
            return
        edited_text = self.extracted_content_view.toPlainText()
        self.extracted_pages[row]["raw_text_snippet"] = edited_text
        self.extracted_pages[row]["tables_markdown"] = []  # Так как всё объединено в отредактированный текст
        self.log_message(f"[+] Ручные правки для страницы №{self.extracted_pages[row]['page']} сохранены!")

    # --- Шаг 2: Распознавание пинов через ИИ ---
    def _run_ai_pinout(self):
        self._sync_config_from_ui()
        if not self.config.api_key:
            QMessageBox.warning(self, "Нет ключа API", "Перейдите во вкладку '4. Настройки' и введите ваш API-ключ.")
            self.tabs.setCurrentIndex(3)
            return

        # Если пользователь еще не нажал Шаг 1, сначала извлечем страницы автоматически
        if not self.extracted_pages:
            pdf_path = self.pdf_path_edit.text().strip()
            if not pdf_path or not os.path.isfile(pdf_path):
                QMessageBox.warning(self, "Нет PDF", "Сначала выберите PDF-даташит.")
                return
            try:
                self.current_pdf_path = pdf_path
                self.extracted_pages = DatasheetParser.extract_relevant_data(
                    pdf_path, max_pages=self.config.max_pdf_pages_to_send, log_fn=self.log_message
                )
                self._on_pdf_extracted(self.extracted_pages)
            except Exception as e:
                QMessageBox.critical(self, "Ошибка чтения PDF", str(e))
                return

        comp_name = self.comp_name_edit.text().strip() or "MyChip"
        pkg_hint = self.pkg_hint_edit.text().strip()

        self._set_busy(True)
        self.ai_worker = AiPinoutWorker(self.config, self.extracted_pages, comp_name, pkg_hint)
        self.ai_worker.log_signal.connect(self.log_message)
        self.ai_worker.finished_signal.connect(self._on_ai_pinout_finished)
        self.ai_worker.error_signal.connect(self._on_worker_error)
        self.ai_worker.start()

    def _on_ai_pinout_finished(self, pins: List[Dict[str, str]], audit: Dict[str, Any]):
        self._set_busy(False)
        self.current_pins = pins

        pkg = audit.get("selected_package", "Не указан")
        exp = audit.get("expected_pin_count", "?")
        notes = audit.get("missing_or_fixed_notes", "ОК")
        audit_text = (
            f"Самопроверка ИИ: Корпус = {pkg} | Ожидалось пинов = {exp} | "
            f"Извлечено = {len(pins)} | Заметка: {notes}"
        )
        self.audit_label.setPlainText(audit_text)
        self.audit_label.setStyleSheet("color: #1b7e28; font-weight: bold;")

        self._populate_pins_table(pins)
        self.log_message(f"[+] Таблица заполнена ({len(pins)} выводов). Проверьте и нажмите кнопку '3. Сгенерировать .kicad_sym'.")

    def _populate_pins_table(self, pins: List[Dict[str, str]]):
        self.pins_table.setRowCount(0)
        for p in pins:
            row = self.pins_table.rowCount()
            self.pins_table.insertRow(row)
            self.pins_table.setItem(row, 0, QTableWidgetItem(str(p.get("num", ""))))
            self.pins_table.setItem(row, 1, QTableWidgetItem(str(p.get("name", ""))))
            self.pins_table.setItem(row, 2, QTableWidgetItem(str(p.get("type", "bidirectional"))))
            self.pins_table.setItem(row, 3, QTableWidgetItem(str(p.get("side", "left"))))

    def _add_empty_pin_row(self):
        row = self.pins_table.rowCount()
        self.pins_table.insertRow(row)
        self.pins_table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
        self.pins_table.setItem(row, 1, QTableWidgetItem(f"PIN_{row + 1}"))
        self.pins_table.setItem(row, 2, QTableWidgetItem("bidirectional"))
        self.pins_table.setItem(row, 3, QTableWidgetItem("right"))

    def _remove_selected_pin_row(self):
        cur = self.pins_table.currentRow()
        if cur >= 0:
            self.pins_table.removeRow(cur)

    # --- Шаг 3: Сохранение .kicad_sym из таблицы ---
    def _save_sym_from_table(self):
        pins_from_ui = []
        for r in range(self.pins_table.rowCount()):
            num = (self.pins_table.item(r, 0).text() if self.pins_table.item(r, 0) else "").strip()
            name = (self.pins_table.item(r, 1).text() if self.pins_table.item(r, 1) else "").strip()
            ptype = (self.pins_table.item(r, 2).text() if self.pins_table.item(r, 2) else "bidirectional").strip()
            side = (self.pins_table.item(r, 3).text() if self.pins_table.item(r, 3) else "left").strip()
            if num and name:
                pins_from_ui.append({"num": num, "name": name, "type": ptype, "side": side})

        if not pins_from_ui:
            QMessageBox.warning(self, "Таблица пуста", "Нет выводов для генерации символа.")
            return

        comp_name = self.comp_name_edit.text().strip() or "Generated_IC"
        out_path = self.out_sym_edit.text().strip() or "generated_lib.kicad_sym"

        try:
            content = KiCadSymbolGenerator.generate(comp_name, pins_from_ui)
            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(content)
            self.log_message(f"[+] Файл библиотеки успешно сохранен: {os.path.abspath(out_path)}")
            QMessageBox.information(self, "Успех", f"Символ '{comp_name}' сохранен в:\n{os.path.abspath(out_path)}")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка сохранения", str(e))

    # --- Ветка ERC-Аудита ---
    def _run_erc_audit(self):
        self._sync_config_from_ui()
        if not self.config.api_key:
            QMessageBox.warning(self, "Нет ключа API", "Сначала укажите API-ключ во вкладке '4. Настройки'.")
            self.tabs.setCurrentIndex(3)
            return

        sch_path = self.sch_path_edit.text().strip()
        if not sch_path or not os.path.isfile(sch_path):
            QMessageBox.warning(self, "Файл не найден", "Выберите существующий файл схемы .kicad_sch.")
            return

        self._set_busy(True)
        self.erc_report_browser.setMarkdown("*Запущена проверка схемы и анализ отчета ИИ-инженером... Пожалуйста, подождите.*")

        self.erc_worker = ErcAgentWorker(self.config, sch_path, self.erc_prompt_edit.text().strip())
        self.erc_worker.log_signal.connect(self.log_message)
        self.erc_worker.finished_signal.connect(self._on_erc_finished)
        self.erc_worker.error_signal.connect(self._on_worker_error)
        self.erc_worker.start()

    def _on_erc_finished(self, markdown_report: str):
        self._set_busy(False)
        self.erc_report_browser.setMarkdown(markdown_report)

    def _on_worker_error(self, err_msg: str):
        self._set_busy(False)
        self.log_message(f"[-] ОШИБКА: {err_msg}")
        QMessageBox.critical(self, "Ошибка выполнения", err_msg[:800])

    # --- Вспомогательные диалоги и настройки ---
    def _browse_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выберите PDF даташит", "", "PDF Files (*.pdf)")
        if path:
            self.pdf_path_edit.setText(path)

    def _browse_out_sym(self):
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить библиотеку KiCad", "generated_lib.kicad_sym", "KiCad Symbol (*.kicad_sym)")
        if path:
            self.out_sym_edit.setText(path)

    def _browse_sch(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выберите схему KiCad", "", "KiCad Schematic (*.kicad_sch)")
        if path:
            self.sch_path_edit.setText(path)

    def _browse_kicad_cli(self):
        path, _ = QFileDialog.getOpenFileName(self, "Укажите kicad-cli", "", "Executable (*.exe *)" if os.name == "nt" else "Executable (*)")
        if path:
            self.kicad_path_edit.setText(path)
            self._check_kicad_cli()

    def _toggle_api_key_visibility(self):
        if self.api_key_edit.echoMode() == QLineEdit.Password:
            self.api_key_edit.setEchoMode(QLineEdit.Normal)
        else:
            self.api_key_edit.setEchoMode(QLineEdit.Password)

    def _on_provider_preset_changed(self, idx: int):
        presets = {
            0: ("https://api.deepseek.com", "deepseek-chat"),
            1: ("https://api.openai.com/v1", "gpt-4o-mini"),
            2: ("https://openrouter.ai/api/v1", "deepseek/deepseek-chat"),
            3: ("http://localhost:11434/v1", "qwen2.5-coder:14b"),
        }
        if idx in presets:
            url, model = presets[idx]
            self.base_url_edit.setText(url)
            self.model_edit.setText(model)

    def _check_kicad_cli(self):
        resolved = resolve_kicad_cli(self.kicad_path_edit.text().strip())
        if resolved:
            self.kicad_status_label.setText(f"Найдено: {resolved}")
            self.kicad_status_label.setStyleSheet("color: #1b7e28; font-weight: bold;")
        else:
            self.kicad_status_label.setText("Не найдено! Укажите путь к kicad-cli вручную.")
            self.kicad_status_label.setStyleSheet("color: #c92a2a; font-weight: bold;")

    def _save_settings(self):
        self._sync_config_from_ui()
        data = {
            "api_key": self.config.api_key,
            "base_url": self.config.base_url,
            "model": self.config.model,
            "kicad_cli_path": self.config.kicad_cli_path,
            "max_pdf_pages": self.config.max_pdf_pages_to_send
        }
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self._check_kicad_cli()
            self.log_message("[+] Настройки успешно сохранены в ui_settings.json")
            QMessageBox.information(self, "Настройки", "Настройки успешно сохранены!")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить настройки: {e}")

    def _load_settings(self):
        if os.path.isfile(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.api_key_edit.setText(data.get("api_key", os.environ.get("DEEPSEEK_API_KEY", "")))
                self.base_url_edit.setText(data.get("base_url", "https://api.deepseek.com"))
                self.model_edit.setText(data.get("model", "deepseek-chat"))
                self.kicad_path_edit.setText(data.get("kicad_cli_path", ""))
                self.max_pages_spin.setValue(int(data.get("max_pdf_pages", 8)))
            except Exception:
                pass
        else:
            self.api_key_edit.setText(os.environ.get("DEEPSEEK_API_KEY", ""))

        # Подставим тестовый PDF по умолчанию, если он есть
        default_pdf = os.path.join("files-to-test", "ATmega328.pdf")
        if os.path.isfile(default_pdf):
            self.pdf_path_edit.setText(os.path.abspath(default_pdf))

        self._check_kicad_cli()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")  # Современный кроссплатформенный стиль
    win = MainWindow()
    win.show()
    sys.exit(app.exec())