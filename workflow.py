import os
import re
import sys
import json
import shutil
import argparse
import subprocess
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Callable

import pdfplumber
from openai import OpenAI


# =====================================================================
# 1. КОНФИГУРАЦИЯ (Готова к привязке к будущему GUI)
# =====================================================================
@dataclass
class AppConfig:
    """Конфигурация системы. Легко заполняется из UI, .env или CLI."""
    api_key: str = field(default_factory=lambda: os.environ.get("DEEPSEEK_API_KEY", ""))
    base_url: str = field(default_factory=lambda: os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"))
    model: str = field(default_factory=lambda: os.environ.get("LLM_MODEL", "deepseek-chat"))
    kicad_cli_path: str = field(default_factory=lambda: os.environ.get("KICAD_CLI_PATH", ""))
    max_pdf_pages_to_send: int = 8  # Защита от переполнения контекста и лишней траты токенов
    temperature: float = 0.00       # Минимальная температура для строгой детерминированности


# =====================================================================
# 2. УНИВЕРСАЛЬНЫЙ ПОИСК KICAD-CLI (Без привязки к версии и ОС)
# =====================================================================
def resolve_kicad_cli(custom_path: str = "") -> Optional[str]:
    """
    Находит исполняемый файл kicad-cli на Windows, macOS и Linux
    для любых версий KiCad (7.0, 8.0, 9.0 и новее).
    """
    # 1. Проверяем пользовательский путь (из будущего UI или переменной окружения)
    if custom_path:
        custom_path = os.path.expanduser(custom_path)
        if os.path.isfile(custom_path):
            return custom_path
        if os.path.isdir(custom_path):
            exe_name = "kicad-cli.exe" if os.name == "nt" else "kicad-cli"
            candidate = os.path.join(custom_path, exe_name)
            if os.path.isfile(candidate):
                return candidate

    # 2. Проверяем системный PATH
    path_bin = shutil.which("kicad-cli")
    if path_bin:
        return path_bin

    # 3. Автопоиск по стандартным путям установки для разных ОС
    if os.name == "nt":
        search_roots = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
        ]
        for root in search_roots:
            if not root or not os.path.exists(root):
                continue
            kicad_base = os.path.join(root, "KiCad")
            if os.path.exists(kicad_base):
                # Сортируем по убыванию, чтобы брать самую свежую установленную версию
                for folder in sorted(os.listdir(kicad_base), reverse=True):
                    cli_path = os.path.join(kicad_base, folder, "bin", "kicad-cli.exe")
                    if os.path.isfile(cli_path):
                        return cli_path

    elif sys.platform == "darwin":  # macOS
        mac_path = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
        if os.path.isfile(mac_path):
            return mac_path

    else:  # Linux / Flatpak / Snap
        linux_candidates = [
            "/usr/bin/kicad-cli",
            "/usr/local/bin/kicad-cli",
            "/snap/bin/kicad-cli",
        ]
        for candidate in linux_candidates:
            if os.path.isfile(candidate):
                return candidate

    return None


