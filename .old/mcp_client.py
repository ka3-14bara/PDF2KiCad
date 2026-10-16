import os
import sys
import json
import asyncio
from openai import OpenAI
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.session import ClientSession

# Инициализируем DeepSeek
client = OpenAI(
    api_key=os.environ.get("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com"
)

# Настройки запуска сервера с использованием sys.executable вашего .env
# Настройки запуска локального MCP-сервера
server_params = StdioServerParameters(
    command=sys.executable,
    args=[os.path.abspath("mcp_server.py")],
    env={
        **os.environ.copy(),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONLEGACYWINDOWSSTDIO": "UTF-8"
    }
)


async def run_kicad_agent(user_prompt):
    print("[*] Подключение к локальному MCP-серверу KiCad v2...")
    
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            # Инициализируем сессию протокола MCP v2
            await session.initialize()
            
            available_tools = await session.list_tools()
            print(f"[+] Доступные локальные инструменты: {[t.name for t in available_tools.tools]}")
            
            # Начальная история сообщений с системными инструкциями
            messages = [
                {
                    "role": "system", 
                    "content": (
                        "Ты — ИИ-инженер. У тебя есть инструменты check_schematic_erc для проверки схем "
                        "и read_erc_report для чтения логов ошибок. Твоя цель — провести полную диагностику "
                        "и выдать подробный, структурированный анализ отчета пользователю на русском языке."
                    )
                },
                {"role": "user", "content": user_prompt}
            ]
            
            # Описание функций для интеграции с API DeepSeek
            openai_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "check_schematic_erc",
                        "description": "Запускает проверку электрических правил (ERC) для схемы KiCad",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "schematic_path": {"type": "string", "description": "Полный путь к файлу схемы .kicad_sch"}
                            },
                            "required": ["schematic_path"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "read_erc_report",
                        "description": "Читает текстовый файл отчета ERC (.rpt) для детального анализа нарушений",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "report_path": {"type": "string", "description": "Полный путь к файлу отчета .rpt"}
                            },
                            "required": ["report_path"]
                        }
                    }
                }
            ]
            
            # МНОГОШАГОВЫЙ АГЕНТСКИЙ ЦИКЛ (Multi-turn Agentic Loop)
            while True:
                response = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=messages,
                    tools=openai_tools
                )
                
                # Исправленный бронебойный парсинг ответов DeepSeek v3 / R1
                if isinstance(response, list) and len(response) > 0:
                    assistant_message = response[0].message
                elif hasattr(response, 'choices') and response.choices:
                    assistant_message = response.choices[0].message
                else:
                    if hasattr(response, 'message'):
                        assistant_message = response.message
                    else:
                        print(f"[-] Не удалось распарсить ответ API: {response}")
                        break
                
                # Добавляем ответ модели в историю чата, чтобы ИИ помнил свои шаги
                messages.append(assistant_message)
                
                # Если ИИ выдал финальный ответ и больше не вызывает инструменты — выходим из цикла
                if not assistant_message.tool_calls:
                    print(f"\n[ИИ Финальный Анализ]:\n{assistant_message.content}")
                    break
                    
                # Если ИИ запросил вызовы инструментов — обрабатываем их по очереди
                for tool_call in assistant_message.tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = json.loads(tool_call.function.arguments)
                    
                    print(f"\n[->] DeepSeek вызывает: '{tool_name}' с аргументами: {tool_args}")
                    
                    # Запускаем выполнение локального инструмента на ПК через наш MCP-сервер
                    result = await session.call_tool(tool_name, tool_args)
                    
                    # Извлекаем текст ответа по новым правилам mcp 2.x из объекта CallToolResult
                    if hasattr(result, 'content') and isinstance(result.content, list) and len(result.content) > 0:
                        tool_output_text = result.content[0].text
                    else:
                        tool_output_text = str(result)
                    
                    print(f"[+] Результат выполнения инструмента получен сервером.")
                    
                    # Передаем результат работы утилиты KiCad обратно в контекст ИИ
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": tool_output_text
                    })
                
                print("[*] Ожидание следующего шага мышления ИИ...")

if __name__ == "__main__":
    real_path = os.path.abspath(
        os.path.join("files-to-test", "test-project-kicad", "test-project-kicad.kicad_sch")
    )
    # Формируем путь к потенциальному отчету, чтобы ИИ знал, где его искать
    report_path = os.path.abspath(
        os.path.join("files-to-test", "test-project-kicad", "test-project-kicad-erc.rpt")
    )
    
    TEST_PROMPT = (
        f"Проведи полный анализ схемы {real_path}. "
        f"Сначала запусти проверку check_schematic_erc. Затем, используя инструмент read_erc_report, "
        f"прочитай полученный файл отчета {report_path}, проанализируй 97 нарушений и выдай мне "
        f"структурированное резюме: какие типы ошибок там преобладают."
    )
    
    asyncio.run(run_kicad_agent(TEST_PROMPT))
