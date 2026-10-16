import subprocess
import os
import shutil
from mcp.server.mcpserver import MCPServer, Context
from mcp.types import CallToolResult, TextContent

mcp = MCPServer("KiCad-Agent")

def find_kicad_cli():
    if shutil.which("kicad-cli"):
        return "kicad-cli"
    if os.name == 'nt':
        program_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        base_kicad_path = os.path.join(program_files, "KiCad")
        if os.path.exists(base_kicad_path):
            for folder in sorted(os.listdir(base_kicad_path), reverse=True):
                cli_path = os.path.join(base_kicad_path, folder, "bin", "kicad-cli.exe")
                if os.path.exists(cli_path):
                    return cli_path
    return "kicad-cli"

@mcp.tool()
async def check_schematic_erc(schematic_path: str, ctx: Context) -> CallToolResult:
    """
    Запускает проверку электрических правил (ERC) для схемы KiCad (.kicad_sch)
    """
    try:
        abs_path = os.path.abspath(schematic_path)
        working_dir = os.path.dirname(abs_path)
        file_name = os.path.basename(abs_path)
        
        executable = find_kicad_cli()
        
        result = subprocess.run(
            [executable, "sch", "erc", file_name],
            capture_output=True,
            text=True,
            shell=True if os.name == 'nt' else False,
            cwd=working_dir,
            encoding="utf-8",
            errors="replace", 
            check=False
        )
        
        output = result.stdout + "\n" + result.stderr
        if not output.strip():
            output = "Проверка ERC завершена успешно. Ошибок не обнаружено!"
            
    except Exception as e:
        output = f"Внутренняя ошибка при выполнении команды: {str(e)}"

    return CallToolResult(
        content=[TextContent(type="text", text=output)],
        is_error=False
    )

# НОВЫЙ ИНСТРУМЕНТ ДЛЯ АГЕНТА
@mcp.tool()
async def read_erc_report(report_path: str, ctx: Context) -> CallToolResult:
    """
    Читает содержимое сгенерированного текстового отчета ERC (.rpt) для анализа ошибок
    """
    try:
        abs_path = os.path.abspath(report_path)
        if not os.path.exists(abs_path):
            return CallToolResult(
                content=[TextContent(type="text", text=f"Ошибка: Файл отчета не найден по пути {abs_path}")],
                is_error=True
            )
            
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
            
        # Защита: если отчет гигантский, обрезаем его для контекста LLM
        if len(content) > 50000:
            content = content[:50000] + "\n... [Отчет обрезан из-за большого размера] ..."
            
        return CallToolResult(
            content=[TextContent(type="text", text=content)],
            is_error=False
        )
    except Exception as e:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Не удалось прочитать отчет: {str(e)}")],
            is_error=True
        )

if __name__ == "__main__":
    mcp.run(transport='stdio')