# =====================================================================
# 3. ПРОДВИНУТЫЙ ПАРСЕР PDF (Скоринг + Фильтр оглавлений + Сжатие)
# =====================================================================
class DatasheetParser:
    # Основные заголовки разделов распиновки (EN + RU)
    HEADER_PATTERNS = [
        r"pin\s+configuration",
        r"pin\s+assignment",
        r"pin\s+description",
        r"pin\s+functions?",
        r"pin\s+definitions?",
        r"pin\s+list",
        r"pinout\s+diagram",
        r"terminal\s+functions?",
        r"terminal\s+assignments?",
        r"ball\s+assignments?",
        r"ballout",
        r"pad\s+description",
        r"lead\s+assignments?",
        r"signal\s+descriptions?",
        r"i/o\s+description",
        r"назначение\s+выводов",
        r"описание\s+выводов",
        r"цоколевка",
    ]

    # Маркеры внутри таблиц (помогают узнать таблицу пинов даже без заголовка)
    TABLE_SIGNALS = [
        r"\bpin\s*(?:no|number|name|#)\b",
        r"\bsymbol\b.*\b(?:type|i/o|description|function)\b",
        r"\b(?:vcc|vdd|gnd|vss)\b",
        r"\b(?:qfn|tqfp|lqfp|soic|dip|bga|ssop|tssop)\b",
    ]

    @classmethod
    def _is_toc_page(cls, text: str) -> bool:
        """Определяет, является ли страница оглавлением (Table of Contents), чтобы не тратить на нее токены."""
        if re.search(r"table\s+of\s+contents|содержание|оглавление", text[:400], re.IGNORECASE):
            return True
        # Если на странице много строк вида "Раздел ......... 15"
        dotted_lines = re.findall(r"\.{4,}\s*\d+", text)
        return len(dotted_lines) >= 5

    @classmethod
    def _score_page(cls, text: str, has_tables: bool) -> int:
        """Начисляет баллы странице по вероятности нахождения на ней распиновки."""
        if not text or cls._is_toc_page(text):
            return 0

        score = 0
        for pattern in cls.HEADER_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                score += 4

        for sig in cls.TABLE_SIGNALS:
            if re.search(sig, text, re.IGNORECASE):
                score += 2

        if has_tables and score > 0:
            score += 3  # Страница с таблицей и ключевыми словами имеет высший приоритет

        return score

    @staticmethod
    def _compress_table(table: List[List[Any]]) -> str:
        """Превращает сырую таблицу pdfplumber в компактный текст с разделителем '|', экономя токены."""
        rows = []
        for row in table:
            if not row:
                continue
            cleaned = [re.sub(r"\s+", " ", str(cell or "")).strip() for cell in row]
            if any(cleaned):  # Пропускаем полностью пустые строки
                rows.append(" | ".join(cleaned))
        return "\n".join(rows)

    @classmethod
    def extract_relevant_data(cls, pdf_path: str, max_pages: int = 8, log_fn: Callable[[str], None] = print) -> List[Dict[str, Any]]:
        if not os.path.isfile(pdf_path):
            raise FileNotFoundError(f"PDF-файл не найден: {pdf_path}")

        log_fn(f"[*] Сканирование даташита: {os.path.basename(pdf_path)}...")
        scored_pages = []

        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)
            log_fn(f"[+] Всего страниц: {total_pages}")

            page_cache = []
            for idx, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                tables = page.extract_tables() or []
                score = cls._score_page(text, bool(tables))
                page_cache.append((idx + 1, text, tables, score))

            # Захватываем релевантные страницы + проверяем продолжение таблиц на следующих страницах
            selected_indices = set()
            for i, (page_num, text, tables, score) in enumerate(page_cache):
                if score >= 4:
                    selected_indices.add(i)
                    # Если на следующей странице продолжается таблица (даже без заголовка), берем и её
                    if i + 1 < total_pages:
                        _, next_text, next_tables, next_score = page_cache[i + 1]
                        if next_tables and not cls._is_toc_page(next_text):
                            selected_indices.add(i + 1)

            # Сортируем отобранные страницы по убыванию полезности, затем по порядку следования
            sorted_candidates = sorted(
                [page_cache[i] for i in selected_indices],
                key=lambda x: x[3],
                reverse=True
            )[:max_pages]

            sorted_candidates.sort(key=lambda x: x[0])

            for page_num, text, tables, score in sorted_candidates:
                log_fn(f"[->] Отобрана стр. №{page_num} (рейтинг релевантности: {score}, таблиц: {len(tables)})")
                compressed_tables = [cls._compress_table(t) for t in tables if t]
                
                # Сжимаем множественные переносы и пробелы для экономии токенов
                compact_text = re.sub(r"[ \t]+", " ", text).strip()
                scored_pages.append({
                    "page": page_num,
                    "tables_markdown": compressed_tables,
                    # Если таблицы отлично извлеклись, режем сырой текст до 2500 символов (для контекста корпуса)
                    "raw_text_snippet": compact_text[:2500] if compressed_tables else compact_text[:5000]
                })

        return scored_pages


