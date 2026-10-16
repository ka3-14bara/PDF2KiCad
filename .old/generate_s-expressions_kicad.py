import datetime

def generate_kicad_sym(component_name, pins):
    """
    Генерирует строку в формате KiCad v6+ (.kicad_sym) для одного компонента.
    pins: список словарей вида [{'num': '1', 'name': 'VCC', 'type': 'power_in', 'side': 'left'}, ...]
    """
    # Заголовок файла библиотеки KiCad v6/v7/v8/v9
    header = f'(kicad_symbol_lib (version 20211014) (generator kicad_symbol_editor)\n'
    
    # Начало описания конкретного символа
    # 'in_bom yes' означает, что компонент попадет в спецификацию проекта
    sym_body = f'  (symbol "{component_name}" (in_bom yes) (on_board yes)\n'
    
    # Базовые текстовые поля (Ref**, Значение, Корпус)
    sym_body += f'    (property "Reference" "U" (id 0) (at -5.08 7.62 0) (effects (font (size 1.27 1.27))))\n'
    sym_body += f'    (property "Value" "{component_name}" (id 1) (at -5.08 5.08 0) (effects (font (size 1.27 1.27))))\n'
    sym_body += f'    (property "Footprint" "" (id 2) (at 0 0 0) (effects (font (size 1.27 1.27)) hide))\n'
    
    # Внутренний графический блок компонента (рисуем прямоугольный корпус и пины)
    # Позиционирование в KiCad идет в миллидюймах (mils) или мм. В коде ниже координаты в условных единицах сетки.
    sym_body += f'    (symbol "{component_name}_1_1"\n'
    
    # Рисуем прямоугольник тела микросхемы: (rectangle (start x y) (end x y) ...)
    sym_body += f'      (rectangle (start -7.62 5.08) (end 7.62 -5.08) (stroke (width 0.254)) (fill (type background)))\n'
    
    # Генерируем пины на основе переданного списка
    # Для простоты: левые пины ставим слева, правые - справа
    y_offset_left = 2.54
    y_offset_right = 2.54
    
    for pin in pins:
        p_num = pin['num']
        p_name = pin['name']
        p_type = pin.get('type', 'bidirectional') # типы: input, output, power_in, passive и т.д.
        
        if pin['side'] == 'left':
            # Пины слева смотрят вправо (угол 0 градусов), длина линии пина 2.54
            sym_body += f'      (pin {p_type} line (at -10.16 {y_offset_left} 0) (length 2.54)\n'
            sym_body += f'        (name "{p_name}" (effects (font (size 1.27 1.27))))\n'
            sym_body += f'        (number "{p_num}" (effects (font (size 1.27 1.27))))\n'
            sym_body += f'      )\n'
            y_offset_left -= 2.54
        else:
            # Пины справа смотрят влево (угол 180 градусов)
            sym_body += f'      (pin {p_type} line (at 10.16 {y_offset_right} 180) (length 2.54)\n'
            sym_body += f'        (name "{p_name}" (effects (font (size 1.27 1.27))))\n'
            sym_body += f'        (number "{p_num}" (effects (font (size 1.27 1.27))))\n'
            sym_body += f'      )\n'
            y_offset_right -= 2.54

    # Закрываем все скобки
    sym_body += '    )\n  )\n)'
    
    return header + sym_body

# --- ТЕСТОВЫЙ ЗАПУСК ---
if __name__ == "__main__":
    # Вот такой чистый JSON/список словарей мы заставим LLM отдавать на выходе
    sampled_pins_from_llm = [
        {'num': '1', 'name': 'VCC', 'type': 'power_in', 'side': 'left'},
        {'num': '2', 'name': 'GND', 'type': 'power_in', 'side': 'left'},
        {'num': '3', 'name': 'TXD', 'type': 'output', 'side': 'right'},
        {'num': '4', 'name': 'RXD', 'type': 'input', 'side': 'right'},
    ]
    
    kicad_file_content = generate_kicad_sym("MySmartChip", sampled_pins_from_llm)
    
    # Сохраняем в файл библиотеки
    with open("my_generated_lib.kicad_sym", "w", encoding="utf-8") as f:
        f.write(kicad_file_content)
        
    print("Файл библиотеки 'my_generated_lib.kicad_sym' успешно создан!")
