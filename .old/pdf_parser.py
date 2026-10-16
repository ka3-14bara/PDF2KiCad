import pdfplumber
import json
import re

def extract_pinout_pages(pdf_path):
    """
    Сканирует PDF, находит страницы с упоминанием распиновки,
    извлекает из них сырой текст и таблицы.
    """
    # Ключевые слова, которые почти всегда есть на страницах с распиновкой
    KEYWORDS = [
        r"pin\s+assignment", 
        r"pin\s+configuration", 
        r"pin\s+description", 
        r"pin\s+functions",
        r"terminal\s+functions",
        r"pin description",
    ]
    
    extracted_data = []
    
    print(f"[*] Анализируем файл: {pdf_path}")
    
    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        print(f"[+] Всего страниц в документе: {total_pages}")
        
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            
            # Проверяем, содержит ли страница нужные маркеры (без учета регистра)
            is_target_page = any(re.search(kw, text, re.IGNORECASE) for kw in KEYWORDS)
            
            if is_target_page:
                print(f"[->] Найдена потенциальная страница с распиновкой: №{page_num}")
                
                # 1. Вытаскиваем сырой текст со страницы
                page_text = text.strip()
                
                # 2. Вытаскиваем таблицы, если pdfplumber смог их распознать в явном виде
                # Это сильно упростит задачу ИИ, так как таблицы не превратятся в кашу
                tables_data = []
                extracted_tables = page.extract_tables()
                for table in extracted_tables:
                    # Чистим строки таблицы от пустых значений и None
                    cleaned_table = [[str(cell).strip() if cell else "" for cell in row] for row in table]
                    tables_data.append(cleaned_table)
                
                # Сохраняем собранный контент со страницы
                extracted_data.append({
                    "page": page_num,
                    "text": page_text,
                    "tables": tables_data
                })
                
    return extracted_data

# --- ТЕСТОВЫЙ ЗАПУСК ---
if __name__ == "__main__":
    # Замените на путь к любому реальному даташиту (например, STM32, ATmega, какой-нибудь чип CH340 и т.д.)
    PDF_FILE = "./files-to-test/ATmega328.pdf" 
    
    try:
        raw_payload = extract_pinout_pages(PDF_FILE)
        
        if not raw_payload:
            print("[-] Нужные страницы не найдены. Попробуйте расширить KEYWORDS.")
        else:
            # Сохраняем промежуточный результат в JSON, который мы и будем отдавать ИИ
            output_json = "raw_extracted_pinout.json"
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump(raw_payload, f, ensure_ascii=False, indent=2)
                
            print(f"\n[+] Сбор данных окончен! Спасенный текст и таблицы сохранены в '{output_json}'")
            print(f"[+] Вместо {PDF_FILE} мы передадим ИИ компактный текстовый JSON.")
            
    except FileNotFoundError:
        print(f"[-] Положите тестовый даташит и назовите его '{PDF_FILE}' для проверки скрипта.")