# =====================================================================
# 4. ГЕНЕРАТОР СИМВОЛОВ KICAD (.kicad_sym) С ДИНАМИЧЕСКОЙ ГЕОМЕТРИЕЙ
# =====================================================================
class KiCadSymbolGenerator:
    VALID_PIN_TYPES = {
        "input", "output", "bidirectional", "tri_state",
        "passive", "free", "unspecified", "power_in",
        "power_out", "open_collector", "open_emitter", "no_connect"
    }

    @staticmethod
    def _escape(val: str) -> str:
        """Экранирует спецсимволы для S-expressions в KiCad."""
        return str(val).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()

    @classmethod
    def generate(cls, component_name: str, pins: List[Dict[str, str]]) -> str:
        """
        Строит валидный файл .kicad_sym по стандарту KLC:
        - с разделителями между функциональными группами и портами (PA, PB, PC, PD)
        - с прижатием выводов GND к нижнему левому краю корпуса
        """
        safe_name = re.sub(r"[^\w\-_.]", "_", component_name.strip()) or "Generated_IC"

        # Умное распределение для больших MCU (> 30 пинов), если сторона не задана жестко ИИ
        total_pins_count = len(pins)
        for p in pins:
            # Если чип большой, перераспределяем порты по сторонам для баланса высоты
            if total_pins_count > 30 and "side" not in p:
                name_up = str(p.get("name", "")).upper()
                if any(k in name_up for k in ("VCC", "VDD", "VSS", "GND", "VBAT", "VDDA", "VSSA", "NRST", "BOOT", "OSC", "VREF")):
                    p["side"] = "left"
                elif name_up.startswith("P") and len(name_up) > 1 and name_up[1] in ("C", "D", "E", "F", "G"):
                    p["side"] = "left"  # Порты C, D, E уходят налево вниз
                else:
                    p["side"] = "right" # Порты A, B остаются справа

        left_pins = [p for p in pins if str(p.get("side", "left")).lower() == "left"]
        right_pins = [p for p in pins if str(p.get("side", "left")).lower() != "left"]

        # Балансировка, если одна из сторон пуста
        if not right_pins and len(left_pins) > 2:
            mid = (len(left_pins) + 1) // 2
            right_pins = left_pins[mid:]
            left_pins = left_pins[:mid]
        elif not left_pins and len(right_pins) > 2:
            mid = (len(right_pins) + 1) // 2
            left_pins = right_pins[:mid]
            right_pins = right_pins[mid:]

        # Классификация левых выводов: 0 - Питание (+), 1 - Системные, 2 - Земля (GND)
        def get_left_group(p) -> int:
            name_up = str(p.get("name", "")).upper()
            if any(k in name_up for k in ("VCC", "VDD", "AVCC", "VBAT", "3V3", "5V")):
                return 0
            if any(k in name_up for k in ("GND", "VSS", "AGND", "PGND")):
                return 2
            return 1

        # Определение префикса порта для правых выводов (например, PB, PC, PD), чтобы делать отступ между ними
        def get_port_prefix(p) -> str:
            name_up = str(p.get("name", "")).upper()
            m = re.match(r"^(P[A-Z])\d+", name_up)
            return m.group(1) if m else name_up[:2]

        left_pins.sort(key=lambda p: (get_left_group(p), str(p.get("name", "")).upper()))
        right_pins.sort(key=lambda p: str(p.get("name", "")).upper())

        # Разбиваем левые пины на верхние/средние и нижние (GND)
        left_pwr = [p for p in left_pins if get_left_group(p) == 0]
        left_sys = [p for p in left_pins if get_left_group(p) == 1]
        left_gnd = [p for p in left_pins if get_left_group(p) == 2]

        # Считаем количество шагов сетки справа (с учетом пустых строк между портами PB / PC / PD)
        right_slots: List[Optional[Dict[str, str]]] = []
        last_prefix = None
        for p in right_pins:
            prefix = get_port_prefix(p)
            if last_prefix is not None and prefix != last_prefix:
                right_slots.append(None)  # Пустой шаг 2.54 мм между портами
            right_slots.append(p)
            last_prefix = prefix

        # Считаем минимально необходимое число слотов слева (с отступом между VCC, System и GND)
        left_top_slots: List[Optional[Dict[str, str]]] = []
        left_top_slots.extend(left_pwr)
        if left_pwr and left_sys:
            left_top_slots.append(None)  # Пустой шаг между питанием и системными пинами
        left_top_slots.extend(left_sys)

        min_left_height = len(left_top_slots) + (1 if left_top_slots and left_gnd else 0) + len(left_gnd)
        total_slots = max(len(right_slots), min_left_height, 1)

        pitch = 2.54
        max_name_len = max((len(str(p.get("name", ""))) for p in pins), default=4)
        half_width = max(7.62, round((max_name_len * 0.95 + 5.08) / pitch) * pitch)
        pin_length = 2.54

        # Вычисляем координаты верхней и нижней границ корпуса по сетке 2.54 мм
        total_span = (total_slots - 1) * pitch
        top_y = round((total_span / 2.0) / pitch) * pitch + pitch
        bottom_y = top_y - total_span - (2 * pitch)
        first_pin_y = top_y - pitch
        last_pin_y = bottom_y + pitch

        lines = [
            '(kicad_symbol_lib (version 20211014) (generator kicad_ai_workflow)',
            f'  (symbol "{cls._escape(safe_name)}" (in_bom yes) (on_board yes)',
            f'    (property "Reference" "U" (id 0) (at 0 {top_y + 3.81:.2f} 0) (effects (font (size 1.27 1.27))))',
            f'    (property "Value" "{cls._escape(safe_name)}" (id 1) (at 0 {top_y + 1.27:.2f} 0) (effects (font (size 1.27 1.27))))',
            f'    (property "Footprint" "" (id 2) (at 0 {bottom_y - 2.54:.2f} 0) (effects (font (size 1.27 1.27)) hide))',
            f'    (symbol "{cls._escape(safe_name)}_0_1"',
            f'      (rectangle (start {-half_width:.2f} {top_y:.2f}) (end {half_width:.2f} {bottom_y:.2f}) (stroke (width 0.254) (type default)) (fill (type background)))',
            '    )',
            f'    (symbol "{cls._escape(safe_name)}_1_1"'
        ]

        def append_pin(pin_dict: Dict[str, str], x_coord: float, y_coord: float, angle: int):
            p_type = pin_dict.get("type", "bidirectional")
            if p_type not in cls.VALID_PIN_TYPES:
                p_type = "bidirectional"
            p_name = cls._escape(pin_dict.get("name", "PIN"))
            p_num = cls._escape(pin_dict.get("num", "1"))
            lines.extend([
                f'      (pin {p_type} line (at {x_coord:.2f} {y_coord:.2f} {angle}) (length {pin_length:.2f})',
                f'        (name "{p_name}" (effects (font (size 1.27 1.27))))',
                f'        (number "{p_num}" (effects (font (size 1.27 1.27))))',
                '      )'
            ])

        # 1. Отрисовка левых верхних и системных выводов (сверху вниз)
        x_left = -(half_width + pin_length)
        for idx, pin in enumerate(left_top_slots):
            if pin is not None:
                append_pin(pin, x_left, first_pin_y - idx * pitch, 0)

        # 2. Отрисовка левых выводов GND (прижаты к самому низу корпуса!)
        for idx, pin in enumerate(reversed(left_gnd)):
            append_pin(pin, x_left, last_pin_y + idx * pitch, 0)

        # 3. Отрисовка правых выводов (с пустыми разделителями между портами PB, PC, PD)
        x_right = half_width + pin_length
        for idx, pin in enumerate(right_slots):
            if pin is not None:
                append_pin(pin, x_right, first_pin_y - idx * pitch, 180)

        lines.extend([
            '    )',
            '  )',
            ')'
        ])
        return "\n".join(lines)

