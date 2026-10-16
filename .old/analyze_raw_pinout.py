import os
import json
from openai import OpenAI

# 1. Инициализируем клиент DeepSeek
# Убедитесь, что вы создали переменную окружения DEEPSEEK_API_KEY
client = OpenAI(
    api_key=os.environ.get("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com"
)

def process_pinout_with_deepseek(raw_json_data):
    """
    Отправляет сырые текстовые блоки и таблицы из pdfplumber в DeepSeek
    и заставляет модель структурировать их в JSON-массив пинов.
    """
    
    # Системный промпт, задающий жесткие правила для ИИ
    system_prompt = (
        "Ты — специализированный ИИ-инженер для проектирования электроники. "
        "Твоя задача — извлечь список контактов (пинов) микросхемы из сырого текста и таблиц даташита.\n\n"
        
        "ПРАВИЛА ИЗВЛЕЧЕНИЯ:\n"
        "1. Найди номера пинов (num), их названия (name) и электрический тип (type).\n"
        "2. Для электрического типа (type) используй ТОЛЬКО следующие значения KiCad:\n"
        "   - 'input' (вход)\n"
        "   - 'output' (выход)\n"
        "   - 'bidirectional' (двунаправленный, GPIO, шины данных)\n"
        "   - 'power_in' (питание: VCC, GND, VDD, VSS)\n"
        "   - 'passive' (резисторы, конденсаторы, пустые выводы, если нужно)\n"
        "3. Определи сторону размещения пина (side): 'left' или 'right'. Равномерно распредели их: "
        "питание (VCC/GND), интерфейсы управления и входы обычно ставь на 'left', выходы и шины данных на 'right'.\n\n"
        
        "ФОРМАТ ОТВЕТА:\n"
        "Ты должен вернуть СТРОГО валидный JSON-массив объектов без какого-либо текста до или после JSON. "
        "Не используй markdown-разметку ```json. Шаблон элемента:\n"
        "[\n"
        "  {\"num\": \"1\", \"name\": \"VCC\", \"type\": \"power_in\", \"side\": \"left\"},\n"
        "  {\"num\": \"2\", \"name\": \"PA0\", \"type\": \"bidirectional\", \"side\": \"right\"}\n"
        "]"
    )

    user_prompt = f"Вот сырые данные, извлеченные из даташита:\n\n{json.dumps(raw_json_data, ensure_ascii=False)}"

    try:
        print("[*] Отправка запроса в DeepSeek...")
        
        response = client.chat.completions.create(
            model="deepseek-chat", # Для строгого структурирования v3 ("deepseek-chat") подходит идеально
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            # Включаем JSON-mode, чтобы модель гарантированно возвращала валидный JSON-объект
            response_format={"type": "json_object"}, 
            temperature=0.1 # Низкая температура для максимальной точности без фантазий
        )
        
        # Получаем строковый ответ модели
        raw_answer = response.choices[0].message.content
        
        # Парсим строку в Python-список
        structured_pins = json.loads(raw_answer)
        
        # DeepSeek может обернуть массив в какой-нибудь ключ типа {"pins": [...]}, проверим это:
        if isinstance(structured_pins, dict) and "pins" in structured_pins:
            return structured_pins["pins"]
        elif isinstance(structured_pins, dict) and not isinstance(structured_pins, list):
            # Если вернул словарь, где ключи - это массивы, заберем первый попавшийся массив
            for val in structured_pins.values():
                if isinstance(val, list):
                    return val
                    
        return structured_pins

    except Exception as e:
        print(f"[-] Ошибка при запросе к DeepSeek или парсинге: {e}")
        return None

# --- ТЕСТОВЫЙ ЗАПУСК СВЯЗКИ ---
if __name__ == "__main__":
    # Загружаем то, что напарсил наш pdfplumber на предыдущем шаге
    try:
        with open("raw_extracted_pinout.json", "r", encoding="utf-8") as f:
            saved_raw_data = json.load(f)
            
        clean_pins = process_pinout_with_deepseek(saved_raw_data)
        
        if clean_pins:
            print("[+] Успешно получены структурированные данные от ИИ!")
            print(json.dumps(clean_pins, indent=2, ensure_ascii=False))
            
            # Теперь этот массив clean_pins можно напрямую передавать 
            # в нашу функцию generate_kicad_sym(), которую мы написали в самом начале!
        
    except FileNotFoundError:
        print("[-] Сначала запустите скрипт из Шага 1, чтобы сгенерировать 'raw_extracted_pinout.json'")