# =====================================================================
# 5. ЯДРО СИСТЕМЫ И ИИ-АГЕНТ (Единый класс для CLI и будущего GUI)
# =====================================================================
class KiCadWorkflowEngine:
    def __init__(self, config: AppConfig, logger: Callable[[str], None] = print):
        self.config = config
        self.log = logger
        self._client: Optional[OpenAI] = None

    @property
    def client(self) -> OpenAI:
        if not self.config.api_key:
            raise ValueError(
                "Не задан API-ключ ИИ! Укажите переменную окружения DEEPSEEK_API_KEY "
                "или передайте ключ через настройки."
            )
        if self._client is None:
            self._client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url)
        return self._client

    # -----------------------------------------------------------------
    # ВЕТКА 1: PDF -> ИИ (с самопроверкой) -> валидация -> .kicad_sym
    # -----------------------------------------------------------------
    def build_symbol_from_pdf(
        self,
        pdf_path: str,
        component_name: str,
        output_sym_path: str,
        package_hint: str = ""
    ) -> str:
        pages_data = DatasheetParser.extract_relevant_data(
            pdf_path, max_pages=self.config.max_pdf_pages_to_send, log_fn=self.log
        )
        if not pages_data:
            raise RuntimeError("Не удалось найти страницы с распиновкой в PDF.")

        # 1. Обновленный промпт внутри build_symbol_from_pdf (в классе KiCadWorkflowEngine):
        system_prompt = (
            "Ты — ведущий инженер-схемотехник, создающий символы по стандарту KiCad Library Conventions (KLC).\n"
            "Твоя задача — извлечь полную таблицу выводов (pinout) микросхемы из даташита.\n\n"
            "СТРОГИЕ ПРАВИЛА ФОРМАТИРОВАНИЯ И ГРУППИРОВКИ:\n"
            "1. Выбери корпус из подсказки или самый массовый и укажи точное число выводов (`expected_pin_count`).\n"
            "2. Имена выводов (`name`): если у пина есть критически важная функция (XTAL, RESET, RXD/TXD, SDA/SCL, MOSI/MISO/SCK), "
            "укажи её через слэш, например: 'PB6/XTAL1', 'PD0/RXD'.\n"
            "3. Инверсные сигналы (active-low, с чертой сверху, символами '#', '/','n' перед именем) записывай в формате KiCad: '~{RESET}', '~{CS}'.\n"
            "4. Электрический тип (`type`): используй ТОЛЬКО 'input', 'output', 'bidirectional', 'power_in', 'power_out', 'passive', 'tri_state', 'open_collector', 'no_connect'. "
            "ВНИМАНИЕ: AREF и VREF всегда 'passive' или 'input', а не 'power_in'!\n"
            "5. Функциональная группировка (`group`) и сторона (`side`):\n"
            "   - 'power_pos' (VCC, VDD, AVCC, VBAT) -> side: 'left'\n"
            "   - 'system' (RESET, XTAL, OSC, CLK, BOOT, AREF) -> side: 'left'\n"
            "   - 'power_gnd' (GND, VSS, AGND, PGND) -> side: 'left'\n"
            "   - 'port_a', 'port_b', 'port_c', 'port_d', 'io', 'data' (порты GPIO и интерфейсы) -> side: 'right'\n\n"
            "СТРОГИЕ ПРАВИЛА:\n"
            "1. Извлекай пины СТРОГО для указанного корпуса (например, LQFP-48). Если в таблице несколько столбцов под разные корпуса, бери данные только для запрошенного корпуса и игнорируй остальные!\n"
            "2. Общее количество выводов (`expected_pin_count`) должно строго соответствовать этому корпусу (например, 48 для LQFP-48).\n"
            "Верни СТРОГО валидный JSON:\n"
            "{\n"
            '  "audit": {"selected_package": "DIP-28", "expected_pin_count": 28, "extracted_pin_count": 28, "missing_or_fixed_notes": ""},\n'
            '  "pins": [\n'
            '    {"num": "1", "name": "PC6/~{RESET}", "type": "bidirectional", "side": "left", "group": "system"}\n'
            "  ]\n"
            "}"
        )
        pkg_note = f"Предпочтительный корпус: {package_hint}\n" if package_hint else ""
        user_prompt = (
            f"Компонент: {component_name}\n{pkg_note}"
            f"Данные из даташита:\n{json.dumps(pages_data, ensure_ascii=False)}"
        )

        self.log(f"[*] Запрос к ИИ ({self.config.model}) со встроенной самопроверкой...")
        response = self.client.chat.completions.create(
            model=self.config.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            temperature=self.config.temperature
        )

        raw_json = response.choices[0].message.content or "{}"
        parsed = json.loads(raw_json)

        audit = parsed.get("audit", {})
        pins = parsed.get("pins", [])

        self.log(
            f"[+] Самопроверка ИИ: корпус={audit.get('selected_package', 'N/A')}, "
            f"ожидалось пинов={audit.get('expected_pin_count', '?')}, "
            f"извлечено={len(pins)}"
        )
        if audit.get("missing_or_fixed_notes"):
            self.log(f"[i] Заметка аудита: {audit['missing_or_fixed_notes']}")

        # Программная поствалидация и очистка дубликатов номеров выводов
        clean_pins = self._validate_and_deduplicate_pins(pins)

        # Если после дедупликации количество сильно не сходится с ожидаемым — делаем точечный запрос коррекции
        expected = audit.get("expected_pin_count")
        if isinstance(expected, int) and expected > 0 and len(clean_pins) != expected:
            self.log(f"[!] Расхождение числа выводов ({len(clean_pins)} != {expected}). Запуск быстрой автокоррекции...")
            clean_pins = self._auto_correct_pins(pages_data, clean_pins, expected, audit.get("selected_package", ""))

        sym_content = KiCadSymbolGenerator.generate(component_name, clean_pins)
        os.makedirs(os.path.dirname(os.path.abspath(output_sym_path)), exist_ok=True)
        with open(output_sym_path, "w", encoding="utf-8") as f:
            f.write(sym_content)

        self.log(f"[+] Готово! Библиотека сохранена в: {os.path.abspath(output_sym_path)}")
        return output_sym_path

    def _validate_and_deduplicate_pins(self, pins: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        """Гарантирует отсутствие дубликатов пинов и корректность полей."""
        seen_nums = set()
        valid_list = []
        for p in pins:
            if not isinstance(p, dict):
                continue
            num = str(p.get("num", "")).strip()
            name = str(p.get("name", "")).strip()
            if not num or not name or num in seen_nums:
                continue
            seen_nums.add(num)

            p_type = str(p.get("type", "bidirectional")).strip().lower()
            side = str(p.get("side", "left")).strip().lower()

            # Автоисправление очевидных типов питания и земли
            if re.match(r"^(vcc|vdd|avcc|avdd|vbat|gnd|vss|agnd|pgnd)$", name, re.IGNORECASE):
                p_type = "power_in"
            elif re.match(r"^(aref|vref|vref\+|vref-)$", name, re.IGNORECASE):
                p_type = "passive"
            elif re.match(r"^(nc|dnc|n\.c\.)$", name, re.IGNORECASE):
                p_type = "no_connect"

            valid_list.append({
                "num": num,
                "name": name,
                "type": p_type,
                "side": "left" if side == "left" else "right"
            })
        return valid_list

    def _auto_correct_pins(
        self, pages_data: List[Dict[str, Any]], current_pins: List[Dict[str, str]], expected_count: int, package: str
    ) -> List[Dict[str, str]]:
        """Точечный запрос рефлексии, если модель пропустила выводы в первый проход."""
        present_nums = [p["num"] for p in current_pins]
        prompt = (
            f"Для корпуса {package} ожидается {expected_count} выводов, но извлечено {len(current_pins)} "
            f"(присутствуют номера: {present_nums}).\n"
            f"Найди пропущенные выводы в таблицах даташита и верни ПОЛНЫЙ исправленный JSON-объект с ключом 'pins'."
        )
        try:
            resp = self.client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": "Ты — строгий верификатор таблиц выводов KiCad. Отвечай только в JSON."},
                    {"role": "user", "content": f"Данные даташита:\n{json.dumps(pages_data, ensure_ascii=False)}\n\nТекущий список:\n{json.dumps(current_pins, ensure_ascii=False)}\n\nЗадание: {prompt}"}
                ],
                response_format={"type": "json_object"},
                temperature=0.0
            )
            corrected = json.loads(resp.choices[0].message.content or "{}").get("pins", [])
            if corrected:
                return self._validate_and_deduplicate_pins(corrected)
        except Exception as e:
            self.log(f"[-] Автокоррекция пропущена: {e}")
        return current_pins

    # -----------------------------------------------------------------
    # ВЕТКА 2: Локальные инструменты ERC и Агент диагностики схем
    # -----------------------------------------------------------------
    def _tool_run_erc(self, schematic_path: str) -> str:
        """Прямой вызов kicad-cli без накладных расходов внешнего MCP-сервера."""
        abs_sch = os.path.abspath(schematic_path)
        if not os.path.isfile(abs_sch):
            return f"Ошибка: файл схемы не найден по пути {abs_sch}"

        cli_exe = resolve_kicad_cli(self.config.kicad_cli_path)
        if not cli_exe:
            return (
                "Ошибка: утилита 'kicad-cli' не найдена в системе. "
                "Установите KiCad 7+ или укажите путь к kicad-cli в настройках."
            )

        base_no_ext = os.path.splitext(abs_sch)[0]
        report_path = f"{base_no_ext}-erc.rpt"

        try:
            cmd = [cli_exe, "sch", "erc", "--output", report_path, abs_sch]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=os.path.dirname(abs_sch),
                encoding="utf-8",
                errors="replace",
                check=False
            )
            stdout_info = (res.stdout + "\n" + res.stderr).strip()
            return (
                f"Команда выполнена (код возврата {res.returncode}).\n"
                f"Вывод консоли: {stdout_info}\n"
                f"Файл отчета сохранен по пути: {report_path}"
            )
        except Exception as e:
            return f"Исключение при запуске kicad-cli: {e}"

    def _tool_read_erc_report(self, report_path: str) -> str:
        """Читает и предварительно сжимает отчет ERC, чтобы не сжигать токены ИИ на гигантских логах."""
        abs_rpt = os.path.abspath(report_path)
        if not os.path.isfile(abs_rpt):
            return f"Ошибка: файл отчета не найден по пути {abs_rpt}"

        with open(abs_rpt, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        # Если отчет огромный, оставляем шапку, сводку и первые 25 000 символов с конкретными ошибками
        max_chars = 25000
        if len(content) > max_chars:
            tail = content[-3000:]  # В конце отчета KiCad всегда пишет итоговую сводку (summary)
            content = (
                content[:max_chars]
                + "\n\n... [Середина отчета пропущена для экономии токенов] ...\n\n"
                + tail
            )
        return content

    def run_erc_agent(self, schematic_path: str, custom_user_query: str = "") -> str:
        """
        Многошаговый агент проверки электрических правил (ERC) со строгим планом действий
        и лимитом шагов (max_turns = 4), чтобы исключить зацикливание и перерасход токенов.
        """
        abs_sch = os.path.abspath(schematic_path)
        expected_rpt = f"{os.path.splitext(abs_sch)[0]}-erc.rpt"

        tools_schema = [
            {
                "type": "function",
                "function": {
                    "name": "check_schematic_erc",
                    "description": "Запускает kicad-cli для проверки электрических правил (ERC) схемы .kicad_sch и генерирует .rpt отчет.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "schematic_path": {"type": "string", "description": "Полный путь к .kicad_sch"}
                        },
                        "required": ["schematic_path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "read_erc_report",
                    "description": "Читает сгенерированный текстовый файл отчета ERC (.rpt).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "report_path": {"type": "string", "description": "Полный путь к .rpt файлу"}
                        },
                        "required": ["report_path"]
                    }
                }
            }
        ]

        system_prompt = (
            "Ты — строгий ИИ-аудитор схемотехники KiCad. Работай лаконично и по фактам.\n"
            "Алгоритм:\n"
            "1. Вызови `check_schematic_erc` для указанной схемы.\n"
            "2. Вызови `read_erc_report` для созданного файла отчета.\n"
            "3. Проведи самопроверку: сгруппируй ошибки по типам (например, `pin_not_connected`, `power_pin_not_driven`), "
            "подсчитай их количество, выдели критические нарушения и дай конкретные инструкции по исправлению в KiCad."
        )

        user_message = custom_user_query or (
            f"Проверь схему '{abs_sch}' (ожидаемый отчет: '{expected_rpt}') "
            f"и выдай структурированный инженерный разбор ошибок."
        )

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ]

        max_turns = 4
        for turn in range(1, max_turns + 1):
            self.log(f"[*] Шаг агента {turn}/{max_turns}...")
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                tools=tools_schema,
                temperature=self.config.temperature
            )

            msg = response.choices[0].message
            messages.append(msg)

            if not msg.tool_calls:
                final_text = msg.content or "Анализ завершен."
                self.log("\n=== ФИНАЛЬНЫЙ ОТЧЕТ ИИ-ИНЖЕНЕРА ===\n")
                self.log(final_text)
                return final_text

            for tool_call in msg.tool_calls:
                fn_name = tool_call.function.name
                try:
                    fn_args = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    fn_args = {}

                self.log(f"[->] Вызов инструмента: {fn_name}({fn_args})")

                if fn_name == "check_schematic_erc":
                    result_str = self._tool_run_erc(fn_args.get("schematic_path", abs_sch))
                elif fn_name == "read_erc_report":
                    result_str = self._tool_read_erc_report(fn_args.get("report_path", expected_rpt))
                else:
                    result_str = f"Неизвестный инструмент: {fn_name}"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": fn_name,
                    "content": result_str
                })

        return "Превышен лимит шагов агента."


# =====================================================================
# 6. КОНСОЛЬНЫЙ ИНТЕРФЕЙС (CLI)
# =====================================================================
def main():
    parser = argparse.ArgumentParser(
        description="KiCad AI Workflow: генерация символов из PDF и интеллектуальный ERC-аудит схем."
    )
    parser.add_argument("--api-key", help="API ключ (по умолчанию берется из DEEPSEEK_API_KEY)", default="")
    parser.add_argument("--base-url", help="URL API (по умолчанию https://api.deepseek.com)", default="")
    parser.add_argument("--model", help="Модель ИИ (по умолчанию deepseek-chat)", default="")
    parser.add_argument("--kicad-cli", help="Путь к kicad-cli (если не в стандартной папке)", default="")

    subparsers = parser.add_subparsers(dest="command", help="Доступные режимы работы")

    # Команда 1: Генерация символа из даташита
    pdf_parser = subparsers.add_parser("pdf2sym", help="Извлечь распиновку из PDF и создать .kicad_sym")
    pdf_parser.add_argument("pdf", help="Путь к PDF-даташиту")
    pdf_parser.add_argument("--name", "-n", required=True, help="Название компонента (например, ATmega328P)")
    pdf_parser.add_argument("--out", "-o", default="generated_lib.kicad_sym", help="Путь к выходному файлу .kicad_sym")
    pdf_parser.add_argument("--package", "-p", default="", help="Уточнение корпуса (например, TQFP-32 или DIP-28)")

    # Команда 2: ERC диагностика схемы
    erc_parser = subparsers.add_parser("erc", help="Проверить схему .kicad_sch и проанализировать ошибки")
    erc_parser.add_argument("schematic", help="Путь к файлу схемы .kicad_sch")
    erc_parser.add_argument("--prompt", default="", help="Дополнительный вопрос или инструкция для ИИ")

    args = parser.parse_args()

    # Собираем конфигурацию (переопределяем переменными из CLI, если переданы)
    cfg = AppConfig()
    if args.api_key:
        cfg.api_key = args.api_key
    if args.base_url:
        cfg.base_url = args.base_url
    if args.model:
        cfg.model = args.model
    if args.kicad_cli:
        cfg.kicad_cli_path = args.kicad_cli

    engine = KiCadWorkflowEngine(cfg)

    # Если скрипт запущен без аргументов (например, по кнопке Run в IDE) — запускаем демо-меню
    if not args.command:
        print("=== KiCad AI Workflow (Интерактивный режим) ===")
        print("1. Извлечь компонент из PDF -> .kicad_sym")
        print("2. Провести ERC-аудит схемы .kicad_sch")
        choice = input("Выберите действие (1 или 2): ").strip()

        if choice == "1":
            default_pdf = os.path.join("files-to-test", "ATmega328.pdf")
            pdf_path = input(f"Путь к PDF [{default_pdf}]: ").strip() or default_pdf
            comp_name = input("Имя компонента [ATmega328P]: ").strip() or "ATmega328P"
            pkg_hint = input("Корпус (опционально, например DIP-28): ").strip()
            out_file = input("Выходной файл [my_generated_lib.kicad_sym]: ").strip() or "my_generated_lib.kicad_sym"
            engine.build_symbol_from_pdf(pdf_path, comp_name, out_file, pkg_hint)

        elif choice == "2":
            default_sch = os.path.join("files-to-test", "test-project-kicad", "test-project-kicad.kicad_sch")
            sch_path = input(f"Путь к схеме [{default_sch}]: ").strip() or default_sch
            engine.run_erc_agent(sch_path)
        else:
            parser.print_help()
        return

    # Выполнение команд CLI
    if args.command == "pdf2sym":
        engine.build_symbol_from_pdf(args.pdf, args.name, args.out, args.package)
    elif args.command == "erc":
        engine.run_erc_agent(args.schematic, args.prompt)


if __name__ == "__main__":
    main()